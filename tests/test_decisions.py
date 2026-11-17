"""Decision behavior against synthetic libraries in the system temporary directory."""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from support import SKILL_ROOT, WorkspaceCase
from douyin_text import decisions, messages, personal_vault as kb, search
from douyin_text.common import write_json


class DecisionTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.vault = self.parent / 'vault'
        self.environment = {'CY_STATE_DIR': str(self.parent / 'state'),
                            'CY_MODEL_DIR': str(self.parent / 'models'),
                            'CY_UPDATE_CHECK': '0', 'CY_SESSION_ID': 'decision-chat'}
        env = patch.dict(os.environ, self.environment)
        env.start()
        self.addCleanup(env.stop)

    def enable(self):
        return kb.enable(self.root, vault=self.vault, create=True, evidence='合成决策测试')

    def payload(self, **changes):
        return {'domain': '工作', 'title': '要不要换岗', 'question': '是否换到能学习 Python 的岗位？',
                'options': ['留任', '换岗'], 'choice': '先留任一个月', 'reason': '先验证新工作的内容',
                'expectation': '拿到明确的岗位说明', 'review_date': date.today().isoformat(),
                'status': '已决定', **changes}

    def create(self, **changes):
        result = decisions.new(self.payload(**changes), self.root)
        self.assertEqual('saved', result['status'], result)
        return result['decision']

    def review_payload(self, row, **changes):
        return {'difference': '实际只拿到口头介绍', 'keep': '继续核对岗位内容',
                'improve': '下次先索要书面说明', 'expected_sha256': row['sha256'], **changes}

    def invoke(self, *args):
        process = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SKILL_ROOT / 'scripts/brain.py'),
                                  '--workspace', str(self.root), *args], capture_output=True,
                                 env={**os.environ, **self.environment}, timeout=30)
        self.assertEqual(0, process.returncode, process.stderr.decode('utf-8', 'replace') +
                         process.stdout.decode('utf-8', 'replace'))
        return json.loads(process.stdout.decode('utf-8-sig'))

    def notice(self, command='quick', **fields):
        output = io.StringIO()
        with redirect_stdout(output):
            messages.emit_json({'status': 'ok', 'tell_user': '原任务已完成。', **fields},
                               command=command, root=self.root, update_notice='')
        return json.loads(output.getvalue())

    def test_new_uses_transactions_index_and_all_domains(self):
        self.enable()
        for domain, folder in kb.DOMAIN_DIRS.items():
            with self.subTest(domain=domain), patch.object(kb, 'stage', wraps=kb.stage) as stage, \
                    patch.object(kb, 'apply', wraps=kb.apply) as apply:
                row = self.create(domain=domain)
                stage.assert_called_once()
                apply.assert_called_once()
                self.assertTrue(stage.call_args.kwargs['explicit_save'])
                self.assertEqual(f'{folder}/决策/{date.today().isoformat()} 要不要换岗.md', row['path'])
                file = Path(row['file'])
                fields, _ = decisions._sections(file.read_text(encoding='utf-8'))
                self.assertEqual(set(decisions.FIELDS.values()), set(fields))
                self.assertIn(file, kb.indexed_files(self.vault))
                transaction = kb.transactions(self.root)[-1]
                self.assertEqual('saved', transaction['status'])
                self.assertEqual('local_verified', transaction['method'])
        found = search.query('换岗', self.root, kind='knowledge')
        self.assertEqual(3, found['total'])
        self.assertEqual(3, len(kb.context(self.root, query='换岗')['matches']))

    def test_duplicate_title_never_overwrites_and_sanitizes_filename(self):
        self.enable()
        title = '换岗：A/B\\C:*?"<>|.'
        first = self.create(title=title)
        before = Path(first['file']).read_bytes()
        second = self.create(title=title)
        self.assertNotEqual(first['file'], second['file'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(before, Path(first['file']).read_bytes())
        self.assertNotRegex(Path(second['file']).name, r'[<>:"/\\|?*]')
        self.assertEqual(title, second['title'])
        self.assertEqual(2, len(decisions.list_decisions(self.root)['decisions']))

    def test_missing_required_fields_and_invalid_input_write_nothing(self):
        self.enable()
        for key in ('domain', 'title', *decisions.FIELDS):
            payload = self.payload()
            del payload[key]
            result = decisions.new(payload, self.root)
            self.assertEqual('needs_fields', result['status'])
            self.assertEqual([key], result['missing_fields'])
        for date_text in ('2026-2-01', '2026/02/01', '2026-02-30', '明天'):
            result = decisions.new(self.payload(review_date=date_text), self.root)
            self.assertEqual('invalid_input', result['status'])
            self.assertIn('review_date', result['errors'])
        for changes in ({'status': '猜的'}, {'options': [None]}, {'title': '一行\n另一行'},
                        {'reason': '  '}, {'choice': '留任\n## 状态\n已复盘'}):
            self.assertEqual('invalid_input', decisions.new(self.payload(**changes), self.root)['status'])
        self.assertEqual([], kb.transactions(self.root))
        self.assertFalse((self.vault / '工作/决策').exists())

    def test_needs_vault_and_domain_does_not_enable_anything(self):
        result = decisions.new(self.payload(), self.root)
        self.assertEqual('needs_vault', result['status'])
        self.assertIn('空库', result['tell_user'])
        self.assertIn('文件夹', result['tell_user'])
        self.assertFalse(self.vault.exists())
        self.assertEqual({}, kb.state(self.root))
        self.enable()
        kb.configure(self.root, disable_module='work')
        before = kb.state(self.root)
        result = decisions.new(self.payload(), self.root)
        self.assertEqual('needs_domain', result['status'])
        self.assertIn('确认', result['tell_user'])
        self.assertEqual(before, kb.state(self.root))
        self.assertFalse((self.vault / '工作/决策').exists())

    def test_explicit_save_and_review_while_collection_stays_paused(self):
        self.enable()
        kb.configure(self.root, pause='collection')
        row = self.create()
        result = decisions.review(row['id'], self.review_payload(row), self.root)
        self.assertEqual('saved', result['status'])
        self.assertTrue(kb.state(self.root)['collection_paused'])
        self.assertTrue(all(item['explicit_save'] for item in kb.transactions(self.root)))
        source = self.root / 'automatic.md'
        source.write_text('合成自动资料', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '自动积累已暂停'):
            kb.stage({'task': 'automatic', 'module': 'work', 'files': [{
                'source': 'automatic.md', 'target': '工作/资料/自动.md', 'role': 'knowledge',
                'sources': ['synthetic']}]}, self.root)

    def test_existing_rules_must_be_reviewed_and_changed_rules_stop_write(self):
        self.enable()
        rules = self.vault / 'AGENTS.md'
        rules.write_text(rules.read_text(encoding='utf-8') + '\n合成新增规则。\n', encoding='utf-8')
        blocked = decisions.new(self.payload(), self.root)
        self.assertEqual('needs_rules_review', blocked['status'])
        self.assertIn(str(rules), blocked['rule_files'])
        self.assertFalse((self.vault / '工作/决策').exists())
        self.invoke('kb', 'rules-reviewed', '--writer', 'local', '--evidence', '已读合成规则，允许本地事务写入')
        self.create()
        value = kb.state(self.root)
        value['write_mode'] = 'target_rules'
        kb.save(value, self.root)
        result = decisions.new(self.payload(title='外部写入入口'), self.root)
        self.assertEqual('awaiting_target_writer', result['status'])
        self.assertTrue(Path(result['manifest']).is_file())
        self.assertEqual(1, len(list((self.vault / '工作/决策').glob('*.md'))))

    def test_list_due_statuses_domains_and_invalid_date(self):
        self.enable()
        today = self.create()
        pending = self.create(title='待定', status='待决定')
        self.create(title='将来', review_date=(date.today() + timedelta(days=1)).isoformat())
        self.create(title='已经复盘', status='已复盘')
        self.create(title='已经放弃', status='已放弃')
        self.create(title='学习决定', domain='学习')
        invalid = self.create(title='错误日期')
        file = Path(invalid['file'])
        file.write_text(file.read_text(encoding='utf-8').replace(invalid['review_date'], '2026-02-30'), encoding='utf-8')
        result = decisions.list_decisions(self.root, due=True, domain='工作')
        self.assertEqual({today['id'], pending['id']}, {row['id'] for row in result['decisions']})
        self.assertEqual([str(file)], [row['file'] for row in result['needs_correction']])
        self.assertIn('复盘日期', result['needs_correction'][0]['error'])
        self.assertIn('修改', result['tell_user'])
        self.assertEqual(6, len(decisions.list_decisions(self.root)['decisions']))

    def test_unreadable_files_are_listed_and_recover(self):
        self.enable()
        row = self.create()
        file = Path(row['file'])
        raw = file.read_bytes()
        file.write_bytes(b'\xff\xfe\xff')
        self.assertEqual(str(file), decisions.list_decisions(self.root)['needs_correction'][0]['file'])
        file.write_bytes(raw)
        self.assertEqual(1, len(decisions.list_decisions(self.root)['decisions']))
        original_read = Path.read_bytes
        def denied(path):
            if path == file:
                raise PermissionError('合成无读取权限')
            return original_read(path)
        file.write_bytes(raw + b'\n')
        with patch.object(Path, 'read_bytes', denied):
            self.assertIn('合成无读取权限', decisions.list_decisions(self.root)['needs_correction'][0]['error'])

    def test_review_preserves_original_bytes_and_appends_three_fields(self):
        self.enable()
        row = self.create()
        ident = row['id']
        file = Path(row['file'])
        data = b'\xef\xbb\xbf' + file.read_bytes().replace(b'\n', b'\r\n')
        data += '\r\n用户自己的补充。\r\n'.encode('utf-8')
        # Keep the free-form note outside the status field.
        data = data.replace('用户自己的补充。'.encode('utf-8'), '## 补充\r\n\r\n用户自己的补充。'.encode('utf-8'))
        file.write_bytes(data)
        row = decisions.list_decisions(self.root)['decisions'][0]
        self.assertEqual(ident, row['id'])
        fields, _ = decisions._sections(data.decode('utf-8-sig'))
        result = decisions.review(row['id'], self.review_payload(row), self.root)
        self.assertEqual('saved', result['status'], result)
        after = file.read_bytes()
        expected_prefix = data.replace('## 状态\r\n\r\n已决定'.encode('utf-8'), '## 状态\r\n\r\n已复盘'.encode('utf-8'))
        self.assertTrue(after.startswith(expected_prefix))
        updated_fields, _ = decisions._sections(after.decode('utf-8-sig'))
        self.assertEqual({**fields, '状态': '已复盘'}, updated_fields)
        for label in decisions.REVIEW_FIELDS.values():
            self.assertIn('### ' + label, after.decode('utf-8-sig'))
        self.assertIn(f'## 复盘（{date.today().isoformat()}）', after.decode('utf-8-sig'))
        self.assertEqual([], decisions.list_decisions(self.root, due=True)['decisions'])
        next_row = decisions.list_decisions(self.root)['decisions'][0]
        decisions.review(next_row['id'], self.review_payload(next_row), self.root)
        self.assertTrue(file.read_bytes().startswith(after))

    def test_review_requires_fields_and_old_hash_never_overwrites_manual_edit(self):
        self.enable()
        row = self.create()
        missing = decisions.review(row['id'], {'difference': '实际不同'}, self.root)
        self.assertEqual(['keep', 'improve', 'expected_sha256'], missing['missing_fields'])
        file = Path(row['file'])
        file.write_bytes(file.read_bytes().replace('先留任一个月'.encode('utf-8'), '手动改为换岗'.encode('utf-8')))
        before = file.read_bytes()
        result = decisions.review(row['id'], self.review_payload(row), self.root)
        self.assertEqual('conflict', result['status'])
        self.assertIn('没有覆盖', result['tell_user'])
        self.assertEqual(before, file.read_bytes())
        self.assertEqual(1, len(kb.transactions(self.root)))

    def test_review_edit_between_stage_and_apply_is_protected_by_transaction(self):
        self.enable()
        row = self.create()
        file, original = Path(row['file']), kb.stage
        manual = file.read_bytes().replace('先留任一个月'.encode('utf-8'), '并发手改内容'.encode('utf-8'))
        def change_after_stage(*args, **kwargs):
            staged = original(*args, **kwargs)
            file.write_bytes(manual)
            return staged
        with patch.object(kb, 'stage', side_effect=change_after_stage):
            result = decisions.review(row['id'], self.review_payload(row), self.root)
        self.assertEqual('pending', result['status'])
        self.assertIn('已修改', result['error'])
        self.assertEqual(manual, file.read_bytes())

    def test_ambiguous_title_requires_id(self):
        self.enable()
        one, two = self.create(), self.create()
        result = decisions.review(one['title'], self.review_payload(one), self.root)
        self.assertEqual('needs_selection', result['status'])
        self.assertEqual(2, len(result['decisions']))
        self.assertEqual('saved', decisions.review(two['path'], self.review_payload(two), self.root)['status'])

    def test_reminders_once_per_decision_per_session_and_every_task_entry(self):
        self.enable()
        row = self.create()
        for command in ('quick', 'plan', 'plan-text', 'creator plan', 'kb context'):
            with patch.dict(os.environ, {'CY_SESSION_ID': command}):
                first = self.notice(command)
                self.assertIn('到复盘日了', first['tell_user'])
                self.assertTrue(first['tell_user'].startswith('原任务已完成。'))
                self.assertNotIn('到复盘日了', self.notice('quick')['tell_user'])
        with patch.dict(os.environ, {'CY_SESSION_ID': 'another-chat'}):
            self.assertIn(row['title'], self.notice()['tell_user'])
        self.assertIn(row['title'], self.notice()['tell_user'])
        self.create(title='第二个决定')
        next_notice = self.notice()['tell_user']
        self.assertIn('第二个决定', next_notice)
        self.assertNotIn(row['title'], next_notice)
        self.assertNotIn('到复盘日了', self.notice('decide list')['tell_user'])

    def test_reminders_fail_silently_without_hiding_task_result(self):
        self.assertEqual('原任务已完成。', self.notice()['tell_user'])
        self.enable()
        row = self.create()
        Path(row['file']).write_bytes(b'\xff')
        self.assertEqual('原任务已完成。', self.notice()['tell_user'])
        for error in (OSError('合成占用'), KeyError('缓存缺失'), RuntimeError('缓存失效')):
            with patch.object(decisions, 'notices', side_effect=error):
                result = self.notice(error='原任务的错误', status='error', tell_user='原任务错误说明。')
                self.assertEqual('原任务的错误', result['error'])
                self.assertEqual('原任务错误说明。', result['tell_user'])

    def test_due_notices_name_three_oldest_and_summarize_once_per_session(self):
        self.enable()
        for days in (3, 1, 5, 2, 7, 4, 6, 8):
            self.create(title=f'合成决定 {days}',
                        review_date=(date.today() - timedelta(days=days)).isoformat())
        ordered = decisions.list_decisions(self.root, due=True)['decisions']
        with patch.object(messages, 'once', wraps=messages.once) as claim:
            first = self.notice()['tell_user']
        self.assertEqual(3, first.count('你在 '))
        self.assertIn('另有 5 个决定也到复盘日了，可以用 decide list --due 查看。', first)
        self.assertEqual(4, claim.call_count)
        positions = [first.index('「' + row['title'] + '」') for row in ordered[:3]]
        self.assertEqual(sorted(positions), positions)
        for row in ordered[3:]:
            self.assertNotIn('「' + row['title'] + '」', first)

        second = self.notice('plan')['tell_user']
        self.assertEqual(3, second.count('你在 '))
        self.assertNotIn('另有', second)
        for row in ordered[:3]:
            self.assertNotIn('「' + row['title'] + '」', second)
        for row in ordered[3:6]:
            self.assertIn('「' + row['title'] + '」', second)
        self.assertEqual(2, self.notice('plan-text')['tell_user'].count('你在 '))
        self.assertEqual('原任务已完成。', self.notice()['tell_user'])
        with patch.dict(os.environ, {'CY_SESSION_ID': 'new-decision-chat'}):
            self.assertEqual(first, self.notice()['tell_user'])

    def test_previously_claimed_decisions_are_excluded_from_summary_count(self):
        self.enable()
        rows = [self.create(title=f'合成决定 {n}') for n in range(8)]
        claimed_rows = (rows[0], rows[4])
        for row in claimed_rows:
            self.assertTrue(messages.once('decision-due:' + str(self.vault.resolve()) + ':' + row['id']))
        with patch.object(messages, 'once', wraps=messages.once) as claim:
            shown = self.notice()['tell_user']
        self.assertEqual(3, shown.count('你在 '))
        self.assertIn('另有 3 个决定也到复盘日了', shown)
        self.assertEqual(4, claim.call_count)
        for row in claimed_rows:
            self.assertNotIn('「' + row['title'] + '」', shown)

    def test_summary_stays_claimed_when_more_decisions_become_due(self):
        self.enable()
        for n in range(4):
            self.create(title=f'已到期决定 {n}')
        self.assertIn('另有 1 个决定', self.notice()['tell_user'])
        for n in range(5):
            self.create(title=f'新增到期决定 {n}')
        with patch.object(messages, 'once', wraps=messages.once) as claim:
            shown = self.notice()['tell_user']
        self.assertEqual(3, shown.count('你在 '))
        self.assertNotIn('另有', shown)
        self.assertEqual(3, claim.call_count)

    def test_all_claimed_notices_list_session_directory_once_and_claim_nothing(self):
        self.enable()
        rows = [self.create(title=f'合成决定 {n}') for n in range(8)]
        for row in rows:
            messages.once('decision-due:' + str(self.vault.resolve()) + ':' + row['id'])
        decisions.list_decisions(self.root)  # Warm cache before checking notice filesystem operations.
        session_dir = (self.parent / 'state/task-notices' /
                       hashlib.sha256(self.environment['CY_SESSION_ID'].encode('utf-8')).hexdigest())
        with patch.object(messages, 'once', wraps=messages.once) as claim, \
                patch.object(os, 'listdir', wraps=os.listdir) as listing:
            self.assertEqual('原任务已完成。', self.notice()['tell_user'])
        claim.assert_not_called()
        listing.assert_called_once_with(session_dir)

    def test_no_session_id_keeps_agent_deduplication_and_three_name_cap(self):
        self.enable()
        for n in range(5):
            self.create(title=f'合成决定 {n}')
        decisions.list_decisions(self.root)
        with patch('updates.session_identity', return_value=None), \
                patch.object(os, 'listdir', wraps=os.listdir) as listing:
            first = self.notice()['tell_user']
            self.assertEqual(first, self.notice()['tell_user'])
        self.assertEqual(3, first.count('你在 '))
        self.assertIn('另有 2 个决定', first)
        listing.assert_not_called()
        self.assertFalse((self.parent / 'state/task-notices').exists())

    def test_unchanged_cache_needs_only_stat_not_bodies_index_or_search(self):
        self.enable()
        row = self.create()
        decisions.list_decisions(self.root)
        cache = self.root / '.local/decisions/index.json'
        before = cache.stat().st_mtime_ns
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('不能重读正文')), \
                patch.object(Path, 'iterdir', side_effect=AssertionError('不能重新枚举目录')), \
                patch.object(kb, 'indexed_files', side_effect=AssertionError('不能重读索引')), \
                patch.object(search, 'query', side_effect=AssertionError('提醒不能触发搜索')):
            self.assertEqual(row['id'], decisions.list_decisions(self.root)['decisions'][0]['id'])
            self.assertIn('到复盘日了', self.notice()['tell_user'])
        self.assertEqual(before, cache.stat().st_mtime_ns)
        self.assertFalse((self.root / '.local/search.sqlite').exists())

    def test_hand_edits_moves_deletion_and_cache_rebuild_follow_files(self):
        self.enable()
        row = self.create()
        decisions.list_decisions(self.root)
        file = Path(row['file'])
        future = (date.today() + timedelta(days=7)).isoformat()
        text = file.read_text(encoding='utf-8').replace('## 复盘日期\n\n' + row['review_date'], '## 复盘日期\n\n' + future)
        file.write_text(text, encoding='utf-8')
        self.assertEqual([], decisions.list_decisions(self.root, due=True)['decisions'])
        renamed = file.with_name('手动改名.md')
        file.rename(renamed)
        refreshed = decisions.list_decisions(self.root)['decisions']
        self.assertEqual([str(renamed)], [item['file'] for item in refreshed])
        self.assertEqual(row['id'], refreshed[0]['id'])
        moved = self.vault / '工作/资料/搬走的决定.md'
        moved.parent.mkdir(parents=True, exist_ok=True)
        renamed.rename(moved)
        self.assertEqual([], decisions.list_decisions(self.root)['decisions'])
        with (self.vault / 'index.md').open('a', encoding='utf-8') as index:
            index.write('\n[搬走](<工作/资料/搬走的决定.md>)\n')
        self.assertEqual([str(moved)], [item['file'] for item in decisions.list_decisions(self.root)['decisions']])
        cache = self.root / '.local/decisions/index.json'
        cache.write_text('损坏的缓存', encoding='utf-8')
        self.assertEqual(row['id'], decisions.list_decisions(self.root)['decisions'][0]['id'])
        moved.unlink()  # Only a synthetic fixture in TemporaryDirectory.
        self.assertEqual([], decisions.list_decisions(self.root)['decisions'])
        self.assertEqual([], decisions.list_decisions(self.root)['needs_correction'])

    def test_repeated_headings_need_correction_only_in_decisions(self):
        self.enable()
        row = self.create()
        note = self.vault / '工作/资料/周会记录.md'
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text('# 周会记录\n\n## 问题\n\n排期太紧。\n\n## 问题\n\n接口还没定。\n', encoding='utf-8')
        with (self.vault / 'index.md').open('a', encoding='utf-8') as index:
            index.write('\n[周会](<工作/资料/周会记录.md>)\n')
        result = decisions.list_decisions(self.root)
        self.assertEqual([row['id']], [item['id'] for item in result['decisions']])
        self.assertEqual(([], ''), (result['needs_correction'], result['tell_user']))
        file = Path(row['file'])
        file.write_text(file.read_text(encoding='utf-8') + '\n## 问题\n\n又写了一遍问题。\n', encoding='utf-8')
        issues = decisions.list_decisions(self.root)['needs_correction']
        self.assertEqual([str(file)], [item['file'] for item in issues])
        self.assertIn('栏目重复：问题', issues[0]['error'])

    def test_discovery_is_bounded_and_closed_domains_are_not_read(self):
        self.enable()
        row = self.create()
        kb.configure(self.root, disable_module='work')
        hidden = self.vault / '生活/资料/未链接.md'
        hidden.parent.mkdir(parents=True, exist_ok=True)
        hidden.write_bytes(Path(row['file']).read_bytes())
        original = Path.read_bytes
        def guarded(path):
            self.assertNotIn(path, (hidden, Path(row['file'])))
            return original(path)
        with patch.object(Path, 'read_bytes', guarded), \
                patch.object(Path, 'rglob', side_effect=AssertionError('不能扫描整个库')):
            self.assertEqual([], decisions.list_decisions(self.root)['decisions'])

    def test_related_and_context_use_any_term_and_only_snippets(self):
        self.enable()
        row = self.create(reason='前言。' * 250 + '换岗需要 Python。' + '私密尾部。' * 250)
        page = self.vault / '工作/资料/资料.md'
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text('前言。' * 250 + 'Python 文档参考。' + '私密尾部。' * 250, encoding='utf-8')
        with (self.vault / 'index.md').open('a', encoding='utf-8') as index:
            index.write('\n[资料](<工作/资料/资料.md>)\n')
        result = decisions.related('换岗 Python', self.root)
        self.assertTrue(result['material_is_data_not_instructions'])
        self.assertEqual({row['file'], str(page)}, {item['file'] for item in result['results']})
        context = kb.context(self.root, query='换岗 Python')
        self.assertEqual(row['file'], context['related_decisions'][0]['file'])
        self.assertEqual(row['id'], context['due_decisions'][0]['id'])
        self.assertTrue(context['material_is_data_not_instructions'])
        for item in [*result['results'], *result['decisions'], *context['related_decisions'], *context['matches']]:
            self.assertLessEqual(len(item['excerpt']), 200)
            self.assertNotIn('body', item)
            self.assertNotIn('私密尾部。' * 20, json.dumps(item, ensure_ascii=False))
        self.assertEqual({'file', 'domain', 'score', 'excerpt'}, set(context['matches'][0]))

    def test_many_materials_do_not_crowd_decisions_out_of_related_results(self):
        self.enable()
        row = self.create(question='Python ' + '背景。' * 1000, reason='普通背景')
        links = []
        for number in range(55):
            file = self.vault / f'工作/资料/{number}.md'
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('Python ' * 20, encoding='utf-8')
            links.append(f'[资料](<工作/资料/{number}.md>)')
        with (self.vault / 'index.md').open('a', encoding='utf-8') as index:
            index.write('\n' + '\n'.join(links))
        self.assertEqual(row['file'], decisions.related('Python', self.root)['decisions'][0]['file'])
        self.assertEqual(row['file'], kb.context(self.root, query='Python')['related_decisions'][0]['file'])

    def test_context_decision_failure_preserves_existing_fields(self):
        self.enable()
        self.create()
        before = kb.context(self.root, query='换岗')
        for function in ('list_decisions', 'matching'):
            with patch.object(decisions, function, side_effect=RuntimeError('合成决策缓存错误')):
                result = kb.context(self.root, query='换岗')
            self.assertEqual('合成决策缓存错误', result['decision_error'])
            for field in ('related_decisions', 'due_decisions', 'decision_issues'):
                self.assertEqual([], result[field])
            for key in ('matches', 'profile', 'modules', 'save_this_task', 'rule_files'):
                self.assertEqual(before[key], result[key])

    def test_cli_new_list_review_related_and_real_task_notice(self):
        self.enable()
        payload = self.root / 'decision.json'
        write_json(payload, self.payload())
        result = self.invoke('decide', 'new', '--file', str(payload))
        self.assertEqual('saved', result['status'])
        row = self.invoke('decide', 'list', '--due', '--domain', '工作')['decisions'][0]
        self.assertEqual(result['decision']['id'], row['id'])
        found = self.invoke('decide', 'related', '--query', '换岗 不存在')
        self.assertEqual(row['file'], found['decisions'][0]['file'])
        source = self.root / '输入.txt'
        source.write_text('合成已有文稿。', encoding='utf-8')
        started = self.invoke('plan-text', str(source))
        self.assertIn('到复盘日了', started['tell_user'])
        self.assertNotIn('到复盘日了', self.invoke('plan-text', str(source))['tell_user'])
        review_file = self.root / 'review.json'
        write_json(review_file, self.review_payload(row))
        self.assertEqual('saved', self.invoke('decide', 'review', row['id'], '--file', str(review_file))['status'])
        self.assertEqual([], self.invoke('decide', 'list', '--due')['decisions'])
