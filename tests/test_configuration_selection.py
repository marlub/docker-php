"""Resolve real Bake inheritance in disposable repositories; no image builds."""

import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from test_images import RepositoryTests
import plan_ci
from image_config import ConfigurationError, DEFAULT_DEBIAN_LABEL
from plan_ci import affected_image_tags, create_ci_plan, is_candidate_superseded
from variant_fixtures import add_os_release_variants


class ConfigurationSelectionTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        add_os_release_variants(self.root)
        self.fixture_ref = self.commit()

    def tags(self, predicate):
        return {entry['tagMinor'] for entry in self.entries() if predicate(entry)}

    def assert_selected(self, base, expected):
        entries = self.entries()
        for purpose in ('test', 'publish'):
            with self.subTest(purpose=purpose):
                plan = create_ci_plan(entries, self.root, base, purpose=purpose)
                self.assertEqual({entry['tagMinor'] for entry in plan['phpimages']}, expected)
                grouped = [entry for group in plan['buildgroups'] for entry in group['variants']]
                self.assertEqual(grouped, plan['phpimages'])
        return plan

    def change_image_pin(self, tag, field='php_image'):
        entry = next(entry for entry in self.entries() if entry['tagMinor'] == tag)
        path = self.root / entry['bake_file']
        old = entry[field]
        current_digest = old.split('@sha256:')[1]
        # Always change the fixture pin, including when the repository updates it.
        digest = ('0' if current_digest[0] != '0' else '1') + current_digest[1:]
        updated = old.split('@')[0] + '@sha256:' + digest
        self.assertNotEqual(old, updated)
        self.assertIn(old, path.read_text())
        path.write_text(path.read_text().replace(old, updated))

    def remove_family(self, base_target):
        entries = [entry for entry in self.entries() if entry['base_target'] == base_target]
        path = self.root / entries[0]['bake_file']
        targets = {entry['bake_target'] for entry in entries}

        def remove_names(match):
            names = [name for name in re.findall(r'"([^"\n]+)"', match[2]) if name not in targets]
            return match[1] + '[' + ', '.join('"' + name + '"' for name in names) + ']'

        path.write_text(re.sub(r'(  targets = )\[([^\n]+)\]', remove_names, path.read_text()))

    def test_reported_trixie_digest_update_selects_only_its_two_variants(self):
        candidates = self.entries()
        self.change_image_pin('8.5-fpm-trixie')
        self.commit()
        plan = self.assert_selected(self.fixture_ref, {'8.5-fpm-trixie', '8.5-fpm-trixie-node24'})
        self.assertEqual([group['id'] for group in plan['buildgroups']], ['php-8-5-trixie'])
        for candidate in candidates:
            if candidate['minor'] == '8.5':
                self.assertEqual(is_candidate_superseded(candidate, self.root), candidate['distribution'] == 'debian')

    def test_repeated_pin_updates_remain_independent_of_existing_digests(self):
        for tag, field, expected in (
            ('8.5-fpm-trixie', 'php_image', {'8.5-fpm-trixie', '8.5-fpm-trixie-node24'}),
            ('8.4-fpm-alpine3.24-node26', 'node_image', {'8.4-fpm-alpine3.24-node26'}),
        ):
            for update in range(2):
                with self.subTest(tag=tag, update=update):
                    before = self.git('rev-parse', 'HEAD')
                    self.change_image_pin(tag, field)
                    self.commit()
                    self.assert_selected(before, expected)

    def test_php_pins_are_independent_for_every_os_release(self):
        for release, tag in (('3.23', '8.4-fpm-alpine3.23'), ('3.24', '8.4-fpm-alpine3.24'),
                             ('bookworm', '8.4-fpm-bookworm'), ('trixie', '8.4-fpm-trixie')):
            with self.subTest(release=release):
                before = self.git('rev-parse', 'HEAD')
                expected = self.tags(lambda entry: entry['minor'] == '8.4' and entry['distribution_release'] == release)
                self.change_image_pin(tag)
                self.commit()
                self.assert_selected(before, expected)

    def test_explicit_node_pin_selects_one_release_and_major(self):
        for tag in ('8.4-fpm-alpine3.24-node26', '8.4-fpm-bookworm-node24', '8.4-fpm-alpine3.23-node28'):
            with self.subTest(tag=tag):
                before = self.git('rev-parse', 'HEAD')
                candidates = self.entries()
                self.change_image_pin(tag, 'node_image')
                self.commit()
                self.assert_selected(before, {tag})
                family = tag.rsplit('-node', 1)[0]
                for candidate in candidates:
                    if candidate['tagMinor'].startswith(family):
                        self.assertEqual(is_candidate_superseded(candidate, self.root), candidate['tagMinor'] == tag)

    def test_default_node_pin_only_selects_consumers_without_overrides(self):
        entry = next(entry for entry in self.entries() if entry['tagMinor'] == '8.4-fpm-alpine3.24-node24')
        path = self.root / entry['bake_file']
        path.write_text(path.read_text().replace('target "php-8-4-alpine-node24" {',
            'target "php-8-4-alpine-node24" {\n  args = { NODE_BASE_IMAGE = "' + entry['node_image'] + '" }'))
        before = self.commit()
        self.change_node()
        self.commit()
        self.assert_selected(before, {'8.2-fpm-alpine3.24-node24', '8.3-fpm-alpine3.24-node24',
                                      '8.5-fpm-alpine3.24-node24'})

    def test_node_stage_code_still_selects_explicit_overrides(self):
        expected = self.tags(lambda entry: entry['dockerfile'] == 'Dockerfile' and entry['node'])
        path = self.root / 'Dockerfile'
        path.write_text(path.read_text().replace('RUN ln -vs', 'RUN ln -vfs'))
        self.commit()
        self.assert_selected(self.fixture_ref, expected)

    def test_unfamiliar_node_pin_layout_keeps_conservative_node_scope(self):
        path = self.root / 'Dockerfile'
        content = path.read_text()
        declaration = re.search(r'^ARG NODE_BASE_IMAGE=.+\n', content, re.MULTILINE)[0]
        path.write_text(content.replace(declaration, '').replace(
            'FROM ${PHP_BASE_IMAGE} AS runtime\n', 'FROM ${PHP_BASE_IMAGE} AS runtime\n' + declaration))
        before = self.commit()
        self.change_node()
        self.commit()
        self.assert_selected(before, self.tags(lambda entry: entry['dockerfile'] == 'Dockerfile' and entry['node']))

    def test_family_packages_and_shared_profiles_follow_inheritance(self):
        path = self.root / 'build/php/8.4.hcl'
        path.write_text(path.read_text().replace('RUNTIME_PACKAGES = "libffi"', 'RUNTIME_PACKAGES = "libffi libgcc"'))
        before = self.commit()
        expected = self.tags(lambda entry: entry['distribution_release'] == '3.23')
        self.assert_selected(self.fixture_ref, expected)
        shared = self.root / 'docker-bake.hcl'
        shared.write_text(shared.read_text().replace('"brotli-libs",', '"brotli-libs", "libexample",'))
        self.commit()
        expected = self.tags(lambda entry: entry['distribution_release'] in ('3.24', 'bookworm'))
        self.assert_selected(before, expected)

    def test_shared_minor_extensions_select_all_its_releases(self):
        expected = self.tags(lambda entry: entry['minor'] == '8.4')
        self.change_arg('8.4', 'REDIS_VERSION', '6.3.99')
        self.commit()
        self.assert_selected(self.fixture_ref, expected)

    def test_resolved_target_settings_select_their_consumers(self):
        path = self.root / 'build/php/8.4.hcl'
        path.write_text(path.read_text().replace('target "_php-8-4-alpine3-23" {',
            'target "_php-8-4-alpine3-23" {\n  extra-hosts = { "example.test" = "127.0.0.2" }'))
        self.commit()
        self.assert_selected(self.fixture_ref, self.tags(lambda entry: entry['distribution_release'] == '3.23'))

    def test_comments_and_ignored_ci_labels_select_nothing(self):
        for filename in ('docker-bake.hcl', 'build/php/8.4.hcl'):
            path = self.root / filename
            path.write_text(path.read_text() + '\n# configuration documentation\n')
        before = self.commit()
        self.assert_selected(self.fixture_ref, set())
        path = self.root / 'docker-bake.hcl'
        path.write_text(path.read_text().replace('target "_common" {',
            'target "_common" {\n  labels = { "org.opencontainers.image.revision" = "ignored", '
            '"io.github.marlub.docker-php.variant" = "ignored" }'))
        self.commit()
        self.assert_selected(before, set())

    def test_plain_images_ignore_node_arguments(self):
        expected = self.tags(lambda entry: entry['minor'] == '8.4' and entry['node'])
        self.change_arg('8.4', 'NODE_VERSION', '24.99.0')
        self.commit()
        self.assert_selected(self.fixture_ref, expected)

    def test_mixed_multicommit_updates_combine_exact_variants(self):
        self.change_image_pin('8.5-fpm-trixie')
        self.commit()
        self.change_image_pin('8.4-fpm-alpine3.23-node28', 'node_image')
        self.commit()
        self.assert_selected(self.fixture_ref,
                             {'8.5-fpm-trixie', '8.5-fpm-trixie-node24', '8.4-fpm-alpine3.23-node28'})

    def test_removing_release_selects_successor_alias_owners_and_supersedes_removed_images(self):
        candidates = self.entries()
        self.remove_family('php-8-4-alpine')
        self.commit()
        expected = self.tags(lambda entry: entry['distribution_release'] == '3.23')
        self.assert_selected(self.fixture_ref, expected)
        removed = [entry for entry in candidates if entry['base_target'] == 'php-8-4-alpine']
        self.assertEqual(affected_image_tags(removed, self.root, self.fixture_ref),
                         {entry['tagMinor'] for entry in removed})
        for candidate in candidates:
            if candidate['minor'] == '8.4':
                self.assertEqual(is_candidate_superseded(candidate, self.root), candidate['distribution'] == 'alpine')

    def test_adding_newer_alpine_release_selects_new_family_and_previous_alias_owners(self):
        path = self.root / 'build/php/8.4.hcl'
        original = path.read_text()
        addition = original[original.index('target "_php-8-4-alpine3-23"'):]
        addition = addition.replace('3-23', '3-25').replace('3.23', '3.25')
        names = ['php-8-4-alpine3-25', 'php-8-4-alpine3-25-node24', 'php-8-4-alpine3-25-node28']
        original = re.sub(r'(  targets = \[[^\n]+)\]',
                          lambda match: match[1] + ', ' + ', '.join('"' + name + '"' for name in names) + ']', original)
        path.write_text(original + '\n' + addition)
        self.commit()
        self.assert_selected(self.fixture_ref,
                             self.tags(lambda entry: entry['minor'] == '8.4' and entry['distribution_release'] in ('3.24', '3.25')))

    def test_default_debian_switch_selects_old_and_new_alias_owners(self):
        candidates = self.entries()
        path = self.root / 'build/php/8.4.hcl'
        content = path.read_text().replace(', "' + DEFAULT_DEBIAN_LABEL + '" = "true"', '')
        content = content.replace('"php-8-4-bookworm" }',
                                  '"php-8-4-bookworm", "' + DEFAULT_DEBIAN_LABEL + '" = "true" }')
        path.write_text(content)
        self.commit()
        self.assert_selected(self.fixture_ref, self.tags(lambda entry: entry['minor'] == '8.4' and entry['distribution'] == 'debian'))
        for candidate in candidates:
            if candidate['minor'] == '8.4':
                self.assertEqual(is_candidate_superseded(candidate, self.root), candidate['distribution'] == 'debian')

    def test_unresolvable_history_warns_and_uses_legacy_manifest_or_shared_scope(self):
        for filename in ('build/php/8.5.hcl', 'docker-bake.hcl'):
            with self.subTest(filename=filename):
                path = self.root / filename
                original = path.read_text()
                path.write_text(original + '\ninvalid !\n')
                before = self.commit()
                path.write_text(original)
                self.commit()
                expected = self.tags(lambda entry: filename == 'docker-bake.hcl' or entry['minor'] == '8.5')
                warning = io.StringIO()
                with redirect_stderr(warning):
                    self.assert_selected(before, expected)
                self.assertIn('::warning::Cannot resolve historical configuration', warning.getvalue())

    def test_invalid_head_fails_even_if_history_is_invalid(self):
        entries = self.entries()
        path = self.root / 'build/php/8.5.hcl'
        path.write_text(path.read_text() + '\ninvalid !\n')
        before = self.commit()
        path.write_text(path.read_text() + '\n# changed again\n')
        self.commit()
        for purpose in ('test', 'publish'):
            with self.assertRaises(ConfigurationError):
                create_ci_plan(entries, self.root, before, purpose=purpose)

    def test_historical_failure_falls_back_for_default_node_pins_and_warns_once(self):
        path = self.root / 'build/php/8.5.hcl'
        content = path.read_text()
        path.write_text(content + '\ninvalid !\n')
        before = self.commit()
        path.write_text(content)
        self.change_node()
        self.commit()
        expected = self.tags(lambda entry: entry['minor'] == '8.5'
                             or (entry['dockerfile'] == 'Dockerfile' and entry['node']))
        warning = io.StringIO()
        with redirect_stderr(warning):
            self.assert_selected(before, expected)
        self.assertEqual(warning.getvalue().count('::warning::Cannot resolve historical configuration'), 2)

    def test_cli_resolves_each_committed_endpoint_once_using_current_loader(self):
        for filename in ('scripts/image_config.py', 'scripts/plan_ci.py'):
            (self.root / filename).write_text('raise RuntimeError("must not execute historical scripts")')
        before = self.commit()
        self.change_image_pin('8.5-fpm-trixie')
        self.change_image_pin('8.4-fpm-alpine3.23-node28', 'node_image')
        self.change_node()
        self.commit()
        head = self.git('rev-parse', 'HEAD')
        # Dirty inputs must not participate either.
        (self.root / 'build/php/8.2.hcl').write_text('invalid !')
        output = self.root / '.git/matrix.json'
        with patch('plan_ci.__file__', str(self.root / 'scripts/plan_ci.py')), \
                patch('plan_ci.load_image_configuration', wraps=plan_ci.load_image_configuration) as loader, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(plan_ci.main(['--base-ref', before, '--out', str(output)]), 0)
        self.assertEqual([call.kwargs['source_ref'] for call in loader.call_args_list], [head, before])


if __name__ == '__main__':
    unittest.main()
