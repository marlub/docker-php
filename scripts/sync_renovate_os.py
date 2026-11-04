#!/usr/bin/env python3
"""Generate release-specific Renovate package rules from committed OS lockfiles."""

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
GENERATED = "Generated OS packages: scripts/sync_renovate_os.py"
ARCHITECTURES = {"amd64": "x86_64", "arm64": "aarch64"}


def generated_configuration(root: Path) -> tuple[list[dict], list[dict]]:
    managers, rules = [], []
    for backend, datasource, distribution in (("apk", "apk", "Alpine"), ("apt", "deb", "Debian")):
        for path in sorted((root / "build" / backend).glob("*/*.json")):
            release, arch = path.parent.name, path.stem
            lock = json.loads(path.read_text())
            release_pattern = r"\d+\.\d+" if backend == "apk" else r"[a-z][a-z0-9]*"
            if arch not in ARCHITECTURES or not re.fullmatch(release_pattern, release):
                raise ValueError(f"Unsupported OS lock path: {path}")
            expected_arch = ARCHITECTURES[arch] if backend == "apk" else arch
            if lock.get("alpine" if backend == "apk" else "debian") != release or lock.get("architecture") != expected_arch:
                raise ValueError(f"OS lock metadata does not match its path: {path}")
            if not isinstance(lock.get("packages"), dict):
                raise ValueError(f"Missing OS lock packages: {path}")
            relative = path.relative_to(root).as_posix()
            pattern = (r'"(?<depName>[a-zA-Z0-9+_.-]+)": "(?<currentValue>[^"\n]+-r[0-9]+)"'
                       if backend == "apk" else
                       r'"(?<depName>[a-z0-9][a-z0-9+.-]*)": "(?<currentValue>[0-9][a-zA-Z0-9.+:~\-]*)"')
            managers.append({
                "description": GENERATED,
                "customType": "regex",
                "managerFilePatterns": ["/^" + re.escape(relative) + "$/"],
                "matchStrings": [pattern],
                "datasourceTemplate": datasource,
                "versioningTemplate": datasource,
            })
            if backend == "apk":
                urls = [f"https://dl-cdn.alpinelinux.org/alpine?branch=v{release}&components=main,community&arch={expected_arch}"]
            else:
                urls = [
                    f"https://deb.debian.org/debian?suite={release}&components=main&binaryArch={arch}",
                    f"https://deb.debian.org/debian?suite={release}-updates&components=main&binaryArch={arch}",
                    f"https://deb.debian.org/debian-security?suite={release}-security&components=main&binaryArch={arch}",
                ]
            rules.append({
                "description": GENERATED,
                "matchDatasources": [datasource],
                "matchFileNames": [relative],
                "groupName": f"{distribution} {release} packages",
                "registryUrls": urls,
            })
    return managers, rules


def synchronize(root: Path, *, check: bool = False) -> bool:
    path = root / "renovate.json"
    config = json.loads(path.read_text())
    managers, rules = generated_configuration(root)
    expected = dict(config)
    for key, generated in (("customManagers", managers), ("packageRules", rules)):
        expected[key] = [entry for entry in config.get(key, []) if entry.get("description") != GENERATED] + generated
    if config == expected:
        return True
    if not check:
        path.write_text(json.dumps(expected, indent=2) + "\n")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail on stale generated rules without writing files")
    args = parser.parse_args()
    try:
        current = synchronize(ROOT, check=args.check)
    except (OSError, ValueError, TypeError) as error:
        print(error, file=sys.stderr)
        return 1
    if args.check and not current:
        print("OS Renovate rules are stale; run python3 scripts/sync_renovate_os.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
