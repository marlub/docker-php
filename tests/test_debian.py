import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch

from test_images import RepositoryTests, ROOT
from plan_ci import create_ci_plan
import refresh_apt_locks
from build_group import PACKAGE_CHECKS, test_image


class DebianTests(RepositoryTests):
    def test_matching_features_pins_and_distinct_families(self):
        entries = self.entries()
        self.assertEqual(len({e['base_target'] for e in entries}), 8)
        for minor in ('8.2', '8.3', '8.4', '8.5'):
            alpine = [e for e in entries if e['minor'] == minor and e['distribution'] == 'alpine']
            debian = [e for e in entries if e['minor'] == minor and e['distribution'] == 'debian']
            self.assertEqual([e['node'] for e in debian], ['', '24'])
            self.assertEqual({e['distribution_release'] for e in debian}, {'trixie'})
            for left, right in zip(alpine, debian):
                for key in ('extensions', 'extension_versions', 'composer_version', 'xhprof'):
                    self.assertEqual(left[key], right[key])
                self.assertTrue(right['php_version'].startswith(minor + '.'))
                self.assertNotEqual(left['base_target'], right['base_target'])
            self.assertIn('-trixie-slim@sha256:', debian[1]['node_image'])

    def test_debian_recipe_and_node_changes_select_only_matching_images(self):
        path = self.root / 'Dockerfile.debian'
        for before, after, count in [('node:24.22.2-', 'node:24.999.0-', 4),
                                     ('RUN cp ', 'RUN cp -v ', 8)]:
            base = self.git('rev-parse', 'HEAD')
            path.write_text(path.read_text().replace(before, after, 1))
            self.commit()
            for purpose in ('test', 'publish'):
                selected = create_ci_plan(self.entries(), self.root, base, purpose=purpose)['phpimages']
                self.assertEqual(len(selected), count)
                self.assertTrue(all(e['distribution'] == 'debian' for e in selected))
                if count == 4:
                    self.assertTrue(all(e['node'] for e in selected))

    def test_apt_locks_select_debian_release(self):
        for arch in ('amd64', 'arm64'):
            path = self.root / f'build/apt/trixie/{arch}.json'
            base = self.git('rev-parse', 'HEAD')
            path.write_text(path.read_text() + '\n')
            self.commit()
            selected = create_ci_plan(self.entries(), self.root, base)['phpimages']
            self.assertEqual(len(selected), 8)
            self.assertEqual({e['distribution'] for e in selected}, {'debian'})

    def test_refresh_selects_only_debian_bases_without_existing_locks(self):
        import shutil
        shutil.rmtree(self.root / 'build/apt')
        with patch.object(refresh_apt_locks, 'ROOT', self.root):
            self.assertEqual(len(refresh_apt_locks.selected_base_images()), 4)
            entries = refresh_apt_locks.selected_base_images('8.4')
            self.assertEqual(len(entries), 1)
            self.assertIn('-fpm-trixie@sha256:', entries[0]['image'])
            self.assertIn('libatomic1', entries[0]['packages'].split())
            self.assertEqual(refresh_apt_locks.selected_base_images('8.1'), [])

    def test_refresh_merges_transitive_pins_and_fails_before_writing_conflicts(self):
        entries = [{'_base_image': 'cached-base', 'packages': 'libzip5'}] * 2
        lock = self.root / 'build/apt/trixie/amd64.json'
        first = {'debian': 'trixie', 'architecture': 'amd64', 'packages': {'libzip5': '1.11.3-1'}}
        second = {**first, 'packages': {'libatomic1': '14.2.0-19'}}
        def result(value):
            return SimpleNamespace(stdout=json.dumps(value).encode())
        with patch.object(refresh_apt_locks, 'ROOT', self.root), patch.object(
                refresh_apt_locks.subprocess, 'run', side_effect=[result(first), result(second)]):
            refresh_apt_locks.refresh('linux/amd64', entries)
        merged = json.loads(lock.read_text())
        self.assertEqual(merged['packages'], first['packages'] | second['packages'])
        original = lock.read_text()
        second['packages'] = {'libzip5': '1.11.3-2'}
        for invalid in (second, {**first, 'architecture': 'arm64'}):
            with patch.object(refresh_apt_locks, 'ROOT', self.root), patch.object(
                    refresh_apt_locks.subprocess, 'run', side_effect=[result(first), result(invalid)]):
                with self.assertRaises(ValueError):
                    refresh_apt_locks.refresh('linux/amd64', entries)
            self.assertEqual(lock.read_text(), original)

    def test_debian_backend_dispatches_package_checks(self):
        self.assertEqual(PACKAGE_CHECKS['debian'], 'tests/test_apt_lock.php')
        class Commands:
            root = ROOT
            calls = []
            def run(self, command, log):
                self.calls.append(command)
        commands = Commands()
        entry = next(e for e in self.entries() if e['distribution'] == 'debian')
        test_image(commands, entry, 'test-image', Path('/tmp'))
        self.assertEqual(commands.calls[0][-1], '/work/tests/test_apt_lock.php')
        self.assertEqual(len(commands.calls), 4)

    def test_renovate_uses_all_three_debian_suites_for_each_architecture(self):
        config = json.loads((ROOT / 'renovate.json').read_text())
        for arch in ('amd64', 'arm64'):
            rule = next(r for r in config['packageRules'] if r.get('matchFileNames') == [f'build/apt/trixie/{arch}.json'])
            self.assertEqual(len(rule['registryUrls']), 3)
            self.assertTrue(all(f'binaryArch={arch}' in url for url in rule['registryUrls']))
            self.assertEqual({re.search(r'suite=([^&]+)', u)[1] for u in rule['registryUrls']},
                             {'trixie', 'trixie-updates', 'trixie-security'})
