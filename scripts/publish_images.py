#!/usr/bin/env python3
"""Publish complete, tested variants; leave incomplete variants eligible for retry."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from select_images import (
    DIGEST_RE, FINGERPRINT_LABEL, INPUTS_LABEL, PLATFORMS, Registry,
    ResolutionError, selection_reason,
)


def validated_digests(entry, directory):
    artifacts = [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]
    if len(artifacts) != len(PLATFORMS):
        raise ResolutionError(f"Expected {len(PLATFORMS)} architecture artifacts, got {len(artifacts)}")
    if sorted(item["platform"] for item in artifacts) != sorted(PLATFORMS):
        raise ResolutionError("Architecture artifacts are duplicated or incomplete")
    for item in artifacts:
        for field in ("tagMinor", "tagPatch", "fingerprint", "build_inputs"):
            if item[field] != entry[field]:
                raise ResolutionError(f"Architecture artifact has an unexpected {field}")
        if not DIGEST_RE.fullmatch(item["digest"]):
            raise ResolutionError("Invalid image digest in architecture artifact")
    return artifacts


def publish_variant(entry, directory, image, registry, run=subprocess.run):
    artifacts = validated_digests(entry, directory)
    for artifact in artifacts:
        config = registry.inspect(f'{image}@{artifact["digest"]}', "Image")
        # Buildx can return a platform map for an index with provenance attached.
        config = config.get(artifact["platform"], config)
        if f'{config.get("os")}/{config.get("architecture")}' != artifact["platform"]:
            raise ResolutionError("Published digest has an unexpected architecture")
        labels = config.get("config", {}).get("Labels", {}) or {}
        if (labels.get(FINGERPRINT_LABEL) != entry["fingerprint"] or
                labels.get(INPUTS_LABEL) != entry["build_inputs"]):
            raise ResolutionError("Published digest has unexpected dependency labels")

    command = ["docker", "buildx", "imagetools", "create",
               "--tag", f'{image}:{entry["tagPatch"]}',
               "--tag", f'{image}:{entry["tagMinor"]}']
    command.extend(f'{image}@{artifact["digest"]}' for artifact in artifacts)
    run(command, check=True, timeout=180)
    # Inspect the new tags, rather than assuming all tag writes succeeded.
    reason = selection_reason(entry, registry, image)
    if reason:
        raise ResolutionError(f"Tag verification failed: {reason}")


def publish(entries, directory, image, registry, run=subprocess.run):
    failed = False
    report = ["### Publication\n", "| Variant | Result |", "| --- | --- |"]
    for entry in entries:
        try:
            publish_variant(entry, directory / entry["tagMinor"], image, registry, run)
            result = "Published"
        except (ResolutionError, KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            failed = True
            result = "Failed: " + str(exc).replace("\n", " ").replace("|", "\\|")
            print(f"{entry['tagMinor']}: {exc}", file=sys.stderr)
        report.append(f"| {entry['tagMinor']} | {result} |")
    return failed, "\n".join(report) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--digests", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--summary")
    args = parser.parse_args()
    try:
        entries = json.loads(Path(args.matrix).read_text())["phpimages"]
        failed, report = publish(entries, Path(args.digests), args.image, Registry())
        print(report)
        if args.summary:
            with open(args.summary, "a") as output:
                output.write(report)
        return int(failed)
    except (KeyError, ValueError, OSError) as exc:
        print(f"Publication failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
