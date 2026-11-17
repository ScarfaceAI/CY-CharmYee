import json
import os
import subprocess
import sys
from unittest.mock import patch

from support import WorkspaceCase, SKILL_ROOT, read_json
from douyin_text import onboarding, setup, credentials, asr


class OnboardingTests(WorkspaceCase):
    def diagnostics(self, key=False, browser=True):
        return {'checks': {'ffmpeg': True, 'ffprobe': True, 'playwright': browser, 'chromium': browser},
                'doubao_credentials_present': key, 'vault_root': None}

    def test_welcome_is_read_only_and_no_key_does_not_block_unpaid_features(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        with patch.object(credentials, 'load', side_effect=AssertionError('do not decrypt keys')), \
             patch.object(asr, 'recognize', side_effect=AssertionError('do not call speech')):
            shown = onboarding.welcome(self.diagnostics(), self.root)
        self.assertTrue(shown['onboarding']['due'])
        self.assertEqual('available_to_agent', shown['readiness']['existing_text'])
        self.assertEqual('ready', shown['readiness']['download_environment'])
        self.assertEqual('needs_speech_credentials', shown['readiness']['new_speech'])
        self.assertEqual('choose_when_requested', shown['readiness']['knowledge_destination'])
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))

    def test_presentation_record_survives_reconfigure_without_becoming_user_acceptance(self):
        onboarding.acknowledge(self.root)
        saved = (self.root / '.local/onboarding.json').read_bytes()
        configured = setup.initialize(self.root)
        self.assertFalse(configured['onboarding']['due'])
        self.assertEqual(saved, (self.root / '.local/onboarding.json').read_bytes())
        self.assertEqual('presentation_record_only_not_user_comprehension', configured['onboarding']['meaning'])

    def test_new_guide_revision_requires_presentation_again_but_keeps_prior_record(self):
        onboarding.acknowledge(self.root)
        data = onboarding.catalog()
        data['revision'] += 1
        with patch.object(onboarding, 'catalog', return_value=data):
            self.assertTrue(onboarding.status(self.root)['due'])
        self.assertFalse(onboarding.status(self.root)['due'])

    def test_local_speech_readiness_does_not_require_browser_or_claim_cloud_verified(self):
        shown = onboarding.welcome(self.diagnostics(key=True, browser=False), self.root, language='en')
        self.assertEqual('ready_for_authorized_trial', shown['readiness']['new_speech'])
        self.assertEqual('needs_dependencies', shown['readiness']['download_environment'])
        self.assertEqual('not_tested', shown['readiness']['cloud_access'])
        self.assertEqual('en', shown['language'])

    def test_cli_first_use_without_config_or_credentials(self):
        isolated = self.parent / 'new-user'
        env = dict(os.environ, CY_UPDATE_CHECK='0')
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SKILL_ROOT / 'scripts/brain.py'),
            '--workspace', str(isolated), 'welcome'], capture_output=True, env=env, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', 'replace'))
        shown = json.loads(result.stdout.decode('utf-8'))
        self.assertTrue(shown['onboarding']['due'])
        self.assertFalse(shown['cloud_asr_called'])
        self.assertFalse(isolated.exists())

    def test_bilingual_catalog_and_welcome_include_search_and_decisions(self):
        catalog = onboarding.catalog()
        expected_ids = {'transcribe', 'analyze', 'download', 'rewrite', 'export',
                        'knowledge', 'personal-vault', 'search', 'decisions', 'resume', 'setup'}
        entries = catalog['capabilities']
        ids = [entry['id'] for entry in entries]
        self.assertEqual(expected_ids, set(ids))
        self.assertEqual(len(expected_ids), len(ids), '能力 ID 不能重复')
        for entry in entries:
            self.assertEqual({'id', 'zh', 'en'}, set(entry))
            for language in ('zh', 'en'):
                self.assertEqual({'title', 'example', 'outcome'}, set(entry[language]))
                for value in entry[language].values():
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.strip(), f'{entry["id"]} 的 {language} 内容不能为空')

        new_titles = {
            'zh': {'search': '全文搜索', 'decisions': '决策记录与复盘'},
            'en': {'search': 'Full-text search', 'decisions': 'Decision records and reviews'},
        }
        isolated = self.parent / 'catalog-user'
        for language in ('zh', 'en'):
            with self.subTest(language=language):
                result = subprocess.run([
                    sys.executable, '-B', '-X', 'utf8', str(SKILL_ROOT / 'scripts/brain.py'),
                    '--workspace', str(isolated), 'welcome', '--lang', language,
                ], capture_output=True, env=dict(os.environ, CY_UPDATE_CHECK='0'), timeout=20)
                self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', 'replace'))
                shown = json.loads(result.stdout.decode('utf-8'))
                self.assertEqual(language, shown['language'])
                self.assertEqual(ids, [entry['id'] for entry in shown['capabilities']])
                self.assertIn(catalog['intro'][language], shown['message'])
                for entry in entries:
                    self.assertIn({'id': entry['id'], **entry[language]}, shown['capabilities'])
                    for value in entry[language].values():
                        self.assertIn(value, shown['message'])
                titles = {entry['id']: entry['title'] for entry in shown['capabilities']}
                for identifier, title in new_titles[language].items():
                    self.assertEqual(title, titles[identifier])
                self.assertFalse(shown['cloud_asr_called'])
                self.assertFalse(isolated.exists())
