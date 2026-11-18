from __future__ import annotations

import copy
from contextlib import nullcontext
import io
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import WorkspaceCase, read_json, write_json
from test_local_asr import recognized
from test_suspects import PublicFixture
from douyin_text import hotwords, lexicon, local_asr, local_asr_worker, suspects, workflow
from douyin_text.common import config, digest


class HotwordTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.public = PublicFixture()
        for name, value in [('PublicLexicon', self.public), ('display_exceptions', ([], {'used': True, 'words': []})),
                            ('general_words', (frozenset({'直接', '元气'}),
                              {'ready': True, 'status': 'ready', 'tell_user': ''})),
                            ('general_frequencies', ({'直接': 1000, '元气': 100},
                              {'ready': True, 'status': 'ready', 'tell_user': ''}))]:
            patched = patch.object(lexicon, name, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)

    def test_selection_sources_scope_exclusion_order_and_limit(self):
        lexicon.add('承意', self.root, note='青岛')
        lexicon.add('孤立词', self.root)
        lexicon.add('单', self.root, note='青岛')
        lexicon.add('我们', self.root, note='青岛')
        lexicon.add('所以', self.root, note='青岛')
        lexicon.add('a,b', self.root, note='青岛')
        imported = self.root / '词表.txt'
        imported.write_text('WorkBuddy\n', encoding='utf-8')
        lexicon.import_words(imported, self.root)
        state = lexicon._state(self.root)
        lexicon._add(state, '元接器', {'kind': 'review', 'context': {'account': '甲'}, 'source': '校对'})
        lexicon._save(self.root, state)
        memory = {'original': '错词', 'replacement': '正词', 'count': 2, 'can_suggest': True,
                  'requires_context': True, 'occurrences': [{'time': '2026-11-17T00:00:00+00:00',
                                                          'context': {'account': '甲'}}]}
        blocked = {**memory, 'replacement': '禁用词', 'can_suggest': False}
        unrelated = {**memory, 'replacement': '异地词', 'occurrences': [
            {'time': '2026-11-17T00:00:00+00:00', 'context': {'account': '乙'}}]}
        with patch.object(lexicon, 'memories', return_value=[memory, blocked, unrelated]):
            rows = hotwords.select({'title': '青岛 WorkBuddy 智能', 'account': '甲'}, self.root,
                                    public=self.public, general={'智能'}, limit=30)
            small = hotwords.select({'title': '青岛 WorkBuddy 智能', 'account': '甲'}, self.root,
                                     public=self.public, general={'智能'}, limit=2)
        words = {row['word']: row for row in rows}
        self.assertEqual({'承意', 'WorkBuddy', '元接器', '青岛', '智能', '正词'}, set(words))
        self.assertEqual('manual', words['承意']['sources'][0]['origins'][0]['kind'])
        self.assertEqual('import', words['WorkBuddy']['sources'][0]['origins'][0]['kind'])
        self.assertEqual('review', words['元接器']['sources'][0]['origins'][0]['kind'])
        self.assertEqual('纠错记忆', words['正词']['sources'][0]['source'])
        self.assertEqual('THUOCL', words['青岛']['sources'][0]['source'])
        self.assertEqual('本视频上下文', words['智能']['sources'][0]['source'])
        self.assertEqual(sorted(rows, key=lambda r: (r['tier'], -r['score'], r['word'])), rows)
        self.assertEqual(rows[:2], small)
        self.assertEqual([], hotwords.select({}, self.root, limit=0))
        for limit in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                hotwords.select({}, self.root, limit=limit)

    def test_prescreen_fuzzy_and_exact_presence(self):
        for raw, word in [('直接', '字节'), ('蓝山', '南山'), ('飞行', '黑行'), ('城意', '陈意'),
                          ('参议', '仓益'), ('新衣', '星移'), ('成亿', '承意'), ('粗词', '出词')]:
            with self.subTest(raw=raw):
                self.assertTrue(hotwords.prescreen(raw, [{'word': word}]))
        self.assertFalse(hotwords.prescreen('今天有雨', [{'word': '承意'}]))
        self.assertEqual([{'word': '承意', 'start': 3, 'end': 5, 'original': '成亿'}],
                         hotwords.prescreen('承意和成亿', [{'word': '承意'}]))
        self.assertFalse(hotwords.prescreen('承意', [{'word': '承意'}]))

    def test_english_screen_ignores_case_space_hyphen_and_width(self):
        selected = [{'word': 'WorkBuddy'}]
        self.assertTrue(hotwords.prescreen('使用 work-body。', selected))
        self.assertFalse(hotwords.prescreen('使用 ＷＯＲＫ-ＢＵＤＤＹ。', selected))
        self.assertFalse(hotwords.prescreen('使用 Blender。', selected))
        self.assertEqual(['work-body'], [h['original'] for h in hotwords.prescreen('WorkBuddy 和 work-body', selected)])

    def test_compare_one_character_difference_and_unrelated_changes(self):
        result = hotwords.compare('原器和元接器，真的。', '元器和原接器，真的！', ['元器'])
        self.assertEqual([{'start': 0, 'end': 2, 'original': '原器', 'replacement': '元器', 'word': '元器'}], result['hits'])
        self.assertEqual(2, result['other_changes'])
        self.assertEqual(1, result['formatting_changes'])
        self.assertEqual(['原', '元'], [r['original'] for r in result['changes']])
        self.assertEqual([True, False], [r['hotword_hit'] for r in result['changes']])

    def test_common_word_guard_rejects_fuzzy_initials_and_allows_one_vowel_gap(self):
        self.assertEqual([], hotwords.compare('直接', '字节', ['字节'], {'直接'})['hits'])
        self.assertEqual(1, hotwords.compare('直接', '字节', ['字节'], {'直接'})['other_changes'])
        self.assertEqual('元气', hotwords.compare('元气', '元器', ['元器'], {'元气'})['hits'][0]['original'])
        self.assertEqual(1, len(hotwords.compare('直接', '字节', ['字节'], set())['hits']))
        self.assertEqual(1, len(hotwords.compare('自己', '字节', ['字节'], {'自己'})['hits']))
        self.assertEqual([], hotwords.compare('直接', '字节', ['字节'], None)['hits'])
        self.assertTrue(hotwords.prescreen('自己', [{'word': '字节'}]))
        self.assertFalse(hotwords.conservative_sounds(('zi', 'ji'), ('zhi', 'jie')))
        self.assertFalse(hotwords.conservative_sounds(('li', 'ji'), ('lie', 'jie')))

    def test_adjacent_unrelated_change_does_not_hide_inside_hotword_block(self):
        result = hotwords.compare('原气型号', '元器版本', ['元器'])
        self.assertEqual('原气', result['hits'][0]['original'])
        self.assertEqual(1, result['other_changes'])
        self.assertEqual('型号', result['changes'][1]['original'])
        self.assertFalse(result['changes'][1]['hotword_hit'])

    def test_alignment_pairs_similar_sounds_instead_of_shifting_the_hotword(self):
        # Real clip: a flat substitution cost aligned this as 己也 -> 字节 with 自 deleted.
        result = hotwords.compare('你就想吧，自己也惹场。', '你就想吧，字节额场。', ['字节'], general={'自己': 90933})
        self.assertEqual([('自己', '字节')], [(h['original'], h['replacement']) for h in result['hits']])
        self.assertEqual([('自己', '字节', True), ('也惹', '额', False)],
                         [(c['original'], c['replacement'], c['hotword_hit']) for c in result['changes']])

    def test_comparison_normalization_and_offsets(self):
        self.assertEqual([], hotwords.compare('ＡＢＣ，元 器。', 'abc 元器！', ['元器'])['hits'])
        hit = hotwords.compare('这，成 亿。', '这：承意！', ['承意'])['hits'][0]
        self.assertEqual((2, 5, '成 亿'), (hit['start'], hit['end'], hit['original']))
        self.assertEqual([], hotwords.compare('其他字', '不同字', ['承意'])['hits'])

    def recognize(self, texts, selected, *, parts=3):
        import numpy as np
        streams = [Mock(result=SimpleNamespace(text=text)) for text in texts]
        recognizer = Mock(create_stream=Mock(side_effect=streams))
        with patch.object(local_asr, 'vad_new'), patch.object(local_asr, 'vad_segments',
                return_value=[(i * 16000, np.ones(16000, dtype=np.float32)) for i in range(parts)]), \
             patch.object(local_asr, 'create', return_value=recognizer) as create, \
             patch.object(local_asr.time, 'monotonic', side_effect=range(100)):
            result = local_asr.recognize(self.wav, {'silero_vad': 'vad'}, 2, 'profile', hotwords=selected)
        if parts:
            create.assert_called_once()
        else:
            create.assert_not_called()
        return result, streams, recognizer

    def test_only_matching_segments_and_segment_specific_words(self):
        result, streams, recognizer = self.recognize(['成亿。', '天气很好。', '原器。', '承意。', '元器。'],
                                                     [{'word': '承意'}, {'word': '元器'}])
        self.assertEqual(5, recognizer.decode_stream.call_count)
        for stream in streams[:3]:
            stream.set_option.assert_not_called()
        streams[3].set_option.assert_called_once_with('hotwords', '承意')
        streams[4].set_option.assert_called_once_with('hotwords', '元器')
        self.assertEqual('成亿。天气很好。原器。', result['text'])
        self.assertEqual(['成亿。', '天气很好。', '原器。'], [r['raw_text'] for r in result['segments']])
        self.assertNotIn('hotword_review', result['segments'][1])
        self.assertGreater(result['hotword_seconds'], 0)

    def test_no_words_or_silence_has_no_second_pass(self):
        result, streams, recognizer = self.recognize(['成亿。'] * 3, [])
        self.assertEqual(3, recognizer.decode_stream.call_count)
        self.assertEqual(0, result['hotword_seconds'])
        result, _, recognizer = self.recognize([], [{'word': '承意'}], parts=0)
        recognizer.decode_stream.assert_not_called()
        self.assertEqual([], result['segments'])

    def test_transcripts_hash_and_profile_unchanged_and_suspects_recompute(self):
        base = {**recognized('成亿。'), 'source': {'id': 'test', 'title': '承意', 'source': '本地', 'author': '甲'},
                'created': '2026-11-17T00:00:00+00:00'}
        candidate = copy.deepcopy(base)
        candidate['hotwords'] = [{'word': '承意', 'sources': [{'source': '个人词库'}]}]
        hit = hotwords.locate_hit(hotwords.compare('成亿。', '承意。', ['承意'])['hits'][0], candidate['segments'], 1)
        candidate['hotword_reviews'] = [{'text': '承意。', 'hits': [hit]}]
        first = workflow.write_result(base, self.root / 'outputs/first', self.root)
        second = workflow.write_result(candidate, self.root / 'outputs/second', self.root)
        self.assertEqual(base['output_sha256'], candidate['output_sha256'])
        self.assertEqual(local_asr.profile_for(base), local_asr.profile_for(candidate))
        for suffix in ('text', 'markdown'):
            self.assertEqual(self.path(first[suffix]).read_bytes(), self.path(second[suffix]).read_bytes())
        workflow.save({'id': 'dy-hotword', 'status': 'awaiting_review', 'results': [second]}, self.root)
        before = digest(self.path(second['result']))
        done = workflow.recalculate_suspects('dy-hotword', self.root)
        self.assertEqual(1, done['suspects']['counts']['hotword'])
        self.assertEqual(before, digest(self.path(second['result'])))
        self.assertIn('热词两遍不一致 1', done['tell_user'])
        self.assertEqual(0, first['suspects']['counts']['hotword'])

    def test_old_four_type_report_still_shows_without_recomputing(self):
        result = {**recognized(), 'source': {'id': 'old', 'title': '旧稿', 'source': '本地', 'author': '甲'},
                  'created': '2026-11-17T00:00:00+00:00'}
        item = workflow.write_result(result, self.root / 'outputs/old', self.root)
        report_path = self.path(item['suspects']['json'])
        report = read_json(report_path)
        del report['counts']['hotword']
        write_json(report_path, report)
        workflow.save({'id': 'dy-old', 'status': 'awaiting_review', 'results': [item]}, self.root)
        self.assertEqual(0, workflow.show('dy-old', self.root)['suspects']['counts']['hotword'])

    def test_phonetic_overlap_merges_into_one_row_with_both_reasons(self):
        lexicon.add('承意', self.root)
        result = recognized('成亿。')
        hit = hotwords.locate_hit(hotwords.compare('成亿。', '承意。', ['承意'])['hits'][0], result['segments'], 1)
        result['hotword_reviews'] = [{'hits': [hit]}]
        report = suspects.analyze(result, {}, suspects.Vocabulary(self.root, public=self.public, general=set()))
        self.assertEqual(1, report['total'])
        self.assertEqual(1, report['counts']['hotword'])
        self.assertEqual(0, report['counts']['phonetic'])
        row = report['items'][0]
        self.assertEqual(['hotword', 'phonetic'], row['merged_types'])
        self.assertEqual(2, len(row['reasons']))
        self.assertEqual('成亿', row['sentence'][row['offset_start']:row['offset_end']])

    def test_disabled_workflow_and_completed_chunks_do_not_listen_again(self):
        # Off by default: in the benchmark, re-listening with hotwords never fixed anything.
        write_json(self.root / 'config.json', {**read_json(self.root / 'config.json'), 'asr_engine': 'local'})
        task = self.task()
        worker = Mock(recognize=Mock(side_effect=[recognized(), RuntimeError('中断')]))
        with patch.object(local_asr, 'waiting_for_model', return_value=None), \
             patch.object(local_asr, 'Worker', return_value=worker), patch.object(hotwords, 'select') as select:
            workflow.run(task['id'], self.root)
        select.assert_not_called()
        self.assertTrue(all(c.kwargs['hotwords'] == [] for c in worker.recognize.call_args_list))
        job = read_json(workflow.task_path(task['id'], self.root).parent / 'jobs.json')[0]
        task = workflow.load(task['id'], self.root)
        path = self.root / '.cache' / job['id'] / workflow.version_for(task) / 'chunk-00000.json'
        before = path.read_bytes()
        write_json(self.root / 'config.json', {**config(self.root), 'asr_hotwords': True})
        worker.recognize.side_effect = lambda *a, **k: recognized()
        worker.recognize.reset_mock()
        with patch.object(local_asr, 'waiting_for_model', return_value=None), \
             patch.object(local_asr, 'Worker', return_value=worker), \
             patch.object(hotwords, 'select', return_value=[{'word': '承意'}]):
            done = workflow.run(task['id'], self.root)
        self.assertEqual('awaiting_review', done['status'], done.get('failures'))
        self.assertEqual(3, worker.recognize.call_count)
        self.assertEqual(before, path.read_bytes())

    def test_worker_excludes_hotword_work_from_calibration(self):
        request_file = self.root / 'request.json'
        response_file = self.root / 'response.json'
        spec = local_asr.specification()
        write_json(request_file, {'audio': str(self.wav), 'threads': 2, 'profile': spec['asr_profile'],
            'queue_timeout': 1, 'hotwords': [{'word': '承意'}], 'response_file': str(response_file),
            'progress_file': str(self.root / 'progress.json')})
        result = {**recognized(), 'model_load_seconds': 2, 'hotword_seconds': 5}
        with patch.object(local_asr_worker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO((str(request_file) + '\n').encode()))), \
             patch.object(local_asr, 'recognition_lock', return_value=nullcontext()), \
             patch.object(local_asr_worker.models, 'ready_paths', return_value={}), \
             patch.object(local_asr, 'recognize', return_value=result) as recognize, \
             patch.object(local_asr, 'calibrate', return_value={}) as calibrate, \
             patch.object(local_asr_worker.time, 'monotonic', side_effect=[0, 10, 20, 21]), \
             patch.dict(sys.modules, {'sherpa_onnx': SimpleNamespace(__version__='2.0.1')}):
            local_asr_worker.main()
        self.assertEqual(3, calibrate.call_args.args[3])
        self.assertEqual([{'word': '承意'}], recognize.call_args.kwargs['hotwords'])
        outcome = read_json(response_file)['result']
        self.assertEqual(3, outcome['recognition_seconds'])
        self.assertEqual(5, outcome['hotword_seconds'])

    def test_worker_request_preserves_sources_and_respects_disabled_config(self):
        selected = [{'word': '承意', 'score': 4, 'sources': [{'source': '个人词库'}]}]
        requests = []

        def respond(line):
            from pathlib import Path
            request = read_json(Path(line.decode().strip()))
            requests.append(request)
            write_json(Path(request['response_file']), {'result': recognized()})

        worker = object.__new__(local_asr.Worker)
        worker.process = Mock(stdin=Mock(write=Mock(side_effect=respond)))
        cfg = config(self.root)
        self.assertIs(False, cfg['asr_hotwords'])
        self.assertEqual(hotwords.DEFAULT_LIMIT, cfg['asr_hotwords_limit'])
        worker.recognize(self.wav, self.root, {**cfg, 'asr_hotwords': True}, 'profile', hotwords=selected)
        worker.recognize(self.wav, self.root, cfg, 'profile', hotwords=selected)
        self.assertEqual(selected, requests[0]['hotwords'])
        self.assertEqual([], requests[1]['hotwords'])

    def test_tiers_keep_user_evidence_ahead_of_public_and_context_terms(self):
        context = {'title': '自己 开始 青岛 元器 冷僻词 WorkBuddy'}
        words = [{'kind': 'word', 'word': '自己', 'score': .01, 'origins': [{'kind': 'manual'}]},
                 {'kind': 'word', 'word': 'WorkBuddy', 'score': .02, 'origins': [{'kind': 'import'}]}]
        frequencies = {'自己': 90000, '开始': 38000, '青岛': 500, '元器': 100, '冷僻词': 3}
        with patch.object(lexicon, 'query', return_value={'results': words}), patch.object(hotwords, 'COMMON_WORD_RANK', 2):
            rows = hotwords.select(context, self.root, public=self.public, general=frequencies)
            limited = hotwords.select(context, self.root, public=self.public, general=frequencies, limit=2)
        self.assertEqual(['WorkBuddy', '自己', '元器', '青岛', '冷僻词'], [r['word'] for r in rows])
        self.assertEqual([1, 1, 2, 2, 3], [r['tier'] for r in rows])
        self.assertEqual(['WorkBuddy', '自己'], [r['word'] for r in limited])
        self.assertNotIn('开始', [r['word'] for r in rows])

    def test_daily_filter_applies_to_public_and_context_but_not_user_layer(self):
        with patch.object(hotwords, 'COMMON_WORD_RANK', 2):
            rows = hotwords.select({'title': '青岛 元器 智能'}, self.root, public=self.public,
                                  general={'青岛': 100, '智能': 90, '元器': 1})
        self.assertEqual(['元器'], [r['word'] for r in rows])
        lexicon.add('青岛', self.root)
        with patch.object(hotwords, 'COMMON_WORD_RANK', 2):
            rows = hotwords.select({'title': '青岛 元器 智能'}, self.root, public=self.public,
                                  general={'青岛': 100, '智能': 90, '元器': 1})
        self.assertEqual(['青岛', '元器'], [r['word'] for r in rows])
        self.assertEqual(1, rows[0]['tier'])

    def test_pure_hotword_insertions_are_other_changes(self):
        for first, second in [('开始。', '开始承意。'), ('', '承意'), ('今天结束', '今天承意结束')]:
            with self.subTest(first=first):
                compared = hotwords.compare(first, second, ['承意'])
                self.assertEqual([], compared['hits'])
                self.assertGreater(compared['other_changes'], 0)
                self.assertTrue(all(not c['hotword_hit'] for c in compared['changes']))

    def test_vad_group_combines_short_segments_and_uses_the_whole_audio(self):
        import numpy as np
        streams = [Mock(result=SimpleNamespace(text=text)) for text in
                   ['自己这一系列。', '字节旗下。', '结束。', '无关。', '字节这一系列。字节旗下。结束。']]
        recognizer = Mock(create_stream=Mock(side_effect=streams))
        def split(start, samples):
            if len(samples) == 22 * 16000:
                return [(start, samples[:7*16000]), (start+7*16000, samples[7*16000:17*16000]),
                        (start+17*16000, samples[17*16000:])]
            return [(start, samples)]
        with patch.object(local_asr, 'vad_new'), patch.object(local_asr, 'vad_segments', return_value=[
                (320, np.ones(22*16000)), (30*16000, np.ones(3*16000))]), \
             patch.object(local_asr, 'split_segment', side_effect=split), \
             patch.object(local_asr, 'create', return_value=recognizer):
            result = local_asr.recognize(self.wav, {'silero_vad': 'vad'}, 2, 'profile', hotwords=[{'word': '字节'}])
        self.assertEqual(5, recognizer.decode_stream.call_count)
        for stream in streams[:4]:
            stream.set_option.assert_not_called()
        streams[4].set_option.assert_called_once_with('hotwords', '字节')
        self.assertEqual(22*16000, len(streams[4].accept_waveform.call_args.args[1]))
        self.assertEqual(['自己这一系列。', '字节旗下。', '结束。', '无关。'], [s['raw_text'] for s in result['segments']])
        self.assertEqual([(.02, 7.02), (7.02, 17.02), (17.02, 22.02), (30, 33)],
                         [(s['start'], s['end']) for s in result['segments']])
        self.assertEqual(1, len(result['hotword_reviews']))
        review = result['hotword_reviews'][0]
        self.assertEqual([1, 2, 3], review['segments'])
        self.assertEqual([{'segment': 1, 'start': 0, 'end': 2}], review['hits'][0]['spans'])
        self.assertEqual('自己', review['hits'][0]['original'])
        self.assertEqual(1, len(review['windows']))
        self.assertTrue(all('hotword_review' not in s for s in result['segments']))

    def test_long_group_falls_back_to_neighbors_within_the_limit(self):
        segments = [{'start': i*10, 'end': (i+1)*10, 'raw_text': '成亿'} for i in range(6)]
        screen = [{'start': 4, 'end': 6, 'word': '承意'}]
        self.assertEqual([(1, 3)], hotwords.review_windows(segments, screen))
        screen += [{'start': 10, 'end': 12, 'word': '承意'}]
        self.assertEqual([(1, 3), (4, 6)], hotwords.review_windows(segments, screen))
        for a, b in hotwords.review_windows(segments, screen):
            self.assertLessEqual(segments[b-1]['end']-segments[a]['start'], hotwords.REVIEW_MAX_SECONDS)
        wide = [{'start': 0, 'end': 15, 'raw_text': '成'}, {'start': 15, 'end': 30, 'raw_text': '亿'}]
        self.assertEqual([(0, 1), (1, 2)], hotwords.review_windows(wide, [{'start': 0, 'end': 2}]))

    def test_long_vad_actual_streams_only_get_each_windows_audio_and_words(self):
        import numpy as np
        samples = np.arange(50*16000, dtype=np.float32)
        texts = ['成亿', '天气', '安静', '普通', '原器', '承意天气', '普通元器']
        streams = [Mock(result=SimpleNamespace(text=t)) for t in texts]
        recognizer = Mock(create_stream=Mock(side_effect=streams))
        with patch.object(local_asr, 'vad_new'), \
             patch.object(local_asr, 'vad_segments', return_value=[(0, samples)]), \
             patch.object(local_asr, 'split_segment', return_value=[(i*160000, samples[i*160000:(i+1)*160000]) for i in range(5)]), \
             patch.object(local_asr, 'create', return_value=recognizer):
            result = local_asr.recognize(self.wav, {'silero_vad': 'vad'}, 2, 'profile',
                                         hotwords=[{'word': '承意'}, {'word': '元器'}])
        self.assertEqual('成亿天气安静普通原器', result['text'])
        self.assertEqual(7, recognizer.decode_stream.call_count)
        streams[5].set_option.assert_called_once_with('hotwords', '承意')
        streams[6].set_option.assert_called_once_with('hotwords', '元器')
        np.testing.assert_array_equal(samples[:20*16000], streams[5].accept_waveform.call_args.args[1])
        np.testing.assert_array_equal(samples[30*16000:], streams[6].accept_waveform.call_args.args[1])
        review = result['hotword_reviews'][0]
        self.assertEqual([[1, 2], [4, 5]], [w['segments'] for w in review['windows']])
        self.assertEqual([1, 5], [h['spans'][0]['segment'] for h in review['hits']])
        for hit in review['hits']:
            self.assertEqual(hit['original'], review['raw_text'][hit['start']:hit['end']])

    def test_multiple_wav_chunks_shift_review_times_and_segment_positions(self):
        write_json(self.root / 'config.json', {**config(self.root), 'asr_engine': 'local', 'asr_hotwords': True})
        raw = '成亿。'
        item = recognized(raw)
        hit = hotwords.locate_hit(hotwords.compare(raw, '承意。', ['承意'])['hits'][0], item['segments'], 1)
        window = {'start': 0, 'end': .1, 'segments': [1], 'raw_start': 0, 'raw_end': 3,
                  'raw_text': raw, 'text': '承意。', 'hits': [hit], 'words': ['承意'], 'seconds': .02}
        review = {'start': 0, 'end': .1, 'segments': [1], 'hits': [hit], 'windows': [window]}
        item['hotword_reviews'] = [review]
        original = copy.deepcopy(item)
        worker = Mock(recognize=Mock(side_effect=lambda *a, **k: copy.deepcopy(item)))
        with patch.object(local_asr, 'waiting_for_model', return_value=None), \
             patch.object(local_asr, 'Worker', return_value=worker), \
             patch.object(hotwords, 'select', return_value=[{'word': '承意'}]):
            done = workflow.run(self.task()['id'], self.root)
        self.assertEqual('awaiting_review', done['status'], done.get('failures'))
        result = read_json(self.path(done['results'][0]['result']))
        self.assertEqual(4, len(result['hotword_reviews']))
        for i, review in enumerate(result['hotword_reviews']):
            self.assertEqual([i+1], review['segments'])
            self.assertEqual(i+1, review['hits'][0]['spans'][0]['segment'])
            self.assertEqual(i+1, review['windows'][0]['hits'][0]['spans'][0]['segment'])
            # Media chunk boundaries follow actual sample offsets, not the
            # requested integer duration (FFmpeg may produce 1.024 s chunks).
            self.assertEqual(result['segments'][i]['start'], review['start'])
            self.assertEqual(result['segments'][i]['end'], review['windows'][0]['end'])
        self.assertEqual(original, item)
        report = read_json(self.path(done['results'][0]['suspects']['json']))
        self.assertEqual(4, report['counts']['hotword'])
        for row in report['items']:
            self.assertEqual('成亿', result['raw_text'][row['raw_start']:row['raw_end']])

    def test_cross_short_segment_hit_is_anchored_once_with_full_span_and_sentence(self):
        segments = [{'start': 0, 'end': 10, 'text': '这里成', 'raw_text': '这里成'},
                    {'start': 10, 'end': 20, 'text': '亿出现', 'raw_text': '亿出现'}]
        raw = ''.join(s['raw_text'] for s in segments)
        screen = hotwords.prescreen(raw, [{'word': '承意'}])
        self.assertEqual([{'word': '承意', 'start': 2, 'end': 4, 'original': '成亿'}], screen)
        self.assertEqual([(0, 2)], hotwords.review_windows(segments, screen))
        hit = hotwords.locate_hit(hotwords.compare(raw, '这里承意出现', ['承意'])['hits'][0], segments, 1)
        self.assertEqual([{'segment': 1, 'start': 2, 'end': 3}, {'segment': 2, 'start': 0, 'end': 1}], hit['spans'])
        result = {'text': raw, 'raw_text': raw, 'segments': segments, 'hotword_reviews': [{'hits': [hit]}]}
        report = suspects.analyze(result, {}, suspects.Vocabulary(self.root, public=self.public, general={'这里', '出现'}))
        self.assertEqual(1, report['total'])
        row = report['items'][0]
        self.assertEqual((1, [1, 2], 0, 20), (row['segment'], row['covered_segments'], row['start'], row['end']))
        self.assertEqual('成亿', row['sentence'][row['offset_start']:row['offset_end']])
        self.assertEqual('成亿', raw[row['raw_start']:row['raw_end']])

    def test_group_review_preserves_hashes_and_merges_phonetic_evidence(self):
        lexicon.add('承意', self.root)
        base = {**recognized('成亿。'), 'source': {'id': 'group', 'title': '承意', 'source': '本地', 'author': '甲'},
                'created': '2026-11-17T00:00:00+00:00'}
        candidate = copy.deepcopy(base)
        hit = hotwords.locate_hit(hotwords.compare('成亿。', '承意。', ['承意'])['hits'][0], base['segments'], 1)
        candidate['hotword_reviews'] = [{'hits': [hit]}]
        workflow.write_result(base, self.root / 'outputs/plain', self.root)
        item = workflow.write_result(candidate, self.root / 'outputs/group', self.root)
        self.assertEqual(base['output_sha256'], candidate['output_sha256'])
        report = read_json(self.path(item['suspects']['json']))
        self.assertEqual(1, report['counts']['hotword'])
        self.assertEqual(0, report['counts']['phonetic'])
        self.assertEqual(['hotword', 'phonetic'], report['items'][0]['merged_types'])
        self.assertEqual(2, len(report['items'][0]['reasons']))


class FrequencyCacheTests(WorkspaceCase):
    def test_select_and_multiple_audio_chunks_share_one_verified_read(self):
        import numpy as np
        data = '直接 1000 d\n元气 100 n\n承意 1 nz\n'.encode()
        path = self.root / 'dict.txt'
        path.write_bytes(data)
        spec = {'id': 'jieba', 'version': 'fixture', 'revision': 'fixed',
                'files': [{'category': 'general', 'path': 'dict.txt', 'size': len(data),
                           'sha256': hashlib.sha256(data).hexdigest()}]}
        read_bytes = Path.read_bytes
        reads = []
        def read(p):
            if p == path:
                reads.append(p)
            return read_bytes(p)
        recognizer = Mock(create_stream=Mock(side_effect=lambda: Mock(result=SimpleNamespace(text='成亿'))))
        with patch.object(lexicon, 'general_manifest', return_value=spec), \
             patch.object(lexicon, 'public_folder', return_value=self.root), \
             patch.object(lexicon.general_frequencies, '_cache', {}, create=True), \
             patch.object(Path, 'read_bytes', read), \
             patch.object(local_asr, 'vad_new'), \
             patch.object(local_asr, 'vad_segments', return_value=[(0, np.ones(16000))]), \
             patch.object(local_asr, 'create', return_value=recognizer):
            lexicon.add('承意', self.root)
            selected = hotwords.select({'title': '承意'}, self.root, public=PublicFixture())
            self.assertEqual(['承意'], [r['word'] for r in selected])
            for _ in range(2):
                local_asr.recognize(self.wav, {'silero_vad': 'vad'}, 2, 'profile', hotwords=selected)
            self.assertEqual([path], reads)
            freq, status = lexicon.general_frequencies()
            self.assertTrue(status['ready'])
            self.assertEqual(1000, freq['直接'])
            with self.assertRaises(TypeError):
                freq['直接'] = 0
            updated = data.replace(b'1000', b'2000')
            path.write_bytes(updated)
            spec['files'][0]['sha256'] = hashlib.sha256(updated).hexdigest()
            self.assertEqual(2000, lexicon.general_frequencies()[0]['直接'])
            self.assertEqual([path, path], reads)

    def test_bad_dictionary_hash_is_not_cached(self):
        path = self.root / 'dict.txt'
        path.write_text('错误 2 n\n', encoding='utf-8')
        data = '正确 2 n\n'.encode()
        spec = {'id': 'jieba', 'version': 'fixture', 'revision': 'fixed',
                'files': [{'category': 'general', 'path': 'dict.txt', 'size': len(data),
                           'sha256': hashlib.sha256(data).hexdigest()}]}
        with patch.object(lexicon, 'general_manifest', return_value=spec), \
             patch.object(lexicon, 'public_folder', return_value=self.root), \
             patch.object(lexicon.general_frequencies, '_cache', {}, create=True):
            self.assertFalse(lexicon.general_frequencies()[1]['ready'])
            path.write_bytes(data)
            self.assertEqual({'正确': 2}, lexicon.general_frequencies()[0])


if __name__ == '__main__':
    import unittest
    unittest.main()
