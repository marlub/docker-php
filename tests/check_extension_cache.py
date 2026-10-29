#!/usr/bin/env python3
"""Explicit BuildKit integration check; mutations stay in a disposable context."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from image_config import load_image_variants, variant_order
from build_group import Commands, test_image


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


@contextmanager
def fresh_builder(directory):
    """Match CI's empty builder so old matching records cannot win a forced solve."""
    name = 'php-forced-pair-' + uuid.uuid4().hex[:12]
    config = directory / 'buildkitd.toml'
    config.write_text('[worker.oci]\n  max-parallelism = 1\n')
    subprocess.run(['docker', 'buildx', 'create', '--name', name, '--driver', 'docker-container',
                    '--buildkitd-config', str(config)], check=True)
    try:
        yield name
    finally:
        result = subprocess.run(['docker', 'buildx', 'rm', '--force', name])
        if result.returncode:
            print(f'Warning: could not remove temporary builder {name}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builder', required=True)
    parser.add_argument('--php', default='8.4')
    parser.add_argument('--base-target', help='Select one base family when a PHP minor has multiple distributions/releases')
    parser.add_argument('--image', default='docker-php:extension-cache-check')
    parser.add_argument('--logs', type=Path, required=True)
    parser.add_argument('--paired-only', action='store_true', help='Check paired normal/forced builds and image tests only')
    args = parser.parse_args()
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    variants = load_image_variants(ROOT, source_ref=source)
    families = {v['base_target'] for v in variants if v['minor'] == args.php}
    if args.base_target:
        families &= {args.base_target}
    if len(families) != 1:
        parser.error('Select exactly one configured family using --php and --base-target')
    family = sorted([v for v in variants if v['base_target'] in families], key=variant_order)
    entry = next(v for v in family if not v['node'])
    nodes = [v for v in family if v['node']]
    args.logs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='php-extension-cache-') as temporary:
        context = Path(temporary)
        for directory in ('scripts', 'build'):
            shutil.copytree(ROOT / directory, context / directory,
                            ignore=shutil.ignore_patterns('__pycache__'))
        for name in (entry['dockerfile'], 'docker-bake.hcl', 'entrypoint.sh'):
            shutil.copy2(ROOT / name, context / name)
        dockerfile = context / entry['dockerfile']
        expected_stages = set(re.findall(r'^FROM extension-base AS extension-([a-z]+)$', dockerfile.read_text(), re.M))
        overrides = {}

        def build(name, expected=None, *, variant=None, selection=None, no_cache=False, builder=None):
            variant = variant or entry
            target = variant['bake_target']
            command = ['docker', 'buildx', 'bake', '-f', 'docker-bake.hcl', '-f', entry['bake_file'],
                       '--builder', builder or args.builder, '--progress', 'rawjson',
                       '--set', f'{target}.output=type=docker', '--set', f'{target}.tags={args.image}',
                       '--set', f'{target}.labels.io.github.marlub.docker-php.variant={variant["tagMinor"]}',
                       '--set', f'{target}.labels.org.opencontainers.image.revision={source}']
            for key, value in overrides.items():
                command += ['--set', f'{target}.args.{key}={value}']
            if selection is not None:
                command += ['--var', f'EXTENSIONS={selection}']
            if no_cache:
                command.append('--no-cache')
            path = args.logs / f'{name}.log'
            started = time.monotonic()
            with path.open('w') as output:
                result = subprocess.run([*command, target], cwd=context,
                                        env={**os.environ, 'BUILDX_BAKE_DISABLE_VARS_ENV_LOOKUP': '1'},
                                        stdout=output, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(f'Build failed; see {path}')
            compiled = compilation_results(path.read_text(), expected_stages)
            if expected is not None and compiled != expected:
                raise AssertionError(f'{name}: executed {compiled}, expected {expected}; see {path}')
            print(f'{name}: compiler steps executed: {", ".join(sorted(compiled)) or "none"}; '
                  f'{time.monotonic() - started:.1f}s', flush=True)

        def image_checks(variant):
            logs = args.logs / ('image-checks-' + variant['tagMinor'])
            logs.mkdir(exist_ok=True)
            test_image(Commands(ROOT), variant, args.image, logs)

        def module_hashes():
            return subprocess.check_output([
                'docker', 'run', '--rm', '--entrypoint', 'sh', args.image, '-c',
                'find "$(php-config --extension-dir)" -type f -name "*.so" -exec sha256sum {} \\; | sort',
            ], text=True)

        build('baseline')
        for node in nodes:
            build('paired-node' + node['node'], set(), variant=node)
        build('unchanged', set())
        # No registry imports are configured. A separate empty builder is essential:
        # the caller's builder can contain older results for the same compiler keys.
        with fresh_builder(context) as forced_builder:
            build('forced-plain', expected_stages, no_cache=True, builder=forced_builder)
            fresh_modules = module_hashes()
            if not fresh_modules.strip():
                raise AssertionError('No PHP module hashes were collected')
            (args.logs / 'forced-plain-modules.sha256').write_text(fresh_modules)
            if args.paired_only:
                image_checks(entry)
            for node in nodes:
                build('forced-node' + node['node'], set(), variant=node, builder=forced_builder)
                node_modules = module_hashes()
                (args.logs / ('forced-node' + node['node'] + '-modules.sha256')).write_text(node_modules)
                if fresh_modules != node_modules:
                    raise AssertionError('Node did not retain the freshly rebuilt PHP modules')
                if args.paired_only:
                    image_checks(node)
        if args.paired_only:
            print('Normal/forced family builds and all selected images verified', flush=True)
            return
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
        for node in nodes:
            build('node-baseline-' + node['node'], set(), variant=node)
        node_stage = re.search(r'^FROM \$\{NODE_BASE_IMAGE[^}]*\} AS node\n', dockerfile.read_text(), re.M)
        if node_stage is None:
            raise AssertionError('Unsupported Node source stage')
        dockerfile.write_text(dockerfile.read_text().replace(
            node_stage[0], node_stage[0].replace(' AS node\n', ' AS node-source\n') +
            'FROM ${PHP_BASE_IMAGE} AS node\n'
            'COPY --from=node-source /usr/local /usr/local\n'
            'COPY --from=node-source /usr/lib /usr/lib\n'
            'RUN chmod 0750 /usr/local/bin/node\n'))
        for node in nodes:
            build('node-source-' + node['node'], set(), variant=node)
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
