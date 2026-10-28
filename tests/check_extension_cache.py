#!/usr/bin/env python3
"""Explicit BuildKit integration check; mutations stay in a disposable context."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from image_config import load_image_variants


def compilation_results(log, expected_stages):
    vertices = {}
    for line in log.splitlines():
        try:
            status = json.loads(line)
        except ValueError:
            continue
        for vertex in status.get('vertexes', []):
            key = vertex['digest']
            vertices[key] = {**vertices.get(key, {}), **vertex}
    compilers = {}
    for vertex in vertices.values():
        match = re.search(r'\[(?:[^\]]* )?extension-([a-z]+) [^\]]+\] RUN ', vertex.get('name', ''))
        if match and match[1] in expected_stages:
            compilers[match[1]] = vertex
    if set(compilers) != expected_stages:
        raise AssertionError(f'Compiler stages: {set(compilers)}, expected {expected_stages}')
    return {name for name, vertex in compilers.items() if not vertex.get('cached', False)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builder', required=True)
    parser.add_argument('--php', default='8.4')
    parser.add_argument('--image', default='docker-php:extension-cache-check')
    parser.add_argument('--logs', type=Path, required=True)
    args = parser.parse_args()
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    entry = next(v for v in load_image_variants(ROOT, source_ref=source)
                 if v['minor'] == args.php and not v['node'])
    args.logs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='php-extension-cache-') as temporary:
        context = Path(temporary)
        for directory in ('scripts', 'build'):
            shutil.copytree(ROOT / directory, context / directory,
                            ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('Dockerfile', 'docker-bake.hcl', 'entrypoint.sh'):
            shutil.copy2(ROOT / name, context / name)
        dockerfile = context / 'Dockerfile'
        expected_stages = set(re.findall(r'^FROM extension-base AS extension-([a-z]+)$', dockerfile.read_text(), re.M))
        overrides = {}

        def build(name, expected=None, *, node=False, selection=None):
            target = entry['bake_target'] + ('-node24' if node else '')
            command = ['docker', 'buildx', 'bake', '-f', 'docker-bake.hcl', '-f', entry['bake_file'],
                       '--builder', args.builder, '--progress', 'rawjson',
                       '--set', f'{target}.output=type=docker', '--set', f'{target}.tags={args.image}',
                       '--set', f'{target}.labels.io.github.marlub.docker-php.variant={entry["tagMinor"]}' + ('-node24' if node else ''),
                       '--set', f'{target}.labels.org.opencontainers.image.revision={source}']
            for key, value in overrides.items():
                command += ['--set', f'{target}.args.{key}={value}']
            if selection is not None:
                command += ['--var', f'EXTENSIONS={selection}']
            path = args.logs / f'{name}.log'
            with path.open('w') as output:
                result = subprocess.run([*command, target], cwd=context,
                                        env={**os.environ, 'BUILDX_BAKE_DISABLE_VARS_ENV_LOOKUP': '1'},
                                        stdout=output, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(f'Build failed; see {path}')
            compiled = compilation_results(path.read_text(), expected_stages)
            if expected is not None and compiled != expected:
                raise AssertionError(f'{name}: executed {compiled}, expected {expected}; see {path}')
            print(f'{name}: compiler steps executed: {", ".join(sorted(compiled)) or "none"}', flush=True)

        build('baseline')
        build('unchanged', set())
        flags = re.search(r'^ARG REDIS_CONFIGURE_OPTIONS="([^"]*)"', dockerfile.read_text(), re.M)[1].split()
        overrides['REDIS_CONFIGURE_OPTIONS'] = ' '.join(reversed(flags))
        build('redis-options', {'redis'})
        tag = entry['xhprof']['tag']
        overrides['XHPROF_TAG'] = tag[1:] if tag.startswith('v') else 'v' + tag
        build('xhprof-metadata', {'xhprof'})
        dockerfile.write_text(dockerfile.read_text().replace(
            'FROM ${COMPOSER_IMAGE} AS composer\n',
            'FROM ${COMPOSER_IMAGE} AS composer-source\n'
            'FROM ${PHP_BASE_IMAGE} AS composer\n'
            'COPY --from=composer-source /composer /composer\n'
            'RUN chmod 0744 /composer\n'))
        build('composer-source', set())
        build('node-baseline', set(), node=True)
        dockerfile.write_text(dockerfile.read_text().replace(
            'FROM ${NODE_BASE_IMAGE:-node:24-alpine} AS node\n',
            'FROM ${NODE_BASE_IMAGE:-node:24-alpine} AS node-source\n'
            'FROM ${PHP_BASE_IMAGE} AS node\n'
            'COPY --from=node-source /usr/local /usr/local\n'
            'COPY --from=node-source /usr/lib /usr/lib\n'
            'RUN chmod 0750 /usr/local/bin/node\n'))
        build('node-source', set(), node=True)
        selection = ' '.join(n for n in entry['extensions'].split() if n != 'grpc')
        build('grpc-disabled', {'grpc'}, selection=selection)
        subprocess.run(['docker', 'run', '--rm', '--entrypoint', 'php', args.image, '-r',
                        'exit(is_file(ini_get("extension_dir")."/grpc.so") ? 1 : 0);'], check=True)
        build('empty', selection='')
        subprocess.run(['docker', 'run', '--rm', '--entrypoint', 'sh', args.image, '-c',
                        'test -z "$EXTENSIONS"; ! command -v composer'], check=True)
        print('Unsupported and empty selections contain no excluded extension/Composer', flush=True)


if __name__ == '__main__':
    main()
