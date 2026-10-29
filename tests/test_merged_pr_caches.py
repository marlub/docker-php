import copy
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_images import RepositoryTests
from find_merged_pr_caches import commit_scope, find_merged_prs, main
from github_api import GitHubAPIError


REPOSITORY = 'marlub/docker-php'
FIRST = 'a' * 40
SECOND = 'b' * 40


def pull(number=42, sha=FIRST):
    return {'number': number, 'state': 'closed', 'merged_at': '2026-10-29T12:00:00Z',
            'merge_commit_sha': sha, 'base': {'ref': 'main', 'repo': {'full_name': REPOSITORY}},
            'head': {'repo': {'full_name': REPOSITORY}}, 'user': {'login': 'renovate[bot]'}}


class FakeAPI:
    def __init__(self, associated=None, details=None):
        self.associated = associated or {}
        self.details = details or {}
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        parsed = urlsplit(path)
        if '/commits/' in path:
            sha = parsed.path.split('/commits/', 1)[1].split('/')[0]
            batch = self.associated.get(sha, [])
            if not isinstance(batch, list):
                return copy.deepcopy(batch)
            page = int(parse_qs(parsed.query)['page'][0])
            return copy.deepcopy(batch[(page - 1) * 100:page * 100])
        return copy.deepcopy(self.details[int(parsed.path.rsplit('/', 1)[1])])


class CommitScopeTests(RepositoryTests):
    def add_commit(self, name):
        (self.root / name).write_text(name)
        return self.commit()

    def test_merge_commit_scope_excludes_side_branch_commits(self):
        self.git('checkout', '-qb', 'feature')
        side = self.add_commit('feature.txt')
        self.git('checkout', 'main')
        direct = self.add_commit('direct.txt')
        self.git('merge', '--no-ff', '-qm', 'merge PR', 'feature')
        merged = self.git('rev-parse', 'HEAD')
        source = self.add_commit('after.txt')
        self.assertEqual(commit_scope(self.root, source, self.base), [source, merged, direct])
        self.assertNotIn(side, commit_scope(self.root, source, self.base))

    def test_squash_merge_scope_uses_main_squash_sha(self):
        self.git('checkout', '-qb', 'feature')
        side = self.add_commit('feature.txt')
        self.add_commit('feature-two.txt')
        self.git('checkout', 'main')
        self.git('merge', '--squash', 'feature')
        source = self.commit()
        self.assertEqual(commit_scope(self.root, source, self.base), [source])
        self.assertNotEqual(source, side)

    def test_rebase_merge_scope_uses_rebased_main_commits(self):
        self.git('checkout', '-qb', 'feature')
        original = self.add_commit('feature.txt')
        self.add_commit('feature-two.txt')
        self.git('checkout', 'main')
        before = self.add_commit('main.txt')
        self.git('checkout', 'feature')
        self.git('rebase', 'main')
        source = self.git('rev-parse', 'HEAD')
        previous = self.git('rev-parse', 'HEAD^')
        self.git('checkout', 'main')
        self.git('merge', '--ff-only', 'feature')
        self.assertEqual(commit_scope(self.root, source, before), [source, previous])
        self.assertNotIn(original, commit_scope(self.root, source, before))

    def test_manual_initial_and_nonancestor_push_inspect_only_pinned_source(self):
        source = self.add_commit('source.txt')
        self.git('checkout', '-qb', 'other', self.base)
        other = self.add_commit('other.txt')
        self.git('checkout', 'main')
        self.add_commit('newer.txt')
        for base in (None, '0' * 40, other):
            with self.subTest(base=base):
                self.assertEqual(commit_scope(self.root, source, base), [source])

    def test_empty_push_and_history_errors(self):
        self.assertEqual(commit_scope(self.root, self.base, self.base), [])
        with self.assertRaises(ValueError):
            commit_scope(self.root, 'main')
        with self.assertRaises(ValueError):
            commit_scope(self.root, self.base, '--all')
        with self.assertRaises(subprocess.CalledProcessError):
            commit_scope(self.root, FIRST)
        with self.assertRaises(subprocess.CalledProcessError):
            commit_scope(self.root, self.base, FIRST)


class MergedPRDiscoveryTests(unittest.TestCase):
    def test_multiple_prs_deduplication_and_pagination(self):
        api = FakeAPI({FIRST: [{'number': 42}] * 101, SECOND: [{'number': 42}, {'number': 43}]},
                      {42: pull(), 43: pull(43, SECOND)})
        self.assertEqual(find_merged_prs(api, REPOSITORY, [FIRST, SECOND]), [42, 43])
        self.assertEqual(api.calls.count(('GET', f'/repos/{REPOSITORY}/pulls/42')), 1)
        self.assertIn(('GET', f'/repos/{REPOSITORY}/commits/{FIRST}/pulls?per_page=100&page=2'), api.calls)

    def test_ineligible_prs_are_excluded(self):
        cases = [
            lambda value: value.update(state='open', merged_at=None),
            lambda value: value.update(merged_at=None),
            lambda value: value['base'].update(ref='release'),
            lambda value: value['base']['repo'].update(full_name='other/repo'),
            lambda value: value['head']['repo'].update(full_name='fork/docker-php'),
            lambda value: value['head'].update(repo=None),
            lambda value: value['user'].update(login='dependabot[bot]'),
            lambda value: value.update(merge_commit_sha=SECOND),
        ]
        for index, modify in enumerate(cases):
            with self.subTest(case=index):
                detail = pull()
                modify(detail)
                self.assertEqual(find_merged_prs(FakeAPI({FIRST: [{'number': 42}]}, {42: detail}),
                                                REPOSITORY, [FIRST]), [])

    def test_repository_names_are_case_insensitive(self):
        detail = pull()
        detail['head']['repo']['full_name'] = REPOSITORY.upper()
        detail['base']['repo']['full_name'] = REPOSITORY.upper()
        self.assertEqual(find_merged_prs(FakeAPI({FIRST: [{'number': 42}]}, {42: detail}),
                                        REPOSITORY, [FIRST]), [42])

    def test_direct_commit_and_empty_scope_find_no_prs(self):
        api = FakeAPI()
        self.assertEqual(find_merged_prs(api, REPOSITORY, [FIRST]), [])
        api.calls.clear()
        self.assertEqual(find_merged_prs(api, REPOSITORY, []), [])
        self.assertEqual(api.calls, [])

    def test_malformed_associations_and_pr_details_fail_validation(self):
        for batch in ({'message': 'unexpected'}, [None], [{}], [{'number': True}],
                      [{'number': 0}], [{'number': '42'}]):
            with self.subTest(batch=batch):
                with self.assertRaises((ValueError, KeyError)):
                    find_merged_prs(FakeAPI({FIRST: batch}), REPOSITORY, [FIRST])
        for modify in (lambda value: value.update(number=43),
                       lambda value: value.update(merged_at='invalid'),
                       lambda value: value.update(merged_at='2026-10-29T12:00:00'),
                       lambda value: value.update(merge_commit_sha='main'),
                       lambda value: value.update(state='unknown'),
                       lambda value: value.update(head=[])):
            detail = pull()
            modify(detail)
            with self.subTest(detail=detail):
                with self.assertRaises((ValueError, KeyError)):
                    find_merged_prs(FakeAPI({FIRST: [{'number': 42}]}, {42: detail}), REPOSITORY, [FIRST])

    def run_cli(self, *, error=None):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            summary = Path(temporary) / 'summary'
            api = FakeAPI({FIRST: [{'number': 42}]}, {42: pull()})
            with patch.dict(os.environ, {'GH_TOKEN': 'test-token'}), \
                    patch('find_merged_pr_caches.commit_scope', return_value=[FIRST]), \
                    patch('find_merged_pr_caches.GitHubAPI', return_value=api), \
                    patch('find_merged_pr_caches.find_merged_prs', side_effect=error,
                          wraps=None if error else find_merged_prs), \
                    patch('sys.stdout', new=io.StringIO()) as stdout:
                result = main(['--repository', REPOSITORY, '--source-ref', FIRST,
                               '--github-output', str(output), '--summary', str(summary)])
            return result, output.read_text(), summary.read_text(), stdout.getvalue()

    def test_cli_emits_json_output_and_summary(self):
        result, output, summary, stdout = self.run_cli()
        self.assertEqual(result, 0)
        self.assertEqual(output, 'merged-prs=[42]\n')
        self.assertIn('#42', summary)
        self.assertNotIn('::warning::', stdout)

    def test_lookup_errors_warn_and_emit_empty_output_without_failing(self):
        errors = [GitHubAPIError(f'HTTP {status}') for status in (403, 404, 429, 500)]
        errors += [ValueError('invalid response'), KeyError('missing field'), OSError('network failure'),
                   subprocess.CalledProcessError(128, ['git'])]
        for error in errors:
            with self.subTest(error=error):
                result, output, summary, stdout = self.run_cli(error=error)
                self.assertEqual(result, 0)
                self.assertEqual(output, 'merged-prs=[]\n')
                self.assertIn('main cache only', summary)
                self.assertIn('::warning::', stdout)

    def test_cli_history_failures_also_emit_empty_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            with patch('sys.stdout', new=io.StringIO()) as stdout:
                result = main(['--repository', REPOSITORY, '--source-ref', 'invalid', '--github-output', str(output)])
            self.assertEqual(result, 0)
            self.assertEqual(output.read_text(), 'merged-prs=[]\n')
            self.assertIn('::warning::', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
