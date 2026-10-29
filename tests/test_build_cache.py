import os
import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parent.parent
CACHE = 'ghcr.io/marlub/php-buildcache'


class CacheRoutingTests(unittest.TestCase):
    def configure(self, **overrides):
        workflow = (ROOT / '.github/workflows/template-images.yml').read_text()
        step = workflow.split('      - name: Configure build cache\n', 1)[1].split('\n      - ', 1)[0]
        script = textwrap.dedent(step.split('        run: |\n', 1)[1])
        env = dict(os.environ, CACHE_IMAGE=CACHE, TAG_MINOR='8.4-fpm-alpine', ARCH='amd64',
                   PUBLISH='false', EVENT_NAME='pull_request', REF='refs/pull/42/merge',
                   ACTOR='renovate[bot]', REPOSITORY='marlub/docker-php',
                   PR_HEAD_REPOSITORY='marlub/docker-php', PR_NUMBER='42', MERGED_PRS='[]')
        env.update(overrides)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                                    env=dict(env, GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(Path(temporary) / 'summary')),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            header, settings = output.read_text().split('set<<CACHE_SETTINGS\n', 1)
        return dict(line.split('=', 1) for line in header.splitlines()), settings.removesuffix('CACHE_SETTINGS\n').splitlines()

    def test_same_repository_pr_reads_main_and_own_cache_and_writes_only_own(self):
        output, settings = self.configure()
        self.assertEqual(output, {'registry': 'ghcr.io', 'write': 'true'})
        self.assertEqual(settings, [
            f'*.cache-from=type=registry,ref={CACHE}:main-8.4-fpm-alpine-amd64',
            f'*.cache-from+=type=registry,ref={CACHE}:pr-42-8.4-fpm-alpine-amd64',
            f'*.cache-to=type=registry,ref={CACHE}:pr-42-8.4-fpm-alpine-amd64,mode=max,ignore-error=true',
        ])

    def test_main_push_and_manual_publication_without_discovered_prs_use_only_main(self):
        for event in ('push', 'workflow_dispatch'):
            with self.subTest(event=event):
                output, settings = self.configure(PUBLISH='true', EVENT_NAME=event, REF='refs/heads/main')
                self.assertEqual(output['write'], 'true')
                self.assertEqual(settings, [
                    f'*.cache-from=type=registry,ref={CACHE}:main-8.4-fpm-alpine-amd64',
                    f'*.cache-to=type=registry,ref={CACHE}:main-8.4-fpm-alpine-amd64,mode=max,ignore-error=true',
                ])

    def test_main_imports_discovered_pr_caches_and_exports_only_main(self):
        for arch in ('amd64', 'arm64'):
            for event in ('push', 'workflow_dispatch'):
                with self.subTest(arch=arch, event=event):
                    output, settings = self.configure(PUBLISH='true', EVENT_NAME=event, REF='refs/heads/main',
                                                      ARCH=arch, MERGED_PRS='[43,42,43]')
                    self.assertEqual(output['write'], 'true')
                    self.assertEqual(settings, [
                        f'*.cache-from=type=registry,ref={CACHE}:main-8.4-fpm-alpine-{arch}',
                        f'*.cache-from+=type=registry,ref={CACHE}:pr-42-8.4-fpm-alpine-{arch}',
                        f'*.cache-from+=type=registry,ref={CACHE}:pr-43-8.4-fpm-alpine-{arch}',
                        f'*.cache-to=type=registry,ref={CACHE}:main-8.4-fpm-alpine-{arch},mode=max,ignore-error=true',
                    ])

    def test_pr_runs_ignore_discovered_merged_prs(self):
        _, settings = self.configure(MERGED_PRS='[99]')
        self.assertFalse(any(':pr-99-' in setting for setting in settings))
        self.assertIn(':pr-42-', settings[-1])

    def test_invalid_discovered_prs_fall_back_to_main_cache(self):
        for value in ('', 'invalid', '{}', 'null', '["42"]', '[true]', '[0]', '[-1]', '[1.5]', '[1e99]', '[42]\n[43]'):
            with self.subTest(value=value):
                _, settings = self.configure(PUBLISH='true', EVENT_NAME='push', REF='refs/heads/main', MERGED_PRS=value)
                self.assertEqual(len(settings), 2)
                self.assertTrue(all(':main-' in setting for setting in settings))

    def test_forks_dependabot_and_other_events_only_read_main(self):
        cases = [
            {'PR_HEAD_REPOSITORY': 'contributor/docker-php'},
            {'ACTOR': 'dependabot[bot]'},
            {'ACTOR': 'dependabot[bot]', 'PUBLISH': 'true', 'EVENT_NAME': 'push', 'REF': 'refs/heads/main'},
            {'EVENT_NAME': 'pull_request_target'},
            {'PUBLISH': 'true', 'EVENT_NAME': 'workflow_dispatch', 'REF': 'refs/heads/feature'},
            {'PUBLISH': 'true'},
            {'EVENT_NAME': 'push', 'REF': 'refs/heads/main'},
        ]
        for case in cases:
            with self.subTest(case=case):
                output, settings = self.configure(MERGED_PRS='[99]', **case)
                self.assertEqual(output['write'], 'false')
                self.assertEqual(settings, [f'*.cache-from=type=registry,ref={CACHE}:main-8.4-fpm-alpine-amd64'])

    def test_every_variant_architecture_and_pr_has_a_distinct_write_reference(self):
        refs = set()
        for minor in ('8.2', '8.3', '8.4', '8.5'):
            for node in ('', '-node24'):
                for arch in ('amd64', 'arm64'):
                    for pr in ('41', '42'):
                        _, settings = self.configure(TAG_MINOR=f'{minor}-fpm-alpine{node}', ARCH=arch, PR_NUMBER=pr)
                        refs.add(settings[-1])
        self.assertEqual(len(refs), 32)

    def test_cache_address_is_independent_of_published_image(self):
        output, settings = self.configure(CACHE_IMAGE='example.test/team/cache')
        self.assertEqual(output['registry'], 'example.test')
        self.assertTrue(all('example.test/team/cache:' in line for line in settings))

    def test_bake_resolves_registry_settings_with_both_image_outputs_and_no_cache(self):
        for arch in ('amd64', 'arm64'):
            for publish in (False, True):
                with self.subTest(arch=arch, publish=publish):
                    event = {'PUBLISH': 'true', 'EVENT_NAME': 'push', 'REF': 'refs/heads/main', 'MERGED_PRS': '[42,43]'} if publish else {}
                    _, settings = self.configure(ARCH=arch, **event)
                    command = ['docker', 'buildx', 'bake', '-f', 'docker-bake.hcl', '-f', 'build/php/8.4.hcl',
                               '--print', '--no-cache']
                    settings += [f'*.platform=linux/{arch}', '*.tags=test', '*.output=type=docker']
                    if publish:
                        settings.append('*.output+=type=oci,dest=/tmp/cache-routing-test.tar')
                    for setting in settings:
                        command += ['--set', setting]
                    result = subprocess.run(command + ['php-8-4'], cwd=ROOT, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    target = json.loads(result.stdout)['target']['php-8-4']
                    scope = 'main' if publish else 'pr-42'
                    self.assertEqual(target['platforms'], [f'linux/{arch}'])
                    self.assertTrue(target['no-cache'])
                    self.assertEqual(len(target['cache-from']), 3 if publish else 2)
                    self.assertEqual(target['cache-to'], [{
                        'type': 'registry', 'ref': f'{CACHE}:{scope}-8.4-fpm-alpine-{arch}',
                        'mode': 'max', 'ignore-error': 'true',
                    }])
                    self.assertEqual([output['type'] for output in target['output']],
                                     ['docker', 'oci'] if publish else ['docker'])


if __name__ == '__main__':
    unittest.main()
