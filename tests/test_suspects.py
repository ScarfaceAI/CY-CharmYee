from __future__ import annotations

import io
from unittest.mock import Mock, patch

from support import WorkspaceCase, read_json, write_json
from douyin_text import asr, cli, content, lexicon, local_asr, numbers, suspects, workflow
from douyin_text.common import digest


class PublicFixture:
    spec = {'files': [{'category': 'IT'}, {'category': 'diming'}]}
    status = {'ready': True, 'tell_user': '测试词库已就绪。'}

    def category(self, category):
        return {'diming': {'青岛': 100}, 'IT': {'元器': 50}}.get(category, {})

    def lookup(self, word, categories=None):
        hits = [c for c in (categories or ('IT', 'diming')) if word in self.category(c)]
        return {'common': bool(hits), 'frequency': None, 'categories': [{'category': c} for c in hits]}


class SuspectTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.public = PublicFixture()
        patcher = patch.object(lexicon, 'PublicLexicon', return_value=self.public)
        patcher.start()
        self.addCleanup(patcher.stop)
        display = patch.object(lexicon, 'display_exceptions', return_value=([], {'used': True, 'words': []}))
        display.start()
        self.addCleanup(display.stop)
        self.general = frozenset({'激发', '元气', '对峙', '可以', '发挥'})
        general = patch.object(lexicon, 'general_words', return_value=(self.general, {
            'ready': True, 'status': 'ready', 'tell_user': ''}))
        general.start()
        self.addCleanup(general.stop)

    def vocabulary(self, general=()):
        return suspects.Vocabulary(self.root, public=self.public, general=general)

    def result(self, text, *, start=0, end=10, **values):
        return {'source': {'id': 'test', 'title': '产品', 'author': '作者甲', 'source': 'https://example.test/video'}, 'engine': 'doubao-auc-turbo',
                'created': '2026-11-17T00:00:00+00:00', 'text': text,
                'segments': [{'text': text, 'start': start, 'end': end, **values}]}

    def analyze(self, text, *, context=None, general=(), **values):
        result = self.result(text, **values)
        return suspects.analyze(result, context or result['source'], self.vocabulary(general))

    def recorded(self, raw, reviewed=None, *, account='作者甲', ident='dy-memory'):
        result = self.result(raw)
        result['source']['author'] = account
        item = workflow.write_result(result, self.root / 'outputs' / ident, self.root)
        task = {'id': ident, 'purpose': 'transcribe', 'evidence': '校对这份稿件',
                'status': 'awaiting_review', 'results': [item], 'failures': []}
        workflow.save(task, self.root)
        if reviewed is not None:
            path = self.root / 'runs' / ident / 'review.txt'
            path.write_text(reviewed, encoding='utf-8')
            registered = content.register_content(ident, [path], 'review', self.root)
            self.assertNotIn('尚未写入', registered.get('tell_user', ''))
        return task

    def test_homophone_requires_unknown_source_and_candidate(self):
        lexicon.add('承意', self.root)
        report = self.analyze('成亿，轻岛，陌生词。', context={'title': '青岛的承意产品'})
        self.assertEqual(['成亿', '轻岛'], [r['original'] for r in report['items']])
        self.assertEqual(['承意', '青岛'], [r['candidates'][0]['word'] for r in report['items']])
        self.assertEqual(0, self.analyze('完全无关。')['total'])
        self.assertEqual(0, self.analyze('轻岛。')['total'])
        lexicon.add('成亿', self.root)
        self.assertEqual(0, self.analyze('成亿。')['total'])

    def test_common_word_never_triggers_homophone(self):
        lexicon.add('击发', self.root)
        lexicon.add('对治', self.root)
        report = self.analyze('激发，元气，对峙。', general=lexicon.general_words()[0])
        self.assertEqual(0, report['counts']['phonetic'])
        self.assertTrue({'激发', '元气', '对峙'} <= lexicon.general_words()[0])

    def test_each_context_field_can_nominate_words_including_numbered_hashtags(self):
        for context in ({'title': '#对峙2国服来了'}, {'tags': ['对峙2国服来了']},
                        {'description': '今天聊对峙'}, {'account': '对峙小队'}):
            with self.subTest(context=context):
                report = self.analyze('对之。', context=context, general={'对峙', '国服', '来了'})
                self.assertEqual(1, report['counts']['phonetic'])
                self.assertEqual('对峙', report['items'][0]['candidates'][0]['word'])
                self.assertEqual('本视频上下文', report['items'][0]['candidates'][0]['source'])

    def test_context_candidate_does_not_override_common_word_protection(self):
        report = self.analyze('对峙。', context={'title': '对治'}, general={'对峙', '对治'})
        self.assertEqual(0, report['counts']['phonetic'])

    def test_context_candidates_do_not_leak_across_videos_on_same_vocabulary(self):
        vocabulary = self.vocabulary({'对峙', '游戏'})
        result = self.result('对之。')
        first = suspects.analyze(result, {'title': '对峙2'}, vocabulary)
        second = suspects.analyze(result, {'title': '游戏'}, vocabulary)
        self.assertEqual(1, first['counts']['phonetic'])
        self.assertEqual(0, second['counts']['phonetic'])
        self.assertEqual([], lexicon.list_words(self.root)['words'])

    def test_missing_general_disables_only_phonetics_and_reports_gap_in_show(self):
        lexicon.add('承意', self.root)
        self.recorded('激发。', '击发。')
        parts = ['成亿。', '激发。', '十五天。', '请不吝点赞订阅转发打赏。']
        result = self.result(''.join(parts))
        result['segments'] = [{'text': p, 'raw_text': p, 'start': i * 10, 'end': i * 10 + 5}
                              for i, p in enumerate(parts)]
        workflow.apply_display(result, {'number_conversion': True, 'cjk_spacing': True})
        item = workflow.write_result(result, self.root / 'outputs/missing', self.root)
        workflow.save({'id': 'dy-missing', 'status': 'awaiting_review', 'results': [item]}, self.root)
        status = {'ready': False, 'status': 'downloading', 'tell_user': lexicon.GENERAL_DOWNLOADING}
        with patch.object(lexicon, 'general_words', return_value=(frozenset(), status)):
            done = workflow.recalculate_suspects('dy-missing', self.root)
        self.assertEqual({'phonetic': 0, 'memory': 1, 'number': 1, 'hallucination': 1}, done['suspects']['counts'])
        self.assertIn('通用词表正在后台下载', done['tell_user'])
        self.assertIn('跳过同音疑点', workflow.show('dy-missing', self.root)['tell_user'])
        report = read_json(self.path(done['results'][0]['suspects']['json']))
        self.assertFalse(report['lexicon']['general']['ready'])
        self.assertIn('跳过同音疑点', self.path(done['results'][0]['suspects']['markdown']).read_text(encoding='utf-8'))

    def test_does_not_cut_common_words_or_join_normal_phrases(self):
        lexicon.add('以发', self.root)
        lexicon.add('可以发挥', self.root)
        self.assertEqual(0, self.analyze('可以发挥。', general={'可以', '发挥'})['total'])

    def test_near_sound_relaxes_only_tone(self):
        lexicon.add('承意', self.root)
        self.assertEqual(1, self.analyze('成亿')['counts']['phonetic'])
        self.assertEqual(1, self.analyze('程一')['counts']['phonetic'])
        self.assertEqual(0, self.analyze('陈亿')['counts']['phonetic'])
        self.assertEqual(0, self.analyze('成义啊')['counts']['memory'])

    def test_learned_words_are_candidates_and_protected(self):
        self.recorded('这个原接器平台。', '这个元接器平台。')
        self.assertIn('元接器', {w['word'] for w in lexicon.list_words(self.root)['words']})
        self.assertEqual(1, self.analyze('圆接器')['counts']['phonetic'])
        self.assertEqual(0, self.analyze('元接器')['total'])

    def test_memory_hits_even_when_original_is_common_with_counts(self):
        self.recorded('激发，激发。', '击发，击发。')
        report = self.analyze('激发。', general={'激发'})
        self.assertEqual({'phonetic': 0, 'memory': 1, 'number': 0, 'hallucination': 0}, report['counts'])
        self.assertEqual('击发', report['items'][0]['candidates'][0]['word'])
        self.assertEqual(2, report['items'][0]['candidates'][0]['count'])

    def test_scene_and_can_suggest_are_required_for_both_paths(self):
        self.recorded('激发，的。', '击发，地。')
        report = self.analyze('激发，的。', context={'title': '击发', 'account': '作者乙'})
        self.assertEqual(0, report['total'])
        self.assertEqual(0, self.analyze('的。')['total'])
        lexicon.forget('激发', self.root)
        self.assertEqual(0, self.analyze('激发')['total'])

    def test_relevant_memory_replacement_is_a_phonetic_candidate(self):
        self.recorded('激发。', '击发。')
        self.assertEqual('击发', self.analyze('鸡发')['items'][0]['candidates'][0]['word'])
        self.assertEqual(0, self.analyze('鸡发', context={'account': '作者乙'})['total'])

    def test_memory_english_does_not_match_inside_a_word(self):
        lexicon.add('WorkBuddy', self.root)
        self.recorded('Workbody。', 'WorkBuddy。')
        report = self.analyze('Workbody，MyWorkbody。')
        self.assertEqual(1, report['counts']['memory'])

    def test_number_rows_are_exact_display_edits_not_spacing(self):
        raw = '十五天，二十人，FPS。'
        result = self.result(raw)
        shown, edits = numbers.apply(raw)
        result.update(raw_text=raw, text=shown, display={'segments': [{'segment': 1, 'raw_start': 0, 'changes': edits}]})
        result['segments'][0].update(raw_text=raw, text=shown)
        report = suspects.analyze(result, {}, self.vocabulary())
        expected = [e for e in edits if e['layer'] == 'number_conversion']
        self.assertEqual(expected, [r['display_change'] for r in report['items']])
        for row in report['items']:
            self.assertEqual(row['original'], raw[row['raw_start']:row['raw_end']])
        self.assertEqual(0, self.analyze('15 天，FPS。')['counts']['number'])

    def test_hallucination_boilerplate_and_explicit_silence(self):
        self.assertEqual(1, self.analyze('请不吝点赞订阅转发打赏。')['counts']['hallucination'])
        self.assertEqual(0, self.analyze('不要说请不吝点赞订阅转发打赏。')['counts']['hallucination'])
        for text in ('感谢观看。', '字幕由小明提供。'):
            self.assertEqual(0, self.analyze(text)['total'])
            self.assertEqual(0, self.analyze(text, speech_present=True)['total'])
            self.assertEqual(1, self.analyze(text, speech_present=False)['counts']['hallucination'])
        self.assertEqual(0, self.analyze('感谢观看', no_speech_prob=suspects.NO_SPEECH_PROBABILITY - .01)['total'])
        self.assertEqual(1, self.analyze('感谢观看', no_speech_prob=suspects.NO_SPEECH_PROBABILITY)['total'])
        result = self.result('感谢观看', start=20, end=22)
        result['silence_intervals'] = [{'start': 19, 'end': 23}]
        self.assertEqual(1, suspects.analyze(result, {}, self.vocabulary())['total'])

    def test_repetition_thresholds_in_and_between_segments(self):
        self.assertEqual(0, self.analyze('欢迎收看' * (suspects.REPEAT_MIN_COUNT - 1))['total'])
        self.assertEqual(1, self.analyze('欢迎收看' * suspects.REPEAT_MIN_COUNT)['counts']['hallucination'])
        self.assertEqual(0, self.analyze('哈哈' * suspects.REPEAT_MIN_COUNT)['total'])
        result = self.result('欢迎收看' * 5)
        result['segments'] = [{'text': '欢迎收看', 'start': i * 2, 'end': i * 2 + 2} for i in range(5)]
        report = suspects.analyze(result, {}, self.vocabulary())
        self.assertEqual(1, report['total'])
        self.assertEqual(0, report['items'][0]['start'])
        self.assertEqual(8, report['items'][0]['end'])

    def test_speed_thresholds_and_missing_time(self):
        text = '今天我们讨论一个非常重要的问题并且给大家介绍具体办法'
        units = len(text)
        self.assertEqual(0, self.analyze(text, end=units / suspects.SPEED_MAX_UNITS_PER_SECOND)['total'])
        self.assertEqual(1, self.analyze(text, end=1)['counts']['hallucination'])
        self.assertEqual(0, self.analyze(text, start=None, end=None)['total'])
        self.assertEqual(0, self.analyze(text, end=.5)['total'])
        self.assertEqual(0, self.analyze('欢迎大家', end=1)['total'])

    def test_legacy_recompute_preserves_all_original_hashes_and_review(self):
        task = self.recorded('成亿。', '成亿。', ident='dy-old')
        original_task = workflow.load(task['id'], self.root)
        source_files = [self.path(task['results'][0][key]) for key in ('result', 'text', 'markdown')]
        before = {str(p): digest(p) for p in source_files}
        first = workflow.recalculate_suspects(task['id'], self.root)
        self.assertEqual(0, first['suspects']['total'])
        lexicon.add('承意', self.root)
        second = workflow.recalculate_suspects(task['id'], self.root)
        self.assertEqual(1, second['suspects']['counts']['phonetic'])
        self.assertEqual(before, {str(p): digest(p) for p in source_files})
        self.assertEqual(original_task['review'], second['review'])
        self.assertEqual('doubao-auc-turbo', read_json(source_files[0])['engine'])
        saved = second['results'][0]['suspects']
        markdown = self.path(saved['markdown']).read_text(encoding='utf-8')
        self.assertIn('成亿 → 承意', markdown)
        self.assertIn('有 1 处疑点（人名或术语 1），按下表逐项核对。', markdown)
        self.assertNotIn('先看 suspects.md', markdown)
        self.assertNotIn('suspects', read_json(source_files[0])['output_sha256'])
        self.assertIn('有 1 处疑点', second['tell_user'])
        shown = workflow.show(task['id'], self.root)
        self.assertEqual(second['suspects'], shown['suspects'])
        self.assertEqual(1, shown['tell_user'].count('有 1 处疑点'))

    def test_tampered_source_stops_recompute(self):
        task = self.recorded('正文。')
        self.path(task['results'][0]['text']).write_text('变化', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '原稿已改变'):
            workflow.recalculate_suspects(task['id'], self.root)

    def test_task_aggregates_all_results_and_missing_is_not_zero(self):
        first = self.recorded('成亿。', ident='dy-one')
        second = self.recorded('成亿。', ident='dy-two')
        first['results'].extend(second['results'])
        workflow.save(first, self.root)
        self.assertEqual(2, workflow.show(first['id'], self.root)['suspects']['missing'])
        lexicon.add('承意', self.root)
        done = workflow.recalculate_suspects(first['id'], self.root)
        self.assertEqual(2, done['suspects']['total'])
        self.assertEqual(2, done['suspects']['ready'])
        self.assertIn('有 2 处疑点', done['tell_user'])

    def test_local_run_automatically_generates_hints_without_extra_asr(self):
        write_json(self.root / 'config.json', {**read_json(self.root / 'config.json'), 'asr_engine': 'local', 'chunk_seconds': 300})
        raw = '十五天。'
        result = {**local_asr.specification(), 'text': raw,
                  'segments': [{'start': 0, 'end': 2, 'text': raw, 'raw_text': raw}],
                  'hotwords': None, 'engine': local_asr.ENGINE, 'sherpa_onnx_version': '2.0.1', 'threads': 3,
                  'worker_pid': 123, 'elapsed_seconds': .01, 'recognizer_threads': 3}
        worker = Mock(recognize=Mock(return_value=result))
        with patch.object(local_asr, 'waiting_for_model', return_value=None), \
             patch.object(local_asr, 'Worker', return_value=worker), \
             patch.object(asr, 'recognize', side_effect=AssertionError('不应调用豆包')):
            done = workflow.run(self.task()['id'], self.root)
        self.assertEqual('awaiting_review', done['status'], done.get('failures'))
        self.assertEqual(1, worker.recognize.call_count)
        self.assertEqual(1, done['suspects']['counts']['number'])
        self.assertEqual(1, done['tell_user'].count('有 1 处疑点'))
        self.assertTrue(self.path(done['results'][0]['suspects']['json']).is_file())

    def test_cli_dispatch_and_all_counts_in_tell_user(self):
        for command, name in [('suspects', 'recalculate_suspects'), ('show', 'show')]:
            with patch.object(workflow, name, return_value={'status': 'awaiting_review'}) as call, patch('sys.stdout', io.StringIO()):
                self.assertEqual(0, cli.main([command, 'dy-test']))
                call.assert_called_once_with('dy-test')
        message = suspects.summary({'phonetic': 2, 'memory': 1, 'number': 2, 'hallucination': 1})
        self.assertIn('有 6 处疑点', message)
        self.assertIn('人名或术语 2、纠错记忆 1、数字 2、疑似编造 1', message)
        self.assertIn('没有检测到疑点', suspects.summary(dict.fromkeys(suspects.LABELS, 0)))


if __name__ == '__main__':
    import unittest
    unittest.main()
