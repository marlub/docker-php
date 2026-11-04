#!/usr/bin/env python3
"""Explicit maintainer operation: resolve additional runtime APT packages in base containers."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile
from image_config import load_image_variants

ROOT = Path(__file__).resolve().parent.parent

def selected_base_images(php=None, base_target=None):
    # Refresh must work even when locks are missing or need replacement.
    variants = load_image_variants(ROOT, source_ref="")
    entries = []
    for variant in variants:
        if variant["node"] or variant["distribution"] != "debian":
            continue
        if php is not None and variant["minor"] != php:
            continue
        if base_target is not None and variant["base_target"] != base_target:
            continue
        args = variant["bake_args"]
        entries.append({"image": variant["php_image"],
                        "packages": " ".join(filter(None, [args["RUNTIME_PACKAGES"], args.get("PACKAGES")]))})
    return entries

def refresh(platform, entries):
    combined = {}
    release = None
    architecture = None
    script = '''set -eu
mkdir /tmp/lock
cd /tmp/lock
tar -xf -
php apt_lock.php capture baseline.json
apt-get update >&2
apt-get install -y --no-install-recommends --no-remove $RUNTIME_PACKAGES >&2
php apt_lock.php seed baseline.json
'''
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w') as archive:
        archive.add(ROOT / 'scripts/apt_lock.php', arcname='apt_lock.php')
    for entry in entries:
        reference = entry.get('_base_image')
        if not reference:
            manifest = json.loads(subprocess.check_output(['docker', 'buildx', 'imagetools', 'inspect', entry['image'], '--raw'], text=True))
            child = next(item for item in manifest['manifests'] if item['platform'].get('os') == 'linux' and item['platform'].get('architecture') == platform.split('/')[1])
            reference = entry['image'].split('@')[0].split(':')[0] + '@' + child['digest']
        result = subprocess.run(['docker', 'run', '--rm', '-i', '--platform', platform,
                                 '--env', 'RUNTIME_PACKAGES=' + entry['packages'],
                                 '--env', 'DEBIAN_FRONTEND=noninteractive',
                                 '--add-host', 'pecl.php.net:127.0.0.1', '--entrypoint', 'sh',
                                 reference, '-c', script], input=payload.getvalue(), stdout=subprocess.PIPE, check=True)
        lock = json.loads(result.stdout)
        if lock['architecture'] != platform.split('/')[1]:
            raise ValueError('Resolved APT architecture mismatch')
        release, architecture = lock['debian'], lock['architecture']
        target = combined.setdefault(release, {})
        for name, version in lock['packages'].items():
            if name in target and target[name] != version:
                raise ValueError(f'Conflicting package versions: {name}')
            target[name] = version
    for release, packages in combined.items():
        directory = ROOT / 'build' / 'apt' / release
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / (platform.split('/')[1] + '.json')
        destination.write_text(json.dumps({'debian': release, 'architecture': architecture,
                                          'packages': dict(sorted(packages.items()))}, indent=2) + '\n')
        print(destination)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-image', help='Use a cached platform-specific base with --php')
    parser.add_argument('--php', help='Refresh using one PHP minor when base package inventories are shared')
    parser.add_argument('--base-target', help='Refresh only this canonical plain Bake target')
    parser.add_argument('--platform', choices=['linux/amd64', 'linux/arm64'], required=True)
    args = parser.parse_args()
    entries = selected_base_images(args.php, args.base_target)
    if not entries: parser.error('No matching PHP Bake targets')
    if args.base_image:
        if len(entries) != 1: parser.error('--base-image requires exactly one matching family; use --php and/or --base-target')
        entries[0]['_base_image'] = args.base_image
    refresh(args.platform, entries)
