"""Video-local prompts and second-pass evidence; never rewrite the first pass."""
from __future__ import annotations

import difflib
from pathlib import Path
import re
import unicodedata

from . import lexicon, suspects
from .common import ROOT

RULES_VERSION = '2'
DEFAULT_LIMIT = 20
COMMON_WORD_RANK = 5000
REVIEW_MAX_SECONDS = 24
FUZZY_INITIALS = (('zh', 'z'), ('ch', 'c'), ('sh', 's'), ('n', 'l'), ('f', 'h'))
FUZZY_FINALS = (('ang', 'an'), ('eng', 'en'), ('ing', 'in'))
VOWEL_GAP_LETTERS = frozenset('aeiou')
ENGLISH_SIMILARITY = 0.8
FUNCTION_WORDS = {'这个', '那个', '这些', '那些', '什么', '怎么', '怎样', '这样', '那样',
                  '这么', '那么', '因为', '所以', '但是', '然后', '而且', '以及', '或者',
                  '如果', '虽然', '就是', '还是', '已经', '的话', '一下', '一个', '没有'}


def select(context: dict, root: Path = ROOT, *, limit: int = DEFAULT_LIMIT,
           public=None, general=None) -> list[dict]:
    """Reuse query's relevance/scope rules, adding only literal public anchors."""
    if type(limit) is not int or limit < 0:
        raise ValueError('asr_hotwords_limit 应为不小于 0 的整数。')
    if not limit:
        return []
    public = public if public is not None else lexicon.PublicLexicon(auto_download=True)
    general = general if general is not None else lexicon.general_frequencies()[0]
    # Rank only multi-character entries. Ties use the word for a stable cutoff.
    daily = set(sorted((w for w in general if len(w) >= 2),
                       key=lambda w: (-general[w], w))[:COMMON_WORD_RANK]) if hasattr(general, 'items') else set()
    selected = {}

    def add(word, tier, score, source):
        if (len(word) < 2 or ',' in word or lexicon._function(word) or word in FUNCTION_WORDS
                or len(normalize(word)[0]) < 2):
            return
        if tier > 1 and word in daily:
            return
        row = selected.setdefault(word, {'word': word, 'tier': tier, 'score': score, 'sources': []})
        if tier < row['tier']:
            row.update(tier=tier, score=score)
        elif tier == row['tier']:
            row['score'] = max(row['score'], score)
        if source not in row['sources']:
            row['sources'].append(source)

    for row in lexicon.query(context, root, public=public, limit=None)['results']:
        if row['kind'] == 'word':
            add(row['word'], 1, row['score'], {'source': '个人词库', 'origins': row['origins']})
        elif row['can_suggest']:
            add(row['replacement'], 1, row['score'], {'source': '纠错记忆', 'original': row['original'],
                'count': row['count'], 'requires_context': row['requires_context']})
    for row in suspects.context_terms(context, frozenset(general)):
        add(row['word'], 3, lexicon.relevance(context, {}, row['word']),
            {'source': row['source'], 'fields': row['fields']})
    normalized = lexicon.context_for(context)
    fields = [normalized[k] for k in ('title', 'description', 'account')] + normalized['tags']
    for category in suspects.PUBLIC_CATEGORIES:
        for word in public.category(category):
            if any(word in text for text in fields):
                add(word, 2, lexicon.relevance(context, {}, word), {'source': 'THUOCL', 'category': category})
    return sorted(selected.values(), key=lambda row: (row['tier'], -row['score'], row['word']))[:limit]


def normalize(text: str) -> tuple[str, list[tuple[int, int]]]:
    chars, positions = [], []
    for i, char in enumerate(text):
        for value in unicodedata.normalize('NFKC', char).casefold():
            if value.isalnum():
                chars.append(value)
                positions.append((i, i + 1))
    return ''.join(chars), positions


def fuzzy(syllable: str) -> str:
    for first, second in FUZZY_INITIALS:
        if syllable.startswith(first):
            syllable = second + syllable[len(first):]
            break
    for first, second in FUZZY_FINALS:
        if syllable.endswith(first):
            syllable = syllable[:-len(first)] + second
            break
    return syllable


def conservative_sounds(left, right):
    """Exact initials; allow at most one missing/extra vowel in the whole word."""
    if len(left) != len(right):
        return False
    gaps = 0
    for first, second in zip(left, right):
        if first == second:
            continue
        parts = [re.fullmatch(r'(zh|ch|sh|[bpmfdtnlgkhjqxrzcsyw]?)([a-z]+)', s) for s in (first, second)]
        if not all(parts) or parts[0][1] != parts[1][1]:
            return False
        shorter, longer = sorted((parts[0][2], parts[1][2]), key=len)
        if len(longer) != len(shorter) + 1 or not any(
                char in VOWEL_GAP_LETTERS and longer[:i] + longer[i + 1:] == shorter
                for i, char in enumerate(longer)):
            return False
        gaps += 1
    return gaps <= 1


def prescreen(text: str, selected: list[dict]) -> list[dict]:
    """Wide phonetic gate; common-word protection is applied only after listening."""
    compact, positions = normalize(text)
    matches = []
    for row in selected:
        word = row['word']
        target, _ = normalize(word)
        if not target:
            continue
        exact = [_span(positions, m.start(), m.end(), text) for m in re.finditer(re.escape(target), compact)]
        if suspects.HAN.fullmatch(word):
            sounds = suspects.pronunciation(word)
            for run in suspects.HAN.finditer(compact):
                for a in range(run.start(), run.end() - len(word) + 1):
                    b = a + len(word)
                    if compact[a:b] == target:
                        continue
                    heard = suspects.pronunciation(compact[a:b])
                    if (tuple(map(fuzzy, heard)) == tuple(map(fuzzy, sounds))
                            or conservative_sounds(heard, sounds)):
                        x, y = _span(positions, a, b, text)
                        matches.append({'word': word, 'start': x, 'end': y, 'original': text[x:y]})
        elif re.search('[A-Za-z]', unicodedata.normalize('NFKC', word)):
            # Whole Latin tokens, optionally joined by spaces/hyphens; no substring
            # match inside another English word. Mixed letter/Han terms also work.
            tokens = list(re.finditer(r'[A-Za-z0-9\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]+|[\u4e00-\u9fff]', text))
            for i, token in enumerate(tokens):
                for end in tokens[i:]:
                    a, b = token.start(), end.end()
                    value, _ = normalize(text[a:b])
                    if len(value) > len(target) * 1.5:
                        break
                    if value == target or any(a < y and b > x for x, y in exact):
                        continue
                    if difflib.SequenceMatcher(None, value, target, autojunk=False).ratio() >= ENGLISH_SIMILARITY:
                        matches.append({'word': word, 'start': a, 'end': b, 'original': text[a:b]})
    return matches


def _span(positions, a, b, text):
    start = positions[a][0] if a < len(positions) else len(text)
    return start, positions[b - 1][1] if b > a else start


def segment_spans(segments):
    cursor, spans = 0, []
    for segment in segments:
        end = cursor + len(segment['raw_text'])
        spans.append((cursor, end))
        cursor = end
    return spans


def review_windows(segments, screen, *, max_seconds=REVIEW_MAX_SECONDS):
    """Use entire VAD groups when possible, otherwise whole neighboring short segments."""
    if not screen:
        return []
    if segments[-1]['end'] - segments[0]['start'] <= max_seconds:
        return [(0, len(segments))]
    spans = segment_spans(segments)
    anchors = {i for hit in screen for i, (a, b) in enumerate(spans)
               if a < hit['end'] and b > hit['start']}
    chosen = set()
    for anchor in sorted(anchors):
        options = []
        for a in range(anchor + 1):
            for b in range(anchor + 1, len(segments) + 1):
                seconds = segments[b - 1]['end'] - segments[a]['start']
                if seconds > max_seconds + 1e-9:
                    break
                complete = sum(spans[a][0] <= hit['start'] and hit['end'] <= spans[b - 1][1] for hit in screen)
                balance = abs((segments[a]['start'] + segments[b - 1]['end']) -
                              (segments[anchor]['start'] + segments[anchor]['end']))
                options.append(((complete, seconds, -balance, -a), (a, b)))
        if options:
            chosen.add(max(options)[1])
    return sorted((a, b) for a, b in chosen if not any(x <= a and b <= y and (x, y) != (a, b) for x, y in chosen))


def locate_hit(hit, segments, first_segment):
    """One hint at the first touched segment; preserve every covered raw-text range."""
    spans = [{'segment': first_segment + i, 'start': max(hit['start'], a) - a,
              'end': min(hit['end'], b) - a}
             for i, (a, b) in enumerate(segment_spans(segments)) if a < hit['end'] and b > hit['start']]
    return {**hit, 'spans': spans}


def offset_review(review, segment_offset, time_offset, chunk):
    def hit(value):
        return {**value, 'spans': [{**span, 'segment': span['segment'] + segment_offset} for span in value['spans']]}
    def located(value):
        return {**value, 'segments': [i + segment_offset for i in value['segments']],
                'start': value['start'] + time_offset, 'end': value['end'] + time_offset,
                'hits': [hit(h) for h in value['hits']]}
    return {**located(review), 'chunk': chunk, 'windows': [located(w) for w in review['windows']]}


def _substitution(first: str, second: str) -> float:
    """Substitution matrix: similar-sounding characters are cheaper to swap, so
    自己也惹／字节额 aligns 自己 with 字节 instead of shifting the word by one."""
    if first == second:
        return 0
    if suspects.HAN.fullmatch(first) and suspects.HAN.fullmatch(second):
        heard, target = suspects.pronunciation(first), suspects.pronunciation(second)
        if tuple(map(fuzzy, heard)) == tuple(map(fuzzy, target)) or conservative_sounds(heard, target):
            return 0.5
    return 1


def _alignment(left, right):
    """Refine replacement blocks so adjacent unrelated edits stay countable."""
    units = []
    for kind, a, b, c, d in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        if kind == 'equal':
            units.extend(('equal', a + i, a + i + 1, c + i, c + i + 1) for i in range(b - a))
            continue
        x, y = left[a:b], right[c:d]
        costs = [list(range(len(y) + 1))]
        for i, char in enumerate(x, 1):
            costs.append([i])
            for j, other in enumerate(y, 1):
                costs[i].append(min(costs[i - 1][j - 1] + _substitution(char, other),
                                    costs[i - 1][j] + 1, costs[i][j - 1] + 1))
        i, j, block = len(x), len(y), []
        while i or j:
            if i and j and costs[i][j] == costs[i - 1][j - 1] + _substitution(x[i - 1], y[j - 1]):
                block.append(('equal' if x[i - 1] == y[j - 1] else 'replace', a + i - 1, a + i, c + j - 1, c + j))
                i, j = i - 1, j - 1
            elif i and costs[i][j] == costs[i - 1][j] + 1:
                block.append(('delete', a + i - 1, a + i, c + j, c + j))
                i -= 1
            else:
                block.append(('insert', a + i, a + i, c + j - 1, c + j))
                j -= 1
        units.extend(reversed(block))
    return units


def compare(first: str, second: str, words: list[str], general=()) -> dict:
    """Align normalized text, but keep original offsets and full word-sized evidence."""
    left, lp = normalize(first)
    right, rp = normalize(second)
    codes = _alignment(left, right)
    hits, used = [], set()
    for word in words:
        target, _ = normalize(word)
        if not target:
            continue
        for occurrence in re.finditer(re.escape(target), right):
            c, d = occurrence.span()
            changed = [i for i, (kind, a, b, u, v) in enumerate(codes)
                       if kind != 'equal' and ((u < d and v > c) if u < v else c < u < d)]
            if not changed:
                continue
            mapped = []
            for kind, a, b, u, v in codes:
                if u < d and v > c:
                    mapped.append((a + max(c - u, 0), a + min(d - u, v - u)) if kind == 'equal' else (a, b))
                elif u == v and c < u < d:
                    mapped.append((a, b))
            a, b = min(x for x, _ in mapped), max(y for _, y in mapped)
            original = left[a:b]
            if not original or original == target:
                continue
            # General words (or an unavailable dictionary) cannot pass through
            # fuzzy initials/finals. A single vowel gap still admits 自己 -> 字节.
            if (general is None or original in general) and (not suspects.HAN.fullmatch(original)
                    or not suspects.HAN.fullmatch(target)
                    or not conservative_sounds(suspects.pronunciation(original), suspects.pronunciation(target))):
                continue
            x, y = _span(lp, a, b, first)
            u, v = _span(rp, c, d, second)
            hits.append({'start': x, 'end': y, 'original': first[x:y],
                         'replacement': second[u:v], 'word': word})
            used.update(changed)
    blocks = []
    for i, (kind, a, b, c, d) in enumerate(codes):
        if kind != 'equal':
            if blocks and blocks[-1][1] == a and blocks[-1][3] == c and blocks[-1][4] == (i in used):
                blocks[-1][1], blocks[-1][3] = b, d
            else:
                blocks.append([a, b, c, d, i in used])
    changes = []
    for a, b, c, d, hit in blocks:
        x, y = _span(lp, a, b, first)
        u, v = _span(rp, c, d, second)
        changes.append({'start': x, 'end': y, 'original': first[x:y], 'replacement': second[u:v],
                        'hotword_hit': hit})
    formatting = sum(normalize(first[a:b])[0] == normalize(second[c:d])[0]
                     for kind, a, b, c, d in difflib.SequenceMatcher(None, first, second, autojunk=False).get_opcodes()
                     if kind != 'equal')
    return {'hits': hits, 'changes': changes, 'formatting_changes': formatting,
            'other_changes': sum(not row['hotword_hit'] for row in changes) + formatting}
