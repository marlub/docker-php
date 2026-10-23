#!/usr/bin/env python3
"""Verify installed versions against committed inputs and the source build record."""
import argparse
import json
import re
import subprocess
import sys
from select_images import REVISION_LABEL, VARIANT_LABEL


def verify_metadata(config, entry, platform):
    config = config.get(platform, config)
    labels = config.get('config', {}).get('Labels', {}) or {}
    if f'{config.get("os")}/{config.get("architecture")}' != platform:
        raise ValueError('Candidate architecture mismatch')
    if labels.get(REVISION_LABEL) != entry['source_ref'] or labels.get(VARIANT_LABEL) != entry['tagMinor']:
        raise ValueError('Candidate source/variant label mismatch')


def verify(image, entry, run=subprocess.check_output):
    labels = json.loads(run(['docker', 'image', 'inspect', image, '--format', '{{json .Config.Labels}}'], text=True))
    if labels.get(REVISION_LABEL) != entry['source_ref'] or labels.get(VARIANT_LABEL) != entry['tagMinor']:
        raise ValueError('Image source/variant labels do not match the selected build')
    actual_php = run(['docker', 'run', '--rm', image, 'php', '-r', 'echo PHP_VERSION;'], text=True).strip()
    if actual_php != entry['php_version']:
        raise ValueError(f'Unexpected PHP version: {actual_php}')
    expected = dict(entry['extension_versions'])
    if 'xhprof' in entry['extensions'].split():
        source = json.loads(run(['docker', 'run', '--rm', image, 'cat', '/usr/local/lib/php/xhprof-build.json'], text=True))
        if any(source[key] != entry['xhprof'][key] for key in ('tag', 'commit')) or not re.fullmatch(r'\d+\.\d+\.\d+', source['version']):
            raise ValueError('XHProf build source identity mismatch')
        expected['xhprof'] = source['version']
    command = ['docker', 'run', '--rm']
    for name in expected: command += ['-e', f'PHP_EXTENSION_{name.upper()}=true']
    command += [image, 'php', '-r', "echo json_encode(array_combine(array_slice($argv, 1), array_map('phpversion', array_slice($argv, 1))));", '--', *expected]
    if expected and json.loads(run(command, text=True)) != expected:
        raise ValueError('Installed extension versions do not match their pins')
    if entry['extensions']:
        expected_composer = entry['composer_version']
        output = run(['docker', 'run', '--rm', image, 'composer', '--version', '--no-ansi'], text=True)
        if not re.search(r'Composer version ' + re.escape(expected_composer) + r'\s', output):
            raise ValueError('Unexpected Composer version')
    if entry['node']:
        actual_node = run(['docker', 'run', '--rm', image, 'node', '--version'], text=True).strip()
        if actual_node != 'v' + entry['node_version']: raise ValueError('Unexpected Node version')
    print('Installed versions match committed pins')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--image')
    mode.add_argument('--metadata', action='store_true')
    parser.add_argument('--platform', choices=['linux/amd64', 'linux/arm64'])
    parser.add_argument('--entry', required=True)
    args = parser.parse_args()
    if args.metadata:
        if not args.platform:
            parser.error('--metadata requires --platform')
        verify_metadata(json.load(sys.stdin), json.loads(args.entry), args.platform)
    else:
        verify(args.image, json.loads(args.entry))
