import copy
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from cleanup_registry_cache import CleanupError, cleanup
from github_api import GitHubAPI, GitHubAPIError


NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
REPOSITORY = 'marlub/docker-php'
PACKAGE_PATH = '/users/marlub/packages/container/php-buildcache'


def version(identifier, tags, age=timedelta(days=8)):
    return {'id': identifier, 'metadata': {'container': {'tags': tags}},
            'updated_at': (NOW - age).isoformat()}


class FakeAPI:
    def __init__(self, versions=(), states=None, owner_type='User'):
        self.versions = {item['id']: copy.deepcopy(item) for item in versions}
        self.states = states or {}
        self.owner_type = owner_type
        self.calls = []
        self.before_get = None
        self.missing_package = False
        self.delete_error = False

    def request(self, method, path, *, missing_ok=False):
        self.calls.append((method, path))
        if path == '/users/marlub':
            return {'type': self.owner_type}
        if '/pulls/' in path:
            state = self.states[int(path.rsplit('/', 1)[1])]
            return {'state': state, 'merged_at': None} if isinstance(state, str) else copy.deepcopy(state)
        parsed = urlsplit(path)
        if parsed.query:
            if self.missing_package:
                assert missing_ok
                return None
            page = int(parse_qs(parsed.query)['page'][0])
            values = list(self.versions.values())
            return copy.deepcopy(values[(page - 1) * 100:page * 100])
        identifier = int(path.rsplit('/', 1)[1])
        if method == 'GET':
            if self.before_get:
                self.before_get(self, identifier)
            return copy.deepcopy(self.versions.get(identifier))
        assert method == 'DELETE' and missing_ok
        if self.delete_error:
            raise CleanupError('HTTP 403')
        self.versions.pop(identifier, None)


class RegistryCleanupTests(unittest.TestCase):
    def run_cleanup(self, api, **kwargs):
        with patch('sys.stdout', new=io.StringIO()):
            return cleanup(api, REPOSITORY, 'php-buildcache', now=NOW, **kwargs)

    def test_preserves_main_open_pr_unknown_and_mixed_tags(self):
        api = FakeAPI([
            version(1, ['main-8.4-fpm-alpine-amd64']),
            version(2, ['pr-42-8.4-fpm-alpine-amd64']),
            version(3, ['pr-43-8.4-fpm-alpine-amd64']),
            version(4, ['pr-42-8.4-fpm-alpine-amd64', 'main-8.4-fpm-alpine-amd64']),
            version(5, ['pr-42-8.4-fpm-alpine-amd64', 'pr-43-8.4-fpm-alpine-amd64']),
            version(6, ['pr-42-8.4-fpm-alpine-amd64', 'unknown']),
            version(7, ['pr-42-malformed']),
        ], states={42: 'closed', 43: 'open'})
        self.assertEqual(self.run_cleanup(api), [2])
        self.assertEqual(set(api.versions), {1, 3, 4, 5, 6, 7})

    def test_targeted_closed_pr_only_deletes_that_pr_including_shared_variant_tags(self):
        api = FakeAPI([
            version(1, ['pr-42-8.4-fpm-alpine-amd64', 'pr-42-8.4-fpm-alpine-node24-amd64']),
            version(2, ['pr-43-8.4-fpm-alpine-arm64']),
            version(3, []),
        ], states={42: 'closed', 43: 'closed'})
        self.assertEqual(self.run_cleanup(api, pr_number=42), [1])
        self.assertEqual(set(api.versions), {2, 3})

    def test_reopened_pr_is_preserved_even_on_closed_event(self):
        api = FakeAPI([version(1, ['pr-42-8.4-fpm-alpine-amd64'])], states={42: 'open'})
        self.assertEqual(self.run_cleanup(api, pr_number=42), [])

    def test_distribution_releases_and_additional_node_majors_use_existing_cleanup_policy(self):
        api = FakeAPI([
            version(1, ['pr-42-8.4-fpm-bookworm-node26-amd64']),
            version(2, ['main-8.4-fpm-bookworm-node26-amd64']),
            version(3, ['pr-43-8.4-fpm-alpine3.25-node26-arm64']),
            version(4, ['pr-42-8.4-fpm-alpine3.25-node26-arm64']),
        ], states={42: 'closed', 43: 'open'})
        self.assertEqual(self.run_cleanup(api), [1, 4])
        self.assertEqual(set(api.versions), {2, 3})

    def test_merged_pr_cache_grace_period_for_targeted_and_daily_cleanup(self):
        for age, deleted in ((timedelta(hours=48, microseconds=-1), []),
                             (timedelta(hours=48), [1]),
                             (timedelta(hours=48, microseconds=1), [1])):
            for targeted in (False, True):
                with self.subTest(age=age, targeted=targeted):
                    api = FakeAPI([version(1, ['pr-42-8.4-fpm-alpine-amd64'])], states={
                        42: {'state': 'closed', 'merged_at': (NOW - age).isoformat()},
                    })
                    self.assertEqual(self.run_cleanup(api, pr_number=42 if targeted else None), deleted)

    def test_old_cache_does_not_shorten_grace_and_fresh_cache_does_not_extend_it(self):
        for merge_age, cache_age, deleted in ((timedelta(hours=1), timedelta(days=30), []),
                                               (timedelta(hours=49), timedelta(minutes=1), [1])):
            with self.subTest(merge_age=merge_age):
                api = FakeAPI([version(1, ['pr-42-8.4-fpm-alpine-amd64'], cache_age)], states={
                    42: {'state': 'closed', 'merged_at': (NOW - merge_age).isoformat()},
                })
                self.assertEqual(self.run_cleanup(api), deleted)

    def test_untagged_cutoff_is_strictly_older_than_seven_days(self):
        api = FakeAPI([
            version(1, [], timedelta(days=7, seconds=1)),
            version(2, [], timedelta(days=7)),
            version(3, [], timedelta(days=6)),
            version(4, [], timedelta(seconds=-1)),
        ])
        self.assertEqual(self.run_cleanup(api), [1])

    def test_pagination_finishes_before_deletions(self):
        api = FakeAPI([version(number, []) for number in range(1, 102)])
        self.assertEqual(len(self.run_cleanup(api)), 101)
        second_page = api.calls.index(('GET', PACKAGE_PATH + '/versions?per_page=100&page=2'))
        first_delete = next(index for index, call in enumerate(api.calls) if call[0] == 'DELETE')
        self.assertLess(second_page, first_delete)
        self.assertFalse(api.versions)

    def test_missing_package_or_disappearing_version_is_a_noop(self):
        api = FakeAPI()
        api.missing_package = True
        self.assertEqual(self.run_cleanup(api), [])
        api = FakeAPI([version(1, [])])
        api.before_get = lambda api, identifier: api.versions.pop(identifier, None)
        self.assertEqual(self.run_cleanup(api), [])

    def test_rechecks_tags_and_pr_state_before_deleting(self):
        for change in ('protected tag', 'reopen'):
            with self.subTest(change=change):
                api = FakeAPI([version(1, ['pr-42-8.4-fpm-alpine-amd64'])], states={42: 'closed'})

                def update(api, identifier):
                    if change == 'protected tag':
                        api.versions[identifier]['metadata']['container']['tags'].append('main-8.4-fpm-alpine-amd64')
                    else:
                        api.states[42] = 'open'

                api.before_get = update
                self.assertEqual(self.run_cleanup(api), [])

    def test_dry_run_does_not_delete_versions(self):
        api = FakeAPI([version(1, [])])
        self.assertEqual(self.run_cleanup(api, dry_run=True), [1])
        self.assertIn(1, api.versions)
        self.assertFalse(any(method == 'DELETE' for method, _ in api.calls))

    def test_organization_packages_use_organization_endpoint(self):
        api = FakeAPI(owner_type='Organization')
        self.assertEqual(self.run_cleanup(api), [])
        self.assertIn(('GET', '/orgs/marlub/packages/container/php-buildcache/versions?per_page=100&page=1'), api.calls)

    def test_api_failures_are_reported(self):
        api = FakeAPI([version(1, [])])
        api.delete_error = True
        with self.assertRaises(CleanupError):
            self.run_cleanup(api)

    def test_missing_metadata_fails_instead_of_deleting(self):
        api = FakeAPI([{'id': 1}])
        with self.assertRaises(KeyError):
            self.run_cleanup(api)
        self.assertFalse(any(method == 'DELETE' for method, _ in api.calls))


class GitHubAPITests(unittest.TestCase):
    def test_only_explicitly_allowed_404s_are_ignored(self):
        for method in ('GET', 'DELETE'):
            for status in (401, 403, 404, 429, 500):
                for missing_ok in (True, False):
                    with self.subTest(method=method, status=status, missing_ok=missing_ok):
                        error = HTTPError('https://api.github.com/test', status, 'failed', {}, None)
                        with patch('github_api.urlopen', side_effect=error):
                            api = GitHubAPI('test-token')
                            if status == 404 and missing_ok:
                                self.assertIsNone(api.request(method, '/test', missing_ok=missing_ok))
                            else:
                                with self.assertRaisesRegex(GitHubAPIError, f'HTTP {status}'):
                                    api.request(method, '/test', missing_ok=missing_ok)


if __name__ == '__main__':
    unittest.main()
