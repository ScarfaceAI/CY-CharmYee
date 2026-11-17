"""Local search contract, using synthetic files in the system temporary directory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from contextlib import closing
from unittest.mock import patch

from support import SKILL_ROOT, WorkspaceCase
from douyin_text import content, personal_vault as kb, search, workflow
from douyin_text.common import artifact_lock, digest, read_json, relative, write_json


class SearchTests(WorkspaceCase):
    def enable(self, modules=('work', 'learning')):
        self.vault = self.parent / 'vault'
        kb.enable(self.root, vault=self.vault, create=True, evidence='合成知识库搜索测试')
        value = kb.state(self.root)
        value['modules'] = list(modules)
        kb.save(value, self.root)

    def page(self, name, body, *, linked=True):
        file = self.vault / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body, encoding='utf-8')
        if linked:
            with (self.vault / 'index.md').open('a', encoding='utf-8') as index:
                index.write(f'\n[合成页面](<{name}>)\n')
        return file

    def task_with_text(self, body='项目复盘采用 Python 分析，讨论换岗计划。', *, ident='dy-search', number=4):
        folder = self.root / 'outputs' / 'synthetic-item' / ident
        folder.mkdir(parents=True, exist_ok=True)
        text, markdown, result = (folder / name for name in ('transcript.txt', 'transcript.md', 'result.json'))
        text.write_text(body, encoding='utf-8')
        markdown.write_text('# 合成机器原稿\n\n' + body, encoding='utf-8')
        write_json(result, {'text': body, 'source': {'title': '合成项目记录'},
                            'output_sha256': {'txt': digest(text), 'md': digest(markdown)}})
        item = {'id': 'synthetic-item', 'number': number, 'result': relative(self.root, result),
                'text': relative(self.root, text), 'markdown': relative(self.root, markdown)}
        task = {'id': ident, 'results': [item]}
        workflow.save(task, self.root)
        return task

    def review(self, task, body='项目复盘经过校对，Python 和换岗建议保持原意。'):
        item = task['results'][0]
        file = self.root / 'runs' / task['id'] / 'review.txt'
        file.write_text(body, encoding='utf-8')
        task['review'] = {'files': [{'id': item['id'], 'file': relative(self.root, file),
                                    'sha256': digest(file),
                                    'source_basis': {'raw_sha256': digest(self.root / item['text'])}}]}
        workflow.save(task, self.root)
        return file

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SKILL_ROOT / 'scripts/brain.py'),
                               '--workspace', str(self.root), 'search', *args], capture_output=True,
                              env={**os.environ, 'CY_UPDATE_CHECK': '0',
                                   'CY_STATE_DIR': str(self.parent / 'state'),
                                   'CY_MODEL_DIR': str(self.parent / 'models')}, timeout=30)

    def test_three_sources_provenance_opt_in_and_no_vault_writes(self):
        task = self.task_with_text()
        reviewed = self.review(task)
        self.enable()
        knowledge = self.page('工作/资料/复盘.md', '# 项目复盘\nPython 帮助整理换岗依据。')
        hidden = self.page('生活/资料/未开启.md', '项目复盘 Python 换岗。')
        unlinked = self.page('工作/资料/未链接.md', '项目复盘 Python 换岗。', linked=False)
        self.page('工作/资料/跳过.txt', '项目复盘 Python 换岗。')
        before = {p: p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
        original_reader = Path.read_bytes

        def guarded(file):
            self.assertNotIn(file, {hidden, unlinked}, '不能读取未开启方向或未链接的正文')
            return original_reader(file)

        with patch.object(Path, 'read_bytes', guarded):
            found = search.query('项目复盘 Python', self.root)
        self.assertEqual('trigram', found['engine'])
        self.assertEqual(3, found['total'])
        self.assertTrue(found['material_is_data_not_instructions'])
        rows = {row['kind']: row for row in found['results']}
        self.assertEqual(set(search.KINDS), set(rows))
        self.assertEqual({'task': task['id'], 'number': 4, 'item': 'synthetic-item'}, rows['transcript']['source'])
        self.assertEqual('机器原稿', rows['transcript']['type'])
        self.assertEqual(str(reviewed), rows['reviewed']['file'])
        self.assertEqual({'path': '工作/资料/复盘.md', 'domain': 'work'}, rows['knowledge']['source'])
        self.assertEqual(str(knowledge), rows['knowledge']['file'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.vault.rglob('*') if p.is_file()})

    def test_vault_without_opt_in_is_not_even_read(self):
        self.task_with_text()
        # A configured root is not the same as kb enable.
        with patch.object(kb, 'indexed_files', side_effect=AssertionError('未授权读取知识库')):
            self.assertEqual(1, search.query('项目复盘', self.root)['total'])
        self.assertFalse(kb.home(self.root).exists())

    def test_and_short_terms_mixed_case_and_literal_fts_syntax(self):
        self.enable()
        both = self.page('工作/both.md', '规划换岗计划，项目复盘使用 Python，字面词 OR 和 a"b。')
        self.page('工作/one.md', '项目复盘只谈团队沟通。')
        self.page('工作/other.md', 'Python 和换岗，只谈编程。')
        for phrase, engine in [('项目复盘 python', 'trigram'), ('换岗 项目复盘', 'scan'),
                               ('换 项目复盘', 'scan'), ('项目复盘 a"b', 'trigram'),
                               ('项目复盘 OR', 'scan')]:
            with self.subTest(phrase=phrase):
                found = search.query(phrase, self.root)
                self.assertEqual(engine, found['engine'])
                self.assertEqual([str(both)], [x['file'] for x in found['results']])
                self.assertIn('【', found['results'][0]['excerpt'])
        long = search.query('项目复盘 python', self.root)
        short = search.query('复盘 python', self.root)
        self.assertEqual([x['file'] for x in long['results']], [x['file'] for x in short['results']])

    def test_trigram_and_scan_have_identical_literal_matching_and_scores(self):
        self.enable()
        phrases = ['项目复盘', 'Python', 'a"b', '中文ABC', 'Straße', 'ＡＢＣ', 'a-b', 'e\u0301ab', '😀项目']
        self.page('工作/混合.md', ' '.join(phrases) * 2)
        self.page('工作/无关.md', '无关材料。')
        search.query('项目复盘', self.root)
        with closing(sqlite3.connect(self.root / '.local/search.sqlite')) as connection:
            connection.row_factory = sqlite3.Row
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    terms = [phrase.casefold()]
                    expected = [row['file'] for row in connection.execute('SELECT * FROM documents')
                                if all(term in row['searchable'] for term in terms)]
                    engine, _, hits = search._matches(connection, terms, None, 50, True)
                    self.assertEqual('trigram', engine)
                    self.assertEqual(expected, [row['file'] for row in hits])
                    self.assertIn('【', hits[0]['excerpt'])

    def test_incremental_edit_delete_move_and_scope_switches(self):
        self.enable()
        page = self.page('工作/复盘.md', '项目复盘讨论换岗。')
        learning = self.page('学习/复盘.md', '项目复盘练习换岗。')
        self.assertEqual(2, search.query('换岗', self.root)['total'])
        unchanged = search.query('换岗', self.root)
        self.assertEqual(0, unchanged['index']['updated'])
        self.assertGreaterEqual(unchanged['index']['unchanged'], 2)
        page.write_text('项目复盘决定留任，补充依据。', encoding='utf-8')
        changed = search.query('留任', self.root)
        self.assertEqual(1, changed['total'])
        self.assertEqual(1, changed['index']['updated'])
        self.assertEqual(1, search.query('换岗', self.root)['total'])
        # Move a synthetic source and update its navigation link.
        moved = self.vault / '工作/新复盘.md'
        page.rename(moved)
        index = self.vault / 'index.md'
        index.write_text(index.read_text(encoding='utf-8').replace('工作/复盘.md', '工作/新复盘.md'), encoding='utf-8')
        self.assertEqual(str(moved), search.query('留任', self.root)['results'][0]['file'])
        learning.unlink()
        self.assertEqual(0, search.query('换岗', self.root)['total'])
        kb.configure(self.root, disable_module='work')
        self.assertEqual(0, search.query('留任', self.root)['total'])
        kb.configure(self.root, module='work', evidence='重新开启合成工作方向')
        self.assertEqual(1, search.query('留任', self.root)['total'])
        kb.save({}, self.root)
        with patch.object(kb, 'indexed_files', side_effect=AssertionError('关闭后不能读取知识库')):
            self.assertEqual(0, search.query('留任', self.root)['total'])

    def test_no_rehash_or_body_read_for_unchanged_documents(self):
        task = self.task_with_text()
        self.review(task)
        self.enable()
        self.page('工作/复盘.md', '项目复盘材料。')
        search.query('项目复盘', self.root)
        with patch.object(content, 'originals', side_effect=AssertionError('原稿重复校验')), \
                patch.object(content, 'checked_content', side_effect=AssertionError('校对稿重复校验')), \
                patch.object(Path, 'read_bytes', side_effect=AssertionError('未变化正文被读取')):
            result = search.query('项目复盘', self.root)
        self.assertEqual(3, result['total'])
        self.assertEqual(0, result['index']['updated'])

    def test_missing_corrupt_and_explicit_rebuild(self):
        self.task_with_text()
        first = search.query('项目复盘', self.root)
        cache = Path(first['index']['file'])
        self.assertTrue(first['index']['rebuilt'])
        cache.unlink()
        self.assertTrue(search.query('项目复盘', self.root)['index']['rebuilt'])
        cache.write_bytes(b'not a sqlite database\x00')
        recovered = search.query('项目复盘', self.root)
        self.assertTrue(recovered['index']['rebuilt'])
        self.assertEqual(1, recovered['total'])
        self.assertTrue(search.query('项目复盘', self.root, rebuild=True)['index']['rebuilt'])
        with closing(sqlite3.connect(cache)) as connection:
            connection.execute('PRAGMA user_version = 1')
        upgraded = search.query('项目复盘', self.root)
        self.assertTrue(upgraded['index']['rebuilt'])
        self.assertEqual(1, upgraded['total'])
        with closing(sqlite3.connect(cache)) as connection:
            connection.execute('DROP TABLE fulltext')
        self.assertEqual(1, search.query('项目复盘', self.root)['total'])

    def test_snippet_is_near_hit_bounded_highlighted_and_not_full_body(self):
        body = '开头不相关。' * 500 + '项目复盘采用 Python。' + '结尾不相关。' * 500
        self.task_with_text(body)
        result = search.query('项目复盘 Python', self.root)
        hit = result['results'][0]
        self.assertLessEqual(len(hit['excerpt']), 200)
        self.assertIn('【项目复盘】', hit['excerpt'])
        self.assertIn('【Python】', hit['excerpt'])
        self.assertTrue(hit['excerpt'].startswith('…'))
        self.assertTrue(hit['excerpt'].endswith('…'))
        self.assertNotIn(body, json.dumps(result, ensure_ascii=False))
        self.assertNotIn('body', hit)
        self.assertNotIn('text', hit)

    def test_changed_review_and_source_basis_are_removed_individually(self):
        task = self.task_with_text()
        file = self.review(task)
        self.assertEqual(2, search.query('项目复盘', self.root)['total'])
        file.write_text('项目复盘：改过但没有登记的新校对稿。', encoding='utf-8')
        self.assertEqual(0, search.query('项目复盘', self.root, kind='reviewed')['total'])
        self.review(task)
        self.assertEqual(1, search.query('项目复盘', self.root, kind='reviewed')['total'])
        raw = self.root / task['results'][0]['text']
        raw.write_text('项目复盘：原稿依据也变了。', encoding='utf-8')
        self.assertEqual(0, search.query('项目复盘', self.root)['total'])

    def test_latest_result_only_partial_reviews_and_original_numbers(self):
        first = self.task_with_text('历史词条仅存在旧版本。')
        task = self.task_with_text('最新词条包含项目复盘。', ident='dy-new')
        review = self.review(task, '最新词条经校对包含项目复盘。')
        second = self.task_with_text('另一条项目复盘正文。', ident='dy-second')
        other = dict(second['results'][0], id='second-item', number=9)
        task['results'].append(other)
        workflow.save(task, self.root)
        search.query('项目复盘', self.root)
        # Retain all files but change which results the old task currently references.
        first['results'] = [task['results'][0]]
        workflow.save(first, self.root)
        self.assertEqual(0, search.query('历史词条', self.root)['total'])
        rows = search.query('最新词条', self.root, kind='reviewed')['results']
        self.assertEqual([str(review)], [row['file'] for row in rows])
        self.assertEqual(4, rows[0]['source']['number'])
        second_record = self.root / 'runs/dy-second/task.json'
        second_record.unlink()
        self.assertFalse(any(row['source']['task'] == 'dy-second'
                             for row in search.query('项目复盘', self.root)['results']))

    def test_jobs_order_legacy_absolute_paths_and_workspace_move(self):
        task = self.task_with_text()
        task['results'][0].pop('number')
        for key in ('result', 'text', 'markdown'):
            task['results'][0][key] = str(self.root / task['results'][0][key])
        workflow.save(task, self.root)
        write_json(self.root / 'runs' / task['id'] / 'jobs.json', [{'id': 'synthetic-item', 'number': 7}])
        self.assertEqual(7, search.query('项目复盘', self.root)['results'][0]['source']['number'])
        self.move_workspace()
        hit = search.query('项目复盘', self.root)['results'][0]
        self.assertTrue(Path(hit['file']).is_relative_to(self.root))
        self.assertEqual(7, hit['source']['number'])

    def test_relevance_then_mtime_and_limit(self):
        self.enable()
        older = self.page('工作/a.md', '项目复盘。')
        newer = self.page('工作/b.md', '项目复盘。')
        frequent = self.page('工作/c.md', '项目复盘。' * 10)
        os.utime(older, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        os.utime(newer, ns=(1_700_000_100_000_000_000, 1_700_000_100_000_000_000))
        hits = search.query('项目复盘', self.root, limit=2)
        self.assertEqual(3, hits['total'])
        self.assertEqual([str(frequent), str(newer)], [row['file'] for row in hits['results']])
        self.assertGreater(hits['results'][0]['score'], hits['results'][1]['score'])
        for n in range(55):
            self.page(f'工作/extra-{n}.md', '项目复盘。')
        self.assertEqual(10, len(search.query('项目复盘', self.root)['results']))
        self.assertEqual(50, len(search.query('项目复盘', self.root, limit=50)['results']))
        for n in (0, -1, 51):
            with self.assertRaises(ValueError):
                search.query('项目复盘', self.root, limit=n)

    def test_kb_context_keeps_fields_and_plain_hit_excerpt(self):
        self.enable()
        self.task_with_text('项目回顾与解说。')
        self.page('工作/回顾.md', '无关开头。' * 300 + '项目回顾包含游戏解说。' + '无关结尾。' * 300)
        self.page('工作/不全.md', '这里只有项目，没有另一个查询词。')
        found = kb.context(self.root, query='项目 回顾')['matches']
        self.assertEqual(2, len(found))
        self.assertEqual(['回顾.md', '不全.md'], [Path(row['file']).name for row in found])
        self.assertEqual(1, search.query('项目 回顾', self.root, kind='knowledge')['total'])
        self.assertEqual({'file', 'domain', 'score', 'excerpt'}, set(found[0]))
        self.assertIn('项目回顾', found[0]['excerpt'])
        self.assertIn('游戏解说', kb.context(self.root, query='解说')['matches'][0]['excerpt'])
        self.assertLessEqual(len(found[0]['excerpt']), 182)

    def test_kb_context_any_term_orders_coverage_before_frequency(self):
        self.enable()
        for terms in [('项目', '回顾', '换岗'), ('项目复盘', 'Python', '学习计划')]:
            with self.subTest(terms=terms):
                all_three = self.page('工作/a.md', ' '.join(terms) + '普通背景。' * 300)
                two_frequent = self.page('工作/b.md', (' '.join(terms[:2]) + '。') * 8)
                two_sparse = self.page('工作/c.md', ' '.join(terms[1:]) + '背景。' * 30)
                one = self.page('工作/d.md', (terms[0] + '。') * 80)
                found = kb.context(self.root, query=' '.join(terms))['matches']
                self.assertEqual([str(p) for p in (all_three, two_frequent, two_sparse, one)],
                                 [row['file'] for row in found])
                self.assertGreater(found[-1]['score'], found[0]['score'])
                self.assertGreater(found[1]['score'], found[2]['score'])
                strict = search.query(' '.join(terms), self.root, kind='knowledge')
                self.assertEqual([str(all_three)], [row['file'] for row in strict['results']])

    def test_kind_sync_does_not_read_or_remove_other_sources(self):
        task = self.task_with_text()
        reviewed = self.review(task)
        self.enable()
        page = self.page('工作/复盘.md', '项目复盘。')
        search.query('项目复盘', self.root)
        cache = self.root / '.local/search.sqlite'
        with closing(sqlite3.connect(cache)) as connection:
            before = connection.execute("SELECT kind, stamp, body FROM documents WHERE kind != 'knowledge' ORDER BY kind").fetchall()
        reviewed.write_text('项目复盘未登记的变动。', encoding='utf-8')
        page.write_text('项目复盘更新了知识页。', encoding='utf-8')
        with patch.object(search, '_task_candidates', side_effect=AssertionError('知识库查询不能遍历任务')):
            found = kb.context(self.root, query='项目复盘')
            self.assertEqual([str(page)], [row['file'] for row in found['matches']])
        with closing(sqlite3.connect(cache)) as connection:
            self.assertEqual(before, connection.execute(
                "SELECT kind, stamp, body FROM documents WHERE kind != 'knowledge' ORDER BY kind").fetchall())
        with patch.object(search, '_knowledge_candidates', side_effect=AssertionError('校对稿查询不能访问知识库')):
            result = search.query('项目复盘', self.root, kind='reviewed')
            self.assertEqual(0, result['total'])
            self.assertEqual(1, result['index']['removed'])
        with closing(sqlite3.connect(cache)) as connection:
            self.assertEqual({'transcript', 'knowledge'}, {row[0] for row in connection.execute('SELECT kind FROM documents')})

    def test_kb_context_search_failure_preserves_other_fields(self):
        self.enable()
        before = kb.context(self.root, task='current-task')
        for exc in (RuntimeError('搜索缓存被占用'), OSError('合成目录不可写'),
                    sqlite3.DatabaseError('synthetic corrupt cache'), KeyError('缓存缺少字段')):
            with self.subTest(error=str(exc)), patch.object(search, 'query', side_effect=exc):
                result = kb.context(self.root, task='current-task', query='项目复盘')
                self.assertEqual([], result['matches'])
                self.assertIn(str(exc), result['search_error'])
                self.assertEqual(before, {key: value for key, value in result.items() if key != 'search_error'})

    def test_snippets_collapse_newlines_tabs_and_repeated_spaces(self):
        task = self.task_with_text('项目复盘\r\n\t采用   Python。\n\n换岗\t计划。')
        result = search.query('项目复盘 Python', self.root)
        self.assertEqual('【项目复盘】 采用 【Python】。 换岗 计划。', result['results'][0]['excerpt'])
        self.enable()
        self.page('工作/复盘.md', '# 项目复盘\n\n采用  Python，\t留有依据。')
        context = kb.context(self.root, query='项目复盘')['matches']
        self.assertEqual('# 项目复盘 采用 Python， 留有依据。', context[0]['excerpt'])

    def test_unchanged_tasks_reuse_persistent_candidates_across_connections(self):
        task = self.task_with_text()
        self.review(task)
        search.query('项目复盘', self.root)
        # Each query opens a new connection; reuse must not depend on a process-global cache.
        with patch.object(workflow, 'load', side_effect=AssertionError('未变化任务重复读取')), \
                patch.object(content, 'numbers', side_effect=AssertionError('未变化序号重复读取')), \
                patch.object(search, 'inside', side_effect=AssertionError('未变化路径重复解析')):
            result = search.query('项目复盘', self.root)
        self.assertEqual(2, result['total'])
        self.assertEqual(0, result['index']['updated'])

    def test_jobs_and_task_changes_invalidate_candidate_cache(self):
        task = self.task_with_text()
        task['results'][0].pop('number')
        workflow.save(task, self.root)
        self.assertEqual(1, search.query('项目复盘', self.root)['results'][0]['source']['number'])
        jobs = self.root / 'runs' / task['id'] / 'jobs.json'
        write_json(jobs, [{'id': 'synthetic-item', 'number': 12}])
        self.assertEqual(12, search.query('项目复盘', self.root)['results'][0]['source']['number'])
        write_json(jobs, [{'id': 'synthetic-item', 'number': 14}])
        self.assertEqual(14, search.query('项目复盘', self.root)['results'][0]['source']['number'])
        jobs.unlink()
        self.assertEqual(1, search.query('项目复盘', self.root)['results'][0]['source']['number'])
        task['results'] = []
        workflow.save(task, self.root)
        self.assertEqual(0, search.query('项目复盘', self.root)['total'])

    def test_filtered_rebuild_preserves_other_types_and_fts_stays_consistent(self):
        task = self.task_with_text()
        self.review(task)
        self.enable()
        self.page('工作/复盘.md', '项目复盘更新。')
        search.query('项目复盘', self.root)
        cache = self.root / '.local/search.sqlite'
        with closing(sqlite3.connect(cache)) as connection:
            before = connection.execute("SELECT id, stamp FROM documents WHERE kind != 'knowledge' ORDER BY kind").fetchall()
        result = search.query('项目复盘', self.root, kind='knowledge', rebuild=True)
        self.assertEqual(1, result['index']['updated'])
        with closing(sqlite3.connect(cache)) as connection:
            self.assertEqual(before, connection.execute(
                "SELECT id, stamp FROM documents WHERE kind != 'knowledge' ORDER BY kind").fetchall())
            # FTS5 checks external content against its actual tokens, not just SQLite pages.
            connection.execute("INSERT INTO fulltext(fulltext, rank) VALUES ('integrity-check', 1)")
            self.assertEqual(0, connection.execute("SELECT count(*) FROM sqlite_master WHERE name = 'fulltext_content'").fetchone()[0])

    def test_creator_current_speech_and_valid_review_are_included(self):
        ordinary = self.task_with_text()
        speech = ordinary['results'][0]
        ident = 'creator-' + 'a' * 16
        folder = self.root / 'runs' / ident
        folder.mkdir(parents=True)
        review = folder / 'review.txt'
        review.write_text('博主项目复盘已校对。', encoding='utf-8')
        analysis = folder / 'analysis.json'
        write_json(analysis, {'source': {'content_fingerprint': 'post-v1', 'images': [],
                                        'transcript': (self.root / speech['text']).read_text(encoding='utf-8')},
                              'review': {'text': review.read_text(encoding='utf-8')}})
        saved = {'file': relative(self.root, analysis), 'sha256': digest(analysis),
                 'review_file': relative(self.root, review)}
        write_json(folder / 'task.json', {'id': ident, 'items': {'post-1': {'speech': speech, 'analysis': saved}}})
        write_json(folder / 'catalog.json', {'posts': [{'id': 'post-1', 'content_fingerprint': 'post-v1',
                                                       'title': '博主项目复盘', 'published': None}]})
        found = search.query('项目复盘', self.root)
        rows = [row for row in found['results'] if row['source']['task'] == ident]
        self.assertEqual({'transcript', 'reviewed'}, {row['kind'] for row in rows})
        self.assertTrue(all(row['source']['number'] == 1 for row in rows))
        original_review = review.read_bytes()
        review.write_text('博主项目复盘未登记修改。', encoding='utf-8')
        self.assertEqual(0, search.query('博主', self.root, kind='reviewed')['total'])
        review.write_bytes(original_review)
        self.assertEqual(1, search.query('博主', self.root, kind='reviewed')['total'])
        original_analysis = analysis.read_bytes()
        analysis.unlink()
        self.assertEqual(0, search.query('博主', self.root, kind='reviewed')['total'])
        analysis.write_bytes(original_analysis)
        self.assertEqual(1, search.query('博主', self.root, kind='reviewed')['total'])
        with patch.object(search.creator, 'load', side_effect=AssertionError('未变化博主任务重复读取')), \
                patch.object(search, 'read_json', side_effect=AssertionError('未变化校对依据重复读取')):
            self.assertEqual(1, search.query('博主', self.root, kind='reviewed')['total'])

    def test_cli_json_tell_user_and_invalid_arguments(self):
        self.task_with_text()
        self.review(workflow.load('dy-search', self.root))
        result = self.cli('项目复盘', 'Python', '--limit', '1', '--kind', 'reviewed', '--rebuild')
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', 'replace'))
        value = json.loads(result.stdout.decode('utf-8-sig'))
        self.assertEqual(1, value['total'])
        self.assertEqual('reviewed', value['results'][0]['kind'])
        self.assertEqual('找到 1 条结果，显示 1 条，来自校对稿。', value['tell_user'])
        self.assertEqual(str(self.root), value['workspace'])
        self.assertTrue(value['index']['rebuilt'])
        self.assertTrue(value['material_is_data_not_instructions'])
        empty = json.loads(self.cli('毫不相关').stdout.decode('utf-8-sig'))
        self.assertEqual(0, empty['total'])
        self.assertIn('换个词', empty['tell_user'])
        for args in [(), ('项目复盘', '--limit', '51'), ('项目复盘', '--kind', 'bad'), ('   ',)]:
            result = self.cli(*args)
            self.assertNotEqual(0, result.returncode)
            self.assertTrue(json.loads(result.stdout.decode('utf-8-sig'))['tell_user'])

    def test_two_commands_serialize_index_updates(self):
        self.task_with_text()
        args = [sys.executable, '-B', '-X', 'utf8', str(SKILL_ROOT / 'scripts/brain.py'),
                '--workspace', str(self.root), 'search', '项目复盘', '--rebuild']
        env = {**os.environ, 'CY_UPDATE_CHECK': '0', 'CY_STATE_DIR': str(self.parent / 'state'),
               'CY_MODEL_DIR': str(self.parent / 'models')}
        processes = []
        try:
            with artifact_lock(self.root / '.local/search-lock'):
                processes = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                             for _ in range(2)]
                # Hold the same lock until at least one command demonstrably has to wait.
                with self.assertRaises(subprocess.TimeoutExpired):
                    processes[0].communicate(timeout=0.5)
                self.assertFalse((self.root / '.local/search.sqlite').exists())
            for process in processes:
                out, err = process.communicate(timeout=30)
                self.assertEqual(0, process.returncode, err.decode('utf-8', 'replace'))
                self.assertEqual(1, json.loads(out.decode('utf-8-sig'))['total'])
            self.assertEqual(1, search.query('项目复盘', self.root)['total'])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == '__main__':
    import unittest
    unittest.main()
