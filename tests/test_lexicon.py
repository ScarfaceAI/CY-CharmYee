from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from support import SKILL_ROOT
from test_models import server
from douyin_text import cli, content, lexicon, models, workflow
from douyin_text.common import digest, read_json, write_json


class LexiconTests(unittest.TestCase):
    def setUp(self):
        base = SKILL_ROOT / '.scratch/m1-lexicon'
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base, prefix='test-')
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name).resolve()
        self.root = self.parent / 'workspace'
        self.root.mkdir()
        write_json(self.root / 'config.json', {'vault_root': None})
        self.env = patch.dict(os.environ, {'CY_MODEL_DIR': str(self.parent / 'models'),
                                          'CY_STATE_DIR': str(self.parent / 'state'), 'CY_UPDATE_CHECK': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.spec = copy.deepcopy(lexicon.manifest())
        self.payloads = {}
        tables = {'IT': '算法\t 20\n人工智能\t100\n', 'chengyu': '十年生聚\t60\n',
                  'diming': '北京\t80\n\n柴塔村\t3?\n', 'lishimingren': '李白\t40\n',
                  'law': '著作权法\t20\n未知词频\t\n', 'food': '丁香\t125472s\n扎啤\t300\n',
                  'medical': '元气\t20000\n激发\t300\n击发\t200\n', 'caijing': '对峙\t300\n'}
        for f in self.spec['files'] + [self.spec['license_file']]:
            data = (tables.get(f.get('category'), '词库测试词\t1\n') if f['path'] != 'LICENSE' else 'MIT test license\n').encode()
            f.update(size=len(data), sha256=hashlib.sha256(data).hexdigest(), sources=['http://127.0.0.1:1/' + f['path']])
            self.payloads[f['path']] = data
        self.manifest = self.parent / 'lexicons.json'
        write_json(self.manifest, self.spec)
        self.manifest_patch = patch.object(lexicon, 'MANIFEST', self.manifest)
        self.manifest_patch.start()
        self.addCleanup(self.manifest_patch.stop)
        self.auto_patch = patch.object(lexicon, 'ensure_public', side_effect=self.offline_public)
        self.auto_patch.start()
        self.addCleanup(self.auto_patch.stop)

    def offline_public(self):
        status = lexicon.public_status()
        return status if status['ready'] else {**status, 'status': 'downloading', 'tell_user': lexicon.DOWNLOADING}

    def install(self):
        source = self.parent / 'import'
        for path, data in self.payloads.items():
            target = source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return lexicon.download_public(source=source)

    def task(self, raw, *, task_id='dy-test', title='AI 产品', account='科技账号', engine='doubao-auc-turbo', display=False):
        result = {'created': '2026-11-01T00:00:00+08:00', 'engine': engine,
                  'source': {'id': 'video-1', 'title': title, 'author': account, 'source': 'https://example.test/video'},
                  'text': raw, 'segments': [{'text': raw, 'raw_text': raw}], 'duration': 10}
        if display:
            workflow.apply_display(result, {'number_conversion': True, 'cjk_spacing': True})
        item = workflow.write_result(result, self.root / 'outputs' / task_id, self.root)
        task = {'id': task_id, 'created': result['created'], 'purpose': 'transcribe', 'evidence': '校对原稿',
                'status': 'awaiting_review', 'results': [item], 'failures': []}
        workflow.save(task, self.root)
        return task

    def register(self, task, body):
        file = self.root / 'runs' / task['id'] / 'review.txt'
        file.write_text(body, encoding='utf-8')
        return content.register_content(task['id'], [file], 'review', self.root)

    def test_manifest_validation_and_pinned_production_sources(self):
        with patch.object(lexicon, 'MANIFEST', SKILL_ROOT / 'assets/lexicons.json'):
            data = lexicon.manifest()
        self.assertEqual(11, len(data['files']))
        for file in data['files'] + [data['license_file']]:
            self.assertEqual('MIT', file['license'])
            self.assertEqual(2, len(file['sources']))
            self.assertTrue(all(data['revision'] in s for s in file['sources']))
        for field, value in [('revision', 'master'), ('license', 'unknown')]:
            broken = {**self.spec, field: value}
            write_json(self.manifest, broken)
            with self.assertRaises(ValueError):
                lexicon.manifest()
        broken = copy.deepcopy(self.spec)
        broken['files'][0]['path'] = '../escape.txt'
        write_json(self.manifest, broken)
        with self.assertRaises(models.ModelError):
            lexicon.manifest()

    def test_manifest_rejects_bad_hash_size_and_duplicates(self):
        for key, value in [('size', 0), ('sha256', 'wrong')]:
            data = copy.deepcopy(self.spec)
            data['files'][0][key] = value
            write_json(self.manifest, data)
            with self.assertRaises(models.ModelError):
                lexicon.manifest()
        data = copy.deepcopy(self.spec)
        data['files'].append(data['files'][0])
        write_json(self.manifest, data)
        with self.assertRaises(ValueError):
            lexicon.manifest()

    def test_local_server_fallback_resume_and_verified_reuse(self):
        requests = []
        routes = {'/' + path: data for path, data in self.payloads.items()}
        with server(routes, requests) as url:
            for f in self.spec['files'] + [self.spec['license_file']]:
                f['sources'] = [url + '/missing', url + '/' + f['path']]
            write_json(self.manifest, self.spec)
            folder = lexicon.public_folder(self.spec)
            partial = models._part(folder, self.spec['files'][0])
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(self.payloads[self.spec['files'][0]['path']][:7])
            result = lexicon.download_public()
            self.assertTrue(result['ready'])
            self.assertTrue(all(f['hash_ok'] for f in result['files'].values()))
            self.assertIn(('/' + self.spec['files'][0]['path'], 'bytes=7-'), requests)
            self.assertEqual(12, len(result['progress']['errors']))
            count = len(requests)
            self.assertTrue(lexicon.download_public()['ready'])
            self.assertEqual(count, len(requests))

    def test_bad_mirror_hash_does_not_install_and_good_mirror_recovers(self):
        requests = []
        routes = {'/' + p: b'x' * len(data) for p, data in self.payloads.items()}
        with server(routes, requests) as url:
            for f in self.spec['files'] + [self.spec['license_file']]:
                f['sources'] = [url + '/' + f['path']]
            write_json(self.manifest, self.spec)
            with self.assertRaises(models.ModelError):
                lexicon.download_public()
            self.assertFalse(lexicon.public_status()['ready'])
            routes.update({'/' + p: data for p, data in self.payloads.items()})
            self.assertTrue(lexicon.download_public()['ready'])

    def test_manual_import_verifies_all_before_replacing(self):
        self.assertTrue(self.install()['ready'])
        folder = lexicon.public_folder(self.spec)
        before = {f['path']: digest(folder / f['path']) for f in self.spec['files']}
        (self.parent / 'import' / self.spec['files'][-1]['path']).write_bytes(b'bad')
        with self.assertRaises(models.ModelError):
            lexicon.download_public(source=self.parent / 'import')
        self.assertEqual(before, {f['path']: digest(folder / f['path']) for f in self.spec['files']})
        with self.assertRaises(models.ModelError):
            lexicon.download_public(source=self.parent / 'import/LICENSE')

    def test_readonly_queries_do_not_download_and_display_degrades_while_preparing(self):
        with patch.object(models.request, 'urlopen', side_effect=AssertionError('不能联网')):
            self.assertFalse(lexicon.public_status()['ready'])
            self.assertEqual([], lexicon.PublicLexicon().candidates())
            self.assertEqual({}, lexicon.PublicLexicon().idioms())
            result = {'text': '十年生聚，十二个人。', 'segments': [{'raw_text': '十年生聚，十二个人。'}]}
            workflow.apply_display(result, {'number_conversion': True, 'cjk_spacing': True})
            self.assertFalse(result['display']['lexicon_exceptions']['used'])
            self.assertIn('后台下载', result['display']['lexicon_exceptions']['tell_user'])
            item = workflow.write_result({**result, 'engine': 'test', 'created': 'now',
                'source': {'id': 'test', 'title': 'test', 'source': 'test'}}, self.root / 'out', self.root)
            self.assertIn('后台下载', item['tell_user'])
            self.assertNotIn('手动导入', item['tell_user'])
        self.assertFalse((self.parent / 'models').exists())

    def test_missing_starts_one_real_background_download_and_never_waits_for_network(self):
        self.auto_patch.stop()
        requests, children = [], []
        entered, release = threading.Event(), threading.Event()
        first = self.spec['files'][0]

        def held(handler):
            entered.set()
            release.wait(5)
            handler.send_response(200)
            handler.end_headers()
            handler.wfile.write(self.payloads[first['path']])

        routes = {'/' + path: data for path, data in self.payloads.items()}
        routes['/' + first['path']] = held
        launch = models._launch

        def captured(*args, **kwargs):
            child = launch(*args, **kwargs)
            children.append(child)
            return child

        with server(routes, requests) as url:
            for f in self.spec['files'] + [self.spec['license_file']]:
                f['sources'] = [url + '/' + f['path']]
            write_json(self.manifest, self.spec)
            try:
                with patch.object(models, '_launch', side_effect=captured) as start:
                    words, snapshot = lexicon.display_exceptions('十年生聚')
                    self.assertEqual([], words)
                    self.assertFalse(snapshot['used'])
                    self.assertIn('后台下载', snapshot['tell_user'])
                    self.assertNotIn('import-public', snapshot['tell_user'])
                    self.assertTrue(entered.wait(3), '后台进程没有请求本地服务器')
                    token = lexicon.public_status()['progress']['token']
                    self.assertEqual('downloading', lexicon.ensure_public()['status'])
                    self.assertEqual(token, lexicon.ensure_public()['progress']['token'])
                    # Registration is another consumer and must neither wait nor spawn again.
                    done = self.register(self.task('Workbody'), 'WorkBuddy')
                    self.assertIn('后台下载', done['tell_user'])
                    self.assertEqual(1, start.call_count)
                    release.set()
                    children[0].wait(timeout=5)
                    ready = lexicon.ensure_public()
                    self.assertTrue(ready['ready'])
                    self.assertEqual(1, start.call_count)
                    self.assertEqual(12, len(requests))
                    self.assertTrue(all(s['hash_ok'] for s in ready['files'].values()))
                    self.assertNotIn('后台下载', lexicon.display_notice({'display': {'lexicon_exceptions': snapshot}}))
                    self.assertEqual(['十年生聚'], lexicon.display_exceptions('十年生聚')[0])
            finally:
                release.set()
                for child in children:
                    child.wait(timeout=5)

    def test_failed_background_preparation_only_then_suggests_manual_import(self):
        self.auto_patch.stop()
        with patch.object(models, '_launch', side_effect=OSError('测试启动失败')) as start:
            self.assertNotIn('手动导入', lexicon.public_status()['tell_user'])
            first = lexicon.ensure_public()
            self.assertEqual('failed', first['status'])
            self.assertIn('手动导入', first['tell_user'])
            self.assertIn('import-public', first['tell_user'])
            self.assertEqual('failed', lexicon.ensure_public()['status'])
            self.assertEqual(1, start.call_count)
        self.assertTrue(self.install()['ready'])
        self.assertNotIn('手动导入', lexicon.ensure_public()['tell_user'])

    def test_dead_background_worker_is_failure_not_an_endless_downloading_notice(self):
        self.auto_patch.stop()
        folder = lexicon.public_folder(self.spec)
        write_json(folder / 'progress.json', {'state': 'downloading', 'pid': 0, 'process_birth': 'gone'})
        with patch.object(models, '_launch') as start:
            status = lexicon.ensure_public()
        self.assertEqual('failed', status['status'])
        self.assertIn('手动导入', status['tell_user'])
        start.assert_not_called()

    def test_category_frequency_names_and_invalid_upstream_df(self):
        self.install()
        public = lexicon.PublicLexicon()
        self.assertEqual(100, public.lookup('人工智能')['frequency'])
        self.assertFalse(public.lookup('普通词')['common'])
        self.assertEqual(['李白'], [x['word'] for x in public.candidates('李', categories=('lishimingren',))])
        self.assertIn('北京', public.category('diming'))
        for word in ('柴塔村', '丁香', '未知词频'):
            self.assertTrue(public.lookup(word)['common'])
            self.assertIsNone(public.lookup(word)['frequency'])

    def test_idioms_preserve_display_and_replay_without_downloaded_data(self):
        self.install()
        raw = '十年生聚，十二个人。'
        result = {'text': raw, 'segments': [{'raw_text': raw}]}
        options = {'number_conversion': True, 'cjk_spacing': True}
        workflow.apply_display(result, options)
        self.assertEqual('十年生聚，12 个人。', result['text'])
        info = result['display']['lexicon_exceptions']
        self.assertEqual(['十年生聚'], info['words'])
        self.assertEqual(self.spec['revision'], info['revision'])
        expected = copy.deepcopy(result)
        with patch.object(lexicon, 'PublicLexicon', side_effect=AssertionError('重算不依赖当前词库')):
            workflow.apply_display(result, options)
        self.assertEqual(expected, result)
        absent = {'text': raw, 'segments': [{'raw_text': raw}], 'display': {
            'lexicon_exceptions': {'used': False, 'revision': None, 'words': [], 'tell_user': lexicon.UNAVAILABLE}}}
        workflow.apply_display(absent, options)
        self.assertEqual('10 年生聚，12 个人。', absent['text'])

    def test_word_add_remove_import_copy_and_chinese_cli(self):
        lexicon.add('承意', self.root, note='产品名', source='任务 1 的校对')
        self.assertEqual('产品名', lexicon.list_words(self.root)['words'][0]['note'])
        file = self.parent / '用户词表.txt'
        file.write_text('# 词表\nWorkBuddy\n李白\t人名\nWorkBuddy\n', encoding='utf-8')
        imported = lexicon.import_words(file, self.root)
        file.write_text('外部文件后来变化', encoding='utf-8')
        self.assertEqual(2, imported['count'])
        self.assertIn('WorkBuddy', (self.root / imported['file']).read_text(encoding='utf-8'))
        origin = next(w for w in lexicon.list_words(self.root)['words'] if w['word'] == '李白')['origins'][0]
        self.assertEqual('用户词表.txt', origin['source'])
        self.assertIn('time', origin)
        lexicon.remove('李白', self.root)
        self.assertNotIn('李白', [w['word'] for w in lexicon.list_words(self.root)['words']])
        args = cli.parser().parse_args(['lexicon', 'add', '测试术语', '--source', '视频 2'])
        self.assertIn('已添加', lexicon.dispatch(args, self.root)['tell_user'])
        self.assertEqual('测试术语', lexicon.dispatch(args, self.root)['word'])

    def test_diff_ignores_punctuation_spacing_and_number_spellings(self):
        for raw, reviewed in [('十二个人用了三点五秒。', '12 个人用了 3.5 秒！'),
                              ('六一六宇宙，二零二六年。', '616 宇宙，2026 年。'),
                              ('百分之三十和一点五万。', '30% 和 15000。'),
                              ('负五度，3% 的人。', '-5 度，百分之三的人！'),
                              ('AI真的好用吗？', 'AI 真的好用吗'), ('三个人', '3 个人')]:
            with self.subTest(raw=raw):
                self.assertEqual([], lexicon.compare(raw, reviewed)['changes'])
        for raw, reviewed in [('十五个', '五十个'), ('1.5 米', '15 米'), ('5%', '5'), ('-5 度', '5 度')]:
            self.assertTrue(lexicon.compare(raw, reviewed)['changes'])

    def test_real_review_fragments_copied_to_isolated_scratch_before_selection(self):
        # Selected from the read-only copied reviews; no private workspace is read by tests.
        raw = '腾讯这边就是 Workbody 鲸吞万物。字节这边就是豆包，是吧？'
        reviewed = '腾讯这边就是 WorkBuddy 鲸吞万物。字节这边就是豆包，是吧？'
        changes = lexicon.compare(raw, reviewed)['changes']
        self.assertEqual([('Workbody', 'WorkBuddy')], [(c['original'], c['replacement']) for c in changes])
        self.assertIn('字节', changes[0]['after'])
        self.assertEqual([('q 克劳', 'QClaw')], [(c['original'], c['replacement']) for c in
            lexicon.compare('反正就是呃叫什么 q 克劳是吧？', '反正就是呃叫什么 QClaw是吧？')['changes']])
        self.assertEqual([('该', '嘅')], [(c['original'], c['replacement']) for c in
            lexicon.compare('六一六宇宙该。', '616 宇宙嘅。')['changes']])
        self.assertEqual([('该', '嘅'), ('哈', '')], [(c['original'], c['replacement']) for c in
            lexicon.compare('阿托维亚该。哈哈哈哈，老乡啊！', '阿托维亚嘅。哈哈哈，老乡啊！')['changes']])

    def test_learns_complete_terms_not_english_substrings_or_ordinary_edits(self):
        self.install()
        raw = '这个元气平台，用 Workbody 和 q 克劳，还有短 ttk，狠狠的坚持。邀请了阿豌豆作为嘉宾。'
        fixed = '这个元器平台，用 WorkBuddy 和 QClaw，还有短 TTK，狠狠地坚持。邀请了阿班豆作为嘉宾。'
        words = lexicon.learned_terms(fixed, lexicon.compare(raw, fixed)['changes'], lexicon.PublicLexicon())
        self.assertEqual({'元器', 'WorkBuddy', 'QClaw', 'TTK', '阿班豆'}, words)

    def test_real_word_boundaries_types_and_suggestion_flags(self):
        self.install()
        public = lexicon.PublicLexicon()
        cases = [
            ('这个元气平台', '这个元器平台', [('元气', '元器', 'term', True)]),
            ('那个呃原接器啊', '那个呃元接器啊', [('原接器', '元接器', 'phrase', True)]),
            ('右食指负责激发', '右食指负责击发', [('激发', '击发', 'phrase', True)]),
            ('邀请了阿豌豆作为嘉宾', '邀请了阿班豆作为嘉宾', [('阿豌豆', '阿班豆', 'term', True)]),
            ('你扎啤吧', '你扎皮吧', [('扎啤', '扎皮', 'phrase', True)]),
            ('对之瓦手这种短游戏', '对峙和瓦手这种短游戏', [('对之', '对峙', 'phrase', True), ('', '和', 'insertion', False)]),
            ('狠狠的坚持', '狠狠地坚持', [('的', '地', 'function', False)]),
            ('玉米鸡蛋配香蕉', '玉米鸡蛋加香蕉', [('配', '加', 'single_character', False)]),
            ('六一六宇宙该', '616 宇宙嘅', [('该', '嘅', 'dialect', True)]),
            ('复联冰斗该', '复联边度嘅', [('冰斗该', '边度嘅', 'dialect', True)]),
            ('能坚持几天呢', '你能坚持几天呢', [('', '你', 'insertion', False)]),
            ('我要打响指了。你扎啤吧', '我要打响指了。哦！你扎啤吧', [('', '哦', 'insertion', False)]),
            ('哈哈哈哈，老乡啊', '哈哈哈，老乡啊', [('哈', '', 'deletion', False)]),
            ('阿托维亚该', '阿托维尼亚该', [('阿托维亚', '阿托维尼亚', 'phrase', True)]),
            ('从养鱼走随啊', '同饮宇宙水啊', [('从养鱼走随', '同饮宇宙水', 'phrase', True)]),
            ('Sure', '啊', [('Sure', '啊', 'function', False)]),
            ('use foobar', 'use foobaz', [('foobar', 'foobaz', 'english', True)]),
        ]
        for raw, fixed, expected in cases:
            with self.subTest(raw=raw):
                changes = lexicon.compare(raw, fixed, public=public, words={'元器', '阿班豆'})['changes']
                self.assertEqual(expected, [(e['original'], e['replacement'], e['type'], e['can_suggest']) for e in changes])
                for e in changes:
                    self.assertEqual(raw[e['raw_start']:e['raw_end']], e['original'])
                    self.assertEqual(fixed[e['review_start']:e['review_end']], e['replacement'])
                    self.assertEqual(raw[max(0, e['raw_start'] - 24):e['raw_start']], e['before'])
                    if e['can_suggest'] and e['type'] != 'english':
                        self.assertTrue(e['requires_context'])
                        self.assertIn('需同账号或同话题', e['context_reason'])

    def test_changes_within_one_word_merge_using_personal_and_public_vocabulary(self):
        self.install()
        for words in ({'甲乙丙丁'}, set()):
            public = lexicon.PublicLexicon()
            if not words:
                public.tables['IT'] = {'甲乙丙丁': 100}
            result = lexicon.compare('甲戊丙己', '甲乙丙丁', public=public, words=words)
            self.assertEqual([('甲戊丙己', '甲乙丙丁')], [(e['original'], e['replacement']) for e in result['changes']])

    def test_bidirectional_maximum_matching_avoids_forward_only_ambiguity(self):
        vocabulary = lexicon.WordBoundaries(lexicon.PublicLexicon(), {'研究', '研究生', '生命', '起源'})
        text = '研究生命起源'
        self.assertEqual(['研究', '生命', '起源'], [text[a:b] for a,b in vocabulary.split(text)])

    def test_query_filters_function_insert_delete_and_single_character_memories(self):
        self.install()
        self.register(self.task('狠狠的坚持。玉米鸡蛋配香蕉。哈哈哈哈。然后结束。'),
                      '狠狠地坚持。玉米鸡蛋加香蕉。哈哈哈。然后结束。哦！')
        rows = lexicon.memories(self.root)
        self.assertEqual({'function', 'single_character', 'deletion', 'insertion'}, {m['type'] for m in rows})
        self.assertTrue(all(not m['can_suggest'] for m in rows))
        self.assertFalse(any(x['kind'] == 'memory' for x in lexicon.query({'account': '科技账号'}, self.root)['results']))

    def test_dialect_and_chinese_memories_require_account_or_topic(self):
        self.install()
        self.register(self.task('这个元气平台。复联冰斗该，六一六宇宙该。', title='平台 #科技', account='甲'),
                      '这个元器平台。复联边度嘅，616 宇宙嘅。')
        all_memories = lexicon.memories(self.root)
        self.assertEqual({'元气', '冰斗该', '该'}, {m['original'] for m in all_memories})
        self.assertTrue(all(m['requires_context'] and m['can_suggest'] for m in all_memories))
        for ctx, expected in [({'title': '元器 边度嘅 嘅 平台', 'account': '乙'}, 0),
                              ({'account': '甲'}, 3), ({'account': '乙', 'tags': ['科技']}, 3)]:
            rows = [m for m in lexicon.query(ctx, self.root)['results'] if m['kind'] == 'memory']
            self.assertEqual(expected, len(rows))
            self.assertTrue(all(m['requires_context'] and m['context_reason'] for m in rows))

    def test_word_level_forget_does_not_disable_other_words_sharing_a_character(self):
        self.install()
        self.register(self.task('这个元气平台。右食指负责激发。气候。'), '这个元器平台。右食指负责击发。气侯。')
        self.assertEqual(0, lexicon.forget('气', self.root)['count'])
        self.assertEqual(1, lexicon.forget('元气', self.root)['count'])
        lexicon.learn(self.root)
        rows = [m for m in lexicon.query({'account': '科技账号'}, self.root)['results'] if m['kind'] == 'memory']
        self.assertEqual(['气候', '激发'], [m['original'] for m in rows])
        self.assertFalse(next(m for m in lexicon.memories(self.root) if m['original'] == '元气')['can_suggest'])
        lexicon.forget('激发', self.root, delete=True)
        self.assertNotIn('激发', [m['original'] for m in lexicon.memories(self.root)])

    def test_domain_frequency_and_proper_names_do_not_grant_global_scope(self):
        self.install()
        public = lexicon.PublicLexicon()
        public.tables['medical'] = {'普通术语': 10000, '较少见词': 9999}
        public.tables['diming'] = {'北京': 100000}
        common = lexicon.classify_change('普通术语', '专业术语', public)
        self.assertEqual(10000, common['original_frequency'])
        self.assertTrue(common['requires_context'])
        self.assertTrue(lexicon.classify_change('较少见词', '专业术语', public)['requires_context'])
        self.assertTrue(lexicon.classify_change('北京', '南京', public)['requires_context'])
        public.tables['medical'] = {}
        specified = lexicon.classify_change('元气', '元器', public, {'元器'})
        self.assertTrue(specified['requires_context'])
        self.assertIsNone(specified['original_frequency'])

    def test_real_chinese_memories_cannot_escape_scene_through_literal_title_matches(self):
        self.install()
        raw = '这个元气平台。右食指负责激发。你扎啤吧。对之瓦手这种短游戏。邀请了阿豌豆作为嘉宾。那个呃原接器啊。从养鱼走随啊。'
        fixed = '这个元器平台。右食指负责击发。你扎皮吧。对峙和瓦手这种短游戏。邀请了阿班豆作为嘉宾。那个呃元接器啊。同饮宇宙水啊。'
        self.register(self.task(raw, title='原样例 #测试', account='甲'), fixed)
        expected = {('元气', '元器'), ('激发', '击发'), ('扎啤', '扎皮'), ('对之', '对峙'),
                    ('阿豌豆', '阿班豆'), ('原接器', '元接器'), ('从养鱼走随', '同饮宇宙水')}
        memories = [m for m in lexicon.memories(self.root) if m['can_suggest']]
        self.assertEqual(expected, {(m['original'], m['replacement']) for m in memories})
        self.assertTrue(all(m['requires_context'] for m in memories))
        unrelated = {'title': '元器 击发 扎皮 对峙 阿班豆 元接器 同饮宇宙水', 'account': '乙'}
        for context, pairs in [(unrelated, set()), ({'account': '甲'}, expected),
                               ({'account': '乙', 'tags': ['测试']}, expected)]:
            rows = [m for m in lexicon.query(context, self.root)['results'] if m['kind'] == 'memory']
            self.assertEqual(pairs, {(m['original'], m['replacement']) for m in rows})

    def test_two_confirming_accounts_promote_chinese_memory_without_bypassing_negative_evidence(self):
        self.install()
        context = {'account': '丙', 'title': '击发'}
        self.register(self.task('激发', task_id='dy-first', account='甲'), '击发')
        self.assertTrue(lexicon.memories(self.root)[0]['requires_context'])
        self.assertFalse(any(m['kind'] == 'memory' for m in lexicon.query(context, self.root)['results']))
        self.register(self.task('激发', task_id='dy-second', account='乙'), '击发')
        promoted = lexicon.memories(self.root)[0]
        self.assertEqual(2, promoted['count'])
        self.assertFalse(promoted['requires_context'])
        self.assertIn('2 个不同账号', promoted['context_reason'])
        self.assertEqual({'甲', '乙'}, set(promoted['accounts']))
        at = '2026-12-01T00:00:00+00:00'
        rows = [m for m in lexicon.query(context, self.root, at=at)['results'] if m['kind'] == 'memory']
        self.assertEqual(['激发'], [m['original'] for m in rows])
        self.register(self.task('激发', task_id='dy-counterevidence', account='丙'), '激发')
        discounted = lexicon.memories(self.root)[0]
        self.assertEqual(2, discounted['count'])
        self.assertEqual(1, len(discounted['unchanged']))
        self.assertLess(lexicon.memory_weight(discounted, context, at), rows[0]['score'])
        lexicon.forget('激发', self.root)
        denied = lexicon.memories(self.root)[0]
        self.assertTrue(denied['denied'])
        self.assertFalse(denied['can_suggest'])
        self.assertLess(lexicon.memory_weight(denied, context, at), lexicon.memory_weight(discounted, context, at))
        self.assertFalse(any(m['kind'] == 'memory' for m in lexicon.query(context, self.root)['results']))

    def test_same_account_repeated_confirmations_and_multiple_tags_do_not_promote(self):
        self.install()
        self.register(self.task('激发。激发。', task_id='dy-first', title='射击 #游戏 #训练', account='甲'), '击发。击发。')
        first = lexicon.memories(self.root)[0]
        self.assertEqual(2, first['count'])
        self.assertTrue(first['requires_context'])
        self.register(self.task('激发', task_id='dy-second', title='新课程 #训练 #游戏', account='甲'), '击发')
        repeated = lexicon.memories(self.root)[0]
        self.assertEqual(3, repeated['count'])
        self.assertTrue(repeated['requires_context'])
        rows = lexicon.query({'account': '乙', 'title': '击发'}, self.root)['results']
        self.assertFalse(any(m['kind'] == 'memory' for m in rows))

    def test_two_confirmed_topics_can_promote_within_one_account(self):
        self.install()
        for number, tag in enumerate(('游戏', '机械')):
            self.register(self.task('激发', task_id=f'dy-topic-{number}', title='#' + tag, account='甲'), '击发')
        memory = lexicon.memories(self.root)[0]
        self.assertEqual(2, memory['count'])
        self.assertEqual(['甲'], memory['accounts'])
        self.assertFalse(memory['requires_context'])
        self.assertIn('2 组不同话题', memory['context_reason'])
        rows = [m for m in lexicon.query({'account': '乙', 'title': '击发'}, self.root)['results'] if m['kind'] == 'memory']
        self.assertEqual(['激发'], [m['original'] for m in rows])

    def test_unchanged_other_scene_and_empty_contexts_are_not_confirmations(self):
        self.install()
        self.register(self.task('激发', task_id='dy-first', account='甲'), '击发')
        self.register(self.task('激发', task_id='dy-unchanged', account='乙'), '激发')
        memory = lexicon.memories(self.root)[0]
        self.assertEqual(1, memory['count'])
        self.assertEqual(['甲'], memory['accounts'])
        self.assertEqual(1, len(memory['unchanged']))
        self.assertTrue(memory['requires_context'])
        occurrences = [{'context': {'title': title}} for title in ('第一条', '第二条')]
        before = copy.deepcopy(occurrences)
        self.assertTrue(lexicon.memory_scope('激发', '击发', 'phrase', occurrences)['requires_context'])
        self.assertEqual(before, occurrences)

    def test_english_names_and_latin_anchored_transliteration_keep_global_scope(self):
        self.install()
        self.register(self.task('腾讯用 Workbody，反正就是呃叫什么 q 克劳是吧？还有短 ttk。', account='甲'),
                      '腾讯用 WorkBuddy，反正就是呃叫什么 QClaw 是吧？还有短 TTK。')
        memories = lexicon.memories(self.root)
        self.assertEqual({('Workbody', 'WorkBuddy'), ('q 克劳', 'QClaw'), ('ttk', 'TTK')},
                         {(m['original'], m['replacement']) for m in memories})
        self.assertTrue(all(m['can_suggest'] and not m['requires_context'] for m in memories))
        rows = [m for m in lexicon.query({'account': '乙', 'title': 'WorkBuddy QClaw TTK'}, self.root)['results']
                if m['kind'] == 'memory']
        self.assertEqual(3, len(rows))
        self.assertTrue(lexicon.classify_change('克劳', 'QClaw', lexicon.PublicLexicon(), {'QClaw'})['requires_context'])

    def test_registration_preserves_snapshots_basis_status_and_raw(self):
        self.install()
        task = self.task('字节使用 Workbody，十二个人。', display=True)
        paths = [self.root / task['results'][0][key] for key in ('result', 'text', 'markdown')]
        before = {str(p): p.read_bytes() for p in paths}
        done = self.register(task, '字节使用 WorkBuddy，12 个人。')
        self.assertEqual('ready_to_deliver', done['status'])
        self.assertIn('1 处', done['tell_user'])
        self.assertIn('Workbody → WorkBuddy', done['tell_user'])
        self.assertIn('已记入纠错记忆', done['tell_user'])
        entry = done['review']['files'][0]
        self.assertEqual(digest(self.root / entry['file']), digest(self.root / entry['snapshot_file']))
        self.assertEqual(digest(paths[1]), entry['source_basis']['raw_sha256'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in paths})
        self.assertEqual(['WorkBuddy'], [w['word'] for w in lexicon.list_words(self.root)['words']])
        self.assertEqual('doubao-auc-turbo', lexicon.memories(self.root)[0]['occurrences'][0]['engine'])

    def test_registration_failure_does_not_roll_back_existing_contract(self):
        task = self.task('原稿')
        with patch.object(lexicon, 'learn_registered', side_effect=OSError('磁盘不可写')):
            done = self.register(task, '校对稿')
        self.assertEqual('ready_to_deliver', done['status'])
        self.assertIn('校对稿已登记成功', done['tell_user'])
        self.assertIn('尚未写入', done['tell_user'])
        self.assertEqual('valid', content.inspect_content(workflow.load(task['id'], self.root), 'review', self.root)[0]['state'])

    def test_learn_idempotent_and_uses_registered_snapshot(self):
        self.install()
        task = self.task('Workbody 和 Workbody')
        with patch.object(lexicon, 'learn_registered', return_value={'tell_user': '稍后补建'}):
            done = self.register(task, 'WorkBuddy 和 WorkBuddy')
        (self.root / done['review']['files'][0]['file']).write_text('尚未登记的编辑', encoding='utf-8')
        first = lexicon.learn(self.root)
        state = (self.root / '.local/lexicon/state.json').read_bytes()
        second = lexicon.learn(self.root)
        self.assertEqual(1, first['reviews_added'])
        self.assertEqual(2, first['memories'][0]['count'])
        self.assertEqual(0, second['reviews_added'])
        self.assertEqual(1, second['reviews_skipped'])
        self.assertEqual(state, (self.root / '.local/lexicon/state.json').read_bytes())

    def test_legacy_hash_mismatch_is_reported_not_learned(self):
        task = self.task('Workbody')
        with patch.object(lexicon, 'learn_registered', return_value={'tell_user': ''}):
            done = self.register(task, 'WorkBuddy')
        done['review']['files'][0].pop('snapshot_file')
        workflow.save(done, self.root)
        (self.root / done['review']['files'][0]['file']).write_text('被改过', encoding='utf-8')
        result = lexicon.learn(self.root)
        self.assertEqual('incomplete', result['status'])
        self.assertEqual([], result['memories'])

    def test_weight_frequency_recency_context_and_denial_are_pure(self):
        base = {'replacement': 'WorkBuddy', 'occurrences': [{'time': '2026-11-01T00:00:00+00:00',
                'context': {'account': '甲', 'tags': ['AI']}}], 'unchanged': []}
        context = {'account': '甲', 'tags': ['AI']}
        at = '2026-11-02T00:00:00+00:00'
        score = lexicon.memory_weight(base, context, at)
        more = copy.deepcopy(base)
        more['occurrences'] *= 2
        self.assertGreater(lexicon.memory_weight(more, context, at), score)
        self.assertLess(lexicon.memory_weight(base, context, '2027-11-02T00:00:00+00:00'), score)
        self.assertLess(lexicon.memory_weight(base, {'account': '乙'}, at), score)
        negative = {**base, 'unchanged': [{'context': context}]}
        self.assertLess(lexicon.memory_weight(negative, context, at), score)
        self.assertEqual(lexicon.memory_weight(negative, {'account': '乙'}, at),
                         lexicon.memory_weight(base, {'account': '乙'}, at))
        self.assertLess(lexicon.memory_weight({**base, 'denied': True}, context, at), score)
        self.assertEqual([], base['unchanged'])

    def test_unchanged_occurrence_lowers_only_relevant_context_and_is_order_independent(self):
        self.install()
        self.register(self.task('Workbody', task_id='dy-unchanged', account='甲'), 'Workbody')
        self.register(self.task('Workbody', task_id='dy-changed', account='甲'), 'WorkBuddy')
        memory = lexicon.memories(self.root)[0]
        self.assertEqual(1, len(memory['unchanged']))
        no_negative = {**memory, 'unchanged': []}
        at = '2026-12-01T00:00:00+00:00'
        self.assertLess(lexicon.memory_weight(memory, {'account': '甲'}, at),
                        lexicon.memory_weight(no_negative, {'account': '甲'}, at))
        self.assertEqual(lexicon.memory_weight(memory, {'account': '乙'}, at),
                         lexicon.memory_weight(no_negative, {'account': '乙'}, at))

    def test_forget_and_delete_stay_forgotten_after_learn(self):
        self.install()
        self.register(self.task('Workbody'), 'WorkBuddy')
        lexicon.forget('Workbody', self.root)
        lexicon.learn(self.root)
        self.assertTrue(lexicon.memories(self.root)[0]['denied'])
        self.assertFalse(any(x['kind'] == 'memory' for x in lexicon.query({'account': '科技账号'}, self.root)['results']))
        lexicon.forget('Workbody', self.root, delete=True)
        lexicon.learn(self.root)
        self.assertEqual([], lexicon.memories(self.root))

    def test_vault_titles_only_use_fake_vault_and_do_not_learn_plain_words(self):
        self.install()
        vault = self.parent / 'fake-vault'
        vault.mkdir()
        for name, title in [('page', '李白'), ('algorithm', '人工智能'), ('ordinary', '今天'), ('long', '今天应该怎么吃饭')]:
            (vault / (name + '.md')).write_text('# ' + title + '\n\n正文里的 PrivateTerm 不学习。', encoding='utf-8')
        write_json(self.root / 'config.json', {'vault_root': str(vault)})
        original = {p.name: p.read_bytes() for p in vault.iterdir()}
        self.assertEqual(2, lexicon.learn(self.root)['titles_added'])
        self.assertEqual(['人工智能', '李白'], [x['word'] for x in lexicon.list_words(self.root)['words']])
        self.assertEqual(0, lexicon.learn(self.root)['titles_added'])
        lexicon.remove('李白', self.root)
        lexicon.learn(self.root)
        self.assertEqual(['人工智能'], [x['word'] for x in lexicon.list_words(self.root)['words']])
        self.assertEqual(original, {p.name: p.read_bytes() for p in vault.iterdir()})

    def test_query_ranks_terms_and_memories_with_context_provenance(self):
        self.install()
        lexicon.add('北京', self.root, source='视频标题')
        lexicon.add('无关术语', self.root)
        self.register(self.task('Workbody', title='AI #模型', account='甲'), 'WorkBuddy')
        result = lexicon.query({'title': '北京的 WorkBuddy', 'description': 'AI', 'tags': ['模型'], 'account': '甲'}, self.root)
        rows = result['results']
        self.assertEqual(sorted([x['score'] for x in rows], reverse=True), [x['score'] for x in rows])
        self.assertEqual({'word', 'memory'}, {x['kind'] for x in rows})
        self.assertIn('北京', [x.get('word') for x in rows])
        self.assertNotIn('无关术语', [x.get('word') for x in rows])
        self.assertEqual('甲', next(x for x in rows if x['kind'] == 'memory')['accounts'][0])

    def test_manual_note_supplies_related_context_without_literal_word_match(self):
        lexicon.add('AKR', self.root, note='对峙游戏中的步枪', source='视频 1')
        result = lexicon.query({'title': '对峙游戏讨论'}, self.root)
        self.assertEqual('AKR', result['results'][0]['word'])

    def test_spoken_content_never_learns(self):
        task = self.task('Workbody')
        file = self.root / 'spoken.txt'
        file.write_text('WorkBuddy', encoding='utf-8')
        with patch.object(lexicon, 'learn_registered', side_effect=AssertionError('口播不学习')):
            content.register_content(task['id'], [file], 'spoken', self.root, evidence='改成口播')
        self.assertEqual([], lexicon.memories(self.root))


if __name__ == '__main__':
    unittest.main()
