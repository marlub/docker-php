#!/usr/bin/env python3
"""Find optional, trusted merged-PR caches for a pinned main build."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess

from github_api import GitHubAPI, GitHubAPIError


SHA = re.compile(r"[a-f0-9]{40}")
REPOSITORY = re.compile(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+")


def git(root, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True, stderr=subprocess.PIPE
    ).strip()


def commit_scope(root, source_ref, base_ref=None):
    if not SHA.fullmatch(source_ref):
        raise ValueError("Source must be a full commit SHA")
    source = git(root, "rev-parse", "--verify", "--end-of-options", f"{source_ref}^{{commit}}")
    if source != source_ref:
        raise ValueError("Source did not resolve to the pinned commit")
    if base_ref is None or base_ref == "0" * 40:
        return [source]
    if not SHA.fullmatch(base_ref):
        raise ValueError("Base must be a full commit SHA")
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base_ref, source],
        text=True, capture_output=True,
    )
    if ancestor.returncode == 1:
        return [source]
    ancestor.check_returncode()
    commits = git(root, "rev-list", "--first-parent", f"{base_ref}..{source}").splitlines()
    if not all(SHA.fullmatch(commit) for commit in commits):
        raise ValueError("Git returned an invalid commit SHA")
    return commits


def require_object(value):
    if not isinstance(value, dict):
        raise ValueError("GitHub returned an invalid object")
    return value


def require_string(value):
    if not isinstance(value, str):
        raise ValueError("GitHub returned an invalid string")
    return value


def pr_number(value):
    if type(value) is not int or value < 1:
        raise ValueError("GitHub returned an invalid PR number")
    return value


def eligible_pr(pull, number, repository, commits):
    pull = require_object(pull)
    if pr_number(pull["number"]) != number:
        raise ValueError("GitHub returned details for a different PR")
    state = require_string(pull["state"])
    if state not in ("open", "closed"):
        raise ValueError("GitHub returned an invalid PR state")
    merged = pull["merged_at"]
    if state != "closed" or merged is None:
        return False
    merged_time = datetime.fromisoformat(require_string(merged).replace("Z", "+00:00"))
    if merged_time.tzinfo is None:
        raise ValueError("GitHub returned a merge timestamp without a timezone")
    merge_sha = require_string(pull["merge_commit_sha"])
    if not SHA.fullmatch(merge_sha):
        raise ValueError("GitHub returned an invalid merge SHA")
    base = require_object(pull["base"])
    head = require_object(pull["head"])
    base_repo = require_object(base["repo"])
    head_repo = head["repo"]
    author = require_string(require_object(pull["user"])["login"])
    return (
        merge_sha in commits
        and require_string(base["ref"]) == "main"
        and require_string(base_repo["full_name"]).casefold() == repository.casefold()
        and head_repo is not None
        and require_string(require_object(head_repo)["full_name"]).casefold() == repository.casefold()
        and author.casefold() != "dependabot[bot]"
    )


def find_merged_prs(api, repository, commits):
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("Repository must be OWNER/REPO")
    if not all(SHA.fullmatch(commit) for commit in commits):
        raise ValueError("Invalid commit scope")
    candidates = set()
    for commit in commits:
        page = 1
        while True:
            batch = api.request("GET", f"/repos/{repository}/commits/{commit}/pulls?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ValueError("GitHub returned an invalid associated-PR list")
            candidates.update(pr_number(require_object(pull)["number"]) for pull in batch)
            if len(batch) < 100:
                break
            page += 1
    scope = set(commits)
    return [number for number in sorted(candidates)
            if eligible_pr(api.request("GET", f"/repos/{repository}/pulls/{number}"),
                           number, repository, scope)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--base-ref")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    warning = None
    try:
        root = Path(__file__).resolve().parent.parent
        commits = commit_scope(root, args.source_ref, args.base_ref)
        token = os.environ.get("GH_TOKEN")
        if not token:
            raise ValueError("GH_TOKEN is required for merged PR cache lookup")
        api = GitHubAPI(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        numbers = find_merged_prs(api, args.repository, commits)
    except (GitHubAPIError, OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        numbers = []
        warning = f"Merged PR cache lookup failed; using main cache only: {error}"
        escaped = warning.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::warning::{escaped}")
    serialized = json.dumps(numbers, separators=(",", ":"))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"merged-prs={serialized}\n")
    message = warning or ("Eligible merged PR caches: " + (", ".join(f"#{number}" for number in numbers) or "none"))
    print(message)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write("### Merged PR cache lookup\n\n" + message + "\n\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
