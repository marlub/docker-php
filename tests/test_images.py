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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from image_config import ConfigurationError, REVISION_LABEL, VARIANT_LABEL
from plan_ci import (PlanningError, affected_image_tags, create_ci_plan,
                     is_candidate_superseded, load_variants, run_git)
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
        for name in ('.gitignore', 'Dockerfile', 'entrypoint.sh', 'README.md', 'renovate.json', 'docker-bake.hcl'):
            shutil.copy(ROOT / name, self.root / name)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'test@example.test')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'core.hooksPath', '/dev/null')
        self.git('remote', 'add', 'origin', '.')
        self.base = self.commit()

    def git(self, *args):
        return run_git(self.root, *args).strip()

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture change')
        return self.git('rev-parse', 'HEAD')

    def entries(self):
        return load_variants(self.root)

    def change_json(self, name, change):
        path = self.root / name
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value))

    def change_arg(self, minor, key, value):
        path = self.root / f'build/php/{minor}.hcl'
        content = path.read_text()
        pattern = rf'(?m)^(    {key} = )[^\n]+'
        if re.search(pattern, content):
            content = re.sub(pattern, lambda m: m[1] + json.dumps(value), content)
        else:
            content = content.replace('  default = {\n', f'  default = {{\n    {key} = {json.dumps(value)}\n', 1)
        path.write_text(content)

    def change_node(self):
        path = self.root / 'Dockerfile'
        path.write_text(re.sub(r'node:24\.\d+\.\d+-', 'node:24.999.0-', path.read_text()))

    def change_pie(self, version='1.999.0', digest='a' * 64, recipe='Dockerfile'):
        path = self.root / recipe
        path.write_text(re.sub(
            r'(ARG PIE_IMAGE="[^"\n]+:)\d+\.\d+\.\d+(-bin@sha256:)[a-f0-9]{64}',
            lambda match: match[1] + version + match[2] + digest,
            path.read_text(),
        ))

    def affected(self, change):
        before = self.git('rev-parse', 'HEAD')
        change()
        self.commit()
        return sorted(affected_image_tags(self.entries(), self.root, before))


class SelectionTests(RepositoryTests):
    def test_eight_variants_and_existing_tags(self):
        entries = self.entries()
        self.assertEqual(len(entries), 8)
        self.assertEqual(entries[0]['tagMinor'], '8.2-fpm-alpine')
        self.assertEqual(entries[-1]['tagMinor'], '8.5-fpm-alpine-node24')
        self.assertEqual(entries[0]['tagPatch'], entries[0]['php_version'] + '-fpm-alpine')
        self.assertNotIn('opcache', entries[-1]['extensions'].split())
        self.assertEqual(entries[0]['source_ref'], self.base)
        self.assertEqual(entries[0]['bake_target'], 'php-8-2')
        self.assertEqual(entries[1]['bake_target'], 'php-8-2-node24')

    def test_no_changes_select_nothing_without_registry(self):
        result = create_ci_plan(self.entries(), self.root, self.base)
        self.assertEqual(result['phpimages'], [])
        self.assertTrue(all(row['state'] == 'Not selected' for row in result['report']))

    def test_resolution_needs_no_daemon_and_ignores_ambient_overrides(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'unix:///nonexistent-docker.sock',
                                     'EXTENSIONS': '', 'PHP_8_2': 'invalid'}):
            self.assertEqual(len(self.entries()), 8)
            self.assertIn('grpc', self.entries()[0]['extension_versions'])

    def test_lock_refresh_can_resolve_bases_without_existing_locks(self):
        import refresh_apk_locks
        shutil.rmtree(self.root / 'build/apk')
        with patch.object(refresh_apk_locks, 'ROOT', self.root):
            self.assertEqual(len(refresh_apk_locks.selected_base_images()), 4)
            self.assertEqual(len(refresh_apk_locks.selected_base_images('8.4')), 1)
            self.assertEqual(refresh_apk_locks.selected_base_images('8.1'), [])

    def test_lock_refresh_uses_the_selected_bake_package_profile(self):
        import refresh_apk_locks
        self.change_arg('8.4', 'RUNTIME_PACKAGES', 'libffi libstdc++')
        self.change_arg('8.4', 'PACKAGES', 'libpng')
        with patch.object(refresh_apk_locks, 'ROOT', self.root):
            selected = refresh_apk_locks.selected_base_images('8.4')[0]
            self.assertEqual(selected['packages'], 'libffi libstdc++ libpng')
            other = refresh_apk_locks.selected_base_images('8.3')[0]
            self.assertNotEqual(other['packages'], selected['packages'])

    def test_shared_tool_pins_are_not_duplicated_in_minor_files(self):
        self.change_arg('8.2', 'PIE_IMAGE', 'ghcr.io/php/pie:latest')
        with self.assertRaises(ConfigurationError):
            self.entries()

    def test_distinct_protobuf_module_version_and_default(self):
        entry = self.entries()[0]
        self.assertEqual(entry['extension_versions']['protobuf'], entry['bake_args']['PROTOBUF_MODULE_VERSION'])
        self.assertNotEqual(entry['extension_versions']['protobuf'], entry['bake_args']['PROTOBUF_VERSION'])
        path = self.root / 'build/php/8.2.hcl'
        path.write_text(re.sub(r'(?m)^    PROTOBUF_MODULE_VERSION = .+\n', '', path.read_text()))
        entry = self.entries()[0]
        self.assertEqual(entry['extension_versions']['protobuf'], entry['bake_args']['PROTOBUF_VERSION'])

    def test_per_minor_pins_are_independent(self):
        original = self.entries()
        for minor in ('8.2', '8.3', '8.4', '8.5'):
            before = self.git('rev-parse', 'HEAD')
            self.change_arg(minor, 'REDIS_VERSION', '6.3.99')
            self.commit()
            entries = self.entries()
            self.assertEqual(len(affected_image_tags(entries, self.root, before)), 2)
            self.assertEqual(next(e for e in entries if e['minor'] == minor)['extension_versions']['redis'], '6.3.99')
        self.assertEqual(original[0]['extension_versions']['redis'], '6.3.0')

    def test_new_minor_can_exclude_an_unsupported_extension(self):
        content = (self.root / 'build/php/8.5.hcl').read_text()
        content = content.replace('PHP_8_5', 'PHP_8_6').replace('php-8-5', 'php-8-6').replace('ci-8-5', 'ci-8-6').replace('php:8.5.12-', 'php:8.6.0-').replace('docker-php:8.5-', 'docker-php:8.6-')
        path = self.root / 'build/php/8.6.hcl'
        path.write_text(content)
        extensions = self.entries()[-1]['extensions'].split()
        self.change_arg('8.6', 'EXTENSIONS', ' '.join(n for n in extensions if n != 'grpc'))
        path.write_text(re.sub(r'(?m)^    GRPC_VERSION = .+\n', '', path.read_text()))
        entries = self.entries()
        self.assertEqual(len(entries), 10)
        for entry in entries[-2:]:
            self.assertEqual(entry['minor'], '8.6')
            self.assertEqual(entry['bake_args']['BUILD_GRPC'], 'false')
            self.assertNotIn('grpc', entry['extension_versions'])
            self.assertNotIn('grpc', entry['extensions'].split())
        self.assertIn('grpc', entries[0]['extension_versions'])

    def test_empty_selection_disables_compilers_and_runtime_packages(self):
        self.change_arg('8.4', 'EXTENSIONS', '')
        entry = next(e for e in self.entries() if e['minor'] == '8.4')
        self.assertEqual(entry['extension_versions'], {})
        self.assertIsNone(entry['xhprof'])
        self.assertEqual(entry['bake_args']['BUNDLED_EXTENSIONS'], '')
        self.assertEqual(entry['bake_args']['INSTALL_RUNTIME_PACKAGES'], 'false')
        self.assertEqual(entry['bake_args']['BUILD_GRPC'], 'false')
        self.assertEqual(entry['bake_args']['BUILD_REDIS'], 'false')

    def test_invalid_or_missing_version_pins_fail(self):
        path = self.root / 'build/php/8.2.hcl'
        original = path.read_text()
        for key, value in [('REDIS_VERSION', ''), ('REDIS_VERSION', 'latest'),
                           ('PROTOBUF_MODULE_VERSION', 'latest'), ('PROTOBUF_MODULE_VERSION', 5),
                           ('XHPROF_COMMIT', 'latest')]:
            with self.subTest(key=key, value=value):
                path.write_text(original)
                self.change_arg('8.2', key, value)
                with self.assertRaises(ConfigurationError): self.entries()
        path.write_text(original)
        path.write_text(re.sub(r'(?m)^    REDIS_VERSION = .+\n', '', original))
        with self.assertRaises(ConfigurationError): self.entries()

    def test_malformed_bake_and_mismatched_base_fail(self):
        path = self.root / 'build/php/8.2.hcl'
        original = path.read_text()
        path.write_text('invalid HCL {')
        with self.assertRaises(ConfigurationError): self.entries()
        path.write_text(original)
        self.change_arg('8.2', 'PHP_BASE_IMAGE', self.entries()[2]['php_image'])
        with self.assertRaises(ConfigurationError): self.entries()

    def test_built_in_opcache_cannot_be_requested_on_php85(self):
        entry = self.entries()[-1]
        self.change_arg('8.5', 'EXTENSIONS', entry['extensions'] + ' opcache')
        with self.assertRaises(ConfigurationError): self.entries()

    def test_configure_options_can_differ_per_minor(self):
        self.change_arg('8.3', 'REDIS_CONFIGURE_OPTIONS', '--enable-redis-session')
        entries = self.entries()
        self.assertNotIn('REDIS_CONFIGURE_OPTIONS', entries[0]['bake_args'])
        self.assertEqual(entries[2]['bake_args']['REDIS_CONFIGURE_OPTIONS'], '--enable-redis-session')

    def test_node_pin_and_stage_changes_select_only_node(self):
        path = self.root / 'Dockerfile'
        for change in (self.change_node, lambda: path.write_text(path.read_text().replace('RUN ln -vs', 'RUN ln -vfs'))):
            before = self.git('rev-parse', 'HEAD'); change(); self.commit()
            for purpose in ('test', 'publish'):
                changed = affected_image_tags(self.entries(), self.root, before, purpose=purpose)
                self.assertEqual(len(changed), 4)
                self.assertTrue(all('node24' in tag for tag in changed))

    def test_shared_bake_and_unknown_dockerfile_changes_select_all(self):
        for name, content in [('docker-bake.hcl', '\n# shared configuration\n'), ('Dockerfile', '\nFROM php AS other\n')]:
            changed = self.affected(lambda: (self.root / name).write_text((self.root / name).read_text() + content))
            self.assertEqual(len(changed), 8)

    def test_pie_and_composer_updates_select_all(self):
        path = self.root / 'Dockerfile'
        for tool in ('PIE_IMAGE', 'COMPOSER_IMAGE'):
            changed = self.affected(lambda: path.write_text(re.sub(rf'^(ARG {tool}="[^"@]+:)[^"@]+', r'\g<1>999.0.0-bin', path.read_text(), flags=re.M)))
            self.assertEqual(len(changed), 8)

    def test_apk_revisions_and_transitives_select_release(self):
        for arch in ('amd64', 'arm64'):
            changed = self.affected(lambda: self.change_json(f'build/apk/3.24/{arch}.json', lambda c: c['packages'].update({'libzip': '1.11.4-r99'})))
            self.assertEqual(len(changed), 8)
        self.assertEqual(affected_image_tags([dict(e, distribution_release='3.25') for e in self.entries()], self.root, self.base), set())

    def test_mixed_changes_across_multiple_push_commits(self):
        self.change_arg('8.2', 'REDIS_VERSION', '6.3.1'); self.commit()
        self.change_arg('8.3', 'XHPROF_COMMIT', 'a' * 40); self.commit()
        tags = affected_image_tags(self.entries(), self.root, self.base)
        self.assertEqual(len(tags), 4)
        self.assertTrue(all(tag.startswith(('8.2-', '8.3-')) for tag in tags))

    def test_pr_selection_uses_checked_out_merge_result(self):
        self.git('checkout', '-qb', 'feature')
        (self.root / 'scripts/new.sh').write_text('echo test\n'); self.commit()
        self.git('checkout', 'main')
        (self.root / 'README.md').write_text('new documentation'); base = self.commit()
        self.git('merge', '--no-ff', '-qm', 'merge result', 'feature')
        self.assertEqual(len(create_ci_plan(self.entries(), self.root, base)['phpimages']), 8)

    def test_renames_additions_and_deletions(self):
        path = self.root / 'scripts/new.sh'; path.write_text('echo test\n'); self.commit()
        self.assertEqual(len(self.affected(lambda: path.rename(self.root / 'moved.txt'))), 8)
        self.assertEqual(len(self.affected(lambda: (self.root / 'moved.txt').unlink())), 8)

    def test_deleted_minor_selects_previously_built_variant_for_supersession(self):
        entries = self.entries(); (self.root / 'build/php/8.2.hcl').unlink(); self.commit()
        self.assertEqual(len(affected_image_tags(entries, self.root, self.base)), 2)
        self.assertEqual(len(self.entries()), 6)

    def test_initial_push_and_manual_selection(self):
        entries = self.entries()
        for purpose in ('test', 'publish'):
            initial = create_ci_plan(entries, self.root, '0' * 40, purpose=purpose)
            self.assertEqual(len(initial['phpimages']), 8); self.assertTrue(initial['renovate_changes'])
            self.assertEqual(len(create_ci_plan(entries, self.root, php='All', purpose=purpose)['phpimages']), 8)
            for minor in ('8.2', '8.3', '8.4', '8.5'):
                selected = create_ci_plan(entries, self.root, php=minor, purpose=purpose)['phpimages']
                self.assertEqual(len(selected), 2); self.assertTrue(all(e['minor'] == minor for e in selected))
        with self.assertRaises(PlanningError): create_ci_plan(entries, self.root, php='8.1')

    def test_invalid_references_fail_selection(self):
        with self.assertRaises(subprocess.CalledProcessError): affected_image_tags(self.entries(), self.root, 'nonexistent')
        with self.assertRaises(subprocess.CalledProcessError): affected_image_tags(self.entries(), self.root, self.base, 'nonexistent')

    def test_cli_outputs_and_failures(self):
        script = self.root / 'scripts/plan_ci.py'; output = self.root / '.git/matrix.json'; gh = self.root / '.git/outputs'
        result = subprocess.run([sys.executable, str(script), '--php', '8.4', '--out', str(output), '--github-output', str(gh)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(output.read_text())['phpimages']), 2)
        self.assertIn('has_changes=true', gh.read_text())
        for extra in (['--published'], ['--image', IMAGE], ['--base-ref', 'missing']):
            self.assertNotEqual(subprocess.run([sys.executable, str(script), '--out', str(output), *extra], capture_output=True).returncode, 0)


class CurrentSourceTests(RepositoryTests):
    def test_relevant_update_supersedes_only_its_minor(self):
        entries = self.entries()
        self.change_arg('8.2', 'REDIS_VERSION', '999.0.0')
        self.commit()
        self.assertTrue(is_candidate_superseded(entries[0], self.root))
        self.assertFalse(is_candidate_superseded(entries[2], self.root))

    def test_node_update_supersedes_only_node(self):
        plain, node = self.entries()[:2]
        self.change_node()
        self.commit()
        self.assertFalse(is_candidate_superseded(plain, self.root))
        self.assertTrue(is_candidate_superseded(node, self.root))

    def test_current_source_and_unrelated_changes_are_allowed(self):
        entry = self.entries()[0]
        self.assertFalse(is_candidate_superseded(entry, self.root))
        (self.root / 'README.md').write_text('documentation update')
        self.commit()
        self.assertFalse(is_candidate_superseded(entry, self.root))

    def test_fetch_failure_missing_and_malformed_sources_fail(self):
        entry = self.entries()[0]
        with self.assertRaises(PlanningError):
            is_candidate_superseded(dict(entry, source_ref='main'), self.root)
        with self.assertRaises(subprocess.CalledProcessError):
            is_candidate_superseded(dict(entry, source_ref='a' * 40), self.root)
        self.git('remote', 'remove', 'origin')
        with self.assertRaises(subprocess.CalledProcessError):
            is_candidate_superseded(entry, self.root)

    def test_check_current_cli_has_distinct_output_and_modes(self):
        script = self.root / 'scripts/plan_ci.py'
        entry = self.entries()[0]
        output = self.root / '.git/current-output'
        args = [sys.executable, str(script), '--check-current', json.dumps(entry), '--github-output', str(output)]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), 'superseded=false\n')
        self.change_arg('8.2', 'REDIS_VERSION', '999.0.0')
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
        self.entry = load_variants(ROOT)[0]
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
        args = [sys.executable, str(ROOT / 'tests/verify_image.py'), '--metadata', '--entry', json.dumps(self.entry)]
        result = subprocess.run(args + ['--platform', 'linux/amd64'], input=json.dumps(self.config), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for extra, data in [([], '{}'), (['--platform', 'linux/amd64'], 'not json'), (['--platform', 'linux/arm64'], json.dumps(self.config))]:
            result = subprocess.run(args + extra, input=data, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


class PublicationStepTests(unittest.TestCase):
    """Exercise the real workflow shell with a registry stub; no Docker daemon."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'digests').mkdir()
        (self.root / 'bin').mkdir()
        self.entry = load_variants(ROOT)[0]
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
        self.entry = load_variants(ROOT)[1]
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
        package = {'version': self.entry['bake_args']['PROTOBUF_VERSION'], 'module_version': self.entry['bake_args']['PROTOBUF_MODULE_VERSION']}
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
                for path in list(ROOT.glob('build/**/*.json')) + list(ROOT.glob('build/php/*.hcl')) + list(ROOT.glob('.github/workflows/*.yml')):
                    if not re.search(pattern,path.relative_to(ROOT).as_posix()):continue
                    for expression in manager['matchStrings']:
                        expression=re.sub(r'\(\?<([a-zA-Z]+)>',r'(?P<\1>',expression)
                        matches+=list(re.finditer(expression,path.read_text()))
            counts.append(len(matches))
        self.assertEqual(counts[:3],[4,28,4])
        self.assertEqual(counts[3:5], [len(json.loads((ROOT/f'build/apk/3.24/{arch}.json').read_text())['packages']) for arch in ('amd64','arm64')])
        self.assertEqual(counts[5:], [1, 1, 0])

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


class WorkflowOutcomeTests(unittest.TestCase):
    def test_aggregate_check_accepts_only_valid_job_outcomes(self):
        workflow = (ROOT / '.github/workflows/build-test.yml').read_text()
        step = workflow.split('      - name: Require successful checks or an intentional empty selection', 1)[1]
        script = textwrap.dedent(step.split('        run: |\n', 1)[1])
        # Selection result, image flag/result, Renovate flag/result, expected outcome.
        cases = [
            ('success', 'true', 'success', 'true', 'success', True),
            ('success', 'true', 'success', 'false', 'skipped', True),
            ('success', 'false', 'skipped', 'true', 'success', True),
            ('success', 'false', 'skipped', 'false', 'skipped', True),
            ('failure', 'false', 'skipped', 'false', 'skipped', False),
            ('cancelled', 'false', 'skipped', 'false', 'skipped', False),
            ('skipped', 'false', 'skipped', 'false', 'skipped', False),
            ('success', 'true', 'failure', 'true', 'success', False),
            ('success', 'true', 'cancelled', 'true', 'success', False),
            ('success', 'true', 'skipped', 'true', 'success', False),
            ('success', 'false', 'success', 'true', 'success', False),
            ('success', 'false', 'failure', 'true', 'success', False),
            ('success', '', 'skipped', 'true', 'success', False),
            ('success', 'true', '', 'true', 'success', False),
            ('success', 'true', 'success', 'true', 'failure', False),
            ('success', 'true', 'success', 'true', 'cancelled', False),
            ('success', 'true', 'success', 'true', 'skipped', False),
            ('success', 'true', 'success', 'false', 'success', False),
            ('success', 'true', 'success', 'false', 'failure', False),
            ('success', 'true', 'success', '', 'skipped', False),
            ('success', 'true', 'success', 'true', '', False),
        ]
        for check, images, build, changed, renovate, succeeds in cases:
            with self.subTest(selection=check, images=images, build=build,
                              renovate_changed=changed, renovate=renovate):
                result = subprocess.run(
                    ['bash', '-e', '-c', script],
                    env=dict(os.environ, CHECK_RESULT=check, HAS_CHANGES=images, BUILD_RESULT=build,
                             RENOVATE_CHANGES=changed, RENOVATE_RESULT=renovate),
                )
                self.assertEqual(result.returncode == 0, succeeds)


if __name__ == '__main__':
    unittest.main()
