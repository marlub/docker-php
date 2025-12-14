#!/usr/bin/env python3
import argparse, json, re
import requests


def fetch_json(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def parse_allow_add_version(version):
    if version is None:
        return None, None

    m = re.match(r"^([0-9]+)\.([0-9]+)$", version)

    if not m:
        raise ValueError("Allow add version must be a minor like 8.1")

    major, minor = map(int, m.groups())

    return major, minor


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
                key = (item["major"], item["minor"], item["variant"], item["os"], item["node"])
                existing[key] = item
            return existing
    except FileNotFoundError:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", nargs="+", default=["fpm"])
    ap.add_argument("--os", nargs="+", default=["alpine"])
    ap.add_argument("--extensions", default="bcmath gd exif intl calendar ldap zip pcntl opcache sockets mysqli pdo_pgsql pdo_mysql redis amqp xdebug pcov xhprof")
    ap.add_argument("--pecl", default="")
    ap.add_argument("--packages", default="")
    ap.add_argument("--node", nargs="+", default=["", "24"])
    ap.add_argument("--out", default="matrix.json")
    ap.add_argument("--source-url", default="https://raw.githubusercontent.com/docker-library/php/master/versions.json")
    ap.add_argument("--allow-add-version", default=None)
    args = ap.parse_args()

    upstream = fetch_json(args.source_url)
    latest = parse_versions(upstream)
    entries = load_existing(args.out)
    minMajor, minMinor = parse_allow_add_version(args.allow_add_version)

    for (major, minor), ver in latest.items():
        for variant in args.variants:
            for os in args.os:
                for node in args.node:
                    key = (major, minor, variant, os, node)

                    if key not in entries and minMajor is not None and major >= minMajor and minMinor is not None and minor >= minMinor:
                        entries[key] = {
                            "major": major,
                            "minor": minor,
                            "patch": ver["patch"],
                            "variant": variant,
                            "os": os,
                            "extensions": args.extensions,
                            "pecl": args.pecl,
                            "packages": args.packages,
                            "node": node,
                        }

    for (major, minor, variant, os, node), entry in entries.items():
        key = (major, minor)
        if key in latest:
            entry["patch"] = latest[key]["patch"]

    entries = sorted(
        entries.values(),
        key=lambda item: (
            item["major"],
            item["minor"],
            item["os"],
            item["variant"],
            int(item["node"] or 0)
        )
    )

    with open(args.out, "w") as f:
        json.dump({"phpimages": list(entries)}, f, indent=2)


if __name__ == "__main__":
    main()
