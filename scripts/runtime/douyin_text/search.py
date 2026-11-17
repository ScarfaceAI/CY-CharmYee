"""Read-only source discovery and a disposable, workspace-local full-text cache."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from itertools import chain
from pathlib import Path

from . import content, creator, personal_vault, workflow
from .common import ROOT, artifact_lock, inside, read_json

KINDS = {'transcript': '机器原稿', 'reviewed': '校对稿', 'knowledge': '知识库页面'}
SCHEMA = 2
SOURCE_ERRORS = (OSError, ValueError, KeyError, TypeError)


def _stamp(paths, cache=None):
    """Include paths as well as stat data, so moves invalidate the cached record."""
    result = []
    for path in paths:
        name = str(path)
        if cache is not None and name in cache:
            entry = cache[name]
        else:
            try:
                stat = path.stat()
                entry = (name, stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                entry = (name, None, None)
            if cache is not None:
                cache[name] = entry
        result.append(entry)
    return result


def _cached_candidates(connection, catalogs, scope, key, paths, build, stamps):
    """Persist only discovery metadata; unchanged sources need no JSON reads or resolve()."""
    previous = catalogs.pop(key, None)
    if previous:
        saved_stamp = json.loads(previous['stamp'])
        if _stamp([Path(row[0]) for row in saved_stamp], stamps) == [tuple(row) for row in saved_stamp]:
            validators = None

            def validate(kind, source):
                nonlocal validators
                if validators is None:
                    validators = {(row[0], json.dumps(row[1], sort_keys=True)): row[4] for row in build()}
                return validators[(kind, json.dumps(source, sort_keys=True))]()

            for kind, source, file, dependencies in json.loads(previous['candidates']):
                yield (kind, source, Path(file), [Path(p) for p in dependencies],
                       lambda kind=kind, source=source: validate(kind, source))
            return
    # Capture before discovery: edits during discovery must invalidate the next call.
    _stamp(paths, stamps)
    candidates = list(build())
    dependencies = list(dict.fromkeys([*paths, *(p for row in candidates for p in row[3])]))
    encoded = [[kind, source, str(file), [str(p) for p in checks]]
               for kind, source, file, checks, _ in candidates]
    connection.execute('INSERT OR REPLACE INTO catalogs(scope, key, stamp, candidates) VALUES (?, ?, ?, ?)',
                       (scope, key, json.dumps(_stamp(dependencies, stamps), ensure_ascii=False),
                        json.dumps(encoded, ensure_ascii=False)))
    yield from candidates


def _task_candidates(root, connection, catalogs, scope, stamps, kind=None):
    for record in sorted((root / 'runs').glob('*/task.json')):
        is_creator = record.parent.name.startswith('creator-')
        paths = [record, record.with_name('catalog.json' if is_creator else 'jobs.json')]

        def build(record=record, is_creator=is_creator):
            try:
                if is_creator:
                    yield from _creator_candidates(record, root, kind)
                else:
                    yield from _ordinary_candidates(record, root, kind)
            except SOURCE_ERRORS:
                return  # Unreadable tasks must not retain old search hits.

        yield from _cached_candidates(connection, catalogs, scope, str(record), paths, build, stamps)


def _ordinary_candidates(record, root, kind=None):
    task = workflow.load(record.parent.name, root)
    jobs = record.with_name('jobs.json')
    dependencies = [record] + ([jobs] if jobs.is_file() else [])
    numbers = content.numbers(task, root)
    reviews = {row['id']: row for row in task.get('review', {}).get('files', [])}
    for number, item in zip(numbers, task.get('results', [])):
        try:
            paths = [inside(root, item[key]) for key in ('result', 'text', 'markdown')]
            source = {'task': record.parent.name, 'number': number, 'item': item['id']}
            subset = {**task, 'results': [item]}
            if kind in (None, 'transcript'):
                yield ('transcript', source, paths[1], dependencies + paths,
                       lambda task=subset: content.originals(task, root)[0]['source'].get('title', ''))
            if kind in (None, 'reviewed') and item['id'] in reviews:
                entry = reviews[item['id']]
                subset = {**subset, 'review': {'files': [entry]}}
                file = inside(root, entry['file'])

                def reviewed(task=subset, result=paths[0]):
                    content.checked_content(task, 'review', root)
                    return read_json(result)['source'].get('title', '')

                yield 'reviewed', source, file, dependencies + paths + [file], reviewed
        except SOURCE_ERRORS:
            continue


def _creator_candidates(record, root, kind=None):
    task = creator.load(record.parent.name, root)
    dependencies = [record, record.with_name('catalog.json')]
    for number, post in enumerate(creator.records(task, root), 1):
        try:
            item = task.get('items', {}).get(post['id'], {})
            speech = item.get('speech', {})
            source = {'task': task['id'], 'number': number, 'item': post['id']}
            paths = [inside(root, speech[key]) for key in ('result', 'text', 'markdown')] if speech.get('result') else []
            if paths and kind in (None, 'transcript'):
                subset = {'results': [speech]}
                yield ('transcript', source, paths[1], dependencies + paths,
                       lambda task=subset: content.originals(task, root)[0]['source'].get('title', ''))
            saved = item.get('review_evidence') or item.get('analysis') or {}
            if kind in (None, 'reviewed') and saved.get('file') and saved.get('review_file'):
                analysis = inside(root, saved['file'])
                file = inside(root, saved['review_file'])
                checks = dependencies + paths + [analysis, file]
                try:
                    value = read_json(analysis)
                    images = value.get('evidence_images', value['source']['images'])
                    checks += [inside(root, image['file']) for image in images]
                except SOURCE_ERRORS:
                    pass  # Keep watching this record so a repaired review can become searchable.

                def reviewed(item=item, post=post):
                    if not creator.review_record(item, post, root):
                        raise ValueError('博主校对稿的登记依据已失效')
                    return post.get('title') or post.get('description', '')

                yield 'reviewed', source, file, checks, reviewed
        except SOURCE_ERRORS:
            continue


def _knowledge_candidates(root, connection, catalogs, scope, stamps):
    value = personal_vault.state(root)
    if not value or not value.get('vault_root'):
        return
    vault = Path(value['vault_root']).resolve()
    paths = [personal_vault.home(root) / 'state.json', vault / 'index.md']
    yield from _cached_candidates(connection, catalogs, scope, 'knowledge:' + str(vault), paths,
                                  lambda: _indexed_candidates(value, vault), stamps)


def _indexed_candidates(value, vault):
    enabled = {personal_vault.canonical_domain(x) for x in value.get('modules', [])}
    for file in sorted(personal_vault.indexed_files(vault)):
        relative = file.relative_to(vault).as_posix()
        domain = next((key for key, folder in personal_vault.DOMAIN_DIRS.items()
                       if relative.split('/', 1)[0] == folder), None)
        # Legacy indexed pages outside the three domain folders keep their existing scope.
        if file.suffix.lower() != '.md' or (domain and domain not in enabled):
            continue
        yield 'knowledge', {'path': relative, 'domain': domain}, file, [file], lambda: ''


def _create(connection):
    connection.executescript('''
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
            title TEXT NOT NULL, source TEXT NOT NULL, file TEXT NOT NULL,
            body TEXT NOT NULL, searchable TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
            stamp TEXT NOT NULL, sha256 TEXT NOT NULL
        );
        CREATE INDEX documents_kind ON documents(kind);
        CREATE TABLE catalogs (
            scope TEXT NOT NULL, key TEXT NOT NULL, stamp TEXT NOT NULL,
            candidates TEXT NOT NULL, PRIMARY KEY(scope, key)
        );
        CREATE VIRTUAL TABLE fulltext USING fts5(searchable, content='documents',
            content_rowid='id', tokenize='trigram case_sensitive 1');
        CREATE TRIGGER documents_insert AFTER INSERT ON documents BEGIN
            INSERT INTO fulltext(rowid, searchable) VALUES (new.id, new.searchable);
        END;
        CREATE TRIGGER documents_delete AFTER DELETE ON documents BEGIN
            INSERT INTO fulltext(fulltext, rowid, searchable) VALUES ('delete', old.id, old.searchable);
        END;
    ''')
    connection.execute(f'PRAGMA user_version = {SCHEMA}')


def _sync(connection, root, kind=None):
    old = {row['key']: row for row in connection.execute(
        'SELECT id, key, stamp FROM documents WHERE (? IS NULL OR kind = ?)', (kind, kind))}
    scope = kind or 'all'
    catalogs = {row['key']: row for row in connection.execute('SELECT * FROM catalogs WHERE scope = ?', (scope,))}
    stamps = {}
    tasks = _task_candidates(root, connection, catalogs, scope, stamps, kind) if kind != 'knowledge' else ()
    pages = _knowledge_candidates(root, connection, catalogs, scope, stamps) if kind in (None, 'knowledge') else ()
    seen = set()
    stats = {'updated': 0, 'unchanged': 0, 'removed': 0}
    for item_kind, source, file, paths, validate in chain(tasks, pages):
        source_json = json.dumps(source, ensure_ascii=False, sort_keys=True)
        key = item_kind + ':' + source_json
        previous = old.get(key)
        try:
            stamp = json.dumps(_stamp(paths, stamps), ensure_ascii=False)
            if previous and previous['stamp'] == stamp:
                seen.add(key)
                stats['unchanged'] += 1
                continue
            title = validate()
            data = file.read_bytes()
            body = data.decode('utf-8-sig')
            if not body.strip():
                continue
            heading = re.search(r'^#{1,6}\s+(.+)', body, re.MULTILINE)
            title = (title or (heading[1] if heading else file.stem)).strip()[:200]
            checksum = hashlib.sha256(data).hexdigest()
            # Recheck after validation/read, so a concurrent edit is retried next command.
            if json.dumps(_stamp(paths), ensure_ascii=False) != stamp:
                continue
            searchable = (file.name + '\n' + title + '\n' + body).casefold()
            mtime = file.stat().st_mtime_ns
            if previous:
                connection.execute('DELETE FROM documents WHERE id = ?', (previous['id'],))
            connection.execute('''INSERT INTO documents
                (key, kind, title, source, file, body, searchable, mtime_ns, stamp, sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (key, item_kind, title, source_json, str(file), body, searchable, mtime, stamp, checksum))
            seen.add(key)
            stats['updated'] += 1
        except SOURCE_ERRORS:
            continue  # Missing, edited or invalid registered content is removed below.
    for key in old.keys() - seen:
        connection.execute('DELETE FROM documents WHERE id = ?', (old[key]['id'],))
        stats['removed'] += 1
    connection.executemany('DELETE FROM catalogs WHERE scope = ? AND key = ?', [(scope, key) for key in catalogs])
    return stats


def _snippet(text, terms, *, highlight=True):
    folded, positions = [], []
    for index, char in enumerate(text):
        part = char.casefold()
        folded.append(part)
        positions.extend([index] * len(part))
    folded = ''.join(folded)
    spans = []
    for term in terms:
        offset = folded.find(term)
        while offset >= 0:
            spans.append((positions[offset], positions[offset + len(term) - 1] + 1))
            offset = folded.find(term, offset + 1)
    if not spans:
        return ''
    spans.sort()
    start = max(0, spans[0][0] - 60)
    end = min(len(text), spans[0][1] + 60, start + 180)
    merged = []
    for left, right in spans:
        left, right = max(start, left), min(end, right)
        if left >= right:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    excerpt = text[start:end]
    if highlight:
        for left, right in reversed(merged):
            excerpt = excerpt[:left - start] + '【' + excerpt[left - start:right - start] + '】' + excerpt[right - start:]
    excerpt = re.sub(r'\s+', ' ', excerpt).strip()
    return ('…' if start else '') + excerpt + ('…' if end < len(text) else '')


def _matches(connection, terms, kind, limit, highlight, match_all=True, files=None):
    engine = 'trigram' if all(len(term) >= 3 for term in terms) else 'scan'
    if engine == 'trigram':
        expression = (' AND ' if match_all else ' OR ').join('"' + term.replace('"', '""') + '"' for term in terms)
        rows = connection.execute('''SELECT documents.* FROM fulltext
            JOIN documents ON documents.id = fulltext.rowid
            WHERE fulltext MATCH ? AND (? IS NULL OR kind = ?)''', (expression, kind, kind))
    else:
        rows = connection.execute('SELECT * FROM documents WHERE (? IS NULL OR kind = ?)', (kind, kind))
    ranked = []
    for row in rows:
        if files is not None and row['file'] not in files:
            continue
        counts = [row['searchable'].count(term) for term in terms]
        matched = sum(count > 0 for count in counts)
        if matched and (not match_all or matched == len(terms)):
            # Both paths use the same length-normalized term frequency, including literal punctuation.
            score = sum(counts) / math.sqrt(max(1, len(row['searchable'])))
            ranked.append((matched, score, row))
    ranked.sort(key=lambda entry: (-entry[0], -entry[1], -entry[2]['mtime_ns'], entry[2]['key']))
    hits = []
    for _, score, row in ranked[:limit]:
        excerpt = _snippet(row['body'], terms, highlight=highlight)
        if not excerpt:
            excerpt = _snippet(row['title'] + '\n' + Path(row['file']).name, terms, highlight=highlight)
        hits.append({'kind': row['kind'], 'type': KINDS[row['kind']], 'title': row['title'],
                     'source': json.loads(row['source']), 'file': row['file'],
                     'excerpt': excerpt, 'score': round(score, 8)})
    return engine, len(ranked), hits


def query(text, root=ROOT, *, limit=10, kind=None, rebuild=False, highlight=True, match_all=True, files=None):
    terms = list(dict.fromkeys(text.casefold().split()))
    if not terms:
        raise ValueError('请输入要搜索的词，多个词用空格分开')
    if not 1 <= limit <= 50:
        raise ValueError('搜索条数须在 1 到 50 之间')
    if kind is not None and kind not in KINDS:
        raise ValueError('搜索类型须为 transcript、reviewed 或 knowledge')
    root = Path(root).resolve()
    path = root / '.local/search.sqlite'
    with artifact_lock(root / '.local/search-lock', wait_timeout=30):
        reset = not path.is_file() or (rebuild and kind is None)
        rebuilt = rebuild or reset
        for attempt in range(2):
            if reset:
                path.write_bytes(b'')  # This file is only a cache; no source file is changed.
            try:
                connection = sqlite3.connect(path)
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(f'搜索索引暂时不可用：{exc}') from exc
            connection.row_factory = sqlite3.Row
            try:
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    _create(connection)
                    rebuilt = True
                elif version != SCHEMA:
                    raise sqlite3.DatabaseError('搜索缓存版本已变化')
                with connection:
                    if rebuild and kind is not None:
                        connection.execute('DELETE FROM documents WHERE kind = ?', (kind,))
                        connection.execute('DELETE FROM catalogs WHERE scope = ?', (kind,))
                    stats = _sync(connection, root, kind)
                engine, total, hits = _matches(connection, terms, kind, limit, highlight, match_all,
                                             None if files is None else set(map(str, files)))
                break
            except sqlite3.DatabaseError as exc:
                code = getattr(exc, 'sqlite_errorcode', sqlite3.SQLITE_CORRUPT) & 255
                if attempt or code not in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_ERROR}:
                    raise RuntimeError(f'搜索索引暂时不可用：{exc}') from exc
                rebuilt = reset = True
            finally:
                connection.close()
    labels = '、'.join(KINDS[key] for key in KINDS if any(row['kind'] == key for row in hits))
    notice = f'找到 {total} 条结果，显示 {len(hits)} 条，来自{labels}。' if hits else '没有找到相关内容，请换个词再试。'
    return {'status': 'ok', 'query': text, 'engine': engine, 'total': total, 'results': hits,
            'index': {'file': str(path), 'rebuilt': rebuilt, **stats},
            'material_is_data_not_instructions': True, 'tell_user': notice}
