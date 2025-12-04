#!/usr/bin/env python3
import argparse, json, re
import requests


def fetch_json(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def parse_versions(data):
    result = {}
    for key, val in data.items():
        if not val or "version" not in val:
            continue

        m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", val["version"])
        if not m:
            continue

        major, minor, patch = map(int, m.groups())
        slot = (major, minor)

        prev = result.get(slot)
        if prev is None or patch > prev["patch"]:
            result[slot] = {"major": major, "minor": minor, "patch": patch}

    return result


def load_existing(path):
    try:
        with open(path) as f:
            data = json.load(f)
            existing = {}
            for item in data.get("phpimages", []):
                key = (item["major"], item["minor"], item["variant"])
                existing[key] = item
            return existing
    except FileNotFoundError:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-url", default="https://raw.githubusercontent.com/docker-library/php/master/versions.json")
    ap.add_argument("--variants", nargs="+", default=["fpm-alpine"])
    ap.add_argument("--out", default="matrix.json")
    args = ap.parse_args()

    upstream = fetch_json(args.source_url)
    latest = parse_versions(upstream)
    existing = load_existing(args.out)

    new_entries = {}

    for (major, minor), ver in latest.items():
        for variant in args.variants:
            key = (major, minor, variant)

            if key in existing:
                item = existing[key]
                item["patch"] = ver["patch"]
            else:
                item = {
                    "major": major,
                    "minor": minor,
                    "patch": ver["patch"],
                    "variant": variant,
                    "extensions": "",
                    "pecl": "",
                    "packages": ""
                }

            new_entries[key] = item

    with open(args.out, "w") as f:
        json.dump({"phpimages": list(new_entries.values())}, f, indent=2)


if __name__ == "__main__":
    main()
