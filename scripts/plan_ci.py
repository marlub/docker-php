#!/usr/bin/env python3
"""Plan image builds and Renovate validation from configuration and Git changes."""

import argparse
from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Literal, Mapping, Sequence, TypedDict

from image_config import ImageVariant, load_image_variants


Purpose = Literal["test", "publish"]
INITIAL_PUSH_SHA = "0" * 40

# Keep trigger exclusions aligned with the workflows' paths-ignore lists.
# Patterns are exact root-relative paths or directory/**.
PR_IGNORED_PATHS = ("docs/**", "README.md", "LICENSE")
RENOVATE_PATHS = ("renovate.json", ".github/workflows/template-renovate.yml")
IMAGE_TEST_IGNORED_PATHS = PR_IGNORED_PATHS + RENOVATE_PATHS
PUBLICATION_IGNORED_PATHS = IMAGE_TEST_IGNORED_PATHS + (
    "tests/**",
    "scripts/test_image.sh",
    "scripts/verify_image.py",
    "scripts/plan_ci.py",
    "scripts/image_config.py",
    "scripts/refresh_apk_locks.py",
    ".github/workflows/build-test.yml",
)


class PlanningError(ValueError):
    """A requested CI plan or publication candidate is invalid."""


@dataclass(frozen=True)
class ChangeSet:
    base_commit: str
    head_commit: str
    paths: tuple[str, ...]
    initial_push: bool = False


class ReportRow(TypedDict):
    tag: str
    state: Literal["Selected", "Not selected"]


class CiPlan(TypedDict):
    phpimages: list[ImageVariant]
    report: list[ReportRow]
    renovate_changes: bool


def run_git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True, stderr=subprocess.PIPE
    )


def _resolve_commit(root: Path, reference: str) -> str:
    return run_git(
        root, "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"
    ).strip()


def load_variants(root: Path) -> list[ImageVariant]:
    return load_image_variants(root, source_ref=_resolve_commit(root, "HEAD"))


def read_changes(root: Path, base_ref: str, head_ref: str = "HEAD") -> ChangeSet:
    """Read the complete diff once, treating renames as deletions and additions."""
    head = _resolve_commit(root, head_ref)
    if base_ref == INITIAL_PUSH_SHA:
        return ChangeSet(base_commit=base_ref, head_commit=head, paths=(), initial_push=True)
    base = _resolve_commit(root, base_ref)
    output = run_git(root, "diff", "--name-only", "--no-renames", "-z", base, head, "--")
    paths = tuple(path for path in output.split("\0") if path)
    return ChangeSet(base_commit=base, head_commit=head, paths=paths)


def _image_ignores(purpose: Purpose) -> tuple[str, ...]:
    if purpose == "test":
        return IMAGE_TEST_IGNORED_PATHS
    if purpose == "publish":
        return PUBLICATION_IGNORED_PATHS
    raise PlanningError(f"Unsupported selection purpose: {purpose}")


def _without_node_stages(dockerfile: str) -> str | None:
    """Project the known Dockerfile layout onto its shared PHP stages."""
    stages = list(re.finditer(
        r"^FROM \$\{NODE_BASE_IMAGE[^}\n]*\} AS node\n", dockerfile, re.MULTILINE
    ))
    node_arguments = re.findall(r"^ARG NODE_BASE_IMAGE=", dockerfile, re.MULTILINE)
    if len(stages) != 1 or len(node_arguments) != 1:
        return None
    start = stages[0].start()
    tail_stages = re.findall(r"^FROM [^\n]+", dockerfile[start:], re.MULTILINE)
    if len(tail_stages) != 2 or tail_stages[1] != "FROM php AS php-node":
        return None
    shared = re.sub(
        r"^ARG NODE_(?:BASE_IMAGE|VERSION)=[^\n]*\n", "", dockerfile[:start],
        flags=re.MULTILINE,
    )
    if "NODE_BASE_IMAGE" in shared or "NODE_VERSION" in shared:
        return None
    return shared


def _file_at_commit(root: Path, commit: str, path: str) -> str | None:
    try:
        return run_git(root, "show", f"{commit}:{path}")
    except subprocess.CalledProcessError:
        return None


def _only_node_stages_changed(root: Path, changes: ChangeSet) -> bool:
    before = _file_at_commit(root, changes.base_commit, "Dockerfile")
    after = _file_at_commit(root, changes.head_commit, "Dockerfile")
    if before is None or after is None:
        return False
    shared_before = _without_node_stages(before)
    return shared_before is not None and shared_before == _without_node_stages(after)


def _affected_image_tags(
    variants: Sequence[ImageVariant], root: Path, changes: ChangeSet, purpose: Purpose
) -> set[str]:
    ignored = _image_ignores(purpose)
    if changes.initial_push:
        return {variant["tagMinor"] for variant in variants}

    affected: set[str] = set()
    for path in changes.paths:
        if any(fnmatchcase(path, pattern) for pattern in ignored):
            continue
        php_manifest = re.fullmatch(r"build/php/([^/]+)\.json", path)
        apk_lock = re.fullmatch(r"build/apk/([^/]+)/[^/]+\.json", path)
        matching: Sequence[ImageVariant]
        if php_manifest:
            matching = [variant for variant in variants if variant["minor"] == php_manifest[1]]
        elif apk_lock:
            matching = [variant for variant in variants if variant["alpine"] == apk_lock[1]]
        elif path == "Dockerfile" and _only_node_stages_changed(root, changes):
            matching = [variant for variant in variants if variant["node"]]
        else:
            matching = variants
        affected.update(variant["tagMinor"] for variant in matching)
    return affected


def affected_image_tags(
    variants: Sequence[ImageVariant], root: Path, base_ref: str,
    head_ref: str = "HEAD", *, purpose: Purpose = "test",
) -> set[str]:
    return _affected_image_tags(variants, root, read_changes(root, base_ref, head_ref), purpose)


def create_ci_plan(
    variants: Sequence[ImageVariant], root: Path, base_ref: str | None = None,
    *, php: str = "All", purpose: Purpose = "test",
) -> CiPlan:
    """Plan independent image and Renovate jobs, or an explicit manual rebuild."""
    _image_ignores(purpose)
    if php != "All" and php not in {variant["minor"] for variant in variants}:
        raise PlanningError(f"Unsupported PHP minor: {php}")

    renovate_changes = False
    if base_ref:
        changes = read_changes(root, base_ref)
        tags = _affected_image_tags(variants, root, changes, purpose)
        renovate_changes = changes.initial_push or any(
            path in RENOVATE_PATHS for path in changes.paths
        )
    else:
        tags = {
            variant["tagMinor"] for variant in variants
            if php == "All" or variant["minor"] == php
        }

    return {
        "phpimages": [variant for variant in variants if variant["tagMinor"] in tags],
        "report": [
            {
                "tag": variant["tagMinor"],
                "state": "Selected" if variant["tagMinor"] in tags else "Not selected",
            }
            for variant in variants
        ],
        "renovate_changes": renovate_changes,
    }


def is_candidate_superseded(variant: ImageVariant, root: Path) -> bool:
    """Check whether newer publication-relevant changes affect this candidate."""
    if not re.fullmatch(r"[a-f0-9]{40}", variant["source_ref"]):
        raise PlanningError("Invalid candidate source commit")
    run_git(root, "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    tags = affected_image_tags(
        [variant], root, variant["source_ref"], "refs/remotes/origin/main", purpose="publish"
    )
    return variant["tagMinor"] in tags


def _append_github_outputs(path: Path | None, values: Mapping[str, str]) -> None:
    if path is not None:
        with path.open("a", encoding="utf-8") as output:
            for name, value in values.items():
                output.write(f"{name}={value}\n")


def _format_report(plan: CiPlan) -> str:
    header = "| Variant | Selection |\n| --- | --- |\n"
    return header + "".join(f'| {row["tag"]} | {row["state"]} |\n' for row in plan["report"])


def _write_plan(
    plan: CiPlan, destination: Path, github_output: Path | None, summary: Path | None
) -> None:
    serialized = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    destination.write_text(serialized + "\n", encoding="utf-8")
    _append_github_outputs(github_output, {
        "matrix": serialized,
        "has_changes": str(bool(plan["phpimages"])).lower(),
        "renovate_changes": str(plan["renovate_changes"]).lower(),
    })
    report = _format_report(plan)
    print(report)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as output:
            output.write("### Variant selection\n\n" + report)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-current", help="Check whether one variant JSON entry is superseded")
    mode.add_argument("--base-ref")
    mode.add_argument("--php", default="All", help="Rebuild one configured PHP minor, or All")
    parser.add_argument(
        "--purpose", choices=("test", "publish"), default="test",
        help=(
            "Select changes for PR testing or publication (default: test); "
            "supersession always uses publication rules"
        ),
    )
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--summary", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    if args.check_current is None and args.out is None:
        parser.error("--out is required for CI planning")
    root = Path(__file__).resolve().parent.parent
    try:
        if args.check_current is not None:
            superseded = is_candidate_superseded(json.loads(args.check_current), root)
            _append_github_outputs(args.github_output, {"superseded": str(superseded).lower()})
            print("Superseded" if superseded else "Current")
        else:
            plan = create_ci_plan(
                load_variants(root), root, args.base_ref, php=args.php, purpose=args.purpose
            )
            _write_plan(plan, args.out, args.github_output, args.summary)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            detail = error.stderr.strip()
        else:
            detail = str(error)
        print(f"CI planning failed: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
