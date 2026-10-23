#!/usr/bin/env python3
"""Select image variants from Git changes; dependency discovery belongs to Renovate."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

PLATFORMS = ("linux/amd64", "linux/arm64")
REVISION_LABEL = "org.opencontainers.image.revision"
VARIANT_LABEL = "io.github.marlub.docker-php.variant"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ResolutionError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.PIPE)


def manifests(root):
    dockerfile = (root / 'Dockerfile').read_text()
    tools = dict(re.findall(r'^ARG (PIE_IMAGE|COMPOSER_IMAGE|NODE_BASE_IMAGE)="([^"\n]+)"$', dockerfile, re.M))
    if set(tools) != {'PIE_IMAGE', 'COMPOSER_IMAGE', 'NODE_BASE_IMAGE'}:
        raise ResolutionError('Missing pinned tool image arguments')
    for reference in tools.values():
        if not re.fullmatch(r'[^\s@]+@sha256:[a-f0-9]{64}', reference):
            raise ResolutionError('Tool images must have version tags and digests')
    source_ref = git(root, 'rev-parse', '--verify', 'HEAD^{commit}').strip()
    entries = []
    for path in sorted((root / 'build/php').glob('*.json')):
        config = json.loads(path.read_text())
        match = re.fullmatch(r'php:((8\.\d+)\.\d+)-fpm-alpine(\d+\.\d+)@sha256:[a-f0-9]{64}', config['image'])
        if not match or match[2] != path.stem:
            raise ResolutionError(f'PHP manifest does not match its image: {path}')
        version, minor, alpine = match.groups()
        names = config['extensions'].split()
        if len(set(names)) != len(names) or any(not re.fullmatch(r'[a-z][a-z0-9_]*', name) for name in names):
            raise ResolutionError('Extensions must be unique module names')
        if minor == '8.5' and 'opcache' in names:
            raise ResolutionError('PHP 8.5 opcache is built in')
        for package in config['pie_packages'].values():
            if not re.fullmatch(r'\d+(?:\.\d+){1,3}', package['version']):
                raise ResolutionError('PIE versions must be exact stable pins')
        if not re.fullmatch(r'v?\d+\.\d+\.\d+', config['xhprof']['tag']) or not re.fullmatch(r'[a-f0-9]{40}', config['xhprof']['commit']):
            raise ResolutionError('XHProf requires a stable tag and full commit')
        for arch, apk_arch in [('amd64', 'x86_64'), ('arm64', 'aarch64')]:
            lock = json.loads((root / f'build/apk/{alpine}/{arch}.json').read_text())
            if lock['alpine'] != alpine or lock['architecture'] != apk_arch or not lock['packages']:
                raise ResolutionError('APK lock release/architecture mismatch')
            if any(not re.fullmatch(r'[a-zA-Z0-9+_.-]+', name) or not re.fullmatch(r'[a-zA-Z0-9+_.~-]+-r\d+', value)
                   for name, value in lock['packages'].items()):
                raise ResolutionError('APK packages require exact revision pins')
        node_match = re.fullmatch(r'node:(24\.\d+\.\d+)-alpine' + re.escape(alpine) + r'@sha256:[a-f0-9]{64}', tools['NODE_BASE_IMAGE'])
        if not node_match:
            raise ResolutionError('Node must stay on major 24 and the PHP Alpine release')
        for node in ['', '24']:
            suffix = '-fpm-alpine' + ('-node24' if node else '')
            entries.append({'minor': minor, 'php_version': version, 'alpine': alpine,
                            'php_image': config['image'], 'node_image': tools['NODE_BASE_IMAGE'],
                            'node': node, 'node_version': node_match[1] if node else '',
                            'extensions': config['extensions'], 'packages': config.get('packages', ''),
                            'tagMinor': minor + suffix, 'tagPatch': version + suffix,
                            'source_ref': source_ref,
                            'extension_versions': {name: data['version'] for name, data in config['pie_packages'].items() if name in names},
                            'xhprof': config['xhprof'] if 'xhprof' in names else None,
                            'composer_version': tools['COMPOSER_IMAGE'].split('@')[0].rsplit(':', 1)[1].removesuffix('-bin')})
    if not entries:
        raise ResolutionError('No PHP manifests found')
    return entries


def plain_dockerfile(text):
    """Separate the known Node stages; unfamiliar structures affect all variants."""
    stages = list(re.finditer(r'^FROM \$\{NODE_BASE_IMAGE[^}\n]*\} AS node\n', text, re.M))
    if len(stages) != 1 or len(re.findall(r'^ARG NODE_BASE_IMAGE=', text, re.M)) != 1:
        return None
    start = stages[0].start()
    tail_stages = re.findall(r'^FROM [^\n]+', text[start:], re.M)
    if len(tail_stages) != 2 or tail_stages[1] != 'FROM php AS php-node':
        return None
    plain = re.sub(r'^ARG NODE_(?:BASE_IMAGE|VERSION)=[^\n]*\n', '', text[:start], flags=re.M)
    if 'NODE_BASE_IMAGE' in plain or 'NODE_VERSION' in plain:
        return None
    return plain


def affected_tags(entries, root, base_ref, head_ref='HEAD'):
    head = git(root, 'rev-parse', '--verify', '--end-of-options', head_ref + '^{commit}').strip()
    if base_ref == '0' * 40:
        return {entry['tagMinor'] for entry in entries}
    base = git(root, 'rev-parse', '--verify', '--end-of-options', base_ref + '^{commit}').strip()
    paths = git(root, 'diff', '--name-only', '--no-renames', '-z', base, head, '--').split('\0')
    affected = set()
    for path in filter(None, paths):
        minor = re.fullmatch(r'build/php/([^/]+)\.json', path)
        alpine = re.fullmatch(r'build/apk/([^/]+)/[^/]+\.json', path)
        if minor:
            affected.update(entry['tagMinor'] for entry in entries if entry['minor'] == minor[1])
        elif alpine:
            affected.update(entry['tagMinor'] for entry in entries if entry['alpine'] == alpine[1])
        elif path == 'Dockerfile':
            before = subprocess.run(['git', '-C', str(root), 'show', base + ':Dockerfile'], capture_output=True, text=True)
            after = subprocess.run(['git', '-C', str(root), 'show', head + ':Dockerfile'], capture_output=True, text=True)
            plain_before = plain_dockerfile(before.stdout) if before.returncode == 0 else None
            plain_after = plain_dockerfile(after.stdout) if after.returncode == 0 else None
            node_only = plain_before is not None and plain_before == plain_after
            affected.update(entry['tagMinor'] for entry in entries if not node_only or entry['node'])
        elif path == 'entrypoint.sh' or path.startswith(('scripts/', 'tests/', '.github/workflows/', 'build/')):
            affected.update(entry['tagMinor'] for entry in entries)
    return affected


def select(entries, root, base_ref=None, php='All'):
    if php != 'All' and php not in {entry['minor'] for entry in entries}:
        raise ResolutionError(f'Unsupported PHP minor: {php}')
    tags = affected_tags(entries, root, base_ref) if base_ref else {entry['tagMinor'] for entry in entries if php == 'All' or entry['minor'] == php}
    return {'phpimages': [entry for entry in entries if entry['tagMinor'] in tags],
            'report': [{'tag': entry['tagMinor'], 'state': 'Selected' if entry['tagMinor'] in tags else 'Not selected'} for entry in entries]}


def check_current(entry, root):
    if not re.fullmatch(r'[a-f0-9]{40}', entry['source_ref']):
        raise ResolutionError('Invalid candidate source commit')
    git(root, 'fetch', '--no-tags', 'origin', '+refs/heads/main:refs/remotes/origin/main')
    return entry['tagMinor'] in affected_tags([entry], root, entry['source_ref'], 'refs/remotes/origin/main')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check-current', help='Check whether one variant JSON entry is superseded')
    mode.add_argument('--base-ref')
    mode.add_argument('--php', default='All', help='Rebuild one configured PHP minor, or All')
    parser.add_argument('--github-output')
    parser.add_argument('--summary')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if not args.check_current and not args.out:
        parser.error('--out is required for variant selection')
    try:
        if args.check_current:
            superseded = check_current(json.loads(args.check_current), root)
            if args.github_output:
                with open(args.github_output, 'a') as output:
                    output.write(f'superseded={str(superseded).lower()}\n')
            print('Superseded' if superseded else 'Current')
            return 0
        result = select(manifests(root), root, args.base_ref, args.php)
        Path(args.out).write_text(canonical(result) + '\n')
        if args.github_output:
            with open(args.github_output, 'a') as output:
                output.write(f'matrix={canonical(result)}\n')
                output.write(f'has_changes={str(bool(result["phpimages"])).lower()}\n')
        report = '| Variant | Selection |\n| --- | --- |\n' + ''.join(f'| {row["tag"]} | {row["state"]} |\n' for row in result['report'])
        print(report)
        if args.summary:
            with open(args.summary, 'a') as output: output.write('### Variant selection\n\n' + report)
    except (ResolutionError, KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'Variant selection failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
