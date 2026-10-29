#!/usr/bin/env python3
"""Opt-in checks for the real group controller and an inexpensive multi-Node cache graph."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from build_group import CachePolicy, Commands, run_group
from image_config import load_image_variants
from check_extension_cache import compilation_results, fresh_builder


class FixtureCommands(Commands):
    def run(self, command, log):
        command = list(command)
        if command[0] == 'regctl':
            # Exercise real Docker/OCI identity checks without pushing any fixture images.
            log.write_text('Registry transfer stubbed after candidate verification\n')
            return
        if command[:3] == ['docker', 'buildx', 'bake']:
            command[command.index('--progress') + 1] = 'rawjson'
        with log.open('w') as output:
            result = subprocess.run(command, cwd=self.root, stdout=output, stderr=subprocess.STDOUT,
                                    env={**os.environ, 'BUILDX_BAKE_DISABLE_VARS_ENV_LOOKUP': '1'})
        if result.returncode:
            raise RuntimeError(f'Command failed; see {log}')


def fixture_check(logs):
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    plain = next(v for v in load_image_variants(ROOT, source) if v['minor'] == '8.4' and not v['node'])
    prefix = 'group-cache-' + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix='php-group-cache-') as temp:
        context = Path(temp)
        (context / 'docker-bake.hcl').write_text('target "_common" {\n  context = "."\n  attest = ["type=provenance,disabled=true"]\n}\n')
        (context / 'Dockerfile').write_text('''ARG PHP_BASE_IMAGE
FROM ${PHP_BASE_IMAGE} AS extension-fixture
RUN head -c 32 /dev/urandom > /module.so
FROM ${PHP_BASE_IMAGE} AS php
COPY --from=extension-fixture /module.so /module.so
FROM php AS php-node
ARG NODE_BASE_IMAGE
LABEL fixture.node="${NODE_BASE_IMAGE}"
''')
        variants = []
        hcl = []
        for major in ('', '24', '26'):
            tag = prefix + ('-node' + major if major else '')
            target = 'fixture' + ('-node' + major if major else '')
            variants.append({**plain, 'bake_target': target, 'bake_file': 'fixture.hcl', 'base_target': 'fixture',
                             'tagMinor': tag, 'tagPatch': tag, 'node': major})
            hcl.append(f'''target "{target}" {{
  inherits = ["_common"]
  target = "{'php-node' if major else 'php'}"
  args = {{ PHP_BASE_IMAGE = "{plain['php_image']}", NODE_BASE_IMAGE = "fixture-node-{major}" }}
}}''')
        (context / 'fixture.hcl').write_text('\n'.join(hcl))
        images = ['docker-php-ci:' + v['tagMinor'] for v in variants]
        try:
            for name, selected in (('all', variants), ('node-only', variants[1:])):
                directory = logs / name
                hashes = []
                commands = FixtureCommands(context)
                def check_image(commands, entry, image, work):
                    value = commands.capture(['docker', 'run', '--rm', '--entrypoint', 'sha256sum', image, '/module.so'])
                    hashes.append(value)
                    if hashes[0] != value:
                        raise AssertionError('A Node variant used different compiler artifacts')
                with fresh_builder(context) as builder, patch('build_group.test_image', check_image):
                    request = {'group_id': 'fixture', 'source_ref': source, 'arch': 'amd64', 'run_attempt': '1',
                               'variants': selected, 'cache': asdict(CachePolicy('example.test/cache')),
                               'builder': builder, 'publish': name == 'all', 'no_cache': True, 'image': 'example.test/fixture'}
                    bundle = run_group(request, context, directory, commands)
                if any(r['status'] != 'success' for r in bundle['results'].values()):
                    raise AssertionError(f'{name}: group failed; see {directory}')
                for index, entry in enumerate(selected):
                    log = (directory / entry['tagMinor'] / 'build.log').read_text()
                    executed = compilation_results(log, {'fixture'})
                    if executed != ({'fixture'} if index == 0 else set()):
                        raise AssertionError(f'{name}/{entry["tagMinor"]}: unexpected compilation: {executed}')
                print(f'{name}: PHP compiled once across {len(selected)} variants; artifact hashes match', flush=True)
        finally:
            subprocess.run(['docker', 'image', 'rm', *images], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def real_check(args):
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    variants = load_image_variants(ROOT, source)
    candidates = [v for v in variants if v['minor'] == args.php]
    families = {v['base_target'] for v in candidates}
    if args.base_target:
        families &= {args.base_target}
    if len(families) != 1:
        raise ValueError('Select exactly one configured family using --php and --base-target')
    selected = [v for v in candidates if v['base_target'] in families]
    env = {**os.environ, 'VARIANTS': json.dumps(selected), 'SOURCE_REF': source, 'ARCH': 'amd64',
           'GITHUB_RUN_ATTEMPT': '1', 'CACHE_IMAGE': 'ghcr.io/marlub/php-buildcache', 'PUBLISH': 'false',
           'NO_CACHE': 'false', 'BUILDER': args.builder, 'IMAGE': '', 'RUNNER_TEMP': str(args.logs),
           'GITHUB_OUTPUT': str(args.logs / 'outputs'), 'GITHUB_STEP_SUMMARY': str(args.logs / 'summary.md'),
           'EVENT_NAME': 'local-check', 'REF': '', 'ACTOR': 'local-check', 'MERGED_PRS': '[]'}
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_group.py'), 'prepare'], cwd=ROOT, env=env, check=True)
    request = args.logs / 'php-build-groups' / selected[0]['base_target'] / 'request.json'
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_group.py'), 'run', '--request', str(request)],
                   cwd=ROOT, env=env, check=True)
    print('Real Alpine group controller and all selected image tests passed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--logs', type=Path, required=True)
    parser.add_argument('--fixture', action='store_true')
    parser.add_argument('--builder')
    parser.add_argument('--php', default='8.4')
    parser.add_argument('--base-target')
    args = parser.parse_args()
    args.logs = args.logs.resolve()
    args.logs.mkdir(parents=True, exist_ok=True)
    architecture = subprocess.check_output(['docker', 'version', '--format', '{{.Server.Arch}}'], text=True).strip()
    if architecture != 'amd64':
        parser.error('This local check currently expects a native AMD64 engine; hosted CI covers both architectures')
    if args.fixture:
        fixture_check(args.logs)
    elif args.builder:
        real_check(args)
    else:
        parser.error('--builder is required for a real configured build')


if __name__ == '__main__':
    main()
