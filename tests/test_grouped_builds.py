import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from test_images import RepositoryTests
from build_group import (CachePolicy, Commands, bake_command, main, prepare_request, run_group,
                         selected_digest, transfer_image)
from check_extension_cache import fresh_builder
from variant_fixtures import add_family_variants
from workflow_helpers import BUILD_ACTION, IMAGE_WORKFLOW, ROOT


def digest(value):
    return 'sha256:' + hashlib.sha256(value.encode()).hexdigest()


def write_oci(path, config_id, image_digest, manifests=None):
    files = {'index.json': {'manifests': manifests if manifests is not None else [{'digest': image_digest}]},
             'blobs/sha256/' + image_digest.split(':')[1]: {'config': {'digest': config_id}}}
    with tarfile.open(path, 'w') as archive:
        for name, value in files.items():
            data = json.dumps(value).encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


class FakeCommands:
    """Emulate external commands while exercising the actual controller and OCI validation."""
    def __init__(self, root, failures=None, cancel=None):
        self.root = root
        self.calls = []
        self.failures = failures or {}
        self.cancel = cancel
        self.current = ''

    def run(self, command, log):
        self.calls.append(command)
        stage = 'test'
        if command[:3] == ['docker', 'buildx', 'bake']:
            self.current = next(value.removeprefix('*.tags=') for value in command if value.startswith('*.tags='))
            stage = 'build'
        elif command[0] == 'regctl':
            stage = 'transfer'
        tag = self.current.split(':', 1)[1]
        log.write_text(' '.join(command) + '\n')
        if self.cancel == (tag, stage):
            raise KeyboardInterrupt('cancelled')
        if self.failures.get(tag) == stage:
            raise subprocess.CalledProcessError(7, command)
        if stage == 'build':
            oci = next((s.split('dest=', 1)[1] for s in command if s.startswith('*.output+=type=oci,dest=')), None)
            if oci:
                write_oci(Path(oci), digest(self.current), digest('manifest-' + self.current))

    def capture(self, command):
        self.calls.append(command)
        return digest(command[3])

    @property
    def builds(self):
        return [c for c in self.calls if c[:3] == ['docker', 'buildx', 'bake']]


class GroupedBuildTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        add_family_variants(self.root)
        self.commit()
        self.family = [v for v in self.entries() if v['base_target'] == 'php-8-4']
        self.directory = self.root / '.git/build-results'
        self.directory.mkdir()

    def request(self, *, variants=None, forced=False):
        return {'group_id': 'php-8-4', 'source_ref': self.family[0]['source_ref'], 'arch': 'amd64', 'run_attempt': '2',
                'variants': self.family if variants is None else variants, 'cache': {'image': 'example.test/cache', 'scope': 'main', 'merged_prs': [42]},
                'builder': 'same-builder', 'publish': True, 'no_cache': forced, 'image': 'example.test/php'}

    def run_controller(self, request, commands):
        with patch('builtins.print'), patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(self.directory / 'summary')}):
            return run_group(request, self.root, self.directory, commands)

    def test_all_variants_share_builder_and_have_distinct_outputs_and_candidates(self):
        request = self.request()
        commands = FakeCommands(self.root)
        bundle = self.run_controller(request, commands)
        self.assertEqual([c[-1] for c in commands.builds], [v['bake_target'] for v in self.family])
        for call in commands.builds:
            self.assertEqual(call[call.index('--builder') + 1], 'same-builder')
        self.assertEqual(len({next(s for s in c if s.startswith('*.tags=')) for c in commands.builds}), 3)
        self.assertEqual(len({next(s for s in c if s.startswith('*.output+=type=oci')) for c in commands.builds}), 3)
        for entry in self.family:
            result = bundle['results'][entry['tagMinor']]
            self.assertEqual(result['status'], 'success')
            self.assertEqual(result['digest'], digest('manifest-docker-php-ci:' + entry['tagMinor']))
            self.assertFalse((self.directory / entry['tagMinor'] / 'image.tar').exists())
            self.assertFalse((self.directory / entry['tagMinor'] / 'oci').exists())
        self.assertEqual(json.loads((self.directory / 'results.json').read_text()), bundle)

    def test_failures_in_every_variant_and_phase_do_not_block_other_candidates(self):
        for entry in self.family:
            for stage in ('build', 'test', 'transfer'):
                with self.subTest(variant=entry['tagMinor'], stage=stage):
                    commands = FakeCommands(self.root, {entry['tagMinor']: stage})
                    bundle = self.run_controller(self.request(forced=True), commands)
                    self.assertEqual(len(commands.builds), 3)
                    for other in self.family:
                        result = bundle['results'][other['tagMinor']]
                        if other == entry:
                            self.assertEqual(result['status'], 'failure')
                            self.assertNotIn('digest', result)
                            self.assertEqual(result['built'], stage != 'build')
                            with self.assertRaises(ValueError): selected_digest(bundle, other, 'amd64', '2')
                        else:
                            self.assertEqual(result['status'], 'success')
                            selected_digest(bundle, other, 'amd64', '2')

    def test_forced_node_only_groups_share_the_first_completed_php_build(self):
        nodes = self.family[1:]
        for failure, expected in ((None, [True, False]), ('test', [True, False]),
                                  ('transfer', [True, False]), ('build', [True, True])):
            with self.subTest(failure=failure):
                commands = FakeCommands(self.root, {nodes[0]['tagMinor']: failure})
                self.run_controller(self.request(variants=nodes, forced=True), commands)
                self.assertEqual([c[-1] for c in commands.builds], [v['bake_target'] for v in nodes])
                self.assertEqual(['--no-cache' in c for c in commands.builds], expected)
                self.assertTrue(all(not any('.cache-from' in value for value in c) for c in commands.builds))
                self.assertTrue(all(any('.cache-to=' in value for value in c) for c in commands.builds))
        commands = FakeCommands(self.root)
        self.run_controller(self.request(forced=True), commands)
        self.assertEqual(['--no-cache' in c for c in commands.builds], [True, False, False])

    def test_actual_bake_arguments_resolve_both_exports_and_forced_cache_policy(self):
        entry = self.family[2]
        for forced in (False, True):
            for fresh in (False, True):
                with self.subTest(forced=forced, fresh=fresh):
                    request = self.request(forced=forced)
                    command = bake_command(entry, request, self.directory, fresh_php=fresh)
                    command.insert(-1, '--print')
                    result = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    targets = json.loads(result.stdout)['target']
                    self.assertEqual(list(targets), [entry['bake_target']])
                    config = targets[entry['bake_target']]
                    self.assertEqual(config.get('no-cache', False), forced and not fresh)
                    self.assertEqual(config['platforms'], ['linux/amd64'])
                    self.assertEqual(config['tags'], ['docker-php-ci:' + entry['tagMinor']])
                    self.assertEqual([v['type'] for v in config['output']], ['docker', 'oci'])
                    self.assertEqual(len(config.get('cache-from', [])), 0 if forced else 2)
                    self.assertEqual(config['cache-to'][0]['mode'], 'max')
                    self.assertTrue(config['args']['NODE_BASE_IMAGE'].startswith('node:26.1.0-'))

    def test_cancellation_stops_later_variants_but_preserves_completed_results(self):
        second = self.family[1]['tagMinor']
        commands = FakeCommands(self.root, cancel=(second, 'test'))
        with self.assertRaises(KeyboardInterrupt):
            self.run_controller(self.request(forced=True), commands)
        self.assertEqual(len(commands.builds), 2)
        bundle = json.loads((self.directory / 'results.json').read_text())
        self.assertEqual(bundle['results'][self.family[0]['tagMinor']]['status'], 'success')
        self.assertEqual(bundle['results'][self.family[2]['tagMinor']]['status'], 'not-run')
        self.assertNotIn('digest', bundle['results'][second])

    def test_debian_without_package_backend_fails_explicitly_before_build(self):
        family = [v for v in self.entries() if v['distribution'] == 'debian']
        request = self.request(variants=family)
        request['group_id'] = family[0]['base_target']
        commands = FakeCommands(self.root)
        bundle = self.run_controller(request, commands)
        self.assertFalse(commands.builds)
        for result in bundle['results'].values():
            self.assertIn('Unsupported package-check backend: debian', result['error'])
            self.assertEqual(result['status'], 'failure')
            self.assertNotIn('digest', result)

    def test_bundle_identity_and_candidate_status_are_required_per_architecture(self):
        bundle = self.run_controller(self.request(), FakeCommands(self.root))
        entry = self.family[0]
        for key, value in (('source_ref', 'old'), ('group_id', 'other-family'), ('arch', 'arm64'), ('run_attempt', '1')):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    selected_digest({**bundle, key: value}, entry, 'amd64', '2')
        for result in ({}, {'status': 'failure', 'digest': digest('x')}, {'status': 'success', 'digest': 'invalid'}):
            with self.assertRaises(ValueError):
                selected_digest({**bundle, 'results': {entry['tagMinor']: result}}, entry, 'amd64', '2')
        for malformed in (None, [], {'results': []}, {**bundle, 'results': {entry['tagMinor']: []}}):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                selected_digest(malformed, entry, 'amd64', '2')

    def test_extract_cli_requires_both_architectures_before_writing_digests(self):
        bundle = self.run_controller(self.request(), FakeCommands(self.root))
        bundles = self.directory / 'downloads'
        for arch in ('amd64', 'arm64'):
            (bundles / arch).mkdir(parents=True)
            (bundles / arch / 'results.json').write_text(json.dumps({**bundle, 'arch': arch}))
        output = self.directory / 'digests'
        command = [sys.executable, str(self.root / 'scripts/build_group.py'), 'extract', '--entry', json.dumps(self.family[0]),
                   '--bundles', str(bundles), '--attempt', '2', '--out', str(output)]
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual((output / 'amd64.digest').read_text(), selected_digest(bundle, self.family[0], 'amd64', '2') + '\n')
        for path in output.iterdir(): path.unlink()
        (bundles / 'arm64/results.json').unlink()
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(list(output.iterdir()), [])
        (bundles / 'arm64/results.json').write_text(json.dumps({**bundle, 'arch': 'arm64', 'run_attempt': '1'}))
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(list(output.iterdir()), [])

    def test_prepare_request_validates_checkout_and_does_not_store_credentials(self):
        env = {'SOURCE_REF': self.family[0]['source_ref'], 'VARIANTS': json.dumps(self.family[::-1]), 'ARCH': 'amd64',
               'GITHUB_RUN_ATTEMPT': '2', 'CACHE_IMAGE': 'example.test/cache', 'PUBLISH': 'true', 'NO_CACHE': 'true',
               'BUILDER': 'builder', 'IMAGE': 'example.test/php', 'RUNNER_TEMP': str(self.directory),
               'ACTOR': 'contributor', 'REF': 'refs/heads/main', 'EVENT_NAME': 'push', 'TOKEN': 'not-for-request'}
        request, directory = prepare_request(self.root, env)
        self.assertEqual([e['node'] for e in request['variants']], ['', '24', '26'])
        self.assertEqual(request['cache']['scope'], 'main')
        self.assertNotIn(env['TOKEN'], (directory / 'request.json').read_text())
        with self.assertRaises(ValueError): prepare_request(self.root, {**env, 'SOURCE_REF': 'old-source'})

    def test_controller_cli_reports_failure_after_siblings_and_stops_on_cancellation(self):
        path = self.directory / 'request.json'
        path.write_text(json.dumps(self.request(forced=True)))
        first, second = [v['tagMinor'] for v in self.family[:2]]
        for commands, expected, attempted in (
            (FakeCommands(self.root, {first: 'test'}), 1, 3),
            (FakeCommands(self.root, cancel=(second, 'test')), 130, 2),
        ):
            with self.subTest(expected=expected):
                handler = signal.getsignal(signal.SIGTERM)
                try:
                    with patch('build_group.ROOT', self.root), patch('build_group.Commands', return_value=commands), \
                         patch('sys.argv', ['build_group.py', 'run', '--request', str(path)]), \
                         patch('builtins.print'), patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(self.directory / 'summary')}):
                        self.assertEqual(main(), expected)
                finally:
                    signal.signal(signal.SIGTERM, handler)
                self.assertEqual(len(commands.builds), attempted)


class CandidateTransferTests(unittest.TestCase):
    def test_transfer_checks_exact_loaded_image_and_returns_digest_only_after_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            image = 'docker-php-ci:8.4-fpm-alpine'
            config_id = digest(image)
            image_digest = digest('manifest')
            write_oci(directory / 'image.tar', config_id, image_digest)
            commands = FakeCommands(directory)
            commands.current = image
            self.assertEqual(transfer_image(commands, image, 'example.test/php', directory, config_id), image_digest)
            self.assertEqual(commands.calls[-1][-1], 'example.test/php@' + image_digest)

    def test_wrong_config_ambiguous_manifest_and_failed_upload_are_rejected(self):
        for failure in ('config', 'manifest', 'malformed', 'transfer'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                image = 'docker-php-ci:8.4-fpm-alpine'
                write_oci(directory / 'image.tar', digest('other') if failure == 'config' else digest(image),
                          digest('manifest'), manifests=[] if failure == 'manifest' else [None] if failure == 'malformed' else None)
                commands = FakeCommands(directory, {image.split(':')[1]: failure})
                commands.current = image
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    transfer_image(commands, image, 'example.test/php', directory, digest(image))

    def test_disposable_builder_is_removed_after_failed_image_checks(self):
        with tempfile.TemporaryDirectory() as temp, patch('check_extension_cache.subprocess.run') as run:
            run.return_value.returncode = 0
            with self.assertRaises(RuntimeError):
                with fresh_builder(Path(temp)) as builder:
                    raise RuntimeError('check failed')
            self.assertEqual(run.call_args.args[0], ['docker', 'buildx', 'rm', '--force', builder])


class WorkflowGuardsTests(unittest.TestCase):
    def test_one_group_call_and_current_attempt_bundles_preserve_independent_publication(self):
        workflow = IMAGE_WORKFLOW.read_text()
        build_job, publish = workflow.split('\n  publish:\n')
        self.assertEqual(build_job.count('uses: docker/setup-buildx-action@'), 1)
        self.assertEqual(build_job.count('uses: ./.github/actions/build-group'), 1)
        self.assertIn('builder: ${{ steps.builder.outputs.name }}', build_job)
        self.assertIn('max-parallelism = 1', build_job)
        self.assertNotIn('steps.plain', workflow)
        self.assertIn('if: ${{ !cancelled() && inputs.publish }}', publish)
        self.assertIn('variant: ${{ fromJson(inputs.variants) }}', publish)
        self.assertIn('fail-fast: false', publish)
        self.assertNotIn('needs.build-and-test.result', publish)
        for arch in ('amd64', 'arm64'):
            self.assertIn('name: build-results-${{ matrix.variant.base_target }}-' + arch + '-attempt-${{ github.run_attempt }}', publish)
        self.assertLess(publish.index('Require successful candidates from this attempt'), publish.index('Verify candidate metadata'))
        action = BUILD_ACTION.read_text()
        self.assertIn("if: ${{ !cancelled() && inputs.publish == 'true' && steps.prepare.outcome == 'success' }}", action)
        self.assertIn('build-results-${{ steps.prepare.outputs.id }}-${{ inputs.arch }}-attempt-${{ github.run_attempt }}', action)
        self.assertIn('if-no-files-found: error', action)
        self.assertEqual(action.count('name: Log in to cache registry'), 1)
        self.assertEqual(action.count('name: Log in to publication registry'), 1)
        for caller in ('build-test.yml', 'build-test-publish.yml'):
            content = (ROOT / '.github/workflows' / caller).read_text()
            self.assertIn('name: ${{ matrix.phpgroup.id }}', content)
            self.assertIn('variants: ${{ toJson(matrix.phpgroup.variants) }}', content)


if __name__ == '__main__':
    unittest.main()
