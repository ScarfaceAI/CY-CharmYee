"""External profiles are read-only; every fixture is synthetic and temporary."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch
from urllib.parse import unquote

from support import SKILL_ROOT, WorkspaceCase
from douyin_text import personal_vault as kb, vault, vault_migration as migration
from douyin_text.common import digest, write_json


class ProfileReadonlyTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.vault = self.parent / 'vault'
        self.external = self.parent / '已有画像.md'
        self.external.write_bytes(('# 外部画像\r\n\r\n偏好：详细。\r\n'
            '<!-- cy-profile:name:start -->\r\n旧管理块保留。\r\n'
            '<!-- cy-profile:name:end -->\r\n').encode('utf-8-sig'))
        self.external_before = self.snapshot(self.external)

    def snapshot(self, path):
        return path.read_bytes(), path.stat().st_mtime_ns

    def enable(self, *, external=True):
        return kb.enable(self.root, vault=self.vault, create=True,
                         profile=self.external if external else None, evidence='引用已有画像，只读。')

    def answer(self, text='以后请简短回答。', **fields):
        return kb.answer({'source': 'user', 'answer': text,
                          'fields': fields or {'preferences': '简短'}}, self.root)['saving']

    def cli(self, *args):
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SKILL_ROOT / 'scripts/brain.py'),
            '--workspace', str(self.root), 'kb', *args], capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout.decode('utf-8', 'replace') +
                         result.stderr.decode('utf-8', 'replace'))
        return json.loads(result.stdout.decode('utf-8-sig'))

    def legacy_transaction(self, *, target='@profile'):
        """Reproduce an on-disk transaction prepared before the read-only rule."""
        value = kb.state(self.root)
        folder = kb.home(self.root) / 'transactions/legacy-profile'
        folder.mkdir(parents=True, exist_ok=True)
        snapshot = folder / '0.payload'
        snapshot.write_text('旧版准备写入的画像正文。', encoding='utf-8')
        destination = self.external if target == '@profile' else self.vault / target
        row = {'source': snapshot.relative_to(self.root).as_posix(),
               'snapshot': snapshot.relative_to(self.root).as_posix(), 'target': target,
               'role': 'profile', 'sha256': digest(snapshot),
               'expected_sha256': kb.file_hash(destination), 'sources': ['user:legacy']}
        record = {'id': folder.name, 'task': 'old-profile', 'module': 'profile', 'profile': True,
                  'signature': kb.fingerprint([('profile', row['sha256'])]), 'vault_root': str(self.vault),
                  'policy_id': value['id'], 'status': 'pending', 'files': [row], 'created_ns': 1}
        write_json(folder / 'transaction.json', record)
        return record

    def test_answer_supplement_has_original_date_domain_and_does_not_create_basic_profile(self):
        self.enable()
        saved = self.answer()
        self.assertEqual('saved', saved['status'], saved)
        supplement = self.vault / kb.PROFILE_SUPPLEMENT
        text = supplement.read_text(encoding='utf-8')
        self.assertIn('<!-- cy-profile:preferences:start -->', text)
        self.assertIn('> 以后请简短回答。', text)
        self.assertIn('所属方向：生活。', text)
        self.assertRegex(text, r'记录于 \d{4}-\d{2}-\d{2}')
        for link in re.findall(r'\[用户回答\]\(([^)]+)\)', text):
            source = (supplement.parent / unquote(link)).resolve()
            self.assertTrue(source.is_relative_to(self.vault))
            self.assertIn('以后请简短回答。', source.read_text(encoding='utf-8'))
        self.assertFalse((self.vault / kb.PROFILE).exists())
        self.assertFalse((self.vault / kb.LEGACY_PROFILE).exists())
        self.assertEqual(self.external_before, self.snapshot(self.external))
        again = self.answer()
        self.assertEqual(saved['id'], again['id'])
        self.assertTrue(again['reused'])
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_registering_external_freezes_existing_basic_and_legacy_pending_transaction(self):
        self.enable(external=False)
        self.assertEqual('saved', self.answer('我是编辑。', background='编辑')['status'])
        basic = self.vault / kb.PROFILE
        before = self.snapshot(basic)
        legacy = self.legacy_transaction(target=kb.PROFILE)
        kb.configure_profile(self.root, external=self.external, evidence='已有画像在这里，只读引用。')
        saved = self.answer('现在想练习写作。', goal='练习写作')
        self.assertEqual('saved', saved['status'])
        self.assertEqual(before, self.snapshot(basic))
        text = (self.vault / kb.PROFILE_SUPPLEMENT).read_text(encoding='utf-8')
        self.assertNotIn('编辑', text)
        self.assertIn('练习写作', text)
        blocked = kb.apply(legacy['id'], self.root)
        self.assertEqual('pending', blocked['status'])
        self.assertIn('原档案保留不动', blocked['error'])
        self.assertEqual(before, self.snapshot(basic))
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_same_merged_content_does_not_reuse_basic_receipt_as_supplement(self):
        self.enable(external=False)
        first = self.answer()
        basic = self.vault / kb.PROFILE
        before = self.snapshot(basic)
        merged = self.root / 'merged.md'
        merged.write_bytes(basic.read_bytes())
        kb.configure_profile(self.root, external=self.external, evidence='改用外部只读画像。')
        result = kb.answer({'source': 'user', 'answer': '以后请简短回答。',
            'fields': {'preferences': '简短'}, 'profile_file': 'merged.md', 'expected_sha256': '-'}, self.root)['saving']
        self.assertEqual('saved', result['status'], result)
        self.assertNotEqual(first['id'], result['id'])
        self.assertEqual(merged.read_bytes(), (self.vault / kb.PROFILE_SUPPLEMENT).read_bytes())
        self.assertEqual(before, self.snapshot(basic))
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_stage_refuses_external_targets_including_internal_profile_flag(self):
        self.enable()
        (self.root / 'candidate.md').write_text('不应写入。', encoding='utf-8')
        for target in ('@profile', str(self.external), '../已有画像.md'):
            for profile in (False, True):
                with self.subTest(target=target, profile=profile):
                    manifest = {'task': 'forbidden', 'module': 'life', 'files': [{
                        'source': 'candidate.md', 'target': target, 'role': 'profile' if profile else 'knowledge',
                        'sources': ['user:test'], 'expected_sha256': digest(self.external)}]}
                    with self.assertRaisesRegex(ValueError, '外部画像只读'):
                        kb.stage(manifest, self.root, profile=profile)
                    self.assertEqual(self.external_before, self.snapshot(self.external))
        self.assertEqual([], kb.transactions(self.root))

    def test_legacy_apply_record_revise_retry_cannot_write_external(self):
        self.enable()
        legacy = self.legacy_transaction()
        for external in (False, True):
            with self.subTest(external=external):
                result = kb.apply(legacy['id'], self.root, external=external)
                self.assertEqual('pending', result['status'])
                self.assertIn('外部画像只读', result['error'])
                self.assertEqual(self.external_before, self.snapshot(self.external))
        record_file = kb.home(self.root) / 'transactions' / legacy['id'] / 'transaction.json'
        before = self.snapshot(record_file)
        (self.root / 'merged.md').write_text('合并稿。', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '外部画像只读'):
            kb.revise(legacy['id'], [{'target': '@profile', 'source': 'merged.md',
                      'expected_sha256': digest(self.external)}], self.root)
        self.assertEqual(before, self.snapshot(record_file))
        result = kb.retry(self.root)
        self.assertEqual(['pending'], [r['status'] for r in result['items']])
        self.assertIn('外部画像只读', result['items'][0]['error'])
        self.assertEqual(self.external_before, self.snapshot(self.external))
        self.assertFalse((self.vault / kb.PROFILE).exists())

    def test_no_external_profile_keeps_canonical_and_legacy_behavior(self):
        self.enable(external=False)
        self.assertEqual('saved', self.answer()['status'])
        self.assertIn('简短', (self.vault / kb.PROFILE).read_text(encoding='utf-8'))
        self.assertFalse((self.vault / kb.PROFILE_SUPPLEMENT).exists())
        legacy = self.vault / kb.LEGACY_PROFILE
        legacy.parent.mkdir(parents=True)
        (self.vault / kb.PROFILE).rename(legacy)
        value = kb.state(self.root)
        value['profile_entry'] = str(legacy)
        kb.save(value, self.root)
        self.assertEqual('saved', self.answer('称呼我小林。', name='小林')['status'])
        self.assertIn('小林', legacy.read_text(encoding='utf-8'))
        self.assertFalse((self.vault / kb.PROFILE).exists())

    def test_profile_cli_register_replace_cancel_preserves_files_and_scope(self):
        self.enable(external=False)
        self.answer()
        basic = self.vault / kb.PROFILE
        before = self.snapshot(basic)
        original_state = kb.state(self.root)
        registered = self.cli('profile', '--external', str(self.external), '--evidence', '请引用这份画像。')
        self.assertEqual(str(self.external), registered['profile_entry'])
        self.answer('现在学习写作。', **{'learning.goal': '写作'})
        supplement_before = self.snapshot(self.vault / kb.PROFILE_SUPPLEMENT)
        other = self.parent / '另一份画像.md'
        other.write_text('另一个来源。', encoding='utf-8')
        other_before = self.snapshot(other)
        replaced = self.cli('profile', '--external', str(other), '--evidence', '改用另一份画像。')
        self.assertEqual(str(other), replaced['profile_entry'])
        cancelled = self.cli('profile', '--none', '--evidence', '取消外部引用。')
        self.assertEqual(str(basic), cancelled['profile_entry'])
        value = kb.state(self.root)
        self.assertEqual(original_state['modules'], value['modules'])
        self.assertEqual(original_state['scope'], value['scope'])
        self.assertEqual('取消外部引用。', value['profile_changes'][-1]['evidence'])
        self.assertRegex(value['profile_changes'][-1]['at'], r'^\d{4}-\d{2}-\d{2}')
        self.assertEqual(before, self.snapshot(basic))
        self.assertEqual(supplement_before, self.snapshot(self.vault / kb.PROFILE_SUPPLEMENT))
        self.assertEqual(other_before, self.snapshot(other))
        self.assertEqual(self.external_before, self.snapshot(self.external))
        self.assertEqual('saved', self.answer('恢复后称呼我小林。', name='小林')['status'])
        self.assertIn('小林', basic.read_text(encoding='utf-8'))

    def test_enable_can_replace_external_and_refreshes_hash(self):
        self.enable()
        other = self.parent / '另一份画像.md'
        other.write_text('另一来源。', encoding='utf-8')
        kb.enable(self.root, vault=self.vault, profile=other, evidence='更换画像。')
        self.assertEqual(str(other), kb.state(self.root)['profile_entry'])
        self.assertEqual(digest(other), kb.state(self.root)['profile_sha256'])
        self.assertEqual('saved', self.answer()['status'])
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_invalid_registration_is_rejected_before_config_or_hook_changes(self):
        workspace_profile = self.root / 'USER.md'
        workspace_profile.write_text('工作区文件。', encoding='utf-8')
        for path in (self.parent / 'missing.md', self.parent, workspace_profile):
            with self.subTest(path=path):
                before = self.snapshot(self.root / 'config.json')
                with self.assertRaises(ValueError):
                    kb.enable(self.root, vault=self.vault, create=True, profile=path, evidence='登记画像。')
                self.assertEqual(before, self.snapshot(self.root / 'config.json'))
                self.assertFalse(self.vault.exists())
                self.assertFalse((self.root / 'AGENTS.md').exists())
        self.enable()
        state_path = kb.home(self.root) / 'state.json'
        before = self.snapshot(state_path)
        for options in ({'external': self.external, 'evidence': ' '},
                        {'none': True, 'evidence': ''},
                        {'external': workspace_profile, 'evidence': '登记。'},
                        {'external': self.vault / 'index.md', 'evidence': '登记。'},
                        {'external': self.parent / 'missing.md', 'evidence': '登记。'},
                        {'external': self.external, 'none': True, 'evidence': '登记。'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                kb.configure_profile(self.root, **options)
            self.assertEqual(before, self.snapshot(state_path))
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_symlink_and_symlink_parent_registration_are_rejected(self):
        # Isolate the filesystem predicate so Windows symlink privilege is not a test requirement.
        for linked in (self.external, self.external.parent):
            with self.subTest(linked=linked), patch.object(migration, '_is_reparse', side_effect=lambda p: p == linked):
                with self.assertRaisesRegex(ValueError, '软链接'):
                    kb.enable(self.root, vault=self.vault, create=True, profile=self.external, evidence='登记。')
        self.enable()
        with patch.object(migration, '_is_reparse', side_effect=lambda p: p == self.external):
            with self.assertRaisesRegex(ValueError, '软链接'):
                kb.configure_profile(self.root, external=self.external, evidence='更换。')
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_external_profile_cannot_be_used_as_host_entry(self):
        with self.assertRaisesRegex(ValueError, '外部画像只读'):
            kb.enable(self.root, vault=self.vault, create=True, profile=self.external,
                      entry=self.external, evidence='登记。')
        self.assertFalse(self.vault.exists())
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_context_reports_both_sources_times_limits_and_confirmation_policy(self):
        self.enable()
        self.answer()
        result = self.cli('context')
        profile, supplement = result['profile'], result['profile_supplement']
        self.assertIn('偏好：详细', profile['text'])
        self.assertTrue(profile['read_only'])
        self.assertEqual(str(self.external), profile['source_path'])
        self.assertIn('简短', supplement['text'])
        self.assertFalse(supplement['read_only'])
        self.assertEqual(str(self.vault / kb.PROFILE_SUPPLEMENT), supplement['source_path'])
        for section in (profile, supplement):
            self.assertTrue(section['modified_at'])
            self.assertFalse(section['truncated'])
            self.assertEqual(digest(Path(section['source_path'])), section['sha256'])
        self.assertEqual('not_compared', result['profile_consistency']['status'])
        self.assertIn('向用户确认', result['profile_consistency']['instruction'])
        self.assertEqual(self.external_before, self.snapshot(self.external))
        for path in (self.external, self.vault / kb.PROFILE_SUPPLEMENT):
            path.write_text('长' * 17000, encoding='utf-8')
        before = self.snapshot(self.external)
        bounded = kb.context(self.root)
        for key in ('profile', 'profile_supplement'):
            self.assertEqual(16000, len(bounded[key]['text']))
            self.assertTrue(bounded[key]['truncated'])
        self.assertEqual(before, self.snapshot(self.external))

    def test_manual_supplement_edit_queues_and_revise_preserves_manual_text(self):
        self.enable()
        self.answer()
        page = self.vault / kb.PROFILE_SUPPLEMENT
        page.write_text(page.read_text(encoding='utf-8') + '\n人工备注：周末休息。\n', encoding='utf-8')
        before = self.snapshot(page)
        queued = self.answer('下周开始学习。', goal='学习')
        self.assertEqual('pending', queued['status'])
        self.assertEqual(before, self.snapshot(page))
        row = next(r for r in queued['files'] if r['role'] == 'profile')
        merged = self.root / 'merged.md'
        merged.write_bytes((self.root / row['snapshot']).read_bytes())
        result = kb.revise(queued['id'], [{'target': kb.PROFILE_SUPPLEMENT, 'source': 'merged.md',
                            'expected_sha256': digest(page)}], self.root)
        self.assertEqual('saved', result['status'])
        self.assertIn('人工备注：周末休息。', page.read_text(encoding='utf-8'))
        self.assertIn('下周开始学习。', page.read_text(encoding='utf-8'))
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_index_repair_refuses_alias_of_external_profile(self):
        self.enable()
        saved = self.answer()
        index = self.vault / 'index.md'
        index.rename(self.vault / '原索引备份.md')
        os.link(self.external, index)
        result = kb.apply(saved['id'], self.root)
        self.assertEqual('pending', result['status'])
        self.assertIn('外部画像只读', result['error'])
        self.assertEqual(self.external_before, self.snapshot(self.external))
        self.assertEqual(self.external_before, self.snapshot(index))
        with self.assertRaisesRegex(ValueError, '外部画像只读'):
            kb.write_index(self.vault, {}, value=kb.state(self.root))

    def test_publisher_prepare_refuses_external_raw_and_compiled_targets(self):
        self.enable()
        task = {'id': 'synthetic-publish', 'purpose': 'compile'}
        source = self.root / 'publish-source.md'
        source.write_text('合成入库原文。', encoding='utf-8')
        entry = {'file': str(source), 'target': '学习/原文.md'}
        for target in ('@profile', str(self.external), '../已有画像.md'):
            with self.subTest(target=target), patch.object(vault, 'publication_plan', return_value=[entry]):
                with self.assertRaisesRegex(ValueError, '外部画像只读'):
                    vault.prepare(task, self.root, pages=[{'file': str(source), 'target': target}])
            with patch.object(vault, 'publication_plan', return_value=[{**entry, 'target': target}]):
                with self.assertRaisesRegex(ValueError, '外部画像只读'):
                    vault.prepare({**task, 'purpose': 'ingest'}, self.root)
        self.assertFalse((self.parent / 'assistant/runs/staging').exists())
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_publisher_retry_rejects_legacy_external_manifest_before_launch(self):
        self.enable()
        assistant = self.parent / 'assistant'
        write_json(assistant / 'config.json', {'stage': 'production', 'ownership': {'knowledge': True},
            'collection_start': '2026-01-01', 'production_vault': str(self.vault)})
        task = {'id': 'legacy-publish', 'status': 'publication_unknown', 'purpose': 'compile',
                'publication': {'inputs': {'source.md': 'legacy'}}}
        manifest = assistant / 'runs/staging/legacy-publish-publish/manifest.json'
        write_json(manifest, {'files': [{'source': 'source.md', 'target': str(self.external)}]})
        with patch.object(vault, 'load', return_value=task), \
                patch.object(vault, 'publication_plan', return_value=[]), patch.object(vault.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, '外部画像只读'):
                vault.publish(task['id'], self.root)
            run.assert_not_called()
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_target_rules_still_requires_writer_for_supplement_only(self):
        self.enable()
        value = kb.state(self.root)
        value['write_mode'] = 'target_rules'
        kb.save(value, self.root)
        first = self.answer()
        second = self.answer('我想学习写作。', **{'learning.goal': '写作'})
        for record in (first, second):
            self.assertEqual('awaiting_target_writer', record['status'])
            self.assertNotIn('@profile', [row['target'] for row in record['files']])
        self.assertFalse((self.vault / kb.PROFILE_SUPPLEMENT).exists())
        for record in (first, second):
            # Simulate the existing authorized writer, then verify its real files.
            for row in record['files']:
                path = self.vault / row['target']
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((self.root / row['snapshot']).read_bytes())
            (self.vault / 'index.md').write_text('\n'.join(
                f"[资料]({row['target']})" for row in record['files']), encoding='utf-8')
            checked = kb.apply(record['id'], self.root, external=True)
            self.assertEqual('saved', checked['status'], checked)
        text = (self.vault / kb.PROFILE_SUPPLEMENT).read_text(encoding='utf-8')
        self.assertIn('简短', text)
        self.assertIn('写作', text)
        self.assertFalse((self.vault / kb.PROFILE).exists())
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def migration_plan(self):
        paths, _, _ = migration._inventory(self.vault)
        manifest = self.root / 'migration.json'
        write_json(manifest, {'files': [{'source': path,
            'target': '生活/个人档案/访谈补充.md' if path == kb.PROFILE_SUPPLEMENT else
                      path.replace('生活/资料/', '学习/资料/')} for path in paths]})
        return migration.plan(self.root, manifest)

    def test_external_allows_reclassification_context_followup_and_index_repair(self):
        self.enable()
        saved = self.answer()
        receipt = kb.home(self.root) / 'transactions' / saved['id'] / 'transaction.json'
        receipt_before = self.snapshot(receipt)
        preview = self.migration_plan()
        self.assertEqual('ready', preview['status'], preview)
        result = migration.apply(self.root, preview['id'], '确认本次合成资料的完整迁移预览。')
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(self.external_before, self.snapshot(self.external))
        self.assertEqual(str(self.external), kb.state(self.root)['profile_entry'])
        moved = self.vault / '生活/个人档案/访谈补充.md'
        self.assertTrue(moved.is_file())
        self.assertEqual(str(moved), kb.context(self.root)['profile_supplement']['source_path'])
        for link in re.findall(r'\[用户回答\]\(([^)]+)\)', moved.read_text(encoding='utf-8')):
            self.assertTrue((moved.parent / unquote(link)).is_file())
        (self.vault / 'index.md').write_text('人工索引。\n', encoding='utf-8')
        repaired = kb.apply(saved['id'], self.root)
        self.assertTrue(repaired['current_verified'], repaired)
        self.assertIn(moved, kb.indexed_files(self.vault))
        self.assertEqual(receipt_before, self.snapshot(receipt))
        self.assertEqual('saved', self.answer('学习计划是练习写作。', **{'learning.goal': '练习写作'})['status'])
        self.assertIn('所属方向：学习。', moved.read_text(encoding='utf-8'))
        self.assertFalse((self.vault / kb.PROFILE_SUPPLEMENT).exists())
        self.assertFalse((self.vault / kb.PROFILE).exists())
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_external_remains_unchanged_during_migration_rollback(self):
        self.enable()
        self.answer()
        supplement = self.vault / kb.PROFILE_SUPPLEMENT
        before = supplement.read_bytes()
        preview = self.migration_plan()
        self.assertEqual('complete', migration.apply(self.root, preview['id'], '确认迁移。')['status'])
        rolled = migration.rollback(self.root, preview['id'])
        self.assertEqual('rolled_back', rolled['status'], rolled)
        self.assertEqual(before, supplement.read_bytes())
        self.assertEqual(str(self.external), kb.state(self.root)['profile_entry'])
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_external_profile_does_not_relax_pending_write_migration_gate(self):
        self.enable()
        self.legacy_transaction()
        preview = self.migration_plan()
        self.assertEqual('blocked_pending_writes', preview['status'])
        result = migration.apply(self.root, preview['id'], '尝试迁移。')
        self.assertEqual('interrupted', result['status'])
        self.assertIn('待处理', result['error'])
        self.assertEqual(self.external_before, self.snapshot(self.external))

    def test_legacy_external_transaction_cannot_follow_replacement_or_cancellation(self):
        self.enable()
        legacy = self.legacy_transaction()
        kb.configure_profile(self.root, none=True, evidence='取消外部画像引用。')
        result = kb.apply(legacy['id'], self.root)
        self.assertEqual('pending', result['status'])
        self.assertIn('外部画像只读', result['error'])
        self.assertFalse((self.vault / kb.PROFILE).exists())
        self.assertEqual(self.external_before, self.snapshot(self.external))
