import json
import re
import subprocess
import sys
import unittest

from test_images import RepositoryTests
from plan_ci import (PlanningError, affected_image_tags, create_ci_plan,
                     is_candidate_superseded, prepare_build_group)


class BuildGroupTests(RepositoryTests):
    def assert_groups(self, plan):
        flattened = [entry for group in plan['buildgroups'] for entry in group['variants']]
        self.assertEqual(sorted(e['tagMinor'] for e in flattened),
                         sorted(e['tagMinor'] for e in plan['phpimages']))
        for group in plan['buildgroups']:
            self.assertTrue(group['variants'])
            self.assertEqual({entry['minor'] for entry in group['variants']}, {group['minor']})
            self.assertEqual(group['variants'], sorted(group['variants'], key=lambda e: bool(e['node'])))

    def test_full_manual_and_initial_push_group_selected_variants(self):
        entries = self.entries()
        for purpose in ('test', 'publish'):
            for options in ({}, {'php': '8.4'}, {'base_ref': '0' * 40}):
                with self.subTest(purpose=purpose, options=options):
                    plan = create_ci_plan(list(reversed(entries)), self.root, purpose=purpose, **options)
                    self.assert_groups(plan)
                    self.assertEqual(len(plan['buildgroups']), 1 if 'php' in options else 4)

    def test_empty_node_only_and_minor_changes_never_broaden_selection(self):
        self.assertEqual(create_ci_plan(self.entries(), self.root, self.base)['buildgroups'], [])
        self.change_node()
        self.commit()
        for purpose in ('test', 'publish'):
            plan = create_ci_plan(self.entries(), self.root, self.base, purpose=purpose)
            self.assert_groups(plan)
            self.assertEqual(len(plan['buildgroups']), 4)
            self.assertTrue(all(len(g['variants']) == 1 and g['variants'][0]['node'] == '24'
                                for g in plan['buildgroups']))
        before = self.git('rev-parse', 'HEAD')
        self.change_arg('8.4', 'REDIS_VERSION', '6.3.99')
        self.commit()
        plan = create_ci_plan(self.entries(), self.root, before)
        self.assert_groups(plan)
        self.assertEqual([g['minor'] for g in plan['buildgroups']], ['8.4'])
        self.assertEqual(len(plan['buildgroups'][0]['variants']), 2)

    def test_plain_only_subset_is_not_expanded(self):
        entry = self.entries()[0]
        plan = create_ci_plan([entry], self.root)
        self.assertEqual(plan['buildgroups'], [{'id': entry['base_target'], 'minor': entry['minor'],
            'distribution': entry['distribution'], 'distribution_release': entry['distribution_release'], 'variants': [entry]}])

    def test_group_validation_rejects_invalid_duplicate_mixed_or_stale_entries(self):
        entries = self.entries()
        for group in (None, {}, [], '[]', [None], [{}], entries[:3],
                      [entries[0], entries[0]], [entries[0], entries[3]],
                      [dict(entries[0], source_ref='a' * 40)],
                      [dict(entries[0], bake_target='unknown')]):
            with self.subTest(group=group):
                with self.assertRaises(PlanningError):
                    prepare_build_group(group, entries)
        for group in (entries[:2][::-1], entries[:1], entries[1:2]):
            self.assertEqual(prepare_build_group(group, entries), sorted(group, key=lambda e: bool(e['node'])))

    def test_group_cli_emits_validated_inputs_and_rejects_other_modes(self):
        script = self.root / 'scripts/plan_ci.py'
        entries = self.entries()
        output = self.root / '.git/group-output'
        for group in (entries[:2][::-1], entries[:1], entries[1:2]):
            output.unlink(missing_ok=True)
            command = [sys.executable, str(script), '--prepare-build-group', json.dumps(group),
                       '--github-output', str(output)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertEqual(json.loads(values['variants']), sorted(group, key=lambda e: bool(e['node'])))
            self.assertEqual(values['id'], group[0]['base_target'])
            self.assertNotEqual(subprocess.run(command + ['--php', '8.4'], capture_output=True).returncode, 0)
        output.unlink()
        result = subprocess.run([sys.executable, str(script), '--prepare-build-group', '[]',
                                 '--github-output', str(output)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())


class SelectionPurposeTests(RepositoryTests):
    def change_file(self, filename):
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_text() if path.exists() else ''
        path.write_text(content.replace('RUN cp ', 'RUN cp -v ', 1) if filename == 'Dockerfile' else content + '\n')
        self.commit()

    def test_publication_inputs_and_pr_only_paths(self):
        cases = {
            'Dockerfile': (8, 8),
            'docker-bake.hcl': (8, 8),
            'entrypoint.sh': (8, 8),
            'build/shared.json': (8, 8),
            'build/php/8.3.hcl': (2, 2),
            'build/apk/3.24/amd64.json': (8, 8),
            'scripts/build_pie_extension.sh': (8, 8),
            'scripts/apk_lock.php': (8, 8),
            '.github/workflows/template-images.yml': (8, 8),
            '.github/actions/build-group/action.yml': (8, 8),
            '.github/workflows/build-test-publish.yml': (8, 8),
            '.github/workflows/build-test.yml': (8, 0),
            '.github/workflows/cache-cleanup.yml': (0, 0),
            'scripts/cleanup_registry_cache.py': (0, 0),
            '.github/workflows/unknown.yml': (8, 8),
            'tests/new_test.py': (8, 0),
            'tests/test_image.sh': (8, 0),
            'tests/verify_image.py': (8, 0),
            'scripts/plan_ci.py': (8, 0),
            'scripts/image_config.py': (8, 0),
            'scripts/refresh_apk_locks.py': (8, 0),
            'scripts/unknown.sh': (8, 8),
            '.github/workflows/template-renovate.yml': (0, 0),
            'renovate.json': (0, 0),
            'README.md': (0, 0),
            'LICENSE': (0, 0),
            'docs/nested/building.md': (0, 0),
            'future-input.json': (8, 8),
            '.dockerignore': (8, 8),
            'new-directory/input.txt': (8, 8),
            'src/docs/runtime.json': (8, 8),
            'new-directory/README.md': (8, 8),
        }
        for filename, counts in cases.items():
            with self.subTest(filename=filename):
                before = self.git('rev-parse', 'HEAD')
                self.change_file(filename)
                for purpose, count in zip(('test', 'publish'), counts):
                    selected = create_ci_plan(self.entries(), self.root, before, purpose=purpose)
                    self.assertEqual(len(selected['phpimages']), count, purpose)
                    renovate_changed = filename in ('renovate.json', '.github/workflows/template-renovate.yml')
                    self.assertEqual(selected['renovate_changes'], renovate_changed)

    def test_mixed_changes_keep_job_selection_independent(self):
        for revision, (filename, test_count, renovate) in enumerate([
            ('tests/new_test.py', 8, False),
            ('renovate.json', 2, True),
            ('README.md', 2, False),
            ('.github/workflows/cache-cleanup.yml', 2, False),
            ('scripts/cleanup_registry_cache.py', 2, False),
        ], start=1):
            with self.subTest(filename=filename):
                before = self.git('rev-parse', 'HEAD')
                self.change_file(filename)
                self.change_arg('8.3', 'REDIS_VERSION', f'6.3.{revision}')
                self.commit()
                tested = create_ci_plan(self.entries(), self.root, before)
                self.assertEqual(len(tested['phpimages']), test_count)
                self.assertEqual(tested['renovate_changes'], renovate)
                published = create_ci_plan(self.entries(), self.root, before, purpose='publish')
                self.assertEqual([e['tagMinor'] for e in published['phpimages']],
                                 ['8.3-fpm-alpine', '8.3-fpm-alpine-node24'])

    def test_installer_renames_and_deletions_select_publication(self):
        path = self.root / 'scripts/build_pie_extension.sh'
        moved = self.root / 'scripts/maintenance.sh'
        path.rename(moved)
        self.commit()
        self.assertEqual(len(affected_image_tags(self.entries(), self.root, self.base, purpose='publish')), 8)
        before = self.git('rev-parse', 'HEAD')
        moved.rename(path)
        self.commit()
        self.assertEqual(len(affected_image_tags(self.entries(), self.root, before, purpose='publish')), 8)
        before = self.git('rev-parse', 'HEAD')
        path.unlink()
        self.commit()
        self.assertEqual(len(affected_image_tags(self.entries(), self.root, before, purpose='publish')), 8)

    def test_cli_purpose_and_empty_output(self):
        self.change_file('tests/new_test.py')
        script = self.root / 'scripts/plan_ci.py'
        output = self.root / '.git/matrix.json'
        github_output = self.root / '.git/github-output'
        args = [sys.executable, str(script), '--base-ref', self.base, '--out', str(output)]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(output.read_text())['phpimages']), 8)
        result = subprocess.run(args + ['--purpose', 'publish', '--github-output', str(github_output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text())['phpimages'], [])
        self.assertIn('has_changes=false\n', github_output.read_text())
        self.assertNotEqual(subprocess.run(args + ['--purpose', 'invalid'], capture_output=True).returncode, 0)

    def test_supersession_ignores_pr_only_paths_but_includes_pipeline(self):
        entry = self.entries()[0]
        for filename in ('tests/new_test.py', 'tests/verify_image.py', 'scripts/plan_ci.py',
                         'scripts/refresh_apk_locks.py', '.github/workflows/build-test.yml',
                         '.github/workflows/cache-cleanup.yml', 'scripts/cleanup_registry_cache.py',
                         '.github/workflows/template-renovate.yml', 'renovate.json'):
            with self.subTest(filename=filename):
                self.change_file(filename)
                self.assertFalse(is_candidate_superseded(entry, self.root))
        self.change_file('.github/workflows/template-images.yml')
        self.assertTrue(is_candidate_superseded(entry, self.root))
        entry = self.entries()[0]
        self.change_file('.github/workflows/build-test-publish.yml')
        self.assertTrue(is_candidate_superseded(entry, self.root))
        entry = self.entries()[0]
        self.change_file('future-input.json')
        self.assertTrue(is_candidate_superseded(entry, self.root))


class RenovateChangeDetectionTests(RepositoryTests):
    def detect(self, base=None):
        output = self.root / '.git/renovate-output'
        output.unlink(missing_ok=True)
        result = subprocess.run([sys.executable, str(self.root / 'scripts/plan_ci.py'),
                                 '--base-ref', base or self.base, '--out', str(self.root / '.git/matrix.json'),
                                 '--github-output', str(output)], cwd=self.root, capture_output=True, text=True)
        values = dict(line.split('=', 1) for line in output.read_text().splitlines()) if output.exists() else {}
        return result, values

    def test_no_changes_and_unrelated_changes_skip_validator(self):
        for changed in (False, True):
            if changed:
                (self.root / 'README.md').write_text('documentation')
                self.commit()
            result, output = self.detect()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output['renovate_changes'], 'false')
            self.assertEqual(output['has_changes'], 'false')

    def test_validator_version_update_emits_true_without_images(self):
        path = self.root / '.github/workflows/template-renovate.yml'
        path.write_text(re.sub(r'renovate@[0-9.]+', 'renovate@44.999.1', path.read_text()))
        self.commit()
        result, output = self.detect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output['renovate_changes'], 'true')
        self.assertEqual(output['has_changes'], 'false')
        matrix = json.loads((self.root / '.git/matrix.json').read_text())
        self.assertEqual(matrix['phpimages'], [])

    def test_relevant_file_edits_deletions_additions_and_renames(self):
        for filename in ('renovate.json', '.github/workflows/template-renovate.yml'):
            with self.subTest(filename=filename):
                path = self.root / filename
                content = path.read_text()
                moved = self.root / 'renamed.txt'
                operations = [
                    ('edit', lambda: path.write_text(content + '\n'), 'false'),
                    ('rename out', lambda: path.rename(moved), 'true'),
                    ('rename back', lambda: moved.rename(path), 'true'),
                    ('delete', path.unlink, 'false'),
                    ('add', lambda: path.write_text(content), 'false'),
                ]
                for operation, change, images in operations:
                    with self.subTest(operation=operation):
                        before = self.git('rev-parse', 'HEAD')
                        change()
                        self.commit()
                        result, output = self.detect(before)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(output['renovate_changes'], 'true')
                        self.assertEqual(output['has_changes'], images)

    def test_detection_uses_merge_result(self):
        self.git('checkout', '-qb', 'feature')
        (self.root / 'renovate.json').write_text('invalid json')
        self.commit()
        self.git('checkout', 'main')
        (self.root / 'README.md').write_text('documentation')
        base = self.commit()
        self.git('merge', '--no-ff', '-qm', 'merge result', 'feature')
        result, output = self.detect(base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output['renovate_changes'], 'true')

    def test_git_error_fails_without_false_success_output(self):
        result, output = self.detect('missing-base')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, {})


if __name__ == '__main__':
    unittest.main()
