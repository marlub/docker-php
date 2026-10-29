#!/usr/bin/env python3
"""Delete disposable GHCR cache versions without touching published images."""

import argparse
from datetime import datetime, timedelta, timezone
import os
import re
import sys
from urllib.parse import quote

from github_api import GitHubAPI, GitHubAPIError


PR_TAG = re.compile(r"pr-([1-9][0-9]*)-[a-zA-Z0-9_][a-zA-Z0-9_.-]*-(?:amd64|arm64)")
UNTAGGED_RETENTION = timedelta(days=7)
MERGED_PR_RETENTION = timedelta(hours=48)


class CleanupError(GitHubAPIError):
    pass


def package_path(api, repository, package):
    owner, _ = repository.split("/", 1)
    owner_path = quote(owner, safe="")
    account = api.request("GET", f"/users/{owner_path}")
    if account["type"] == "Organization":
        prefix = "orgs"
    elif account["type"] == "User":
        prefix = "users"
    else:
        raise CleanupError(f"Unsupported package owner type: {account['type']}")
    return f"/{prefix}/{owner_path}/packages/container/{quote(package, safe='')}"


def list_versions(api, path):
    # Finish pagination before deleting: deletions would shift subsequent pages.
    versions = {}
    page = 1
    while True:
        batch = api.request("GET", f"{path}/versions?per_page=100&page={page}", missing_ok=True)
        if batch is None:
            return []
        for version in batch:
            versions[version["id"]] = version
        if len(batch) < 100:
            return list(versions.values())
        page += 1


def eligible_version(version, *, pr_number, now, pr_expired):
    # Fail on missing metadata rather than treating it as an untagged version.
    tags = version["metadata"]["container"]["tags"]
    if not tags:
        if pr_number is not None:
            return False
        updated = datetime.fromisoformat(version["updated_at"].replace("Z", "+00:00"))
        return now - updated > UNTAGGED_RETENTION
    matches = [PR_TAG.fullmatch(tag) for tag in tags]
    if not all(matches):
        return False
    numbers = {int(match[1]) for match in matches}
    # Shared versions with tags from another PR (or main) are preserved.
    if len(numbers) != 1:
        return False
    number = numbers.pop()
    return (pr_number is None or number == pr_number) and pr_expired(number)


def cleanup(api, repository, package, *, pr_number=None, now=None, dry_run=False):
    now = now or datetime.now(timezone.utc)
    path = package_path(api, repository, package)
    states = {}

    def pr_expired(number):
        if number not in states:
            pull = api.request("GET", f"/repos/{repository}/pulls/{number}")
            merged = pull["merged_at"]
            states[number] = pull["state"] == "closed" and (
                merged is None or now - datetime.fromisoformat(merged.replace("Z", "+00:00")) >= MERGED_PR_RETENTION
            )
        return states[number]

    candidates = list_versions(api, path)
    deleted = []
    for version in candidates:
        if not eligible_version(version, pr_number=pr_number, now=now, pr_expired=pr_expired):
            continue
        version_path = f"{path}/versions/{version['id']}"
        # Builds can add a protected tag after listing; inspect again before deletion.
        current = api.request("GET", version_path, missing_ok=True)
        if current is None:
            continue
        states.clear()
        if not eligible_version(current, pr_number=pr_number, now=now, pr_expired=pr_expired):
            continue
        if not dry_run:
            api.request("DELETE", version_path, missing_ok=True)
        deleted.append(version["id"])
        tags = ", ".join(current["metadata"]["container"]["tags"]) or "untagged"
        print(f"{'Would delete' if dry_run else 'Deleted'} cache version {version['id']} ({tags})")
    print(f"{'Selected' if dry_run else 'Removed'} {len(deleted)} cache versions")
    return deleted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="Repository owning the PRs, OWNER/REPO")
    parser.add_argument("--package", default="php-buildcache")
    parser.add_argument("--pr", type=int, help="Restrict cleanup to a single closed PR")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", args.repository):
        parser.error("--repository must be OWNER/REPO")
    if args.pr is not None and args.pr < 1:
        parser.error("--pr must be positive")
    # A dedicated cache package is required: never point this at runtime images.
    if args.package != "php-buildcache":
        parser.error("--package must be the dedicated php-buildcache package")
    token = os.environ.get("GH_TOKEN")
    if not token:
        parser.error("GH_TOKEN is required")
    try:
        cleanup(GitHubAPI(token, os.environ.get("GITHUB_API_URL", "https://api.github.com")),
                args.repository, args.package, pr_number=args.pr, dry_run=args.dry_run)
    except (GitHubAPIError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"Cache cleanup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
