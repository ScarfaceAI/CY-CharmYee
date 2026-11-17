from __future__ import annotations

import copy
import io
from pathlib import Path
from unittest.mock import Mock, patch

import support
from support import WorkspaceCase, read_json, write_json
from douyin_text import asr, background, cli, content, local_asr, numbers, subtitles, workflow
from douyin_text.common import config, digest


RAW = ['只有一百八十二毫克，', '腾讯的十八个智能体，手游FPS的上限。']
DISPLAYED = ['只有 182 毫克，', '腾讯的 18 个智能体，手游 FPS 的上限。']


def recognized():
    return {**local_asr.specification(), 'text': ''.join(RAW), 'segments': [
        {'start': i, 'end': i + 1, 'text': text, 'raw_text': text} for i, text in enumerate(RAW)],
        'hotwords': None, 'engine': local_asr.ENGINE, 'sherpa_onnx_version': '2.0.1', 'threads': 3,
        'worker_pid': 123, 'elapsed_seconds': 0.01, 'recognizer_threads': 3}


class DisplayTests(WorkspaceCase):
    def configure(self, **values):
        write_json(self.root / 'config.json', {**read_json(self.root / 'config.json'),
                   'asr_engine': 'local', 'chunk_seconds': 300, **values})

    def local_run(self, task=None):
        worker = Mock(recognize=Mock(side_effect=lambda *a, **k: recognized()))
        with patch.object(local_asr, 'waiting_for_model', return_value=None), \
             patch.object(local_asr, 'Worker', return_value=worker), \
             patch.object(asr, 'recognize', side_effect=AssertionError('不应调用豆包')), \
             patch.object(workflow.credentials, 'load', side_effect=AssertionError('不应读取凭据')):
            done = workflow.run((task or self.task())['id'], self.root)
        self.assertEqual('awaiting_review', done['status'], done.get('failures'))
        return done

    def snapshot(self, folder=None):
        return {p.relative_to(self.root).as_posix(): digest(p)
                for p in (folder or self.root).rglob('*') if p.is_file()}

    def test_local_new_result_and_plain_reviewed_subtitles(self):
        self.configure()
        done = self.local_run()
        item = done['results'][0]
        result = read_json(self.path(item['result']))
        self.assertEqual(''.join(RAW), result['raw_text'])
        self.assertEqual(''.join(DISPLAYED), result['text'])
        self.assertEqual(RAW, [s['raw_text'] for s in result['segments']])
        self.assertEqual(DISPLAYED, [s['text'] for s in result['segments']])
        self.assertEqual(result['text'] + '\n', self.path(item['text']).read_text(encoding='utf-8'))
        self.assertIn('模型原文保存在 result.json', self.path(item['markdown']).read_text(encoding='utf-8'))
        self.assertEqual(result['asr_profile'], local_asr.profile_for(result))
        self.assertEqual(result['asr_profile'], local_asr.profile_for({**result, 'display': {}}))
        display = result['display']
        self.assertTrue(display['number_conversion'])
        self.assertTrue(display['cjk_spacing'])
        self.assertEqual(numbers.RULES_VERSION, display['rules_version'])
        for i, record in enumerate(display['segments']):
            self.assertEqual(i + 1, record['segment'])
            self.assertEqual(numbers.apply(RAW[i])[1], record['changes'])
            self.assertEqual(RAW[i], result['raw_text'][record['raw_start']:record['raw_start'] + len(RAW[i])])
        files = workflow.export_srt(done['id'], self.root)
        plain = Path(files[0]).read_text(encoding='utf-8')
        self.assertIn(DISPLAYED[0], plain)
        self.assertIn('00:00:00,000 --> 00:00:01,000', plain)
        reviewed = self.root / 'review.txt'
        reviewed.write_text(result['text'].replace('只有', '仅有'), encoding='utf-8')
        content.register_content(done['id'], [reviewed], 'review', self.root)
        exported = subtitles.export(done['id'], self.root, reviewed=True)
        self.assertTrue(exported['files'], exported)
        self.assertIn('仅有 182 毫克', Path(exported['files'][0]).read_text(encoding='utf-8'))
        self.assertFalse(exported['skipped'])

    def test_switches_off_keep_raw_verbatim_and_invalid_options_fail_before_asr(self):
        self.configure(number_conversion=False, cjk_spacing=False)
        done = self.local_run()
        result = read_json(self.path(done['results'][0]['result']))
        self.assertEqual(''.join(RAW), result['text'])
        self.assertEqual(result['text'], result['raw_text'])
        self.assertTrue(all(not s['changes'] for s in result['display']['segments']))
        for key in ('number_conversion', 'cjk_spacing'):
            for value in ('false', 0, None):
                self.configure(**{key: value})
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, '布尔值'):
                    workflow.display_options(config(self.root))
                worker = Mock()
                with patch.object(workflow.media, 'extract', side_effect=AssertionError('配置错误不应提取音轨')), \
                     self.assertRaisesRegex(ValueError, '布尔值'):
                    workflow._transcribe({'id': 'invalid-config'}, done, self.root, retry_failed=False, worker=worker)
                worker.recognize.assert_not_called()
                self.configure(number_conversion=False, cjk_spacing=False)

    def test_multichunk_separators_raw_and_unchanged_segments_are_preserved(self):
        result = recognized()
        result['text'] += '\n空白以外不改。'
        result['segments'].append({'start': 2, 'end': 3, 'text': '空白以外不改。', 'raw_text': '空白以外不改。'})
        original = copy.deepcopy(result)
        workflow.apply_display(result, {'number_conversion': True, 'cjk_spacing': True})
        self.assertEqual(original['text'], result['raw_text'])
        self.assertEqual(''.join(DISPLAYED) + '\n空白以外不改。', result['text'])
        self.assertEqual([], result['display']['segments'][-1]['changes'])
        workflow.apply_display(result, {'number_conversion': False, 'cjk_spacing': False})
        self.assertEqual(original['text'], result['text'])

    def test_cache_and_predisplay_local_result_are_byte_unchanged(self):
        self.configure()
        done = self.local_run()
        item = done['results'][0]
        # An authentic pre-display schema: no display or whole-text RAW, segment RAW retained.
        old = read_json(self.path(item['result']))
        old.pop('display')
        old.pop('raw_text')
        old['text'] = ''.join(RAW)
        for segment in old['segments']:
            segment['text'] = segment['raw_text']
        workflow.write_result(old, self.path(item['result']).parent, self.root)
        before = self.snapshot(self.path(item['result']).parent)
        self.configure(number_conversion=False, cjk_spacing=False)
        with patch.object(numbers, 'apply', side_effect=AssertionError('缓存不应重算')):
            reused = self.local_run(self.task())
        self.assertEqual('reused', reused['results'][0]['status'])
        self.assertEqual(before, self.snapshot(self.path(item['result']).parent))
        # Explicit redisplay can use the segment RAW of pre-display local results.
        new = workflow.redisplay(done['id'], self.root, number_conversion=True, cjk_spacing=True)
        self.assertEqual(''.join(DISPLAYED), read_json(self.path(new['results'][0]['result']))['text'])
        self.assertEqual(before, self.snapshot(self.path(item['result']).parent))

    def test_doubao_and_legacy_task_bytes_do_not_depend_on_display_settings(self):
        self.configure(asr_engine='doubao')
        with patch.object(numbers, 'apply', side_effect=AssertionError('豆包不能应用展示层')):
            done = self.recognize_run(self.task())
        item = done['results'][0]
        before = self.snapshot(self.path(item['result']).parent)
        result = read_json(self.path(item['result']))
        self.assertNotIn('display', result)
        self.assertNotIn('raw_text', result)
        done.pop('asr_engine')
        done.pop('asr_profile')
        workflow.save(done, self.root)
        task_bytes = workflow.task_path(done['id'], self.root).read_bytes()
        self.configure(asr_engine='local', number_conversion=True, cjk_spacing=True)
        with patch.object(numbers, 'apply', side_effect=AssertionError('旧任务不能应用展示层')):
            self.assertEqual(done, workflow.run(done['id'], self.root))
        self.assertEqual(task_bytes, workflow.task_path(done['id'], self.root).read_bytes())
        self.assertEqual(before, self.snapshot(self.path(item['result']).parent))

    def test_imported_text_stays_byte_exact_and_redisplay_is_noop(self):
        self.configure()
        source = self.root / 'provided.txt'
        raw = '十五天，616宇宙\n手游FPS的上限。'.encode('utf-8')
        source.write_bytes(raw)
        task = content.plan_text(source, self.root)
        workflow.confirm(task['id'], 'transcribe', '导入文稿', root=self.root)
        with patch.object(numbers, 'apply', side_effect=AssertionError('文本导入不能应用展示层')):
            done = workflow.run(task['id'], self.root)
        self.assertEqual(raw, self.path(done['results'][0]['text']).read_bytes())
        before = self.snapshot()
        outcome = workflow.redisplay(done['id'], self.root)
        self.assertEqual('no_local_result', outcome['status'])
        self.assertEqual(before, self.snapshot())

    def test_redisplay_new_revision_keeps_old_hashes_and_invalidates_review_basis(self):
        self.configure()
        done = self.local_run()
        item = copy.deepcopy(done['results'][0])
        review = self.root / 'review.txt'
        review.write_text(''.join(DISPLAYED), encoding='utf-8')
        registered = content.register_content(done['id'], [review], 'review', self.root)
        before = self.snapshot(self.path(item['result']).parent)
        basis = copy.deepcopy(registered['review'])
        self.configure(cjk_spacing=False)
        with patch.object(local_asr, 'Worker', side_effect=AssertionError('不应识别')):
            new = workflow.redisplay(done['id'], self.root, number_conversion=False)
        result = read_json(self.path(new['results'][0]['result']))
        self.assertEqual(''.join(RAW), result['text'])
        self.assertEqual(''.join(RAW), result['raw_text'])
        self.assertEqual(done['asr_profile'], new['asr_profile'])
        self.assertIn(new['revision'], new['results'][0]['result'])
        self.assertEqual(before, self.snapshot(self.path(item['result']).parent))
        self.assertEqual(basis, new['review'])
        self.assertEqual('stale', content.inspect_content(new, 'review', self.root)[0]['state'])
        self.assertIn('复核', new['tell_user'])
        self.assertEqual(item, new['display_history'][0]['results'][0])
        self.assertEqual(item['result'], result['display']['source_result']['file'])
        self.assertIsNotNone(workflow.cached_result({'id': item['id']}, new, self.root))
        with self.assertRaisesRegex(ValueError, '依据已变化；复核后重新登记'):
            subtitles.export(new['id'], self.root, reviewed=True)
        # Reapply from RAW, never stack new edits onto the prior display.
        second = workflow.redisplay(new['id'], self.root, number_conversion=True, cjk_spacing=True)
        self.assertEqual(''.join(DISPLAYED), read_json(self.path(second['results'][0]['result']))['text'])

    def test_no_local_result_has_no_side_effect_even_on_config_and_lock_files(self):
        for done in (self.task(), self.recognize_run(self.task())):
            before = self.snapshot()
            outcome = workflow.redisplay(done['id'], self.root, number_conversion=False)
            self.assertEqual('no_local_result', outcome['status'])
            self.assertIn('没有', outcome['tell_user'])
            self.assertEqual(before, self.snapshot())

    def test_redisplay_rejects_tampered_original_and_missing_raw(self):
        self.configure()
        done = self.local_run()
        item = done['results'][0]
        path = self.path(item['text'])
        original = path.read_bytes()
        path.write_text('被改过。', encoding='utf-8')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, '原稿已改变'):
            workflow.redisplay(done['id'], self.root)
        self.assertEqual(before, self.snapshot())
        path.write_bytes(original)
        result = read_json(self.path(item['result']))
        result['segments'][0].pop('raw_text')
        write_json(self.path(item['result']), result)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, '缺少逐段模型原文'):
            workflow.redisplay(done['id'], self.root)
        self.assertEqual(before, self.snapshot())

    def test_cli_dispatch_options_and_defaults(self):
        for options, expected in [([], {'number_conversion': None, 'cjk_spacing': None}),
                                  (['--numbers', 'off', '--spacing', 'on'],
                                   {'number_conversion': False, 'cjk_spacing': True})]:
            with patch.object(workflow, 'redisplay', return_value={'status': 'awaiting_review'}) as redisplay, \
                 patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(0, cli.main(['redisplay', 'dy-test', *options]))
            redisplay.assert_called_once_with('dy-test', **expected)

    def test_redisplay_honors_running_task_lock_and_same_text_review_remains_valid(self):
        self.configure()
        done = self.local_run()
        review = self.root / 'review.txt'
        review.write_text(''.join(DISPLAYED), encoding='utf-8')
        content.register_content(done['id'], [review], 'review', self.root)
        before = self.snapshot()
        with background.task_lock(done['id'], self.root), self.assertRaisesRegex(RuntimeError, '另一个任务'):
            workflow.redisplay(done['id'], self.root)
        self.assertEqual(before, self.snapshot())
        new = workflow.redisplay(done['id'], self.root)
        self.assertEqual('valid', content.inspect_content(new, 'review', self.root)[0]['state'])

    def test_redisplay_preserves_prior_delivery_registration_in_history(self):
        self.configure()
        done = self.local_run()
        review = self.root / 'review.txt'
        review.write_text(''.join(DISPLAYED), encoding='utf-8')
        content.register_content(done['id'], [review], 'review', self.root)
        content.deliver(done['id'], 'review', self.root)
        old = workflow.load(done['id'], self.root)
        delivery = copy.deepcopy(old['delivery'])
        checksum = digest(self.path(delivery['file']))
        new = workflow.redisplay(done['id'], self.root, number_conversion=False)
        self.assertNotIn('delivery', new)
        self.assertEqual(delivery, new['display_history'][-1]['delivery'])
        self.assertEqual(checksum, digest(self.path(delivery['file'])))
