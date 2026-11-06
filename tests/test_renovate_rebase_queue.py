import copy
import ast
from datetime import datetime, timezone
from fnmatch import fnmatchcase
import io
import json
from pathlib import Path
import sys
import re
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from github_api import GitHubAPI, GitHubAPIError
from renovate_rebase_queue import MARKER, QueueError, RebaseQueue, main, validate_state


ROOT = Path(__file__).resolve().parent.parent
REPO = 'marlub/docker-php'
BASE = 'a' * 40
NEXT_BASE = 'b' * 40
NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


def pull(number):
    return {
        'number': number, 'state': 'open', 'draft': False,
        'user': {'login': 'renovate[bot]'}, 'auto_merge': {'merge_method': 'squash'},
        'head': {'sha': f'{number:040x}', 'ref': f'renovate/update-{number}', 'repo': {'full_name': REPO}},
        'base': {'sha': BASE, 'ref': 'main', 'repo': {'full_name': REPO}},
        'created_at': f'2026-11-{number % 5 + 1:02d}T00:00:00Z', 'labels': [],
        'mergeable': True, 'mergeable_state': 'behind', 'merged_at': None,
    }


def run(pull, *, identifier=100, workflow=1, status='completed', conclusion='failure', attempt=1):
    return {
        'id': identifier, 'workflow_id': workflow, 'head_sha': pull['head']['sha'],
        'head_branch': pull['head']['ref'], 'path': '.github/workflows/build-test.yml',
        'status': status, 'conclusion': conclusion, 'run_attempt': attempt,
    }


class FakeAPI:
    def __init__(self, pulls=None):
        self.pulls = {p['number']: copy.deepcopy(p) for p in (pulls or [pull(1), pull(2)])}
        self.base = BASE
        self.comments = []
        self.runs = []
        self.labels = {'rebase'}
        self.calls = []
        self.fail_before = None
        self.fail_after = None
        self.hook = None

    def state(self):
        return json.loads(self.comments[-1]['body'].split('```json\n')[1].split('\n```')[0])

    def request(self, method, path, *, missing_ok=False, body=None):
        call = (method, path, copy.deepcopy(body))
        self.calls.append(call)
        if self.hook:
            self.hook(self, method, path, body)
        if self.fail_before and self.fail_before(*call):
            raise GitHubAPIError('injected failure before mutation')
        result = self.respond(method, path, missing_ok, body)
        if self.fail_after and self.fail_after(*call):
            raise GitHubAPIError('injected lost response after mutation')
        return copy.deepcopy(result)

    def respond(self, method, path, missing_ok, body):
        parsed = urlsplit(path)
        route = parsed.path.removeprefix(f'/repos/{REPO}')
        query = parse_qs(parsed.query)
        page = int(query.get('page', ['1'])[0])

        def batch(values):
            return values[(page - 1) * 100:page * 100]

        if method == 'GET':
            if route == '/issues':
                return [{'number': 23, 'title': 'Dependency Dashboard', 'user': {'login': 'renovate[bot]'}}]
            if route == '/issues/23/comments':
                return batch(self.comments)
            if route == '/pulls':
                return batch([p for p in self.pulls.values() if p['state'] == 'open'])
            if route.startswith('/pulls/'):
                return self.pulls[int(route.rsplit('/', 1)[1])]
            if route == '/git/ref/heads/main':
                return {'object': {'sha': self.base}}
            if route == '/actions/runs':
                return {'workflow_runs': batch([r for r in self.runs if r['head_sha'] == query['head_sha'][0]])}
            if route == '/labels/rebase':
                return {'name': 'rebase'} if 'rebase' in self.labels else None
        if method == 'POST' and route == '/issues/23/comments':
            comment = {'id': len(self.comments) + 1, 'user': {'login': 'github-actions[bot]'}, 'body': body['body']}
            self.comments.append(comment)
            return comment
        if method == 'PATCH' and route.startswith('/issues/comments/'):
            comment = next(c for c in self.comments if c['id'] == int(route.rsplit('/', 1)[1]))
            comment['body'] = body['body']
            return comment
        if method == 'POST' and route == '/labels':
            self.labels.add(body['name'])
            return body
        if method == 'POST' and route.endswith('/labels'):
            p = self.pulls[int(route.split('/')[2])]
            p['labels'] = [{'name': name} for name in set(body['labels']) | {l['name'] for l in p['labels']}]
            return p['labels']
        if method == 'DELETE' and route.endswith('/labels/rebase'):
            p = self.pulls[int(route.split('/')[2])]
            p['labels'] = [label for label in p['labels'] if label['name'] != 'rebase']
            return None
        raise AssertionError((method, path, body))

    def requested(self):
        return [int(path.split('/issues/')[1].split('/')[0]) for method, path, _ in self.calls
                if method == 'POST' and path.endswith('/labels') and '/issues/' in path]

    def refresh(self, number, *, status='in_progress', conclusion=None):
        p = self.pulls[number]
        p['head']['sha'] = f'{number + 1000:040x}'
        p['mergeable_state'] = 'clean'
        p['labels'] = []  # Mend consumes rebase before CI completes.
        self.runs.append(run(p, status=status, conclusion=conclusion))


class RebaseQueueTests(unittest.TestCase):
    def reconcile(self, api):
        queue = RebaseQueue(api, REPO, now=NOW)
        queue.reconcile()
        return queue

    def test_reserves_before_label_and_reuses_one_comment(self):
        api = FakeAPI()

        def assert_reserved(api, method, path, body):
            if method == 'POST' and path.endswith('/issues/1/labels'):
                self.assertEqual(api.state()['active']['number'], 1)
                self.assertEqual(api.state()['active']['phase'], 'prepared')

        api.hook = assert_reserved
        self.reconcile(api)
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])
        self.assertEqual(len(api.comments), 1)
        self.assertEqual(api.state()['active']['phase'], 'requested')

    def test_label_removal_does_not_release_slot_and_old_failures_are_ignored(self):
        api = FakeAPI()
        api.runs = [run(api.pulls[1])]
        self.reconcile(api)
        api.pulls[1]['labels'] = []
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])
        self.assertEqual(api.state()['active']['number'], 1)

    def test_current_ci_failure_hands_off_even_with_other_workflow_running(self):
        api = FakeAPI()
        self.reconcile(api)
        api.refresh(1, status='completed', conclusion='failure')
        other = run(api.pulls[1], workflow=2, status='in_progress', conclusion=None)
        other['path'] = 'dynamic/github-code-scanning/codeql'
        api.runs.append(other)
        self.reconcile(api)
        self.assertEqual(api.requested(), [1, 2])
        self.assertEqual(api.state()['attempts']['1']['result'], 'failed')
        self.assertEqual(api.state()['active']['number'], 2)

    def test_finished_success_holds_until_merge_then_hands_off(self):
        api = FakeAPI()
        self.reconcile(api)
        api.refresh(1, status='completed', conclusion='success')
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])
        api.pulls[1].update(state='closed', merged_at='2026-11-06T12:10:00Z')
        self.reconcile(api)
        self.assertEqual(api.requested(), [1, 2])
        self.assertEqual(api.state()['attempts']['1']['result'], 'merged')

    def test_missing_ci_and_in_progress_failures_hold_slot(self):
        api = FakeAPI()
        self.reconcile(api)
        api.refresh(1)
        api.runs = []
        self.reconcile(api)
        api.runs = [run(api.pulls[1], status='in_progress', conclusion='failure')]
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])

    def test_failed_pr_retries_only_after_main_changes(self):
        api = FakeAPI([pull(1)])
        self.reconcile(api)
        api.refresh(1, status='completed', conclusion='failure')
        self.reconcile(api)
        api.pulls[1]['mergeable_state'] = 'behind'
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])
        api.base = NEXT_BASE
        self.reconcile(api)
        self.assertEqual(api.requested(), [1, 1])
        self.assertEqual(api.state()['active']['requested_base'], NEXT_BASE)

    def test_main_change_while_checking_waits_then_refreshes_same_pr(self):
        api = FakeAPI()
        self.reconcile(api)
        api.refresh(1)
        api.base = NEXT_BASE
        api.pulls[1]['mergeable_state'] = 'behind'
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])
        api.runs[0].update(status='completed', conclusion='success')
        self.reconcile(api)
        self.assertEqual(api.requested(), [1, 1])
        self.assertEqual(api.state()['active']['requested_base'], NEXT_BASE)

    def test_new_head_and_latest_run_ignore_stale_failures_and_rerun_attempts(self):
        api = FakeAPI()
        self.reconcile(api)
        api.refresh(1, status='completed', conclusion='failure')
        # The API exposes the latest attempt on the same run ID during a manual rerun.
        api.runs[0].update(status='in_progress', conclusion=None, run_attempt=2)
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])
        api.runs[0].update(status='completed', conclusion='failure')
        api.runs.append(run(api.pulls[1], identifier=101, status='in_progress', conclusion=None, attempt=2))
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])
        # A newer head also makes the previous head's failed run irrelevant.
        api.pulls[1]['head']['sha'] = 'c' * 40
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])
        self.assertEqual(api.state()['active']['observed_head'], 'c' * 40)

    def test_unrelated_workflows_do_not_release_the_slot(self):
        api = FakeAPI()
        self.reconcile(api)
        api.refresh(1, status='completed', conclusion='failure')
        api.runs[0]['path'] = '.github/workflows/cache-cleanup.yml'
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])

    def test_default_codeql_pr_ref_failure_releases_the_slot(self):
        api = FakeAPI()
        self.reconcile(api)
        api.refresh(1)
        codeql = run(api.pulls[1], workflow=2)
        codeql.update(path='dynamic/github-code-scanning/codeql', head_branch='refs/pull/1/head')
        api.runs.append(codeql)
        self.reconcile(api)
        self.assertEqual(api.requested(), [1, 2])

    def test_other_pr_refs_and_image_workflows_with_pr_refs_are_ignored(self):
        for path, branch in (
            ('dynamic/github-code-scanning/codeql', 'refs/pull/2/head'),
            ('.github/workflows/build-test.yml', 'refs/pull/1/head'),
        ):
            with self.subTest(path=path, branch=branch):
                api = FakeAPI()
                self.reconcile(api)
                api.refresh(1, status='completed', conclusion='failure')
                api.runs[0].update(path=path, head_branch=branch)
                self.reconcile(api)
                self.assertEqual(api.requested(), [1])

    def test_excludes_ineligible_clean_conflicted_and_unknown_prs(self):
        changes = [
            lambda p: p.update(draft=True),
            lambda p: p.update(auto_merge=None),
            lambda p: p['user'].update(login='someone'),
            lambda p: p['head']['repo'].update(full_name='someone/fork'),
            lambda p: p['base'].update(ref='develop'),
            lambda p: p['head'].update(ref='feature/branch'),
            lambda p: p.update(mergeable=False, mergeable_state='dirty'),
            lambda p: p.update(mergeable=None, mergeable_state='unknown'),
            lambda p: p.update(mergeable_state='clean'),
        ]
        for change in changes:
            with self.subTest(change=change):
                p = pull(1)
                change(p)
                api = FakeAPI([p])
                self.reconcile(api)
                self.assertEqual(api.requested(), [])
                self.assertEqual(api.comments, [])

    def test_existing_manual_label_blocks_new_requests(self):
        api = FakeAPI()
        api.pulls[2]['labels'] = [{'name': 'rebase'}]
        queue = self.reconcile(api)
        self.assertEqual(api.requested(), [])
        self.assertIn('Paused', queue.messages[0])

    def test_new_manual_label_or_main_change_during_selection_defers(self):
        for change in ('label', 'base', 'eligibility'):
            with self.subTest(change=change):
                api = FakeAPI()

                def race(api, method, path, body):
                    if method == 'GET' and path.endswith('/pulls/1'):
                        if change == 'label':
                            api.pulls[2]['labels'] = [{'name': 'rebase'}]
                        elif change == 'base':
                            api.base = NEXT_BASE
                        else:
                            for p in api.pulls.values():
                                p['auto_merge'] = None

                api.hook = race
                self.reconcile(api)
                self.assertEqual(api.requested(), [])

    def test_selection_is_rechecked_after_reserving_before_label_mutation(self):
        for change in ('head', 'label', 'unknown'):
            with self.subTest(change=change):
                api = FakeAPI()

                def race(api, method, path, body):
                    if method == 'POST' and path.endswith('/comments'):
                        if change == 'head':
                            api.refresh(1)
                        elif change == 'label':
                            api.pulls[2]['labels'] = [{'name': 'rebase'}]
                        else:
                            api.pulls[1].update(mergeable=None, mergeable_state='unknown')

                api.hook = race
                self.reconcile(api)
                self.assertEqual(api.requested(), [])
                self.assertEqual(api.state()['active']['number'], 1)

    def test_closed_or_disabled_active_pr_releases_slot(self):
        for update in ({'state': 'closed'}, {'auto_merge': None}, {'draft': True}):
            with self.subTest(update=update):
                api = FakeAPI()
                self.reconcile(api)
                api.pulls[1].update(update)
                self.reconcile(api)
                self.assertEqual(api.requested(), [1, 2])
                self.assertEqual(api.pulls[1]['labels'], [])

    def test_pages_pulls_and_state_comments_before_selecting(self):
        prs = [pull(n) for n in range(1, 102)]
        for p in prs[:-1]:
            p['auto_merge'] = None
        api = FakeAPI(prs)
        api.comments = [{'id': n, 'body': 'ordinary comment', 'user': {'login': 'someone'}} for n in range(1, 101)]
        self.reconcile(api)
        self.reconcile(api)
        self.assertEqual(api.requested(), [101])
        self.assertEqual(len(api.comments), 101)
        self.assertTrue(any('pulls?state=open&per_page=100&page=2' in path for _, path, _ in api.calls))

    def test_least_recent_attempt_precedes_oldest_pr(self):
        api = FakeAPI()
        self.reconcile(api)
        api.refresh(1, status='completed', conclusion='failure')
        api.pulls[2]['auto_merge'] = None
        self.reconcile(api)
        api.base = NEXT_BASE
        api.pulls[1]['mergeable_state'] = 'behind'
        api.pulls[2]['auto_merge'] = {}
        self.reconcile(api)
        self.assertEqual(api.requested(), [1, 2])

    def test_failed_state_save_cannot_add_label(self):
        api = FakeAPI()
        api.fail_before = lambda method, path, body: method == 'POST' and path.endswith('/comments')
        with self.assertRaises(GitHubAPIError):
            self.reconcile(api)
        self.assertEqual(api.requested(), [])
        api.fail_before = None
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])

    def test_lost_reservation_response_recovers_without_duplicate_comment(self):
        api = FakeAPI()
        api.fail_after = lambda method, path, body: method == 'POST' and path.endswith('/comments')
        with self.assertRaises(GitHubAPIError):
            self.reconcile(api)
        api.fail_after = None
        self.reconcile(api)
        self.assertEqual(len(api.comments), 1)
        self.assertEqual(api.requested(), [1])

    def test_uncertain_label_write_recovers_same_request(self):
        for lost in (False, True):
            with self.subTest(lost=lost):
                api = FakeAPI()
                failure = lambda method, path, body: method == 'POST' and path.endswith('/issues/1/labels')
                if lost:
                    api.fail_after = failure
                else:
                    api.fail_before = failure
                with self.assertRaises(GitHubAPIError):
                    self.reconcile(api)
                self.assertEqual(api.state()['active']['number'], 1)
                api.fail_before = api.fail_after = None
                self.reconcile(api)
                self.assertEqual(api.state()['active']['number'], 1)
                self.assertNotIn(2, api.requested())

    def test_label_consumed_after_uncertain_write_does_not_request_again(self):
        api = FakeAPI()
        api.fail_after = lambda method, path, body: method == 'POST' and path.endswith('/issues/1/labels')
        with self.assertRaises(GitHubAPIError):
            self.reconcile(api)
        api.fail_after = None
        api.refresh(1)
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])

    def test_failure_cleanup_must_finish_before_next_request(self):
        api = FakeAPI()
        self.reconcile(api)
        api.refresh(1, status='completed', conclusion='failure')
        api.pulls[1]['labels'] = [{'name': 'rebase'}]
        api.fail_before = lambda method, path, body: method == 'DELETE'
        with self.assertRaises(GitHubAPIError):
            self.reconcile(api)
        self.assertEqual(api.state()['active']['phase'], 'releasing')
        self.assertEqual(api.requested(), [1])
        api.fail_before = None
        self.reconcile(api)
        self.assertEqual(api.requested(), [1, 2])
        self.assertEqual(api.pulls[1]['labels'], [])

    def test_missing_repository_label_is_created(self):
        api = FakeAPI()
        api.labels.clear()
        self.reconcile(api)
        self.assertIn('rebase', api.labels)
        self.assertEqual(api.requested(), [1])

    def test_malformed_or_duplicate_controller_state_stops_mutations(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                api = FakeAPI()
                self.reconcile(api)
                api.calls.clear()
                if duplicate:
                    api.comments.append(copy.deepcopy(api.comments[0]))
                else:
                    api.comments[0]['body'] = MARKER + '\n```json\n{}\n```'
                with self.assertRaises(QueueError):
                    self.reconcile(api)
                self.assertFalse(any(method != 'GET' for method, _, _ in api.calls))

    def test_only_controller_authored_state_is_trusted(self):
        api = FakeAPI()
        api.comments = [{'id': 1, 'user': {'login': 'someone'}, 'body': MARKER + '\ninvalid'}]
        self.reconcile(api)
        self.assertEqual(api.requested(), [1])
        self.assertEqual(len(api.comments), 2)

    def test_state_validation_rejects_invalid_sha_and_missing_history(self):
        api = FakeAPI()
        self.reconcile(api)
        for field, value in (('requested_head', 'invalid'), ('number', True)):
            state = api.state()
            state['active'][field] = value
            with self.assertRaises(QueueError):
                validate_state(state)
        state = api.state()
        state['attempts'] = {}
        with self.assertRaises(QueueError):
            validate_state(state)

    def test_main_reports_errors_without_token(self):
        with patch.dict('os.environ', {}, clear=True), patch('sys.stdout', new=io.StringIO()) as output:
            self.assertEqual(main(['--repository', REPO]), 1)
        self.assertIn('GH_TOKEN is required', output.getvalue())


class GitHubJSONRequestTests(unittest.TestCase):
    def test_request_encodes_json_and_keeps_get_compatible(self):
        captured = []

        def open_request(request, timeout):
            captured.append(request)
            response = io.BytesIO(b'{"id": 1}')
            return response

        with patch('github_api.urlopen', side_effect=open_request):
            api = GitHubAPI('token')
            self.assertEqual(api.request('POST', '/test', body={'labels': ['rebase']}), {'id': 1})
            api.request('GET', '/test')
        self.assertEqual(json.loads(captured[0].data), {'labels': ['rebase']})
        self.assertEqual(captured[0].get_header('Content-type'), 'application/json')
        self.assertIsNone(captured[1].data)


class QueueWorkflowTests(unittest.TestCase):
    def test_events_permissions_and_trusted_checkout(self):
        workflow = (ROOT / '.github/workflows/renovate-rebase-queue.yml').read_text()
        self.assertIn('  push:\n    branches: [main]', workflow)
        self.assertIn('  pull_request_target:', workflow)
        self.assertIn("    workflows: ['Test PHP images with extensions', 'CodeQL', 'PR #*']", workflow)
        self.assertIn("    branches: ['renovate/**', 'refs/pull/*/head']", workflow)
        self.assertIn('    types: [completed]', workflow)
        self.assertNotIn('  schedule:', workflow)
        self.assertIn('  group: renovate-rebase-queue\n  cancel-in-progress: false', workflow)
        self.assertNotIn('  queue: max', workflow)
        self.assertIn('  contents: read\n  actions: read\n  pull-requests: write\n  issues: write', workflow)
        self.assertIn('ref: ${{ github.event.repository.default_branch }}', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertNotIn('ref: ${{ github.event.pull_request.head', workflow)
        self.assertIn("github.event.pull_request.user.login == 'renovate[bot]'", workflow)
        self.assertIn("github.event.action != 'closed' || github.event.pull_request.merged != true", workflow)

    def test_completion_filters_exclude_queue_publication_cleanup_and_main(self):
        workflow = (ROOT / '.github/workflows/renovate-rebase-queue.yml').read_text()
        section = workflow.split('  workflow_run:', 1)[1].split('  workflow_dispatch:', 1)[0]
        names = ast.literal_eval(re.search(r'^    workflows: (.+)$', section, re.M)[1])
        branches = ast.literal_eval(re.search(r'^    branches: (.+)$', section, re.M)[1])
        for name, branch, expected in (
            ('Test PHP images with extensions', 'renovate/php82-php', True),
            ('PR #68', 'refs/pull/68/head', True),
            ('CodeQL', 'renovate/php82-php', True),
            ('Renovate rebase queue', 'main', False),
            ('Renovate rebase queue', 'renovate/php82-php', False),
            ('Build & publish PHP images with extensions', 'main', False),
            ('Clean up registry build caches', 'renovate/php82-php', False),
            ('Push on main', 'main', False),
            ('Test PHP images with extensions', 'feature/human-pr', False),
        ):
            with self.subTest(name=name, branch=branch):
                matches = any(fnmatchcase(name, pattern) for pattern in names) and any(
                    fnmatchcase(branch, pattern) for pattern in branches)
                self.assertEqual(matches, expected)

    def test_pr_triggers_exclude_metadata_and_ci_duplicate_events(self):
        workflow = (ROOT / '.github/workflows/renovate-rebase-queue.yml').read_text()
        section = workflow.split('  pull_request_target:', 1)[1].split('  workflow_run:', 1)[0]
        events = re.search(r'^    types: \[(.+)\]$', section, re.M)[1].split(', ')
        self.assertEqual(set(events), {'closed', 'ready_for_review', 'converted_to_draft',
                                       'auto_merge_enabled', 'auto_merge_disabled'})
        self.assertTrue(set(events).isdisjoint({'opened', 'reopened', 'edited', 'labeled', 'unlabeled', 'synchronize'}))


if __name__ == '__main__':
    unittest.main()
