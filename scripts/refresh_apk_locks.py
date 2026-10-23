#!/usr/bin/env python3
"""Explicit maintainer operation: resolve additional runtime APK packages in base containers."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent.parent

def refresh(platform, entries):
    combined = {}
    release = None
    architecture = None
    script = '''set -eu
mkdir /tmp/lock
cd /tmp/lock
tar -xf -
php apk_lock.php capture baseline.json
apk add --no-cache --upgrade $(sh alpine_packages.sh runtime) >&2
php apk_lock.php seed baseline.json
'''
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w') as archive:
        for name in ('apk_lock.php', 'alpine_packages.sh'):
            archive.add(ROOT / 'scripts' / name, arcname=name)
    for entry in entries:
        reference = entry.get('_base_image')
        if not reference:
            manifest = json.loads(subprocess.check_output(['docker', 'buildx', 'imagetools', 'inspect', entry['image'], '--format', '{{json .Manifest}}'], text=True))
            child = next(item for item in manifest['manifests'] if item['platform'].get('os') == 'linux' and item['platform'].get('architecture') == platform.split('/')[1])
            reference = entry['image'].split('@')[0].split(':')[0] + '@' + child['digest']
        result = subprocess.run(['docker', 'run', '--rm', '-i', '--platform', platform,
                                 '--add-host', 'pecl.php.net:127.0.0.1', '--entrypoint', 'sh',
                                 reference, '-c', script], input=payload.getvalue(), stdout=subprocess.PIPE, check=True)
        lock = json.loads(result.stdout)
        release, architecture = lock['alpine'], lock['architecture']
        target = combined.setdefault(release, {})
        for name, version in lock['packages'].items():
            if name in target and target[name] != version:
                raise ValueError(f'Conflicting package versions: {name}')
            target[name] = version
    for release, packages in combined.items():
        directory = ROOT / 'build' / 'apk' / release
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / (platform.split('/')[1] + '.json')
        destination.write_text(json.dumps({'alpine': release, 'architecture': architecture,
                                          'packages': dict(sorted(packages.items()))}, indent=2) + '\n')
        print(destination)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-image', help='Use a cached platform-specific base with --php')
    parser.add_argument('--php', help='Refresh using one PHP minor when base package inventories are shared')
    parser.add_argument('--platform', choices=['linux/amd64', 'linux/arm64'], required=True)
    args = parser.parse_args()
    paths = sorted((ROOT/'build/php').glob((args.php or '*') + '.json'))
    if not paths: parser.error('No matching PHP manifests')
    entries = [json.loads(path.read_text()) for path in paths]
    if args.base_image:
        if len(entries) != 1: parser.error('--base-image requires --php')
        entries[0]['_base_image'] = args.base_image
    refresh(args.platform, entries)
