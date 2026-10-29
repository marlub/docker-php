#!/usr/bin/env python3
"""Opt-in registry cache transfer check using disposable Docker resources."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


MANIFEST_TYPES = 'application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'


def docker(*arguments):
    result = subprocess.run(['docker', *arguments], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"docker {' '.join(arguments)} failed: {result.stderr[-3000:]}")
    return result.stdout.strip()


def compiler_cached(log):
    vertices = {}
    for line in log.splitlines():
        try:
            status = json.loads(line)
        except ValueError:
            continue
        for vertex in status.get('vertexes', []):
            vertices[vertex['digest']] = {**vertices.get(vertex['digest'], {}), **vertex}
    matches = [vertex for vertex in vertices.values()
               if re.search(r'\[(?:linux/\S+ )?compiler [^\]]+\] RUN ', vertex.get('name', ''))]
    if len(matches) != 1:
        raise AssertionError(f'Expected one compiler vertex, found {len(matches)}')
    return bool(matches[0].get('cached', False))


def verify_archives(directory, phase, arch):
    with tarfile.open(directory / f'{phase}-{arch}-docker.tar') as archive:
        manifest = json.load(archive.extractfile('manifest.json'))
        docker_config = json.load(archive.extractfile(manifest[0]['Config']))
    with tarfile.open(directory / f'{phase}-{arch}-oci.tar') as archive:
        index = json.load(archive.extractfile('index.json'))
        digest = index['manifests'][0]['digest'].split(':')[1]
        manifest = json.load(archive.extractfile('blobs/sha256/' + digest))
        config_digest = manifest['config']['digest'].split(':')[1]
        oci_config = json.load(archive.extractfile('blobs/sha256/' + config_digest))
    if docker_config != oci_config or docker_config['architecture'] != arch:
        raise AssertionError(f'{phase}/{arch}: Docker and OCI configs or architecture differ')


def delete_pr_manifest(endpoint, arch):
    url = f'{endpoint}/v2/pr-cache/manifests/pr-42-{arch}'
    with urlopen(Request(url, method='HEAD', headers={'Accept': MANIFEST_TYPES}), timeout=15) as response:
        digest = response.headers['Docker-Content-Digest']
    with urlopen(Request(f'{endpoint}/v2/pr-cache/manifests/{digest}', method='DELETE'), timeout=15):
        pass
    try:
        with urlopen(Request(url, method='HEAD', headers={'Accept': MANIFEST_TYPES}), timeout=15):
            raise AssertionError('PR cache manifest still exists after deletion')
    except HTTPError as error:
        if error.code != 404:
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--logs', type=Path, required=True)
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)
    prefix = 'php-cache-check-' + uuid.uuid4().hex[:10]
    network = prefix + '-net'
    registry = prefix + '-registry'
    builders = []
    network_created = registry_created = False
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        context = Path(temporary)
        # A native intermediate stage produces a target-specific artifact, so the
        # cache lifecycle is checked on both platforms without requiring emulation.
        (context / 'Dockerfile').write_text(
            'FROM --platform=$BUILDPLATFORM busybox:1.37.0 AS compiler\n'
            'ARG TARGETARCH\n'
            'RUN mkdir /out && printf "compiled-for-%s\\n" "$TARGETARCH" > /out/result\n'
            'FROM scratch\nCOPY --from=compiler /out/result /result\n'
        )
        (context / 'buildkitd.toml').write_text(f'[registry."{registry}:5000"]\n  http = true\n')
        try:
            docker('network', 'create', network)
            network_created = True
            docker('run', '-d', '--name', registry, '--network', network,
                   '-p', '127.0.0.1::5000', '-e', 'REGISTRY_STORAGE_DELETE_ENABLED=true', 'registry:2')
            registry_created = True
            address = docker('port', registry, '5000/tcp').splitlines()[0]
            endpoint = 'http://' + address
            for attempt in range(40):
                try:
                    with urlopen(endpoint + '/v2/', timeout=1):
                        break
                except (URLError, OSError):
                    if attempt == 39:
                        raise
                    time.sleep(0.25)
            for phase in ('pr', 'main', 'main-only'):
                builder = prefix + '-' + phase
                docker('buildx', 'create', '--name', builder, '--driver', 'docker-container',
                       '--driver-opt', f'network={network}', '--buildkitd-config', str(context / 'buildkitd.toml'))
                builders.append(builder)
                for arch in ('amd64', 'arm64'):
                    pr_ref = f'{registry}:5000/pr-cache:pr-42-{arch}'
                    main_ref = f'{registry}:5000/main-cache:main-{arch}'
                    command = ['docker', 'buildx', 'build', '--builder', builder, '--platform', f'linux/{arch}',
                               '--progress', 'rawjson', '--provenance=false',
                               '--output', f'type=docker,dest={context}/{phase}-{arch}-docker.tar',
                               '--output', f'type=oci,dest={context}/{phase}-{arch}-oci.tar']
                    if phase == 'main':
                        command += ['--cache-from', f'type=registry,ref={pr_ref}']
                    if phase != 'pr':
                        command += ['--cache-from', f'type=registry,ref={main_ref}']
                    command += ['--cache-to', f'type=registry,ref={pr_ref if phase == "pr" else main_ref},mode=max', str(context)]
                    log = args.logs / f'{phase}-{arch}.log'
                    print(f'{phase}/{arch}: building with fresh {phase} builder; log {log}', flush=True)
                    with log.open('w') as output:
                        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
                    if result.returncode:
                        raise RuntimeError(f'{phase}/{arch}: build failed; see {log}')
                    cached = compiler_cached(log.read_text())
                    if cached != (phase != 'pr'):
                        raise AssertionError(f'{phase}/{arch}: compiler cached={cached}; see {log}')
                    verify_archives(context, phase, arch)
                    print(f'{phase}/{arch}: compiler cached={cached}; matching Docker/OCI exports', flush=True)
                docker('buildx', 'rm', '--force', builder)
                builders.remove(builder)
                if phase == 'main':
                    # Separate repositories ensure that deleting an identical PR
                    # manifest cannot remove main's manifest by its shared digest.
                    for arch in ('amd64', 'arm64'):
                        delete_pr_manifest(endpoint, arch)
                    print('Removed both PR cache manifests before main-only builds', flush=True)
            print('Registry cache transfer and independence verified on amd64 and arm64', flush=True)
        finally:
            for builder in builders:
                subprocess.run(['docker', 'buildx', 'rm', '--force', builder], check=False)
            if registry_created:
                subprocess.run(['docker', 'rm', '-fv', registry], check=False, stdout=subprocess.DEVNULL)
            if network_created:
                subprocess.run(['docker', 'network', 'rm', network], check=False, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
