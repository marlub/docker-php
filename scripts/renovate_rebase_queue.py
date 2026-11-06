#!/usr/bin/env python3
"""Request one Mend Renovate rebase at a time, retaining the slot through CI."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode

from github_api import GitHubAPI, GitHubAPIError


MARKER = "<!-- renovate-rebase-queue:v1 -->"
BOT = "renovate[bot]"
CONTROLLER = "github-actions[bot]"
LABEL = "rebase"
SHA = re.compile(r"[a-f0-9]{40}")
REPOSITORY = re.compile(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+")
FAILED = {"failure", "timed_out", "cancelled", "action_required", "stale", "startup_failure"}
RESULTS = {"requested", "failed", "merged", "closed", "ineligible"}
CI_PATHS = {".github/workflows/build-test.yml", "dynamic/github-code-scanning/codeql"}


class QueueError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise QueueError(message)


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def valid_number(value):
    return type(value) is int and value > 0


def validate_state(state):
    require(isinstance(state, dict) and set(state) == {"version", "active", "attempts"}
            and type(state["version"]) is int and state["version"] == 1,
            "Unsupported or malformed queue state; inspect the controller's dashboard comment")
    require(isinstance(state["attempts"], dict), "Invalid attempt history")
    for number, attempt in state["attempts"].items():
        require(isinstance(number, str) and number.isdecimal() and int(number) > 0
                and isinstance(attempt, dict)
                and set(attempt) == {"base", "head", "requested_at", "result"}
                and valid_sha(attempt["base"]) and valid_sha(attempt["head"])
                and attempt["result"] in RESULTS,
                "Invalid queue attempt record")
        try:
            stamp = datetime.fromisoformat(attempt["requested_at"])
            require(stamp.tzinfo is not None, "Queue timestamp has no timezone")
        except (TypeError, ValueError):
            raise QueueError("Invalid queue timestamp") from None
    active = state["active"]
    if active is not None:
        require(isinstance(active, dict)
                and set(active) == {"number", "requested_head", "requested_base", "observed_head", "phase", "result"}
                and valid_number(active["number"])
                and valid_sha(active["requested_head"]) and valid_sha(active["requested_base"])
                and (active["observed_head"] is None or valid_sha(active["observed_head"]))
                and active["phase"] in {"prepared", "requested", "releasing"}
                and active["result"] in RESULTS,
                "Invalid active queue entry")
        attempt = state["attempts"].get(str(active["number"]))
        require(attempt is not None and attempt["base"] == active["requested_base"]
                and attempt["head"] == active["requested_head"]
                and attempt["result"] == active["result"]
                and ((active["phase"] == "releasing" and active["result"] != "requested")
                     or (active["phase"] != "releasing" and active["result"] == "requested")),
                "Active request does not match attempt history")
    return state


def eligible(pull, repository):
    return (pull.get("state") == "open" and not pull.get("draft", True)
            and pull.get("user", {}).get("login") == BOT
            and isinstance(pull.get("auto_merge"), dict)
            and pull.get("base", {}).get("ref") == "main"
            and pull.get("base", {}).get("repo", {}).get("full_name") == repository
            and (pull.get("head", {}).get("repo") or {}).get("full_name") == repository
            and pull.get("head", {}).get("ref", "").startswith("renovate/"))


def behind(pull):
    return pull.get("mergeable") is True and pull.get("mergeable_state") == "behind"


def has_label(pull):
    return any(label.get("name") == LABEL for label in pull.get("labels", []))


class RebaseQueue:
    def __init__(self, api, repository, *, now=None):
        require(REPOSITORY.fullmatch(repository) is not None, "Repository must be OWNER/REPO")
        self.api = api
        self.repository = repository
        self.prefix = f"/repos/{repository}"
        self.now = now or datetime.now(timezone.utc)
        self.messages = []
        self.comment_id = None
        self.dashboard = None
        self.state = {"version": 1, "active": None, "attempts": {}}

    def log(self, message):
        self.messages.append(message)

    def pages(self, path, field=None):
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            response = self.api.request("GET", f"{self.prefix}{path}{separator}per_page=100&page={page}")
            batch = response[field] if field else response
            require(isinstance(batch, list), f"Invalid paginated response for {path}")
            yield from batch
            if len(batch) < 100:
                break
            page += 1

    def pull(self, number):
        pull = self.api.request("GET", f"{self.prefix}/pulls/{number}")
        require(isinstance(pull, dict) and pull.get("number") == number,
                "GitHub returned a different PR")
        require(valid_sha(pull.get("head", {}).get("sha")), "PR has an invalid head SHA")
        return pull

    def load(self):
        dashboards = [issue for issue in self.pages("/issues?state=open&creator=renovate%5Bbot%5D")
                      if issue.get("title") == "Dependency Dashboard" and "pull_request" not in issue
                      and issue.get("user", {}).get("login") == BOT]
        require(len(dashboards) == 1, "Expected one open Renovate Dependency Dashboard")
        self.dashboard = dashboards[0]["number"]
        comments = [comment for comment in self.pages(f"/issues/{self.dashboard}/comments")
                    if comment.get("user", {}).get("login") == CONTROLLER
                    and MARKER in comment.get("body", "")]
        require(len(comments) <= 1, "Multiple controller state comments; reconcile them manually")
        if comments:
            comment = comments[0]
            self.comment_id = comment["id"]
            try:
                payload = comment["body"].split("```json\n", 1)[1].split("\n```", 1)[0]
                self.state = validate_state(json.loads(payload))
            except (IndexError, ValueError, TypeError):
                raise QueueError("Malformed queue state; inspect the controller's dashboard comment") from None

    def save(self):
        validate_state(self.state)
        active = self.state["active"]
        status = f"Active PR: #{active['number']} ({active['phase']})." if active else "No active rebase request."
        body = (f"{MARKER}\n### Renovate rebase queue\n\n{status}\n\n"
                "Managed by the Renovate rebase queue workflow. Do not delete this state while a request is active.\n\n"
                f"```json\n{json.dumps(self.state, sort_keys=True, indent=2)}\n```\n")
        require(len(body) < 65000, "Queue state exceeds GitHub's comment limit")
        if self.comment_id is None:
            response = self.api.request("POST", f"{self.prefix}/issues/{self.dashboard}/comments", body={"body": body})
            self.comment_id = response["id"]
        else:
            self.api.request("PATCH", f"{self.prefix}/issues/comments/{self.comment_id}", body={"body": body})

    def untracked_requests(self, pulls):
        active = self.state["active"]
        return [pull["number"] for pull in pulls if has_label(pull)
                and (active is None or pull["number"] != active["number"])]

    def add_label(self, pull):
        # State persistence and label creation take API round trips. Do not act on a stale selection.
        pull = self.pull(pull["number"])
        active = self.state["active"]
        if pull.get("state") == "closed":
            self.release("merged" if pull.get("merged_at") else "closed")
            return
        if not eligible(pull, self.repository):
            self.release("ineligible")
            return
        if pull["head"]["sha"] != active["requested_head"]:
            active["observed_head"] = pull["head"]["sha"]
            active["phase"] = "requested"
            self.save()
            self.log(f"#{pull['number']} already changed; holding its slot for current CI.")
            return
        if self.untracked_requests(list(self.pages("/pulls?state=open"))):
            self.log("Paused: another rebase request appeared after reserving the slot.")
            return
        if not has_label(pull) and not behind(pull):
            self.log(f"Holding #{pull['number']}: branch is no longer confirmed behind main; no label added.")
            return
        if not has_label(pull):
            # Label creation is separate from adding it, so a missing repository label is recoverable.
            label = self.api.request("GET", f"{self.prefix}/labels/{LABEL}", missing_ok=True)
            if label is None:
                self.api.request("POST", f"{self.prefix}/labels", body={
                    "name": LABEL, "color": "ededed", "description": "Request Renovate to rebase this PR",
                })
            self.api.request("POST", f"{self.prefix}/issues/{pull['number']}/labels", body={"labels": [LABEL]})
        self.state["active"]["phase"] = "requested"
        self.save()
        self.log(f"Requested a rebase for #{pull['number']}; waiting for Mend to refresh its branch.")

    def reserve(self, pull, base):
        number = pull["number"]
        self.state["active"] = {
            "number": number, "requested_head": pull["head"]["sha"], "requested_base": base,
            "observed_head": None, "phase": "prepared", "result": "requested",
        }
        self.state["attempts"][str(number)] = {
            "base": base, "head": pull["head"]["sha"], "requested_at": self.now.isoformat(), "result": "requested",
        }
        # Write-ahead reservation: a failed/uncertain label mutation must never free the slot.
        self.save()
        self.add_label(pull)

    def release(self, result):
        active = self.state["active"]
        active["phase"] = "releasing"
        active["result"] = result
        self.state["attempts"][str(active["number"])]["result"] = result
        self.save()
        self.finish_release()

    def finish_release(self):
        active = self.state["active"]
        # Delete only this controller's request. Failure leaves the releasing entry for the next run.
        self.api.request("DELETE", f"{self.prefix}/issues/{active['number']}/labels/{LABEL}", missing_ok=True)
        self.log(f"Released #{active['number']}: {active['result']}.")
        self.state["active"] = None
        self.save()

    def ci_status(self, pull):
        head = pull["head"]["sha"]
        query = urlencode({"head_sha": head})
        latest = {}
        for run in self.pages(f"/actions/runs?{query}", "workflow_runs"):
            path = run.get("path", "").split("@", 1)[0]
            branches = {pull["head"]["ref"]}
            if path == "dynamic/github-code-scanning/codeql":
                # Default-setup CodeQL runs use a PR ref, unlike ordinary PR workflows.
                branches.add(f"refs/pull/{pull['number']}/head")
            if (run.get("head_sha") != head or run.get("head_branch") not in branches
                    or path not in CI_PATHS):
                continue
            # GET /actions/runs returns the latest attempt of a run, including reruns in progress.
            key = run["workflow_id"]
            if key not in latest or run["id"] > latest[key]["id"]:
                latest[key] = run
        failed = [run for run in latest.values()
                  if run["status"] == "completed" and run.get("conclusion") in FAILED]
        if failed:
            return "failed"
        if latest and all(run["status"] == "completed" for run in latest.values()):
            return "finished"
        return "waiting"

    def reconcile_active(self, base):
        active = self.state["active"]
        if active["phase"] == "releasing":
            self.finish_release()
            return
        pull = self.pull(active["number"])
        if pull.get("state") == "closed":
            self.release("merged" if pull.get("merged_at") else "closed")
            return
        if not eligible(pull, self.repository):
            self.release("ineligible")
            return
        head = pull["head"]["sha"]
        if head == active["requested_head"]:
            if active["phase"] == "prepared":
                self.add_label(pull)
            else:
                self.log(f"Waiting for Mend to refresh #{pull['number']}; old-commit checks are ignored.")
            return
        if active["observed_head"] != head:
            active["observed_head"] = head
            active["phase"] = "requested"
            self.save()
        status = self.ci_status(pull)
        if status == "failed":
            self.release("failed")
        elif status == "finished" and behind(pull) and active["requested_base"] != base:
            # main advanced during CI. Keep the same slot and refresh again instead of cancelling CI early.
            self.reserve(pull, base)
        else:
            self.log(f"Holding #{pull['number']}: waiting for current CI, auto-merge, or closure.")

    def reconcile(self):
        self.load()
        branch = self.api.request("GET", f"{self.prefix}/git/ref/heads/main")
        base = branch["object"]["sha"]
        require(valid_sha(base), "main has an invalid SHA")
        pulls = list(self.pages("/pulls?state=open"))
        untracked = self.untracked_requests(pulls)
        if untracked:
            self.log("Paused: existing rebase labels outside the active queue entry: "
                     + ", ".join(f"#{number}" for number in untracked) + ".")
            return
        if self.state["active"] is not None:
            self.reconcile_active(base)
            if self.state["active"] is not None:
                return
        candidates = []
        for pull in pulls:
            if not eligible(pull, self.repository):
                continue
            attempt = self.state["attempts"].get(str(pull["number"]))
            if attempt and attempt["base"] == base and attempt["result"] == "failed":
                continue
            candidates.append(pull)
        candidates.sort(key=lambda pull: (
            self.state["attempts"].get(str(pull["number"]), {}).get("requested_at", ""),
            pull["created_at"], pull["number"],
        ))
        for candidate in candidates:
            pull = self.pull(candidate["number"])
            if not eligible(pull, self.repository) or not behind(pull):
                continue
            # Re-read before mutation; another actor may have requested a rebase while we inspected CI.
            if self.untracked_requests(list(self.pages("/pulls?state=open"))):
                self.log("Paused: another rebase request appeared during reconciliation.")
                return
            current = self.api.request("GET", f"{self.prefix}/git/ref/heads/main")["object"]["sha"]
            if current != base:
                self.log("main changed during reconciliation; defer to the next event or manual run.")
                return
            # Keep all open-PR history and a small recent closed-PR history to bound the comment size.
            open_numbers = {str(item["number"]) for item in pulls}
            recent = sorted(self.state["attempts"], key=lambda n: self.state["attempts"][n]["requested_at"], reverse=True)[:20]
            self.state["attempts"] = {n: attempt for n, attempt in self.state["attempts"].items()
                                      if n in open_numbers or n in recent}
            self.reserve(pull, base)
            return
        self.log("No eligible outdated PR needs a rebase request.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    queue = None
    code = 0
    try:
        token = os.environ.get("GH_TOKEN")
        require(bool(token), "GH_TOKEN is required")
        queue = RebaseQueue(GitHubAPI(token, os.environ.get("GITHUB_API_URL", "https://api.github.com")), args.repository)
        queue.reconcile()
        messages = queue.messages
    except (QueueError, GitHubAPIError, OSError, ValueError, KeyError, TypeError) as error:
        messages = (queue.messages if queue else []) + [f"Queue stopped: {error}. Inspect the dashboard state and rerun manually after resolving the problem."]
        code = 1
    for message in messages:
        print(message)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write("### Renovate rebase queue\n\n" + "\n".join(f"- {message}" for message in messages) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
