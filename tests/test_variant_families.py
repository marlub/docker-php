import json
import re
import shutil
import unittest
from unittest.mock import patch

from test_images import RepositoryTests
from image_config import ConfigurationError, variant_order
from plan_ci import create_ci_plan, is_candidate_superseded, prepare_build_group, PlanningError
from variant_fixtures import add_family_variants


class FamilyConfigurationTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        add_family_variants(self.root)
        self.fixture_ref = self.commit()

    def family(self, base):
        return [entry for entry in self.entries() if entry['base_target'] == base]

    def test_discover_two_distributions_with_two_node_majors_each(self):
        entries = self.entries()
        self.assertEqual(len(entries), 12)
        plan = create_ci_plan(entries, self.root, php='8.4')
        self.assertEqual(len(plan['phpimages']), 6)
        self.assertEqual([g['id'] for g in plan['buildgroups']], ['php-8-4', 'php-8-4-bookworm'])
        for group in plan['buildgroups']:
            self.assertEqual([v['node'] for v in group['variants']], ['', '24', '26'])
            self.assertEqual({v['base_target'] for v in group['variants']}, {group['id']})
            self.assertEqual({(v['distribution'], v['distribution_release']) for v in group['variants']},
                             {(group['distribution'], group['distribution_release'])})
        self.assertEqual(plan['buildgroups'][0]['distribution'], 'alpine')
        self.assertEqual(plan['buildgroups'][1]['distribution'], 'debian')
        self.assertEqual(plan['buildgroups'][1]['distribution_release'], 'bookworm')
        self.assertEqual(len({v[key] for v in entries for key in ('tagMinor', 'tagPatch')}), 24)
        self.assertEqual(self.family('php-8-4-bookworm')[1]['node_image'].split(':')[1].split('@')[0],
                         '24.1.0-bookworm-slim')

    def test_subsets_order_and_validation_do_not_add_plain_or_other_nodes(self):
        family = self.family('php-8-4')
        for selected in (family, family[:1], family[1:], family[2:]):
            with self.subTest(selected=[v['node'] for v in selected]):
                plan = create_ci_plan(selected[::-1], self.root)
                self.assertEqual(plan['buildgroups'][0]['variants'], sorted(selected, key=variant_order))
                self.assertEqual(prepare_build_group(selected[::-1], self.entries()), sorted(selected, key=variant_order))
        self.assertEqual(create_ci_plan(self.entries(), self.root, self.fixture_ref)['buildgroups'], [])
        with self.assertRaises(PlanningError):
            prepare_build_group([family[0], self.family('php-8-4-bookworm')[1]], self.entries())
        with self.assertRaises(PlanningError):
            prepare_build_group([family[1], family[1]], self.entries())

    def test_node_majors_sort_numerically(self):
        path = self.root / 'build/php/8.4.hcl'
        path.write_text(path.read_text().replace('node26', 'node100').replace('node:26.1.0', 'node:100.1.0'))
        family = self.family('php-8-4')
        self.assertEqual([e['node'] for e in prepare_build_group(family[::-1], self.entries())], ['', '24', '100'])

    def test_bad_shared_arguments_recipe_node_pins_and_tags_are_rejected(self):
        path = self.root / 'build/php/8.4.hcl'
        original = path.read_text()
        replacements = [
            ('NODE_BASE_IMAGE = "node:26.1.0-alpine3.24', 'EXTENSIONS = "redis", NODE_BASE_IMAGE = "node:26.1.0-alpine3.24'),
            ('NODE_BASE_IMAGE = "node:26.1.0-alpine3.24', 'NODE_BASE_IMAGE = "node:26.1.0-bookworm'),
            ('NODE_BASE_IMAGE = "node:26.1.0-alpine3.24', 'NODE_BASE_IMAGE = "node:24.1.0-alpine3.24'),
            ('node:26.1.0-alpine3.24@sha256:' + '1' * 64, 'node:latest'),
            ('tags = ["docker-php:8.4-fpm-bookworm"]', 'tags = ["docker-php:8.4-fpm-alpine"]'),
            ('target "php-8-4-node26" {', 'target "php-8-4-node26" {\n  dockerfile = "Dockerfile.debian"'),
        ]
        for before, after in replacements:
            with self.subTest(after=after):
                path.write_text(original.replace(before, after, 1))
                with self.assertRaises(ConfigurationError):
                    self.entries()
        path.write_text(original)

    def test_missing_canonical_target_is_rejected(self):
        path = self.root / 'build/php/8.4.hcl'
        path.write_text(path.read_text().replace('targets = ["php-8-4", ', 'targets = [', 1))
        with self.assertRaises(ConfigurationError):
            self.entries()

    def test_minor_lock_and_recipe_selections_respect_families(self):
        lock = self.root / 'build/apk/3.24/amd64.json'
        lock.write_text(lock.read_text() + '\n')
        self.commit()
        selected = create_ci_plan(self.entries(), self.root, self.fixture_ref)
        self.assertEqual(len(selected['phpimages']), 9)
        self.assertTrue(all(v['distribution'] == 'alpine' for v in selected['phpimages']))
        before = self.git('rev-parse', 'HEAD')
        self.change_node()
        self.commit()
        selected = create_ci_plan(self.entries(), self.root, before)
        self.assertEqual(len(selected['phpimages']), 5)
        self.assertTrue(all(v['node'] and v['distribution'] == 'alpine' for v in selected['phpimages']))
        before = self.git('rev-parse', 'HEAD')
        recipe = self.root / 'Dockerfile.debian'
        recipe.write_text(recipe.read_text().replace('RUN cp ', 'RUN cp -v ', 1))
        self.commit()
        selected = create_ci_plan(self.entries(), self.root, before)
        self.assertEqual(len(selected['phpimages']), 3)
        self.assertTrue(all(v['distribution'] == 'debian' for v in selected['phpimages']))
        before = self.git('rev-parse', 'HEAD')
        self.change_arg('8.4', 'REDIS_VERSION', '6.3.99')
        self.commit()
        selected = create_ci_plan(self.entries(), self.root, before)
        self.assertEqual(len(selected['phpimages']), 6)

    def test_apk_refresh_ignores_debian_and_does_not_need_locks(self):
        import refresh_apk_locks
        shutil.rmtree(self.root / 'build/apk')
        with patch.object(refresh_apk_locks, 'ROOT', self.root):
            entries = refresh_apk_locks.selected_base_images('8.4')
        self.assertEqual(len(entries), 1)
        self.assertIn('-alpine3.24@', entries[0]['image'])

    def test_pie_publication_exemption_is_independent_for_each_recipe(self):
        entries = self.entries()
        for recipe in ('Dockerfile', 'Dockerfile.debian'):
            with self.subTest(recipe=recipe):
                before = self.git('rev-parse', 'HEAD')
                self.change_pie(recipe=recipe)
                self.commit()
                expected = {entry['tagMinor'] for entry in entries if entry['dockerfile'] == recipe}
                tested = create_ci_plan(self.entries(), self.root, before)
                self.assertEqual({entry['tagMinor'] for entry in tested['phpimages']}, expected)
                self.assertEqual(create_ci_plan(self.entries(), self.root, before, purpose='publish')['phpimages'], [])
                for entry in entries:
                    self.assertFalse(is_candidate_superseded(entry, self.root), entry['tagMinor'])

        # A PIE-only update to one recipe must not hide a real change to the other.
        before = self.git('rev-parse', 'HEAD')
        self.change_pie(version='2.0.0', recipe='Dockerfile.debian')
        self.change_node()
        self.commit()
        published = create_ci_plan(self.entries(), self.root, before, purpose='publish')
        self.assertEqual({entry['tagMinor'] for entry in published['phpimages']},
                         {entry['tagMinor'] for entry in entries
                          if entry['dockerfile'] == 'Dockerfile' and entry['node']})
        # PIE plus Node changes within a recipe retain all configured Node majors.
        before = self.git('rev-parse', 'HEAD')
        self.change_pie(version='2.0.0')
        path = self.root / 'Dockerfile'
        path.write_text(path.read_text().replace('node:24.999.0-', 'node:24.999.1-'))
        self.commit()
        published = create_ci_plan(self.entries(), self.root, before, purpose='publish')
        self.assertEqual({entry['tagMinor'] for entry in published['phpimages']},
                         {entry['tagMinor'] for entry in entries
                          if entry['dockerfile'] == 'Dockerfile' and entry['node']})

    def test_renovate_extracts_target_specific_pins_and_keeps_every_major_explicit(self):
        config = json.loads((self.root / 'renovate.json').read_text())
        manager = next(m for m in config['customManagers'] if any('NODE_BASE_IMAGE' in pattern for pattern in m['matchStrings']))
        pattern = re.sub(r'\(\?<([a-zA-Z]+)>', r'(?P<\1>', manager['matchStrings'][0])
        pins = [match.groupdict() for match in re.finditer(pattern, (self.root / 'build/php/8.4.hcl').read_text())]
        self.assertEqual(len(pins), 3)
        self.assertEqual({pin['depName'] for pin in pins}, {'node'})
        self.assertEqual({pin['currentValue'].split('.')[0] for pin in pins}, {'24', '26'})
        self.assertTrue(all(re.fullmatch(r'sha256:[a-f0-9]{64}', pin['currentDigest']) for pin in pins))
        rule = next(rule for rule in config['packageRules'] if rule.get('matchPackageNames') == ['node'])
        self.assertEqual(rule['matchUpdateTypes'], ['major'])
        self.assertFalse(rule['enabled'])
        self.assertNotIn('allowedVersions', rule)


if __name__ == '__main__':
    unittest.main()
