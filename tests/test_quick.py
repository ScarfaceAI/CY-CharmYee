from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from support import SKILL_ROOT, WorkspaceCase, read_json, write_json
from test_local_asr import recognized
from douyin_text import acquisition, asr, background, cli, content, local_asr, media, messages, models, quick, workflow
from douyin_text.common import digest
import brain
import updates


class QuickTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        write_json(self.root / 'config.json', {'asr_engine': 'local', 'chunk_seconds': 300,
                   'asr_background_threshold_seconds': 120, 'asr_hotwords': False})
        self.worker = Mock(recognize=Mock(side_effect=lambda *a, **k: recognized('测试原文。')))
        for replacement in (
            patch.dict(os.environ, {'CY_STATE_DIR': str(self.parent / 'state'),
                       'CY_MODEL_DIR': str(self.parent / 'models'), 'CY_SESSION_ID': 'quick-chat'}),
            patch.object(cli, 'ROOT', self.root),
            patch.object(local_asr, 'Worker', return_value=self.worker),
            patch.object(local_asr, 'waiting_for_model', return_value=None),
            patch.object(asr, 'recognize', side_effect=AssertionError('不能调用豆包')),
            patch.object(workflow.credentials, 'load', side_effect=AssertionError('不能读取用户凭据')),
            patch.object(media.douyin_browser, 'resolve_url', side_effect=AssertionError('不能访问网络')),
            patch.object(media, 'acquire_batch', side_effect=AssertionError('不能访问抖音')),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(list(args))
        self.assertEqual('', stderr.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertIsInstance(result['tell_user'], str)
        if args[0] == 'quick' or result.get('status') in {'error', 'failed', 'incomplete'}:
            self.assertTrue(result['tell_user'])
        return code, result

    def fake_link(self, values, scope, run_dir, root, **options):
        job = {'id': 'douyin-123456789', 'title': '链接替身', 'author': '测试',
               'source': values[0], 'media': str(self.wav), 'published': None,
               'input_number': 1, 'input': values[0]}
        write_json(root / '.cache' / job['id'] / 'source/source.json', {
            'file': 'source.wav', 'sha256': digest(self.wav), 'media_id': job['id'], 'job': job})
        (root / 'source.wav').write_bytes(self.wav.read_bytes())
        options['on_ready']({'jobs': [job], 'failures': [], 'input_number': 1})
        return media.Discovery([job])

    def test_local_file_records_original_evidence_and_uses_existing_workflow(self):
        with patch.object(workflow, 'plan', wraps=workflow.plan) as plan, \
                patch.object(workflow, 'confirm', wraps=workflow.confirm) as confirm, \
                patch.object(workflow, 'run', wraps=workflow.run) as run:
            code, result = self.invoke('quick', str(self.wav), '--evidence', '请把这段录音转成文字')
        self.assertEqual(0, code)
        self.assertEqual('awaiting_review', result['status'])
        self.assertFalse(result['reused'])
        self.assertEqual('测试原文。\n', self.path(result['text']).read_text(encoding='utf-8'))
        self.assertEqual(1, len(result['suspects']['files']))
        for path in result['suspects']['files'][0].values():
            self.assertTrue(self.path(path).is_file())
        saved = workflow.load(result['id'], self.root)
        self.assertEqual('请把这段录音转成文字', saved['evidence'])
        self.assertEqual('transcribe', saved['purpose'])
        self.assertEqual(saved['suspects']['total'], result['suspects']['total'])
        plan.assert_called_once()
        confirm.assert_called_once()
        run.assert_called_once()

    def test_share_link_and_short_link_use_normal_cache_without_recognition_again(self):
        for link in ('分享 https://www.douyin.com/video/123456789 看看这条', 'https://v.douyin.com/example/'):
            with self.subTest(link=link), patch.object(media, 'acquire_batch', side_effect=self.fake_link), \
                    patch.object(media.douyin_browser, 'resolve_url', return_value='https://www.douyin.com/video/123456789'):
                code, result = self.invoke('quick', link)
            self.assertEqual(0, code)
            self.assertEqual('awaiting_review', result['status'])
            self.assertEqual(quick.DEFAULT_EVIDENCE, workflow.load(result['id'], self.root)['evidence'])
        self.assertTrue(result['reused'])
        self.assertEqual(1, self.worker.recognize.call_count)
        # A share URL with an explicit video ID can use the same cache offline.
        code, cached = self.invoke('quick', 'https://www.iesdouyin.com/share/video/123456789/')
        self.assertEqual(0, code, cached)
        self.assertTrue(cached['reused'])
        self.assertEqual(1, self.worker.recognize.call_count)

    def test_second_local_run_reuses_unchanged_artifacts_and_skips_model_wait(self):
        _, first = self.invoke('quick', str(self.wav))
        original = self.path(first['text']).read_bytes()
        self.worker.recognize.reset_mock()
        with patch.object(local_asr, 'waiting_for_model', side_effect=AssertionError('缓存不需要模型')):
            code, second = self.invoke('quick', str(self.wav))
        self.assertEqual(0, code)
        self.assertTrue(second['reused'])
        self.assertIn('以前的结果', second['tell_user'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['text'], second['text'])
        self.assertEqual(original, self.path(second['text']).read_bytes())
        self.worker.recognize.assert_not_called()

    def test_changed_cached_transcript_does_not_silently_reuse_or_retranscribe(self):
        _, first = self.invoke('quick', str(self.wav))
        self.path(first['text']).write_text('已被改动', encoding='utf-8')
        self.worker.recognize.reset_mock()
        code, result = self.invoke('quick', str(self.wav))
        self.assertEqual(1, code)
        self.assertFalse(result['reused'])
        self.assertTrue(any('原稿被修改' in failure['error'] for failure in result['failures']))
        self.worker.recognize.assert_not_called()

    def test_text_files_use_plan_text_preserve_bytes_and_report_cached_import(self):
        for suffix in ('.txt', '.md'):
            source = self.parent / ('原稿' + suffix)
            source.write_bytes('原稿 12 个字。\r\n保留换行。'.encode('utf-8'))
            with patch.object(content, 'plan_text', wraps=content.plan_text) as plan_text, \
                    patch.object(workflow, 'plan', side_effect=AssertionError('文字稿不用媒体 plan')):
                code, result = self.invoke('quick', str(source))
            self.assertEqual(0, code)
            plan_text.assert_called_once()
            saved = workflow.load(result['id'], self.root)
            self.assertEqual(source.read_bytes(), self.path(saved['text_imports'][0]['file']).read_bytes())
            self.assertEqual(source.read_text(encoding='utf-8'), self.path(result['text']).read_text(encoding='utf-8'))
            self.assertIn('原样导入', result['tell_user'])
            self.assertEqual(1, result['suspects']['missing'])
            _, again = self.invoke('quick', str(source))
            self.assertTrue(again['reused'])
            self.assertIn('以前的结果', again['tell_user'])
        self.worker.recognize.assert_not_called()

    def test_multiple_inputs_profiles_collections_and_ambiguous_links_offer_full_flow(self):
        listing = self.parent / 'links.txt'
        listing.write_text('https://www.douyin.com/video/123\nhttps://www.douyin.com/video/456', encoding='utf-8')
        inputs = [(str(self.wav), str(self.wav)),
                  ('https://www.douyin.com/video/123 https://www.douyin.com/video/456',),
                  ('https://www.douyin.com/video/123 https://www.douyin.com/video/123',), (str(listing),),
                  ('https://www.douyin.com/user/example',),
                  ('https://www.douyin.com/collection/123',), ('https://www.douyin.com/',)]
        for values in inputs:
            with self.subTest(values=values):
                code, result = self.invoke('quick', *values)
                self.assertEqual(1, code)
                self.assertEqual('needs_scope', result['status'])
                self.assertIn('完整流程', result['tell_user'])
                self.assertIn(result['next_command'], result['tell_user'])
                self.assertTrue(result['next_command'].startswith('plan '))
        self.worker.recognize.assert_not_called()
        self.assertFalse((self.root / 'runs').exists())

    def test_transcript_containing_a_link_is_imported_as_prose(self):
        source = self.parent / '含链接原稿.txt'
        source.write_text('原稿引用了 https://www.douyin.com/video/123456789 作为例子。', encoding='utf-8')
        code, result = self.invoke('quick', str(source))
        self.assertEqual(0, code, result)
        self.assertEqual(source.read_text(encoding='utf-8'), self.path(result['text']).read_text(encoding='utf-8'))
        self.assertEqual('text', workflow.load(result['id'], self.root)['input_kind'])
        self.worker.recognize.assert_not_called()

    def test_short_profile_is_rejected_before_acquisition_or_recognition(self):
        with patch.object(media.douyin_browser, 'resolve_url', return_value='https://www.douyin.com/user/example'):
            code, result = self.invoke('quick', 'https://v.douyin.com/profile/')
        self.assertEqual(1, code)
        self.assertEqual('needs_scope', result['status'])
        self.assertIn('confirm ' + result['id'], result['next_command'])
        self.assertEqual('needs_scope', workflow.load(result['id'], self.root)['status'])
        self.worker.recognize.assert_not_called()

    def test_background_handoff_has_concrete_status_and_cancel_commands(self):
        write_json(self.root / 'config.json', {'asr_engine': 'local', 'asr_background_threshold_seconds': 0})
        def handoff(task, root, progress, options):
            task.update(status='transcribing_in_background', tell_user='已在后台开始。')
            workflow.save(task, root)
            return task
        with patch.object(background, 'start', side_effect=handoff) as start:
            code, result = self.invoke('quick', str(self.wav))
        self.assertEqual(0, code)
        start.assert_called_once()
        for action in ('status', 'cancel'):
            self.assertIn(action + ' ' + result['id'], result['tell_user'])
        self.assertIsNone(result['text'])
        self.worker.recognize.assert_not_called()

    def test_waiting_model_preserves_own_notice_and_adds_progress_once(self):
        waiting = {'status': 'waiting_model', 'tell_user': '模型尚未就绪，已启动下载。',
                   'model_status': {'download_active': True, 'model': models.DEFAULT_MODEL,
                                    'progress': {'downloaded_bytes': 25, 'total_bytes': 100}}}
        with patch.object(local_asr, 'waiting_for_model', return_value=waiting), \
                patch.object(media, 'extract', side_effect=AssertionError('模型未好，不提取音轨')):
            for number in range(2):
                code, result = self.invoke('quick', str(self.wav))
                self.assertEqual(0, code)
                self.assertEqual('waiting_model', result['status'])
                self.assertIn('模型尚未就绪，已启动下载。', result['tell_user'])
                self.assertIn('run ' + result['id'], result['tell_user'])
                self.assertIn('models cancel', result['tell_user'])
                self.assertEqual(number == 0, '25.0%' in result['tell_user'])
        self.worker.recognize.assert_not_called()

    def test_acquisition_failures_keep_receipts_and_once_per_chat_fallback(self):
        failure = {'stage': 'acquisition', 'reason': 'media_unavailable', 'error': '作品不可用', 'input_number': 1}
        with patch.object(media, 'acquire_batch', return_value=media.Discovery(failures=[failure])):
            for number in range(2):
                code, result = self.invoke('quick', 'https://www.douyin.com/video/123456789')
                self.assertEqual(1, code)
                self.assertEqual('failed', result['status'])
                self.assertIn('show ' + result['id'], result['tell_user'])
                self.assertEqual(number == 0, messages.ACQUISITION_FALLBACK in result['tell_user'])
                self.assertTrue(workflow.load(result['id'], self.root)['failures'])
            with patch.dict(os.environ, {'CY_SESSION_ID': 'another-chat'}):
                _, result = self.invoke('quick', 'https://www.douyin.com/video/123456789')
                self.assertIn(messages.ACQUISITION_FALLBACK, result['tell_user'])
        self.worker.recognize.assert_not_called()

    def test_short_link_resolution_failure_has_task_and_fallback(self):
        with patch.object(media.douyin_browser, 'resolve_url', side_effect=OSError('离线')):
            code, result = self.invoke('quick', 'https://v.douyin.com/example/')
        self.assertEqual(1, code)
        self.assertTrue(result['id'])
        self.assertIn(messages.ACQUISITION_FALLBACK, result['tell_user'])

    def test_parse_and_execution_errors_include_tell_user_and_keep_error(self):
        for args, usage_error in [(('quick',), True), (('quick', '--unknown'), True),
                                  (('quick', '不存在.wav'), False)]:
            with self.subTest(args=args):
                code, result = self.invoke(*args)
                self.assertEqual(1, code)
                self.assertEqual('error', result['status'])
                self.assertTrue(result['error'])
                self.assertEqual(usage_error, '--help' in result['tell_user'])

    def test_parser_raises_a_distinct_usage_error(self):
        with self.assertRaises(messages.UsageError):
            cli.parser().parse_args(['run'])

    def test_execution_errors_do_not_recommend_help(self):
        for error_type in (RuntimeError, ValueError):
            with self.subTest(error_type=error_type), patch.object(workflow, 'run', side_effect=error_type('音频未识别到可用语音')):
                code, result = self.invoke('run', 'dy-test')
                self.assertEqual(1, code)
                self.assertEqual('音频未识别到可用语音', result['error'])
                self.assertEqual('这次没有完成：音频未识别到可用语音。', result['tell_user'])

    def test_quiet_command_keeps_an_empty_tell_user(self):
        with patch.object(workflow, 'export_srt', return_value=[]):
            code, result = self.invoke('export-srt', 'dy-test')
        self.assertEqual(0, code)
        self.assertEqual({'files': [], 'workspace': str(self.root), 'tell_user': ''}, result)

    def test_empty_notices_keep_existing_guidance_and_failure_explanations(self):
        for own in (None, '', '  ', 42):
            self.assertEqual('', messages.tell_user({'tell_user': own})['tell_user'])
        for field in ('hint', 'message', 'next', 'question'):
            with self.subTest(field=field):
                self.assertEqual('请查看疑点清单。', messages.tell_user({field: '请查看疑点清单。'})['tell_user'])
        for status in ('error', 'failed', 'incomplete', 'publish_failed', 'publication_unknown'):
            with self.subTest(status=status):
                self.assertIn('未全部完成', messages.tell_user({'status': status})['tell_user'])

    def test_acquisition_exception_at_json_boundary_has_fallback(self):
        with patch.object(quick, 'run', side_effect=acquisition.AcquisitionError([{'reason': 'http_error'}])):
            code, result = self.invoke('quick', 'https://www.douyin.com/video/123456789')
        self.assertEqual(1, code)
        self.assertIn(messages.ACQUISITION_FALLBACK, result['tell_user'])

    def test_global_model_notice_is_read_only_and_deduplicates_across_commands(self):
        folder = self.parent / 'models' / models.DEFAULT_MODEL
        write_json(folder / 'progress.json', {'state': 'downloading', 'downloaded_bytes': 1, 'total_bytes': 2})
        with patch.object(models, '_alive', return_value=True), \
                patch.object(models, 'status', side_effect=AssertionError('不校验模型')), \
                patch.object(cli.workflow, 'show', return_value={'status': 'awaiting_review', 'tell_user': '有 2 处疑点。'}):
            _, first = self.invoke('show', 'example')
            _, repeat = self.invoke('show', 'example')
        self.assertIn('50.0%', first['tell_user'])
        self.assertEqual('有 2 处疑点。', repeat['tell_user'])
        self.assertEqual(['progress.json'], [p.name for p in folder.iterdir()])

    def test_nested_acquisition_gaps_are_not_lost_or_confused_with_asr_failures(self):
        variants = [{'failures': [{'stage': 'audio'}]}, {'acquisition_gaps': [{'reason': 'http_error'}]},
                    {'items': [{'gaps': [{'error': '校对素材获取失败'}]}]},
                    {'status': 'failed', 'error': '超时'}]
        for result in variants:
            with patch.dict(os.environ, {'CY_SESSION_ID': '' , 'CODEX_THREAD_ID': '', 'CODEX_SESSION_ID': ''}):
                output = messages.tell_user(result, command='inspect-source')
            self.assertIn(messages.ACQUISITION_FALLBACK, output['tell_user'])
        self.assertNotIn(messages.ACQUISITION_FALLBACK, messages.tell_user({
            'status': 'failed', 'failures': [{'stage': 'asr', 'error': '识别失败'}]})['tell_user'])

    def test_existing_tell_user_is_preserved_and_exact_sentences_deduplicated(self):
        own = '有 2 处疑点。请先校对。'
        output = messages.tell_user({'tell_user': own}, update_notice=own + '承意有新版本。')
        self.assertEqual(own + '承意有新版本。', output['tell_user'])

    def test_creator_summary_reads_media_failure_without_treating_asr_error_as_acquisition(self):
        file = self.root / 'runs/creator-test/task.json'
        result = {'counts': {'failed_items': 1}, 'task_file': 'runs/creator-test/task.json'}
        write_json(file, {'items': {'one': {'speech_error': '识别失败'}}})
        self.assertNotIn(messages.ACQUISITION_FALLBACK, messages.tell_user(result, root=self.root)['tell_user'])
        write_json(file, {'items': {'one': {'media_error': '下载失败'}}})
        self.assertIn(messages.ACQUISITION_FALLBACK, messages.tell_user(result, root=self.root)['tell_user'])

    def test_update_notice_is_in_json_once_per_session_even_on_error(self):
        info = {'version': '999.0.0', 'url': 'https://example.invalid/release'}
        with patch.dict(os.environ, {'CY_UPDATE_CHECK': '1'}), \
                patch.object(updates, 'latest_release', return_value=info) as fetch:
            _, first = self.invoke('quick', '不存在.wav')
            _, second = self.invoke('quick', '不存在.wav')
            with patch.dict(os.environ, {'CY_SESSION_ID': 'next-chat'}):
                _, other = self.invoke('quick', '不存在.wav')
        self.assertIn('承意有新版本 999.0.0', first['tell_user'])
        self.assertNotIn('承意有新版本', second['tell_user'])
        self.assertIn('承意有新版本', other['tell_user'])
        fetch.assert_called_once()

    def test_brain_session_argument_and_check_update_share_the_same_claim(self):
        info = {'version': '999.0.0', 'url': 'https://example.invalid/release'}
        with patch.dict(os.environ, {'CY_UPDATE_CHECK': '1'}), patch.object(updates, 'latest_release', return_value=info):
            outputs = []
            for command in (['check-update'], ['quick', '不存在.wav'], ['update']):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(sys, 'argv', ['brain.py', '--workspace', str(self.root), '--session-id', 'explicit-chat', *command]), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    brain.main()
                self.assertEqual('', stderr.getvalue())
                outputs.append(json.loads(stdout.getvalue()))
            self.assertIn('承意有新版本', outputs[0]['tell_user'])
            self.assertNotIn('承意有新版本', outputs[1]['tell_user'])
            self.assertNotIn('承意有新版本', outputs[2]['tell_user'])

    def test_parallel_global_notices_use_one_claim(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(messages.once, ['same', 'same']))
        self.assertEqual(1, sum(claims))

    def test_top_level_brain_error_is_json_with_tell_user(self):
        process = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SKILL_ROOT / 'scripts/brain.py'),
                                  '--workspace', str(SKILL_ROOT), 'quick', str(self.wav)],
                                 capture_output=True, encoding='utf-8', check=False)
        self.assertEqual(1, process.returncode)
        self.assertEqual('', process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual('error', result['status'])
        self.assertIn('工作目录', result['tell_user'])


if __name__ == '__main__':
    __import__('unittest').main()
