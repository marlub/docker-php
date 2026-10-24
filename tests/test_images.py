import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from select_images import (PLATFORMS, REVISION_LABEL, VARIANT_LABEL, ResolutionError,
                           affected_tags, check_current, git, manifests, select)
from verify_image import verify, verify_metadata

IMAGE = 'example.test/php'


def digest(value):
    return 'sha256:' + hashlib.sha256(value.encode()).hexdigest()


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('build', 'scripts', '.github'):
            shutil.copytree(ROOT / name, self.root / name, ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('Dockerfile', 'entrypoint.sh', 'README.md', 'renovate.json'):
            shutil.copy(ROOT / name, self.root / name)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'test@example.test')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'core.hooksPath', '/dev/null')
        self.git('remote', 'add', 'origin', '.')
        self.base = self.commit()

    def git(self, *args):
        return git(self.root, *args).strip()

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture change')
        return self.git('rev-parse', 'HEAD')

    def entries(self):
        return manifests(self.root)

    def change_json(self, name, change):
        path = self.root / name
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value))

    def change_node(self):
        path = self.root / 'Dockerfile'
        path.write_text(re.sub(r'node:24\.\d+\.\d+-', 'node:24.999.0-', path.read_text()))

    def affected(self, change):
        before = self.git('rev-parse', 'HEAD')
        change()
        self.commit()
        return sorted(affected_tags(self.entries(), self.root, before))



class SelectionTests(RepositoryTests):
    def test_eight_variants_and_existing_tags(self):
        entries = self.entries()
        self.assertEqual(len(entries), 8)
        self.assertEqual(entries[0]['tagMinor'], '8.2-fpm-alpine')
        self.assertEqual(entries[-1]['tagMinor'], '8.5-fpm-alpine-node24')
        self.assertEqual(entries[0]['tagPatch'], entries[0]['php_version'] + '-fpm-alpine')
        self.assertNotIn('opcache', entries[-1]['extensions'].split())
        self.assertEqual(entries[0]['source_ref'], self.base)
        self.assertNotIn('input_hash', entries[0])
        self.assertNotIn('inputs', entries[0])

    def test_no_changes_select_nothing_without_registry(self):
        result = select(self.entries(), self.root, self.base)
        self.assertEqual(result['phpimages'], [])
        self.assertTrue(all(row['state'] == 'Not selected' for row in result['report']))

    def test_telemetry_extensions_and_distinct_module_version(self):
        for entry in self.entries():
            with self.subTest(variant=entry['tagMinor']):
                packages = json.loads((self.root / f"build/php/{entry['minor']}.json").read_text())['pie_packages']
                self.assertTrue({'opentelemetry', 'grpc', 'protobuf'} <= set(entry['extensions'].split()))
                for name in ('opentelemetry', 'grpc'):
                    self.assertEqual(entry['extension_versions'][name], packages[name]['version'])
                self.assertEqual(entry['extension_versions']['protobuf'], packages['protobuf']['module_version'])
                self.assertNotEqual(entry['extension_versions']['protobuf'], packages['protobuf']['version'])
        package_version = json.loads((self.root / 'build/php/8.2.json').read_text())['pie_packages']['protobuf']['version']
        self.change_json('build/php/8.2.json', lambda c: c['pie_packages']['protobuf'].pop('module_version'))
        self.assertEqual(self.entries()[0]['extension_versions']['protobuf'], package_version)

    def test_invalid_module_version_fails(self):
        for value in ('latest', '5.36.2;command', '', None, 5):
            with self.subTest(value=value):
                self.change_json('build/php/8.2.json', lambda c: c['pie_packages']['protobuf'].update(module_version=value))
                with self.assertRaises(ResolutionError):
                    self.entries()

    def test_php_specific_updates(self):
        for minor in ('8.2', '8.3', '8.4', '8.5'):
            with self.subTest(minor=minor):
                changed = self.affected(lambda: self.change_json(f'build/php/{minor}.json', lambda c: c['pie_packages']['redis'].update(version='6.3.1')))
                self.assertEqual(changed, [minor + '-fpm-alpine', minor + '-fpm-alpine-node24'])

    def test_php_base_digest_and_xhprof_update(self):
        for field in ('base', 'xhprof'):
            def change():
                self.change_json('build/php/8.3.json', lambda c: c.update(image=c['image'].split('@')[0] + '@' + digest('new')) if field == 'base' else c['xhprof'].update(commit='a' * 40))
            self.assertEqual(len(self.affected(change)), 2)

    def test_formatting_only_manifest_change_selects_minor(self):
        changed = self.affected(lambda: self.change_json('build/php/8.2.json', lambda c: None))
        self.assertEqual(len(changed), 2)

    def test_node_pin_and_stage_changes_select_only_node(self):
        changed = self.affected(self.change_node)
        self.assertEqual(len(changed), 4)
        self.assertTrue(all('node24' in tag for tag in changed))
        path = self.root / 'Dockerfile'
        changed = self.affected(lambda: path.write_text(path.read_text().replace('RUN ln -vs', 'RUN ln -vfs')))
        self.assertEqual(len(changed), 4)

    def test_unknown_dockerfile_structure_selects_all(self):
        changed = self.affected(lambda: (self.root / 'Dockerfile').write_text((self.root / 'Dockerfile').read_text() + '\nFROM php AS other\n'))
        self.assertEqual(len(changed), 8)

    def test_shared_sources_workflows_and_tests_select_all(self):
        for filename in ('entrypoint.sh', 'scripts/install_extensions.sh', 'scripts/refresh_apk_locks.py', '.github/workflows/template-images.yml', 'Dockerfile', 'tests/new_test.py'):
            with self.subTest(filename=filename):
                path = self.root / filename
                path.parent.mkdir(exist_ok=True)
                self.assertEqual(len(self.affected(lambda: path.write_text('# change\n' + (path.read_text() if path.exists() else '')))), 8)

    def test_pie_and_composer_updates_select_all(self):
        path = self.root / 'Dockerfile'
        for tool in ('PIE_IMAGE', 'COMPOSER_IMAGE'):
            changed = self.affected(lambda: path.write_text(re.sub(rf'^(ARG {tool}="[^"@]+:)[^"@]+', r'\g<1>999.0.0-bin', path.read_text(), flags=re.M)))
            self.assertEqual(len(changed), 8)

    def test_apk_revisions_and_transitives_select_release(self):
        for arch in ('amd64', 'arm64'):
            changed = self.affected(lambda: self.change_json(f'build/apk/3.24/{arch}.json', lambda c: c['packages'].update({'libzip': '1.11.4-r99'})))
            self.assertEqual(len(changed), 8)
        self.assertEqual(affected_tags([dict(e, alpine='3.25') for e in self.entries()], self.root, self.base), set())

    def test_docs_renovate_and_unrelated_changes_select_nothing(self):
        for filename in ('README.md', 'renovate.json', 'notes.txt'):
            self.assertEqual(self.affected(lambda: (self.root / filename).write_text('{}\n')), [])

    def test_mixed_changes_across_multiple_push_commits(self):
        self.change_json('build/php/8.2.json', lambda c: c['pie_packages']['redis'].update(version='6.3.1'))
        self.commit()
        self.change_json('build/php/8.3.json', lambda c: c['xhprof'].update(commit='a' * 40))
        self.commit()
        tags = affected_tags(self.entries(), self.root, self.base)
        self.assertEqual(len(tags), 4)
        self.assertTrue(all(tag.startswith(('8.2-', '8.3-')) for tag in tags))

    def test_pr_selection_uses_checked_out_merge_result(self):
        self.git('checkout', '-qb', 'feature')
        (self.root / 'scripts/new.sh').write_text('echo test\n')
        self.commit()
        self.git('checkout', 'main')
        (self.root / 'README.md').write_text('new documentation')
        base = self.commit()
        self.git('merge', '--no-ff', '-qm', 'merge result', 'feature')
        self.assertEqual(len(select(self.entries(), self.root, base)['phpimages']), 8)

    def test_renames_additions_and_deletions(self):
        (self.root / 'scripts/new.sh').write_text('echo test\n')
        self.commit()
        changed = self.affected(lambda: (self.root / 'scripts/new.sh').rename(self.root / 'moved.txt'))
        self.assertEqual(len(changed), 8)
        changed = self.affected(lambda: (self.root / 'moved.txt').rename(self.root / 'scripts/back.sh'))
        self.assertEqual(len(changed), 8)
        self.assertEqual(len(self.affected(lambda: (self.root / 'scripts/back.sh').unlink())), 8)

    def test_deleted_manifest_selects_previously_built_minor_for_supersession(self):
        entries = self.entries()
        (self.root / 'build/php/8.2.json').unlink()
        self.commit()
        self.assertEqual(len(affected_tags(entries, self.root, self.base)), 2)
        self.assertEqual(len(self.entries()), 6)

    def test_initial_push_and_manual_selection(self):
        entries = self.entries()
        self.assertEqual(len(select(entries, self.root, '0' * 40)['phpimages']), 8)
        self.assertEqual(len(select(entries, self.root, php='All')['phpimages']), 8)
        for minor in ('8.2', '8.3', '8.4', '8.5'):
            selected = select(entries, self.root, php=minor)['phpimages']
            self.assertEqual(len(selected), 2)
            self.assertTrue(all(e['minor'] == minor for e in selected))
        with self.assertRaises(ResolutionError):
            select(entries, self.root, php='8.1')

    def test_invalid_references_fail_selection(self):
        with self.assertRaises(subprocess.CalledProcessError):
            affected_tags(self.entries(), self.root, 'nonexistent')
        with self.assertRaises(subprocess.CalledProcessError):
            affected_tags(self.entries(), self.root, self.base, 'nonexistent')

    def test_reruns_repeat_selection(self):
        self.change_json('build/php/8.2.json', lambda c: c.update(packages=''))
        self.commit()
        selection = select(self.entries(), self.root, self.base)
        source = self.git('rev-parse', 'HEAD')
        (self.root / 'README.md').write_text('later documentation')
        self.commit()
        self.git('checkout', '--detach', source)
        retry = select(self.entries(), self.root, self.base)
        self.assertEqual(retry, selection)
        self.assertEqual(len(retry['phpimages']), 2)

    def test_invalid_configuration_fails(self):
        self.change_json('build/php/8.2.json', lambda c: c['xhprof'].update(commit='latest'))
        with self.assertRaises(ResolutionError):
            self.entries()
        self.git('checkout', '--', 'build/php/8.2.json')
        self.change_json('build/apk/3.24/amd64.json', lambda c: c.update(architecture='aarch64'))
        with self.assertRaises(ResolutionError):
            self.entries()
        self.git('checkout', '--', 'build/apk/3.24/amd64.json')
        self.change_json('build/apk/3.24/amd64.json', lambda c: c['packages'].update(libzip='~1'))
        with self.assertRaises(ResolutionError):
            self.entries()
        self.git('checkout', '--', 'build/apk/3.24/amd64.json')
        path = self.root / 'Dockerfile'
        path.write_text(path.read_text().replace('node:24.', 'node:26.'))
        with self.assertRaises(ResolutionError):
            self.entries()

    def test_cli_outputs_and_removed_registry_options(self):
        script = self.root / 'scripts/select_images.py'
        output = self.root / '.git/matrix.json'
        github_output = self.root / '.git/github-output'
        result = subprocess.run([sys.executable, str(script), '--php', '8.4', '--out', str(output), '--github-output', str(github_output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(output.read_text())['phpimages']), 2)
        self.assertIn('has_changes=true', github_output.read_text())
        self.assertNotIn('has_repairs', github_output.read_text())
        result = subprocess.run([sys.executable, str(script), '--base-ref', self.base, '--out', str(output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text())['phpimages'], [])
        for extra in (['--published'], ['--image', IMAGE], ['--base-ref', 'missing']):
            result = subprocess.run([sys.executable, str(script), '--out', str(output), *extra], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


class CurrentSourceTests(RepositoryTests):
    def test_relevant_update_supersedes_only_its_minor(self):
        entries = self.entries()
        self.change_json('build/php/8.2.json', lambda c: c['pie_packages']['redis'].update(version='999.0.0'))
        self.commit()
        self.assertTrue(check_current(entries[0], self.root))
        self.assertFalse(check_current(entries[2], self.root))

    def test_node_update_supersedes_only_node(self):
        plain, node = self.entries()[:2]
        self.change_node()
        self.commit()
        self.assertFalse(check_current(plain, self.root))
        self.assertTrue(check_current(node, self.root))

    def test_current_source_and_unrelated_changes_are_allowed(self):
        entry = self.entries()[0]
        self.assertFalse(check_current(entry, self.root))
        (self.root / 'README.md').write_text('documentation update')
        self.commit()
        self.assertFalse(check_current(entry, self.root))

    def test_fetch_failure_missing_and_malformed_sources_fail(self):
        entry = self.entries()[0]
        with self.assertRaises(ResolutionError):
            check_current(dict(entry, source_ref='main'), self.root)
        with self.assertRaises(subprocess.CalledProcessError):
            check_current(dict(entry, source_ref='a' * 40), self.root)
        self.git('remote', 'remove', 'origin')
        with self.assertRaises(subprocess.CalledProcessError):
            check_current(entry, self.root)

    def test_check_current_cli_has_distinct_output_and_modes(self):
        script = self.root / 'scripts/select_images.py'
        entry = self.entries()[0]
        output = self.root / '.git/current-output'
        args = [sys.executable, str(script), '--check-current', json.dumps(entry), '--github-output', str(output)]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), 'superseded=false\n')
        self.change_json('build/php/8.2.json', lambda c: c['pie_packages']['redis'].update(version='999.0.0'))
        self.commit()
        output.unlink()
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), 'superseded=true\n')
        for extra in (['--php', '8.2'], ['--base-ref', self.base]):
            result = subprocess.run(args + extra, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.entry = manifests(ROOT)[0]
        self.config = {'os': 'linux', 'architecture': 'amd64', 'config': {'Labels': {
            REVISION_LABEL: self.entry['source_ref'], VARIANT_LABEL: self.entry['tagMinor']}}}

    def test_matching_metadata_and_platform_wrappers(self):
        verify_metadata(self.config, self.entry, 'linux/amd64')
        verify_metadata({'linux/amd64': self.config}, self.entry, 'linux/amd64')

    def test_wrong_architecture_source_or_variant_fails(self):
        with self.assertRaises(ValueError):
            verify_metadata(self.config, self.entry, 'linux/arm64')
        for label in (REVISION_LABEL, VARIANT_LABEL):
            config = copy.deepcopy(self.config)
            config['config']['Labels'][label] = 'wrong'
            with self.assertRaises(ValueError):
                verify_metadata(config, self.entry, 'linux/amd64')
        with self.assertRaises(ValueError):
            verify_metadata({}, self.entry, 'linux/amd64')

    def test_metadata_cli_requires_platform_and_valid_json(self):
        args = [sys.executable, str(ROOT / 'scripts/verify_image.py'), '--metadata', '--entry', json.dumps(self.entry)]
        result = subprocess.run(args + ['--platform', 'linux/amd64'], input=json.dumps(self.config), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for extra, data in [([], '{}'), (['--platform', 'linux/amd64'], 'not json'), (['--platform', 'linux/arm64'], json.dumps(self.config))]:
            result = subprocess.run(args + extra, input=data, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


class WorkflowTests(unittest.TestCase):
    def test_publication_is_commit_driven_and_queued(self):
        workflow = (ROOT / '.github/workflows/build-test-publish.yml').read_text()
        self.assertNotIn('schedule:', workflow)
        self.assertIn('queue: max', workflow)
        self.assertIn('cancel-in-progress: false', workflow)
        self.assertIn('      - tests/**', workflow)
        self.assertIn('BASE_REF: ${{ github.event.before }}', workflow)
        self.assertIn('ref: ${{ github.event_name == \'workflow_dispatch\' && \'main\' || github.sha }}', workflow)
        self.assertIn('run: test "$GITHUB_REF" = refs/heads/main', workflow)
        for minor in ('All', '8.2', '8.3', '8.4', '8.5'):
            self.assertIn("'" + minor + "'", workflow)
        selection_job = workflow.split('  select-variants:', 1)[1].split('  build-and-test:', 1)[0]
        self.assertNotIn('docker/login-action', selection_job)
        self.assertNotIn('docker/setup-buildx-action', selection_job)
        self.assertNotIn('--published', workflow)
        self.assertNotIn('has_repairs', workflow)

    def test_source_and_variant_identity_follow_the_tested_oci_image(self):
        workflow = (ROOT / '.github/workflows/template-images.yml').read_text()
        self.assertIn('org.opencontainers.image.revision=${{ inputs.source-ref }}', workflow)
        self.assertIn('io.github.marlub.docker-php.variant=${{ fromJson(inputs.variant).tagMinor }}', workflow)
        self.assertNotIn('input_hash', workflow)
        self.assertIn('needs: build-and-test', workflow)
        self.assertIn('    if: inputs.publish', workflow)
        self.assertIn('overwrite: true', workflow)
        self.assertIn('name: digests-${{ fromJson(inputs.variant).tagMinor }}-amd64', workflow)
        self.assertIn('name: digests-${{ fromJson(inputs.variant).tagMinor }}-arm64', workflow)
        self.assertNotIn('continue-on-error', workflow)
        self.assertNotIn('concurrency:', workflow)
        self.assertIn('regctl image copy', workflow)
        self.assertIn('test "$config" = "$(docker image inspect test', workflow)

    def test_native_architecture_gate_and_variant_independence(self):
        workflow = (ROOT / '.github/workflows/template-images.yml').read_text()
        self.assertIn('arch: [amd64, arm64]', workflow)
        header = workflow.split('\n  publish:\n', 1)[1].split('    steps:', 1)[0]
        self.assertIn('needs: build-and-test', header)
        self.assertIn('if: inputs.publish', header)
        self.assertNotIn('always()', header)
        for name in ('build-test.yml', 'build-test-publish.yml'):
            workflow = (ROOT / '.github/workflows' / name).read_text()
            call = workflow.split('\n  build-and-test:\n', 1)[1].split('\n  validate-', 1)[0]
            self.assertIn('fail-fast: false', call)
            self.assertIn('variant: ${{ toJson(matrix.phpimage) }}', call)
            self.assertNotIn('selected-images', workflow)


class PublicationStepTests(unittest.TestCase):
    """Exercise the real workflow shell with a registry stub; no Docker daemon."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'digests').mkdir()
        (self.root / 'bin').mkdir()
        self.entry = manifests(ROOT)[0]
        self.digests = {'amd64': digest('amd64'), 'arm64': digest('arm64')}
        for arch, value in self.digests.items():
            (self.root / 'digests' / (arch + '.digest')).write_text(value + '\n')
            config = {'os': 'linux', 'architecture': arch, 'config': {'Labels': {
                REVISION_LABEL: self.entry['source_ref'], VARIANT_LABEL: self.entry['tagMinor']}}}
            (self.root / (arch + '.json')).write_text(json.dumps(config))
        self.manifest = {'digest': digest('index'), 'manifests': [
            {'platform': {'os': 'linux', 'architecture': arch}, 'digest': value} for arch, value in self.digests.items()]}
        for key in ('tagMinor', 'tagPatch'):
            (self.root / (self.entry[key] + '.json')).write_text(json.dumps(self.manifest))
        docker = self.root / 'bin/docker'
        docker.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['MOCK_ROOT'])
with (root / 'calls').open('a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('REGISTRY_FAILURE'): sys.exit(9)
if sys.argv[3] == 'create': sys.exit(8 if os.environ.get('CREATE_FAILURE') else 0)
ref = sys.argv[4]
if '@' in ref:
    name = next(arch for arch in ('amd64', 'arm64') if (root / 'digests' / (arch + '.digest')).read_text().strip() == ref.split('@')[1])
else:
    name = ref.rsplit(':', 1)[1]
print((root / (name + '.json')).read_text())
""")
        docker.chmod(0o755)
        self.env = dict(os.environ, MOCK_ROOT=str(self.root), RUNNER_TEMP=str(self.root),
                        PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'],
                        ENTRY=json.dumps(self.entry), IMAGE=IMAGE,
                        TAG_MINOR=self.entry['tagMinor'], TAG_PATCH=self.entry['tagPatch'])

    def run_step(self, name):
        workflow = (ROOT / '.github/workflows/template-images.yml').read_text()
        step = workflow.split('      - name: ' + name + '\n', 1)[1].split('\n      - ', 1)[0]
        script = textwrap.dedent(step.split('        run: |\n', 1)[1])
        return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=ROOT, env=self.env, capture_output=True, text=True)

    def test_verified_candidates_publish_exact_digests(self):
        result = self.run_step('Verify candidate metadata')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_step('Publish and verify tags')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (self.root / 'calls').read_text().splitlines()]
        create = next(call for call in calls if call[2] == 'create')
        self.assertEqual(create[-2:], [IMAGE + '@' + self.digests[arch] for arch in ('amd64', 'arm64')])
        self.assertNotIn('build', create)

    def test_missing_and_malformed_digest_files_fail(self):
        path = self.root / 'digests/arm64.digest'
        original = path.read_text()
        for value in ('invalid', original + original):
            path.write_text(value)
            self.assertNotEqual(self.run_step('Verify candidate metadata').returncode, 0)
        path.unlink()
        self.assertNotEqual(self.run_step('Verify candidate metadata').returncode, 0)

    def test_candidate_identity_and_registry_errors_propagate(self):
        path = self.root / 'arm64.json'
        config = json.loads(path.read_text())
        config['config']['Labels'][REVISION_LABEL] = 'wrong'
        path.write_text(json.dumps(config))
        self.assertNotEqual(self.run_step('Verify candidate metadata').returncode, 0)
        self.env['REGISTRY_FAILURE'] = '1'
        self.assertNotEqual(self.run_step('Verify candidate metadata').returncode, 0)

    def test_missing_duplicate_extra_and_wrong_manifest_children_fail(self):
        path = self.root / (self.entry['tagPatch'] + '.json')
        for kind in ('missing', 'duplicate', 'extra', 'digest', 'index', 'index format', 'malformed'):
            manifest = copy.deepcopy(self.manifest)
            if kind == 'missing': manifest['manifests'].pop()
            elif kind == 'duplicate': manifest['manifests'][1] = manifest['manifests'][0]
            elif kind == 'extra': manifest['manifests'].append(manifest['manifests'][0])
            elif kind == 'digest': manifest['manifests'][0]['digest'] = digest('wrong')
            elif kind == 'index': manifest['digest'] = digest('other index')
            elif kind == 'index format': manifest['digest'] = 'invalid'
            path.write_text('not json' if kind == 'malformed' else json.dumps(manifest))
            with self.subTest(kind=kind):
                self.assertNotEqual(self.run_step('Publish and verify tags').returncode, 0)

    def test_create_failure_stops_tag_verification(self):
        self.env['CREATE_FAILURE'] = '1'
        self.assertNotEqual(self.run_step('Publish and verify tags').returncode, 0)
        calls = (self.root / 'calls').read_text().splitlines()
        self.assertEqual(len(calls), 1)


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.entry = manifests(ROOT)[1]
        self.entry['xhprof'] = dict(self.entry['xhprof'], tag='v2.3.10')
        self.php = self.entry['php_version']
        self.composer = self.entry['composer_version']
        self.node = self.entry['node_version']
        self.source = dict(self.entry['xhprof'], version='2.3.9')
        self.versions = dict(self.entry['extension_versions'], xhprof='2.3.9')
        self.labels = {REVISION_LABEL: self.entry['source_ref'], VARIANT_LABEL: self.entry['tagMinor']}

    def run_image(self, command, **kwargs):
        if command[:3] == ['docker', 'image', 'inspect']:
            return json.dumps(self.labels)
        if command[-1] == 'echo PHP_VERSION;':
            return self.php
        if command[-1].endswith('xhprof-build.json'):
            return json.dumps(self.source)
        if 'composer' in command:
            return f'Composer version {self.composer} 2026-10-01'
        if 'node' in command:
            return 'v' + self.node
        return json.dumps(self.versions)

    def test_xhprof_tag_and_module_versions_can_differ(self):
        self.assertEqual(self.source['tag'], 'v2.3.10')
        verify('test', self.entry, self.run_image)

    def test_protobuf_package_version_is_not_its_module_version(self):
        package = json.loads((ROOT / f"build/php/{self.entry['minor']}.json").read_text())['pie_packages']['protobuf']
        self.assertEqual(self.versions['protobuf'], package['module_version'])
        self.assertNotEqual(self.versions['protobuf'], package['version'])
        verify('test', self.entry, self.run_image)
        self.versions['protobuf'] = package['version']
        with self.assertRaises(ValueError):
            verify('test', self.entry, self.run_image)

    def test_wrong_source_or_variant_label_fails(self):
        for label in (REVISION_LABEL, VARIANT_LABEL):
            old = self.labels[label]
            self.labels[label] = 'wrong'
            with self.assertRaises(ValueError):
                verify('test', self.entry, self.run_image)
            self.labels[label] = old

    def test_unexpected_php_tool_and_extension_versions_fail(self):
        for field, value in [('php', '8.2.0'), ('composer', '2.10.0'), ('node', '24.0.0')]:
            with self.subTest(field=field):
                original = getattr(self, field)
                setattr(self, field, value)
                with self.assertRaises(ValueError):
                    verify('test', self.entry, self.run_image)
                setattr(self, field, original)
        self.versions['redis'] = '0.0.1'
        with self.assertRaises(ValueError):
            verify('test', self.entry, self.run_image)

    def test_xhprof_source_and_actual_version_must_match(self):
        self.source['commit'] = 'a' * 40
        with self.assertRaises(ValueError):
            verify('test', self.entry, self.run_image)
        self.source['commit'] = self.entry['xhprof']['commit']
        self.versions['xhprof'] = '2.3.10'
        with self.assertRaises(ValueError):
            verify('test', self.entry, self.run_image)


class RenovateTests(unittest.TestCase):
    def test_custom_managers_extract_all_pins(self):
        config=json.loads((ROOT/'renovate.json').read_text())
        counts=[]
        for manager in config['customManagers']:
            matches=[]
            for pattern in manager['managerFilePatterns']:
                pattern=pattern.strip('/')
                for path in ROOT.glob('build/**/*.json'):
                    if not re.search(pattern,path.relative_to(ROOT).as_posix()):continue
                    for expression in manager['matchStrings']:
                        expression=re.sub(r'\(\?<([a-zA-Z]+)>',r'(?P<\1>',expression)
                        matches+=list(re.finditer(expression,path.read_text()))
            counts.append(len(matches))
        self.assertEqual(counts[:3],[4,28,4])
        self.assertEqual(counts[3:5], [len(json.loads((ROOT/f'build/apk/3.24/{arch}.json').read_text())['packages']) for arch in ('amd64','arm64')])

    def test_revision_aware_architecture_specific_lookup(self):
        config=json.loads((ROOT/'renovate.json').read_text())
        apk=[m for m in config['customManagers'] if m['datasourceTemplate']=='apk']
        self.assertEqual(len(apk),2)
        self.assertTrue(all('branch=v3.24' in m['registryUrlTemplate'] for m in apk))
        self.assertEqual({m['registryUrlTemplate'].split('arch=')[1] for m in apk},{'x86_64','aarch64'})
    def test_only_nonmajor_updates_automerge(self):
        config=json.loads((ROOT/'renovate.json').read_text())
        self.assertFalse(config['automerge'])
        rules=[r for r in config['packageRules'] if r.get('automerge')]
        self.assertTrue(rules);self.assertTrue(all('major' not in r['matchUpdateTypes'] for r in rules))
    def test_aggregate_check_accepts_only_valid_job_outcomes(self):
        import os
        workflow=(ROOT/'.github/workflows/build-test.yml').read_text()
        script=workflow.split('      - name: Require successful checks or an intentional empty selection',1)[1].split('        run: |\n',1)[1]
        import textwrap
        script=textwrap.dedent(script)
        for check, build, changes, succeeds in [
            ('success','success','true',True), ('success','skipped','false',True),
            ('failure','skipped','false',False), ('success','failure','true',False),
            ('success','skipped','true',False), ('success','success','false',False),
            ('success','skipped','',False), ('cancelled','success','true',False)]:
            with self.subTest(check=check,build=build,changes=changes):
                result=subprocess.run(['bash','-e','-c',script],env=dict(os.environ,CHECK_RESULT=check,BUILD_RESULT=build,HAS_CHANGES=changes))
                self.assertEqual(result.returncode==0,succeeds)

if __name__ == '__main__':
    unittest.main()
