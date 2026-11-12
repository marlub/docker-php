import json
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from test_images import RepositoryTests, ROOT
from image_config import ConfigurationError, DEFAULT_DEBIAN_LABEL
from build_group import CachePolicy
from plan_ci import create_ci_plan, is_candidate_superseded, prepare_build_group
from sync_renovate_os import GENERATED, generated_configuration, synchronize
from variant_fixtures import add_os_release_variants
import refresh_apk_locks
import refresh_apt_locks


class ReleaseAliasTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        add_os_release_variants(self.root)
        self.fixture_ref = self.commit()

    def test_canonical_tags_and_aliases_are_unique_and_resolved_before_selection(self):
        entries = self.entries()
        self.assertEqual(len(entries), 23)
        tags = [tag for entry in entries for tag in entry['publishTags']]
        self.assertEqual(len(tags), len(set(tags)))
        for entry in entries:
            with self.subTest(tag=entry['tagMinor']):
                suffix = ('-node' + entry['node']) if entry['node'] else ''
                os_tag = ('alpine' if entry['distribution'] == 'alpine' else '') + entry['distribution_release']
                self.assertEqual(entry['tagMinor'], entry['minor'] + '-fpm-' + os_tag + suffix)
                self.assertEqual(entry['publishTags'][:2], [entry['tagMinor'], entry['tagPatch']])
                owns = entry['distribution_release'] in ('3.24', 'trixie')
                floating = '-alpine' if entry['distribution'] == 'alpine' else ''
                expected = [v + '-fpm' + floating + suffix for v in (entry['minor'], entry['php_version'])] if owns else []
                if entry['tagMinor'] == '8.5-fpm-trixie-node24':
                    expected.append('latest')
                self.assertEqual(entry['publishTags'][2:], expected)
        older = [e for e in entries if e['distribution_release'] == '3.23']
        selected = create_ci_plan(older, self.root)['phpimages']
        self.assertEqual(selected, older)
        self.assertTrue(all(len(e['publishTags']) == 2 for e in selected))
        # An older-only Node major never gets an alias through fallback.
        self.assertNotIn('8.4-fpm-alpine-node28', tags)
        self.assertNotIn('8.4-fpm-node26', tags)

    def test_latest_moves_to_newer_minor_and_supersedes_queued_owner(self):
        entries = self.entries()
        previous = next(e for e in entries if 'latest' in e['publishTags'])
        self.assertEqual(previous['tagMinor'], '8.5-fpm-trixie-node24')
        self.assertEqual(sum('latest' in e['publishTags'] for e in entries), 1)
        self.assertTrue(all('latest' not in e['publishTags'] for e in create_ci_plan(entries, self.root, php='8.4')['phpimages']))
        source = (self.root / 'build/php/8.5.hcl').read_text()
        source = source.replace('PHP_8_5', 'PHP_8_10').replace('php-8-5', 'php-8-10').replace('ci-8-5', 'ci-8-10')
        source = re.sub(r'php:8\.5\.\d+-', 'php:8.10.0-', source).replace('docker-php:8.5-', 'docker-php:8.10-')
        (self.root / 'build/php/8.10.hcl').write_text(source)
        self.commit()
        owners = [e for e in self.entries() if 'latest' in e['publishTags']]
        self.assertEqual([e['tagMinor'] for e in owners], ['8.10-fpm-trixie-node24'])
        self.assertTrue(is_candidate_superseded(previous, self.root))
        lower = next(e for e in entries if e['tagMinor'] == '8.4-fpm-trixie')
        self.assertFalse(is_candidate_superseded(lower, self.root))
        self.assertFalse(is_candidate_superseded(owners[0], self.root))

    def test_latest_follows_default_debian_after_minor_removal(self):
        (self.root / 'build/php/8.5.hcl').unlink()
        path = self.root / 'build/php/8.4.hcl'
        text = path.read_text().replace(', "' + DEFAULT_DEBIAN_LABEL + '" = "true"', '')
        text = text.replace('"php-8-4-bookworm" }', '"php-8-4-bookworm", "' + DEFAULT_DEBIAN_LABEL + '" = "true" }')
        path.write_text(text)
        owners = [e for e in self.entries() if 'latest' in e['publishTags']]
        self.assertEqual([e['tagMinor'] for e in owners], ['8.4-fpm-bookworm-node26'])

    def test_latest_is_selected_when_its_previous_minor_is_removed(self):
        (self.root / 'build/php/8.5.hcl').unlink()
        self.commit()
        for purpose in ('test', 'publish'):
            selected = create_ci_plan(self.entries(), self.root, self.fixture_ref, purpose=purpose)['phpimages']
            self.assertEqual([e['tagMinor'] for e in selected], ['8.4-fpm-trixie-node24'])
            self.assertIn('latest', selected[0]['publishTags'])

    def test_lower_minor_update_does_not_supersede_latest(self):
        previous = next(e for e in self.entries() if 'latest' in e['publishTags'])
        self.change_arg('8.2', 'REDIS_VERSION', '6.3.99')
        self.commit()
        self.assertFalse(is_candidate_superseded(previous, self.root))

    def test_latest_uses_highest_numeric_node_major_in_default_family(self):
        previous = next(e for e in self.entries() if 'latest' in e['publishTags'])
        path = self.root / 'build/php/8.5.hcl'
        text = path.read_text()
        text = text.replace('"php-8-5-trixie-node24"]', '"php-8-5-trixie-node24", "php-8-5-trixie-node100", "php-8-5-trixie-node26"]')
        for major in (100, 26):
            text += f'''
target "php-8-5-trixie-node{major}" {{
  inherits = ["_php-8-5-trixie"]
  target = "php-node"
  args = {{ NODE_BASE_IMAGE = "node:{major}.1.0-trixie-slim@sha256:{'e' * 64}" }}
  tags = ["docker-php:8.5-fpm-trixie-node{major}"]
}}
'''
        path.write_text(text)
        self.commit()
        owners = [e for e in self.entries() if 'latest' in e['publishTags']]
        self.assertEqual([e['tagMinor'] for e in owners], ['8.5-fpm-trixie-node100'])
        self.assertTrue(is_candidate_superseded(previous, self.root))

    def test_latest_never_falls_back_to_a_plain_image(self):
        path = self.root / 'build/php/8.5.hcl'
        path.write_text(path.read_text().replace(', "php-8-5-trixie-node24"', '', 1))
        with self.assertRaisesRegex(ConfigurationError, 'requires a Node variant for latest'):
            self.entries()

    def test_numeric_alpine_order_and_default_debian_switch(self):
        path = self.root / 'build/php/8.4.hcl'
        text = path.read_text().replace('alpine3.23', 'alpine3.9').replace('alpine3.24', 'alpine3.10')
        # Move the explicit default from Trixie to Bookworm.
        text = text.replace(', "' + DEFAULT_DEBIAN_LABEL + '" = "true"', '')
        text = text.replace('"php-8-4-bookworm" }', '"php-8-4-bookworm", "' + DEFAULT_DEBIAN_LABEL + '" = "true" }')
        path.write_text(text)
        recipe = self.root / 'Dockerfile'
        recipe.write_text(recipe.read_text().replace('alpine3.24', 'alpine3.10'))
        for other in (self.root / 'build/php').glob('*.hcl'):
            if other != path:
                other.write_text(other.read_text().replace('alpine3.24', 'alpine3.10'))
        entries = [e for e in self.entries() if e['minor'] == '8.4']
        self.assertEqual({e['distribution_release'] for e in entries if len(e['publishTags']) > 2}, {'3.10', 'bookworm'})
        self.commit()
        previous = next(e for e in self.entries() if e['minor'] == '8.4' and e['distribution_release'] == 'trixie')
        # A newer alias-owner change must supersede an already tested candidate.
        path.write_text(text.replace('"' + DEFAULT_DEBIAN_LABEL + '" = "true"', '"' + DEFAULT_DEBIAN_LABEL + '" = "false"').replace(
            '"php-8-4-trixie" }', '"php-8-4-trixie", "' + DEFAULT_DEBIAN_LABEL + '" = "true" }'))
        self.commit()
        self.assertTrue(is_candidate_superseded(previous, self.root))

    def test_default_labels_and_release_tags_are_validated(self):
        path = self.root / 'build/php/8.4.hcl'
        original = path.read_text()
        for content in (
            original.replace(DEFAULT_DEBIAN_LABEL, 'unused-default'),
            original.replace('"php-8-4-bookworm" }', '"php-8-4-bookworm", "' + DEFAULT_DEBIAN_LABEL + '" = "true" }'),
            original.replace('"' + DEFAULT_DEBIAN_LABEL + '" = "true"', '"' + DEFAULT_DEBIAN_LABEL + '" = "yes"'),
            original.replace('"php-8-4-alpine" }', '"php-8-4-alpine", "' + DEFAULT_DEBIAN_LABEL + '" = "true" }'),
            original.replace('docker-php:8.4-fpm-alpine3.23', 'docker-php:8.4-fpm-alpine3.22'),
            original.replace('docker-php:8.4-fpm-trixie', 'docker-php:8.4-fpm'),
        ):
            with self.subTest(content=content[-150:]):
                path.write_text(content)
                with self.assertRaises(ConfigurationError):
                    self.entries()
        path.write_text(original)

    def test_duplicate_release_family_is_rejected_even_with_distinct_targets(self):
        path = self.root / 'build/php/8.4.hcl'
        text = path.read_text()
        text = text.replace('"php-8-4-alpine3-23", ', '"php-8-4-duplicate", "php-8-4-alpine3-23", ', 1)
        text += '''
target "php-8-4-duplicate" {
  inherits = ["_php-8-4-alpine3-23"]
  labels = { "io.github.marlub.docker-php.base-target" = "php-8-4-duplicate" }
  target = "php"
  tags = ["docker-php:8.4-fpm-alpine3.23"]
}
'''
        path.write_text(text)
        with self.assertRaises(ConfigurationError):
            self.entries()

    def test_release_locks_build_groups_and_caches_stay_separate(self):
        entries = self.entries()
        groups = create_ci_plan(entries, self.root, php='8.4')['buildgroups']
        self.assertEqual(len(groups), 4)
        writes = set()
        for group in groups:
            self.assertEqual(prepare_build_group(group['variants'], entries), group['variants'])
            self.assertEqual(len({(e['distribution'], e['distribution_release']) for e in group['variants']}), 1)
            for entry in group['variants']:
                writes.add(CachePolicy('example.test/cache', 'main').settings(entry['tagMinor'], 'amd64')[-1])
        self.assertEqual(len(writes), 11)
        for backend, release in (('apk', '3.23'), ('apk', '3.24'), ('apt', 'bookworm'), ('apt', 'trixie')):
            before = self.git('rev-parse', 'HEAD')
            path = self.root / f'build/{backend}/{release}/amd64.json'
            path.write_text(path.read_text() + '\n')
            self.commit()
            for purpose in ('test', 'publish'):
                selected = create_ci_plan(self.entries(), self.root, before, purpose=purpose)['phpimages']
                self.assertTrue(selected)
                self.assertEqual({e['distribution_release'] for e in selected}, {release})

    def test_lock_refresh_filters_families_and_rejects_ambiguous_overrides(self):
        for module, target, release in ((refresh_apk_locks, 'php-8-4-alpine3-23', 'alpine3.23'),
                                         (refresh_apt_locks, 'php-8-4-bookworm', 'bookworm')):
            with self.subTest(target=target), patch.object(module, 'ROOT', self.root):
                self.assertEqual(len(module.selected_base_images('8.4')), 2)
                selected = module.selected_base_images('8.4', target)
                self.assertEqual(len(selected), 1)
                self.assertIn('-fpm-' + release + '@', selected[0]['image'])
                self.assertEqual(module.selected_base_images('8.3', target), [])
                self.assertEqual(module.selected_base_images(base_target='missing'), [])
                command = [sys.executable, str(self.root / 'scripts' / (module.__name__ + '.py')),
                           '--php', '8.4', '--platform', 'linux/amd64', '--base-image', 'cached-base']
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('exactly one matching family', result.stderr)

    def test_refresh_keeps_each_release_in_its_own_lock(self):
        for module, backend, field, releases, arch in (
            (refresh_apk_locks, 'apk', 'alpine', ('3.23', '3.24'), 'x86_64'),
            (refresh_apt_locks, 'apt', 'debian', ('bookworm', 'trixie'), 'amd64'),
        ):
            results = [SimpleNamespace(stdout=json.dumps({field: release, 'architecture': arch,
                       'packages': {'libffi' if backend == 'apk' else 'libffi8': str(i) + ('-r0' if backend == 'apk' else '')}}).encode())
                       for i, release in enumerate(releases, 1)]
            with patch.object(module, 'ROOT', self.root), patch.object(module.subprocess, 'run', side_effect=results):
                module.refresh('linux/amd64', [{'_base_image': 'cached-base', 'packages': 'libffi'}] * 2)
            for i, release in enumerate(releases, 1):
                lock = json.loads((self.root / f'build/{backend}/{release}/amd64.json').read_text())
                self.assertEqual(lock[field], release)
                self.assertEqual(list(lock['packages'].values()), [str(i) + ('-r0' if backend == 'apk' else '')])

    def test_apk_refresh_rejects_wrong_architecture_before_writing(self):
        path = self.root / 'build/apk/3.24/amd64.json'
        original = path.read_bytes()
        result = SimpleNamespace(stdout=json.dumps({'alpine': '3.24', 'architecture': 'aarch64', 'packages': {}}).encode())
        with patch.object(refresh_apk_locks, 'ROOT', self.root), patch.object(refresh_apk_locks.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'architecture mismatch'):
                refresh_apk_locks.refresh('linux/amd64', [{'_base_image': 'cached-base', 'packages': ''}])
        self.assertEqual(path.read_bytes(), original)


class GeneratedRenovateTests(RepositoryTests):
    def test_production_configuration_is_current(self):
        self.assertTrue(synchronize(ROOT, check=True))
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/sync_renovate_os.py'), '--check'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_releases_require_sync_and_preserve_handwritten_configuration(self):
        path = self.root / 'renovate.json'
        original = path.read_bytes()
        before = json.loads(original)
        add_os_release_variants(self.root)
        result = subprocess.run([sys.executable, str(self.root / 'scripts/sync_renovate_os.py'), '--check'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stale', result.stderr)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(synchronize(self.root))
        self.assertTrue(synchronize(self.root, check=True))
        after = json.loads(path.read_text())
        for key in before:
            if key in ('customManagers', 'packageRules'):
                self.assertEqual([r for r in before[key] if r.get('description') != GENERATED],
                                 [r for r in after[key] if r.get('description') != GENERATED])
            else:
                self.assertEqual(before[key], after[key])

    def test_every_release_has_architecture_specific_extraction_and_repositories(self):
        add_os_release_variants(self.root)
        managers, rules = generated_configuration(self.root)
        self.assertEqual(len(managers), 8)
        self.assertEqual(len(rules), 8)
        for manager, rule in zip(managers, rules):
            name = rule['matchFileNames'][0]
            backend, release, filename = name.split('/')[1:]
            arch = filename.removesuffix('.json')
            self.assertRegex(name, manager['managerFilePatterns'][0][1:-1])
            expression = re.sub(r'\(\?<([a-zA-Z]+)>', r'(?P<\1>', manager['matchStrings'][0])
            lock = json.loads((self.root / name).read_text())
            extracted = {m['depName']: m['currentValue'] for m in re.finditer(expression, (self.root / name).read_text())}
            self.assertEqual(extracted, lock['packages'])
            if backend == 'apk':
                self.assertEqual(rule['registryUrls'], [f'https://dl-cdn.alpinelinux.org/alpine?branch=v{release}&components=main,community&arch={lock["architecture"]}'])
            else:
                self.assertEqual(len(rule['registryUrls']), 3)
                self.assertEqual({re.search(r'suite=([^&]+)', url)[1] for url in rule['registryUrls']},
                                 {release, release + '-updates', release + '-security'})
                self.assertTrue(all('binaryArch=' + arch in url for url in rule['registryUrls']))
            self.assertIn(release, rule['groupName'])

    def test_php_and_node_managers_extract_both_release_suffixes(self):
        add_os_release_variants(self.root)
        config = json.loads((self.root / 'renovate.json').read_text())
        for image in ('PHP_BASE_IMAGE', 'NODE_BASE_IMAGE'):
            manager = next(m for m in config['customManagers'] if any(image in s for s in m['matchStrings']))
            self.assertEqual(manager['versioningTemplate'], 'docker')
            expression = re.sub(r'\(\?<([a-zA-Z]+)>', r'(?P<\1>', manager['matchStrings'][0])
            matches = list(re.finditer(expression, (self.root / 'build/php/8.4.hcl').read_text()))
            self.assertTrue(any('-alpine3.23' in m['currentValue'] for m in matches))
            self.assertTrue(any('-alpine3.24' in m['currentValue'] for m in matches))
        php_rule = next(r for r in config['packageRules'] if r.get('matchPackageNames') == ['php']
                        and r.get('matchFileNames') == ['build/php/8.4.hcl'])
        self.assertEqual(php_rule['allowedVersions'], r'/^8\.4\./')

    def test_deleted_locks_are_removed_and_invalid_metadata_fails_without_writing(self):
        path = self.root / 'build/apk/3.24/amd64.json'
        original = json.loads(path.read_text())
        config_path = self.root / 'renovate.json'
        before = config_path.read_bytes()
        for key, value in (('alpine', '3.23'), ('architecture', 'aarch64'), ('packages', [])):
            path.write_text(json.dumps({**original, key: value}))
            with self.assertRaises(ValueError):
                synchronize(self.root)
            self.assertEqual(config_path.read_bytes(), before)
        path.unlink()
        synchronize(self.root)
        config = json.loads(config_path.read_text())
        self.assertFalse(any(r.get('matchFileNames') == ['build/apk/3.24/amd64.json'] for r in config['packageRules']))
