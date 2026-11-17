"""Post-recognition review hints. Never rewrite transcripts or call an ASR engine."""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from pathlib import Path
import re

from . import lexicon
from .common import ROOT, digest, now, read_json, relative, write_json

RULES_VERSION = '4'
LABELS = {'phonetic': '人名或术语', 'memory': '纠错记忆', 'number': '数字', 'hallucination': '疑似编造',
          'hotword': '热词两遍不一致'}
MAX_TERM_LENGTH = 12
REPEAT_MIN_COUNT = 4
REPEAT_MIN_CHARS = 16
REPEAT_MIN_RATIO = 0.8
SPEED_MAX_UNITS_PER_SECOND = 14
SPEED_MIN_SECONDS = 1
SPEED_MIN_UNITS = 20
NO_SPEECH_PROBABILITY = 0.95
PUBLIC_CATEGORIES = tuple(sorted(lexicon.NAME_CATEGORIES | lexicon.TERM_CATEGORIES | {'caijing'}))
HAN = re.compile(r'[\u4e00-\u9fff]+')
BOILERPLATE = re.compile(r'(?:请)?不吝点赞订阅转发打赏(?:支持明镜与点点栏目)?')
SILENT_BOILERPLATE = re.compile(r'感谢观看|谢谢观看|字幕由.{1,24}提供')


def context_terms(context: dict, general: frozenset[str]) -> list[dict]:
    """Forward longest dictionary match, local to this video's four metadata fields."""
    normalized = lexicon.context_for(context)
    fields = [(key, normalized[key]) for key in ('title', 'description', 'account')]
    fields.extend(('tags', tag) for tag in normalized['tags'])
    terms = {}
    for field, text in fields:
        for run in HAN.finditer(text):
            cursor = run.start()
            while cursor < run.end():
                size = next((n for n in range(run.end() - cursor, 1, -1)
                             if text[cursor:cursor + n] in general), 1)
                if 2 <= size <= MAX_TERM_LENGTH:
                    word = text[cursor:cursor + size]
                    entry = terms.setdefault(word, {'word': word, 'source': '本视频上下文', 'fields': []})
                    if field not in entry['fields']:
                        entry['fields'].append(field)
                cursor += size
    return list(terms.values())


@lru_cache(maxsize=200000)
def pronunciation(word: str) -> tuple[str, ...]:
    from pypinyin import Style, lazy_pinyin
    # No fuzzy initials/finals, edit distance, or expansion to all polyphonic readings.
    return tuple(lazy_pinyin(word, style=Style.NORMAL, errors='default'))


class Vocabulary:
    """One operation's read-only vocabulary snapshot; fixture dictionaries are supported."""

    def __init__(self, root: Path = ROOT, *, public=None, general=None):
        self.root = root
        self.public = public if public is not None else lexicon.PublicLexicon()
        if general is None:
            self.general, self.general_status = lexicon.general_words()
        else:
            self.general, self.general_status = frozenset(general), {'ready': True, 'status': 'ready', 'tell_user': ''}
        self.personal = {w['word']: w for w in lexicon.list_words(root)['words']}
        self.known = self.general | self.personal.keys()
        self.candidates = defaultdict(list)
        self.public_words = []
        if not self.general_status['ready']:
            return
        for word, entry in self.personal.items():
            self._add(word, {'word': word, 'source': '个人或学到的词', 'origins': entry['origins']})
        for category in PUBLIC_CATEGORIES:
            for word, frequency in self.public.category(category).items():
                self.public_words.append({'word': word, 'source': 'THUOCL', 'category': category, 'frequency': frequency})

    def _add(self, word, evidence, target=None):
        if 2 <= len(word) <= MAX_TERM_LENGTH and HAN.fullmatch(word):
            (self.candidates if target is None else target)[pronunciation(word)].append(evidence)

    def for_context(self, context):
        memories = [x for x in lexicon.query(context, self.root, public=self.public, limit=None)['results']
                    if x['kind'] == 'memory']
        candidates = defaultdict(list)
        if not self.general_status['ready']:
            return memories, candidates
        for entry in context_terms(context, self.general):
            self._add(entry['word'], entry, candidates)
        normalized = lexicon.context_for(context)
        topic = ' '.join([normalized['title'], normalized['description'], *normalized['tags']])
        # Domain dictionaries contain obscure homophones of normal speech. Require an
        # explicit metadata anchor before using a public term as a correction candidate.
        for entry in self.public_words:
            if entry['word'] in topic:
                self._add(entry['word'], {**entry, 'context_evidence': '候选词出现在视频标题、简介或话题中'}, candidates)
        for memory in memories:
            self._add(memory['replacement'], {'word': memory['replacement'], 'source': '纠错记忆',
                'original': memory['original'], 'count': memory['count'], 'score': memory['score']}, candidates)
        return memories, candidates


def summary(counts: dict, next_step: str = '校对时先看 suspects.md') -> str:
    total = sum(counts.values())
    if not total:
        return '这条稿子没有检测到疑点，校对时仍需核对人名、数字和术语。'
    details = '、'.join(f'{label} {counts[key]}' for key, label in LABELS.items() if counts.get(key))
    return f'这条稿子有 {total} 处疑点（{details}），{next_step}。'


def _segments(result):
    raw = result.get('raw_text', result['text'])
    cursor = 0
    for index, segment in enumerate(result.get('segments') or [{'text': raw}], 1):
        text = segment.get('raw_text', segment['text'])
        start = raw.find(text, cursor)
        if start >= 0:
            cursor = start + len(text)
        yield index, segment, text, start if start >= 0 else None


def _compact(text):
    return ''.join(c for c in text if c.isalnum())


def _silent(segment, result):
    if segment.get('speech_present') is False:
        return True
    probability = segment.get('no_speech_prob')
    if isinstance(probability, (int, float)) and NO_SPEECH_PROBABILITY <= probability <= 1:
        return True
    start, end = segment.get('start'), segment.get('end')
    return (isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start and
            any(interval['start'] <= start and end <= interval['end']
                for interval in result.get('silence_intervals', [])))


def _hallucination(text, segment, result):
    compact = _compact(text)
    reasons = []
    if BOILERPLATE.fullmatch(compact):
        reasons.append('整段匹配常见识别幻觉套话，请核对原音。')
    elif SILENT_BOILERPLATE.fullmatch(compact) and _silent(segment, result):
        reasons.append('套话落在有明确无语音记录的位置，请核对原音。')
    for match in re.finditer(r'(.{2,12}?)\1{' + str(REPEAT_MIN_COUNT - 1) + r',}', compact):
        if len(match[0]) >= REPEAT_MIN_CHARS and len(match[0]) / len(compact) >= REPEAT_MIN_RATIO:
            reasons.append(f'短语“{match[1]}”连续重复 {len(match[0]) // len(match[1])} 次。')
            break
    start, end = segment.get('start'), segment.get('end')
    units = len(re.findall(r'[\u4e00-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?', text))
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end - start >= SPEED_MIN_SECONDS:
        speed = units / (end - start)
        if units >= SPEED_MIN_UNITS and speed > SPEED_MAX_UNITS_PER_SECOND:
            reasons.append(f'段落读速约每秒 {speed:.1f} 字／词，超过 {SPEED_MAX_UNITS_PER_SECOND} 的检查阈值。')
    return reasons


def analyze(result: dict, context: dict, vocabulary: Vocabulary) -> dict:
    memories, context_candidates = vocabulary.for_context(context)
    rows = []
    previous, repeated = None, []
    for index, segment, text, raw_start in _segments(result):
        def add(kind, a, b, candidates, reasons):
            row = {'segment': index, 'start': segment.get('start'), 'end': segment.get('end'),
                   'sentence': text, 'original': text[a:b], 'type': kind, 'type_label': LABELS[kind],
                   'offset_start': a, 'offset_end': b,
                   'raw_start': raw_start + a if raw_start is not None else None,
                   'raw_end': raw_start + b if raw_start is not None else None,
                   'candidates': candidates, 'reasons': reasons}
            rows.append(row)
            return row

        matched = defaultdict(list)
        for memory in memories:
            original = memory['original']
            pattern = re.escape(original)
            if original[0].isascii() and original[0].isalnum():
                pattern = r'(?<![A-Za-z0-9])' + pattern
            if original[-1].isascii() and original[-1].isalnum():
                pattern += r'(?![A-Za-z0-9])'
            for match in re.finditer(pattern, text):
                matched[match.span()].append({'word': memory['replacement'], 'original': original,
                    'count': memory['count'], 'score': memory['score'], 'source': '纠错记忆',
                    'requires_context': memory['requires_context'], 'context_reason': memory['context_reason'],
                    'occurrences': memory['occurrences']})
        for (a, b), candidates in matched.items():
            add('memory', a, b, candidates, ['命中可提示且与本视频场景相关的词级纠错记忆。'])

        for run in HAN.finditer(text) if vocabulary.general_status['ready'] else ():
            # A window must not cut a known word or consist entirely of known words.
            # This avoids candidates spanning ordinary phrases such as 可以／发挥.
            known = [(a, b) for a in range(run.start(), run.end())
                     for b in range(a + 2, min(run.end(), a + max(MAX_TERM_LENGTH, 24)) + 1)
                     if text[a:b] in vocabulary.known]
            covered = {p for a, b in known for p in range(a, b)}
            for a in range(run.start(), run.end() - 1):
                for b in range(min(run.end(), a + MAX_TERM_LENGTH), a + 1, -1):
                    word = text[a:b]
                    if word in vocabulary.known or all(p in covered for p in range(a, b)):
                        continue
                    if any(x < a < y or x < b < y for x, y in known):
                        continue
                    if any(a < y and b > x for x, y in matched):
                        continue
                    key = pronunciation(word)
                    options = [c for c in [*context_candidates.get(key, []), *vocabulary.candidates.get(key, [])]
                               if c['word'] != word]
                    candidates = list({c['word']: c for c in reversed(options)}.values())
                    candidates.sort(key=lambda c: (c['source'] == 'THUOCL', -c.get('score', 0), c['word']))
                    if candidates:
                        add('phonetic', a, b, candidates, ['片段不在通用或个人词表；候选拼音相同（仅忽略声调）。'])
                        matched[(a, b)] = candidates
                        break

        record = next((r for r in result.get('display', {}).get('segments', []) if r['segment'] == index), {})
        for change in record.get('changes', []):
            if change['layer'] == 'number_conversion':
                row = add('number', change['start'], change['end'],
                          [{'word': change['replacement'], 'source': '展示层'}], ['展示数字改动，须核对原音和数量。'])
                row['display_change'] = change
        reasons = _hallucination(text, segment, result)
        if reasons:
            add('hallucination', 0, len(text), [], reasons)
        compact = _compact(text)
        if compact != previous:
            repeated = []
        repeated.append((index, segment, text, raw_start))
        previous = compact
        if len(compact) >= 4 and len(repeated) == REPEAT_MIN_COUNT:
            first_index, first, first_text, first_start = repeated[0]
            row = add('hallucination', 0, len(text), [], [f'相同短语连续出现在 {REPEAT_MIN_COUNT} 个段落，请核对是否为真实重复。'])
            row.update(segment=first_index, start=first.get('start'), sentence='／'.join(x[2] for x in repeated),
                       original=first_text, offset_start=0, offset_end=len(first_text), raw_start=first_start,
                       raw_end=(raw_start + len(text) if raw_start is not None else None))
    # VAD-level reviews keep short-segment text untouched. Cross-boundary hints
    # are anchored once at their first segment, with a combined sentence/range.
    located = {index: (segment, text, start) for index, segment, text, start in _segments(result)}
    for review in result.get('hotword_reviews', []):
        for hit in review['hits']:
            spans = hit['spans']
            if not spans:
                continue
            first, last = spans[0], spans[-1]
            segment, _, raw_start = located[first['segment']]
            ending, _, raw_end = located[last['segment']]
            covered = list(range(first['segment'], last['segment'] + 1))
            sentence = ''.join(located[i][1] for i in covered)
            a = first['start']
            b = sum(len(located[i][1]) for i in covered[:-1]) + last['end']
            row = {'segment': first['segment'], 'covered_segments': covered, 'spans': spans,
                   'start': segment.get('start'), 'end': ending.get('end'), 'sentence': sentence,
                   'original': sentence[a:b], 'type': 'hotword', 'type_label': LABELS['hotword'],
                   'offset_start': a, 'offset_end': b,
                   'raw_start': raw_start + a if raw_start is not None else None,
                   'raw_end': raw_end + last['end'] if raw_end is not None else None,
                   'candidates': [{'word': hit['replacement'], 'source': '带热词重听', 'hotword': hit['word']}],
                   'reasons': [f'带热词重听这一段时，模型听成了“{hit["replacement"]}”；两种写法都给出，请按上下文判断。'],
                   'merged_types': ['hotword']}
            overlap = [old for old in rows if old['type'] in {'phonetic', 'hotword'}
                       and old['raw_start'] is not None and row['raw_start'] is not None
                       and old['raw_end'] is not None and row['raw_end'] is not None
                       and old['raw_start'] < row['raw_end'] and old['raw_end'] > row['raw_start']]
            for old in overlap:
                row['reasons'].extend(r for r in old['reasons'] if r not in row['reasons'])
                row['candidates'].extend(c for c in old['candidates']
                                         if c['word'] not in {x['word'] for x in row['candidates']})
                row['merged_types'] = sorted(set(row['merged_types']) | set(old.get('merged_types', [old['type']])))
                rows.remove(old)
            rows.append(row)
    rows.sort(key=lambda row: (row['segment'], row['offset_start'], row['offset_end'], list(LABELS).index(row['type'])))
    counts = {kind: sum(row['type'] == kind for row in rows) for kind in LABELS}
    notices = [] if vocabulary.general_status['ready'] else [vocabulary.general_status['tell_user']]
    return {'schema_version': 1, 'rules_version': RULES_VERSION, 'created': now(),
            'context': lexicon.context_for(context), 'counts': counts, 'total': len(rows), 'items': rows,
            'lexicon': {'general': vocabulary.general_status, 'pinyin': 'pypinyin 0.55.0',
                        'public_ready': vocabulary.public.status['ready'], 'personal_words': len(vocabulary.personal)},
            'notices': notices, 'tell_user': summary(counts) + ''.join(notices)}


def _time(value):
    if not isinstance(value, (int, float)):
        return '时间未知'
    return f'{int(value) // 60:02}:{value % 60:06.3f}'


def render(report):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', '')
    lines = ['# 疑点清单', '', summary(report['counts'], '按下表逐项核对') + ''.join(report.get('notices', [])), '',
             '疑点只供核对，不等于错误；正文未插标记。时间为识别段落范围，不能当作逐字时间。', '',
             '| 时间 | 原句 | 疑点类型 | 片段与候选 | 依据 |', '|---|---|---|---|---|']
    for row in report['items']:
        options = '、'.join(c['word'] + (f'（曾改 {c["count"]} 次）' if 'count' in c else '') for c in row['candidates'])
        lines.append('| ' + ' | '.join(map(cell, [f'{_time(row["start"])}～{_time(row["end"])}', row['sentence'],
            row['type_label'], row['original'] + (' → ' + options if options else ''), '；'.join(row['reasons'])])) + ' |')
    if report['lexicon']['general']['ready'] and not report['lexicon']['public_ready']:
        lines.extend(['', '本次公开领域词库未就绪，同音候选仅使用本视频上下文、现有个人词和相关记忆。'])
    return '\n'.join(lines) + '\n'


def generate(path: Path, context: dict, vocabulary: Vocabulary, root: Path = ROOT) -> dict:
    result = read_json(path)
    report = analyze(result, context, vocabulary)
    report['source_result'] = {'file': relative(root, path), 'sha256': digest(path)}
    write_json(path.with_name('suspects.json'), report)
    path.with_name('suspects.md').write_text(render(report), encoding='utf-8')
    return read_summary(path, root)


def read_summary(path: Path, root: Path = ROOT) -> dict:
    json_path, markdown = path.with_name('suspects.json'), path.with_name('suspects.md')
    if not json_path.is_file() or not markdown.is_file():
        return {'status': 'not_generated', 'tell_user': '这条稿子尚未生成疑点清单，可用 suspects 命令补算。'}
    report = read_json(json_path)
    if report['source_result']['sha256'] != digest(path):
        return {'status': 'stale', 'tell_user': '这条稿子的疑点清单与当前结果不一致，请用 suspects 命令重算。'}
    return {'status': 'ready', 'json': relative(root, json_path), 'markdown': relative(root, markdown),
            'notices': report.get('notices', []),
            **{k: report[k] for k in ('created', 'counts', 'total', 'tell_user')}}
