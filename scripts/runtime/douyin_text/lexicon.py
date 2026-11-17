"""Local vocabulary and review evidence. Queries never rewrite text or call ASR."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import difflib
import hashlib
import math
from pathlib import Path
import re
import unicodedata

from . import models, numbers
from .common import ROOT, SKILL_ROOT, artifact_lock, config, digest, inside, now, read_json, relative, write_json

MANIFEST = SKILL_ROOT / 'assets/lexicons.json'
UNAVAILABLE = '公开词库尚未就绪，本次照常处理文稿和记录纠错。'
DOWNLOADING = '公开词库正在后台下载，下次起生效；本次照常降级处理。'
DOWNLOAD_FAILED = '公开词库下载失败，本次照常降级处理；可用 lexicon import-public 手动导入，或用 lexicon download 重试。'
TERM_CATEGORIES = {'IT', 'medical', 'law', 'car'}
NAME_CATEGORIES = {'lishimingren', 'diming'}
PLAIN_WORDS = {'今天', '明天', '我们', '你们', '他们', '自己', '这个', '那个', '什么', '学习',
               '生活', '工作', '方法', '问题', '时间', '内容', '开始', '成功', '坚持', '普通',
               'the', 'and', 'sure', 'hello', 'yes', 'no'}
FUNCTION_CHARS = set('的地得了吗呢吧啊哦嘛哈呀呃嗯哎唉哇和与及之我你他她它们是把被在于从向给')
DIALECT_CHARS = set('嘅咩喺唔冇咗啲佢哋嚟嗰啱')
# Clause separators fill gaps in the domain lexicon, without adding personal terms.
BOUNDARY_WORDS = {'负责', '平台', '这个', '那个', '这种', '作为', '这里', '那里', '邀请', '所谓'}
TYPE_LABELS = {'term': '专名或术语', 'phrase': '词语或短语', 'english': '英文词', 'dialect': '方言',
               'function': '虚词或语气词', 'insertion': '纯插入', 'deletion': '纯删除',
               'single_character': '单字', 'other': '其他'}


def manifest() -> dict:
    data = read_json(MANIFEST)
    if data.get('schema_version') != 1 or data.get('id') != 'thuocl':
        raise ValueError('词库清单版本或标识不正确。')
    if not re.fullmatch('[0-9a-f]{40}', data.get('revision', '')) or data.get('license') != 'MIT':
        raise ValueError('词库必须固定提交版本并声明 MIT 许可。')
    files = data['files'] + [data['license_file']]
    paths = [models._relative(f['path']) for f in files]
    categories = [f['category'] for f in data['files']]
    if not categories or len(set(categories)) != len(categories) or len(set(paths)) != len(paths):
        raise ValueError('词库清单为空或有重复类别、文件。')
    for f in files:
        models._validate_digest(f)
        if f.get('license') != 'MIT' or not f.get('sources'):
            raise ValueError('词库文件缺少许可或下载来源。')
    return data


def public_folder(spec: dict) -> Path:
    return models._path(models.model_root(), 'lexicons/thuocl-' + spec['revision'])


def public_status() -> dict:
    spec = manifest()
    folder = public_folder(spec)
    result = models.data_status(folder, spec['files'] + [spec['license_file']])
    return {**result,
            'revision': spec['revision'], 'directory': str(folder),
            'tell_user': _public_notice(result)}


def _public_notice(status: dict) -> str:
    if status['ready']:
        return '公开词库已就绪，文件大小与 SHA-256 均通过校验。'
    return {'downloading': DOWNLOADING, 'failed': DOWNLOAD_FAILED}.get(status.get('status'), UNAVAILABLE)


def ensure_public() -> dict:
    try:
        spec = manifest()
        result = models.ensure_data(public_folder(spec), spec['files'] + [spec['license_file']])
        return {**result, 'tell_user': _public_notice(result)}
    except (OSError, ValueError, KeyError, models.ModelError) as exc:
        return {'ready': False, 'status': 'failed', 'reason': str(exc), 'tell_user': DOWNLOAD_FAILED}


def download_public(*, source: Path | None = None) -> dict:
    spec = manifest()
    source = Path(source).expanduser().resolve() if source is not None else None
    result = models.acquire_data(public_folder(spec), spec['files'] + [spec['license_file']], source=source)
    return {**public_status(), 'progress': result['progress']}


class PublicLexicon:
    """One snapshot per operation; active consumers may request nonblocking preparation."""

    def __init__(self, *, auto_download: bool = False):
        self.tables: dict[str, dict[str, int | None]] = {}
        self.spec = {}
        try:
            self.spec = manifest()
            self.status = ensure_public() if auto_download else public_status()
        except (OSError, ValueError, KeyError, models.ModelError) as exc:
            self.status = {'ready': False, 'tell_user': UNAVAILABLE, 'reason': str(exc)}

    def category(self, category: str) -> dict[str, int | None]:
        if not self.status['ready']:
            return {}
        if category not in self.tables:
            item = next((f for f in self.spec['files'] if f['category'] == category), None)
            if item is None:
                raise ValueError('未知词库类别：' + category)
            path = models._path(public_folder(self.spec), item['path'])
            # Check the bytes actually parsed, including changes after the initial status check.
            data = path.read_bytes()
            if len(data) != item['size'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ValueError('词库读取时发生变化：' + category)
            table = {}
            for line in data.decode('utf-8-sig').splitlines():
                if not line.strip():
                    continue
                word, frequency = line.rsplit('\t', 1)
                word, frequency = word.strip(), frequency.strip()
                # Upstream has missing/malformed DF values. Preserve membership, not an invented frequency.
                value = int(frequency) if frequency.isdecimal() else None
                if word not in table or (value is not None and (table[word] is None or value > table[word])):
                    table[word] = value
            self.tables[category] = table
        return self.tables[category]

    def lookup(self, word: str, categories: list[str] | None = None) -> dict:
        hits = [{'category': c, 'frequency': self.category(c)[word]}
                for c in (categories or [f['category'] for f in self.spec.get('files', [])])
                if word in self.category(c)]
        return {'word': word, 'common': bool(hits), 'frequency': max((x['frequency'] for x in hits if x['frequency'] is not None), default=None),
                'categories': hits, 'ready': self.status['ready'], 'tell_user': self.status['tell_user']}

    def candidates(self, text: str = '', *, categories: tuple[str, ...] = ('lishimingren', 'diming'),
                   limit: int = 50) -> list[dict]:
        rows = [{'word': word, 'frequency': frequency, 'category': c}
                for c in categories for word, frequency in self.category(c).items() if not text or text in word]
        return sorted(rows, key=lambda x: (-(x['frequency'] or 0), x['word'], x['category']))[:max(0, limit)]

    def idioms(self) -> dict[str, int | None]:
        return self.category('chengyu')


def display_exceptions(text: str, recorded: dict | None = None) -> tuple[list[str], dict]:
    # Persist the exact applicable words as well as their pinned source. Replays work offline.
    if recorded is not None:
        if not isinstance(recorded.get('words'), list) or any(not isinstance(w, str) for w in recorded['words']):
            raise ValueError('展示词库例外记录不完整，无法重算。')
        return list(recorded['words']), dict(recorded)
    public = PublicLexicon(auto_download=True)
    try:
        words = sorted(word for word in public.idioms() if word in text)
    except (OSError, ValueError) as exc:
        public.status = {'ready': False, 'reason': str(exc)}
        words = []
    if not public.status['ready']:
        return [], {'used': False, 'revision': None, 'words': [],
                    'tell_user': public.status.get('tell_user', DOWNLOAD_FAILED)}
    item = next(f for f in public.spec['files'] if f['category'] == 'chengyu')
    return words, {'used': True, 'id': 'thuocl', 'revision': public.spec['revision'],
                   'sha256': item['sha256'], 'words': words}


def display_notice(result: dict) -> str | None:
    saved = result.get('display', {}).get('lexicon_exceptions', {})
    if saved.get('used') is not False:
        return None
    status = ensure_public()
    return ('本次沿用未使用公开词库的展示例外快照；公开词库已就绪，新结果可使用。'
            if status['ready'] else status['tell_user'])


def _state(root: Path) -> dict:
    path = root / '.local/lexicon/state.json'
    state = read_json(path) if path.exists() else {'schema_version': 2, 'words': {}, 'reviews': {},
                                                 'forgotten': {}, 'removed_words': []}
    if state.get('schema_version') != 2:
        raise ValueError('旧版字符级测试记忆不适用，请在新工作区学习词级记忆。')
    return state


def _save(root: Path, state: dict) -> None:
    write_json(root / '.local/lexicon/state.json', state)


def _word(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 100 or any(c in value for c in '\r\n\t'):
        raise ValueError('词条应为 1～100 个字符，不能包含换行或制表符。')
    return value


def _add(state: dict, word: str, origin: dict, *, note: str = '') -> None:
    entry = state['words'].setdefault(word, {'word': word, 'note': note, 'origins': []})
    if note:
        entry['note'] = note
    if origin not in entry['origins']:
        entry['origins'].append(origin)


def add(word: str, root: Path = ROOT, *, note: str = '', source: str = '') -> dict:
    word = _word(word)
    with artifact_lock(root / '.local/lexicon'):
        state = _state(root)
        state['removed_words'] = [w for w in state['removed_words'] if w != word]
        _add(state, word, {'kind': 'manual', 'source': source, 'time': now()}, note=note)
        _save(root, state)
    return {'status': 'added', 'word': word, 'tell_user': f'已添加词条“{word}”，仅保存在当前工作区。'}


def remove(word: str, root: Path = ROOT) -> dict:
    word = _word(word)
    with artifact_lock(root / '.local/lexicon'):
        state = _state(root)
        state['words'].pop(word, None)
        if word not in state['removed_words']:
            state['removed_words'].append(word)
        _save(root, state)
    return {'status': 'removed', 'tell_user': f'已移除词条“{word}”；自动学习不会重新加入，可用 lexicon add 恢复。'}


def list_words(root: Path = ROOT) -> dict:
    return {'status': 'ready', 'words': sorted(_state(root)['words'].values(), key=lambda x: x['word'])}


def import_words(file: Path, root: Path = ROOT) -> dict:
    data = file.read_bytes()
    words = [_word(line.split('\t', 1)[0]) for line in data.decode('utf-8-sig').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    checksum = hashlib.sha256(data).hexdigest()
    with artifact_lock(root / '.local/lexicon'):
        state = _state(root)
        snapshot = root / '.local/lexicon/imports' / (checksum + '.txt')
        if not snapshot.exists():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_bytes(data)
        if digest(snapshot) != checksum:
            raise ValueError('已保存的词表快照发生变化。')
        # Parse our own copy, not the external path which may change or disappear.
        words = [_word(line.split('\t', 1)[0]) for line in snapshot.read_text(encoding='utf-8-sig').splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        origin = {'kind': 'import', 'source': file.name, 'sha256': checksum,
                  'file': relative(root, snapshot), 'time': now()}
        for word in words:
            state['removed_words'] = [w for w in state['removed_words'] if w != word]
            _add(state, word, origin)
        _save(root, state)
    return {'status': 'imported', 'count': len(set(words)), 'file': relative(root, snapshot),
            'tell_user': f'已复制词表并导入 {len(set(words))} 个词，来源文件名和导入时间已记录。'}


def context_for(source: dict) -> dict:
    title = str(source.get('title') or '')
    description = str(source.get('description') or source.get('desc') or '')
    tags = source.get('tags') or source.get('topics') or []
    if isinstance(tags, str):
        tags = re.split(r'[\s,#，]+', tags)
    tags = [str(t.get('name', '')) if isinstance(t, dict) else str(t) for t in tags]
    tags += re.findall(r'#([^\s#]+)', title + ' ' + description)
    account = source.get('account') or source.get('author') or source.get('account_name') or ''
    if isinstance(account, dict):
        account = account.get('name') or account.get('nickname') or ''
    return {'title': title, 'description': description, 'account': str(account),
            'tags': sorted({t.strip().lstrip('#') for t in tags if t.strip()})}


def _number(value: str) -> str | None:
    """Comparison-only canonical form. Does not change display rules or their thresholds."""
    if value.startswith('百分之'):
        parsed = _number(value[3:])
        return parsed + '%' if parsed is not None else None
    if value.endswith('%'):
        parsed = _number(value[:-1])
        return parsed + '%' if parsed is not None else None
    if value.startswith(('-', '+', '负', '正')):
        parsed = _number(value[1:])
        return ('-' if value[0] in '-负' else '') + parsed if parsed is not None else None
    for scale, factor in (('亿', 100000000), ('万', 10000)):
        if scale in value:
            first, rest = value.split(scale, 1)
            left, right = _number(first), _number(rest) if rest else '0'
            if left is None or right is None:
                return None
            try:
                return format((Decimal(left) * factor + Decimal(right)).normalize(), 'f')
            except InvalidOperation:
                return None
    value = value.replace('点', '.')
    if re.fullmatch(r'[零〇一二两三四五六七八九]+(?:\.[零〇一二三四五六七八九]+)?', value):
        value = ''.join(numbers.DIGITS.get(c, c) for c in value)
    elif re.search('[十百千]', value):
        whole, dot, fraction = value.partition('.')
        integer = numbers._integer(whole)
        if integer is None or (dot and any(c not in numbers.DIGITS for c in fraction)):
            return None
        value = str(integer) + ('.' + ''.join(numbers.DIGITS[c] for c in fraction) if dot else '')
    try:
        parsed = Decimal(value)
        if parsed.is_finite():
            return format(parsed.normalize(), 'f')
    except InvalidOperation:
        pass
    return None


def _comparison(text: str) -> tuple[str, list[tuple[int, int]]]:
    chars, offsets = [], []
    for index, char in enumerate(text):
        for c in unicodedata.normalize('NFKC', char):
            if not c.isspace():
                chars.append(c)
                offsets.append((index, index + 1))
    compact = ''.join(chars)
    pattern = re.compile(r'(?:百分之)?[-+负正]?[0-9零〇一二两三四五六七八九十百千万亿]+(?:[.点][0-9零〇一二三四五六七八九]+)?[万亿]?%?')
    result, positions, cursor = [], [], 0

    def plain(start, end):
        for i in range(start, end):
            if not unicodedata.category(compact[i]).startswith(('P', 'Z')):
                result.append(compact[i])
                positions.append(offsets[i])

    for match in pattern.finditer(compact):
        plain(cursor, match.start())
        value = _number(match[0])
        if value is None:
            plain(match.start(), match.end())
        else:
            token = '\x01' + value + '\x02'
            result.extend(token)
            positions.extend([(offsets[match.start()][0], offsets[match.end() - 1][1])] * len(token))
        cursor = match.end()
    plain(cursor, len(compact))
    return ''.join(result), positions


def _character_compare(raw: str, reviewed: str) -> dict:
    """Character alignment with original offsets; formatting-only edits are invisible."""
    left, lp = _comparison(raw)
    right, rp = _comparison(reviewed)
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    edits, unchanged = [], []

    def record(a, b, c, d):
        return {'original': raw[a:b], 'replacement': reviewed[c:d], 'raw_start': a, 'raw_end': b,
                'review_start': c, 'review_end': d, 'before': raw[max(0, a - 24):a],
                'after': raw[b:b + 24], 'review_before': reviewed[max(0, c - 24):c],
                'review_after': reviewed[d:d + 24]}

    for tag, i, j, k, l in matcher.get_opcodes():
        if tag == 'equal':
            unchanged.append(left[i:j])
            continue
        # Keep Latin product names whole (Workbody, not just bo).
        while (0 < i < len(lp) and 0 < k < len(rp) and lp[i - 1][1] == lp[i][0] and
               rp[k - 1][1] == rp[k][0] and left[i - 1] == right[k - 1] and re.fullmatch('[A-Za-z]', left[i - 1])):
            i, k = i - 1, k - 1
        while (0 < j < len(left) and 0 < l < len(right) and lp[j - 1][1] == lp[j][0] and
               rp[l - 1][1] == rp[l][0] and left[j] == right[l] and re.fullmatch('[A-Za-z]', left[j])):
            j, l = j + 1, l + 1
        a = lp[i][0] if i < len(lp) else len(raw)
        b = lp[j - 1][1] if j > i else a
        c = rp[k][0] if k < len(rp) else len(reviewed)
        d = rp[l - 1][1] if l > k else c
        edit = record(a, b, c, d)
        if edits and a <= edits[-1]['raw_end'] and c <= edits[-1]['review_end']:
            previous = edits.pop()
            a, c = previous['raw_start'], previous['review_start']
            edit.update(original=raw[a:b], replacement=reviewed[c:d], raw_start=a, review_start=c,
                        before=previous['before'], review_before=previous['review_before'])
        edits.append(edit)
    separated = []
    for edit in edits:
        a, b, c, d = (edit[k] for k in ('raw_start', 'raw_end', 'review_start', 'review_end'))
        boundary = re.search('[。！？；\n]', raw[a:b])
        # Ignoring punctuation can join a sentence-final correction to deletion of a
        # repeated opening word in the next sentence. Retain that observed boundary.
        if boundary and boundary.end() < b - a and reviewed[d:d + 1] == boundary[0]:
            split = a + boundary.start()
            separated.extend((record(a, split, c, d), record(split + 1, b, d + 1, d + 1)))
        else:
            separated.append(edit)
    return {'changes': separated, 'unchanged': unchanged}


class WordBoundaries:
    """Bidirectional maximum matching, with unknown runs bounded by known words/particles."""

    def __init__(self, public: PublicLexicon, words=()):
        self.words = set(words) | BOUNDARY_WORDS
        for file in public.spec.get('files', []):
            self.words.update(public.category(file['category']))
        self.words = {w for w in self.words if re.fullmatch(r'[\u4e00-\u9fff]{2,}', w)}
        self.maximum = max(map(len, self.words), default=2)

    def split(self, text: str) -> list[tuple[int, int]]:
        spans = []
        for run in re.finditer(r'[A-Za-z][A-Za-z0-9]*|[\u4e00-\u9fff]+', text):
            if run[0][0].isascii():
                spans.append(run.span())
                continue
            value = run[0]

            def match(reverse):
                tokens, i = [], len(value) if reverse else 0
                while (i > 0) if reverse else (i < len(value)):
                    available = i if reverse else len(value) - i
                    size = next((n for n in range(min(self.maximum, available), 1, -1)
                                 if (value[i - n:i] if reverse else value[i:i + n]) in self.words), 1)
                    a, b = (i - size, i) if reverse else (i, i + size)
                    # 配/加 are standalone verbs here, not function-word corrections.
                    known = size > 1 or value[a:b] in FUNCTION_CHARS | DIALECT_CHARS | {'配', '加', '该'}
                    tokens.append((a, b, known))
                    i = a if reverse else b
                return list(reversed(tokens)) if reverse else tokens

            choices = (match(False), match(True))
            tokens = min(choices, key=lambda ts: (sum(not known for _, _, known in ts), len(ts),
                                                 sum(b - a == 1 for a, b, _ in ts)))
            merged = []
            for a, b, known in tokens:
                if merged and not known and not merged[-1][2]:
                    merged[-1] = (merged[-1][0], b, False)
                else:
                    merged.append((a, b, known))
            spans.extend((run.start() + a, run.start() + b) for a, b, _ in merged)
        return spans


def _function(text: str) -> bool:
    return bool(text) and all(c in FUNCTION_CHARS for c in text)


def memory_scope(original: str, replacement: str, kind: str, occurrences=()) -> dict:
    """Pure scope policy: Chinese sources need evidence from distinct confirmed contexts."""
    if kind not in {'term', 'phrase', 'english', 'dialect'}:
        return {'requires_context': False, 'context_reason': '该类型仅保留记录，不用于提示。'}
    # Keep a Latin-anchored transcription of an English proper name (q 克劳 -> QClaw)
    # global, without extending that exemption to arbitrary Chinese -> English edits.
    transliterated = re.fullmatch(r'([A-Za-z][A-Za-z0-9]*)\s*[\u4e00-\u9fff]+', original)
    latin_name = (kind == 'term' and re.fullmatch(r'[A-Za-z][A-Za-z0-9.+-]*', replacement) and
                  transliterated and replacement.casefold().startswith(transliterated[1].casefold()))
    if latin_name or (not re.search(r'[\u4e00-\u9fff]', original) and kind != 'dialect'):
        return {'requires_context': False, 'context_reason': '英文或字母专名、词语可跨场景提示。'}
    contexts = [context_for(e['context']) for e in occurrences]
    accounts = {c['account'] for c in contexts if c['account']}
    # One review with several tags, or repeated reviews with the same tags, is one
    # topic context. Empty metadata cannot provide evidence of a different context.
    topics = {tuple(c['tags']) for c in contexts if c['tags']}
    if len(accounts) >= 2:
        return {'requires_context': False, 'context_reason': f'已在 {len(accounts)} 个不同账号确认，可跨场景提示。'}
    if len(topics) >= 2:
        return {'requires_context': False, 'context_reason': f'已在 {len(topics)} 组不同话题确认，可跨场景提示。'}
    return {'requires_context': True, 'context_reason': '含汉字原文尚缺跨场景确认，需同账号或同话题。'}


def classify_change(original: str, replacement: str, public: PublicLexicon, words=(), *, occurrences=()) -> dict:
    if not original:
        kind = 'insertion'
    elif not replacement:
        kind = 'deletion'
    elif _function(original) or _function(replacement):
        kind = 'function'
    elif any(c in DIALECT_CHARS for c in replacement):
        kind = 'dialect'
    elif re.fullmatch('[\u4e00-\u9fff]', original) or re.fullmatch('[\u4e00-\u9fff]', replacement):
        kind = 'single_character'
    elif replacement in words or any(replacement in public.category(c) for c in NAME_CATEGORIES):
        kind = 'term'
    elif re.fullmatch('[\u4e00-\u9fff]{2,}', original) and re.fullmatch('[\u4e00-\u9fff]{2,}', replacement):
        kind = 'phrase'
    elif re.fullmatch('[A-Za-z]+', original) and re.fullmatch('[A-Za-z]+', replacement):
        kind = 'english'
    else:
        kind = 'other'
    frequency = public.lookup(original)['frequency'] if original else None
    return {'type': kind, 'type_label': TYPE_LABELS[kind],
            'can_suggest': kind in {'term', 'phrase', 'english', 'dialect'},
            **memory_scope(original, replacement, kind, occurrences),
            'original_frequency': frequency}


def compare(raw: str, reviewed: str, *, public: PublicLexicon | None = None, words=()) -> dict:
    """Expand aligned differences to vocabulary boundaries, retaining nonsuggestible edits."""
    base = _character_compare(raw, reviewed)
    if not base['changes']:
        return base
    public = public if public is not None else PublicLexicon()
    vocabulary = WordBoundaries(public, words)
    raw_tokens, review_tokens = vocabulary.split(raw), vocabulary.split(reviewed)
    left, lp = _comparison(raw)
    right, rp = _comparison(reviewed)
    aligned = [(lp[i + n], rp[j + n]) for i, j, length in
               difflib.SequenceMatcher(None, left, right, autojunk=False).get_matching_blocks() for n in range(length)]

    def expand(start, end, tokens, text, unknown):
        touching = [(a, b) for a, b in tokens if
                    (unknown or text[a:b] in vocabulary.words or text[a:b].isascii()) and
                    ((a < end and b > start) or (start == end and a < start < b))]
        return (min([start, *[a for a, _ in touching]]), max([end, *[b for _, b in touching]]))

    candidates = []
    for edit in base['changes']:
        a, b, c, d = (edit[k] for k in ('raw_start', 'raw_end', 'review_start', 'review_end'))
        # Separate an added connective from its neighbouring corrected word: 之 -> 峙 + 和.
        extra = len(reviewed[c:d]) - len(raw[a:b])
        if raw[a:b] and extra > 0 and _function(reviewed[d - extra:d]):
            candidates.append((b, b, d - extra, d, True))
            d -= extra
        atomic = (_function(raw[a:b]) and _function(reviewed[c:d]) or
                  (a == b and _function(reviewed[c:d])) or (c == d and _function(raw[a:b])) or
                  (b - a == d - c == 1 and reviewed[c:d] in DIALECT_CHARS))
        if not atomic:
            unknown = b - a <= 1 or d - c <= 1
            previous = None
            while previous != (a, b, c, d):
                previous = a, b, c, d
                a, b = expand(a, b, raw_tokens, raw, unknown)
                c, d = expand(c, d, review_tokens, reviewed, unknown)
                for (x, y), (u, v) in aligned:
                    if a <= x and y <= b:
                        c, d = min(c, u), max(d, v)
                    if c <= u and v <= d:
                        a, b = min(a, x), max(b, y)
        candidates.append((a, b, c, d, atomic))
    merged = []
    for a, b, c, d, atomic in sorted(candidates):
        if merged and not atomic and not merged[-1][4]:
            x, y, u, v, _ = merged[-1]
            if (a < y and x < b) or (c < v and u < d):
                merged[-1] = (min(a, x), max(b, y), min(c, u), max(d, v), False)
                continue
        merged.append((a, b, c, d, atomic))
    changes = []
    for a, b, c, d, _ in merged:
        original, replacement = raw[a:b], reviewed[c:d]
        if _comparison(original)[0] == _comparison(replacement)[0]:
            continue
        changes.append({'original': original, 'replacement': replacement, 'raw_start': a, 'raw_end': b,
                        'review_start': c, 'review_end': d, 'before': raw[max(0, a - 24):a], 'after': raw[b:b + 24],
                        'review_before': reviewed[max(0, c - 24):c], 'review_after': reviewed[d:d + 24],
                        **classify_change(original, replacement, public, words)})
    return {'changes': changes, 'unchanged': base['unchanged']}


def _bigrams(text: str) -> set[str]:
    value = re.sub(r'[^\w]', '', text.casefold())
    return {value[i:i + 2] for i in range(len(value) - 1)}


def relevance(context: dict, origin: dict, term: str = '') -> float:
    """Pure, explainable context score; no external service or hidden language model."""
    context, origin = context_for(context), context_for(origin)
    score = 3.0 if context['account'] and context['account'] == origin['account'] else 0.0
    score += 2.0 * len(set(context['tags']) & set(origin['tags']))
    a = _bigrams(context['title'] + context['description'])
    b = _bigrams(origin['title'] + origin['description'])
    if a and b:
        score += 2.0 * len(a & b) / len(a | b)
    joined = ' '.join([context['title'], context['description'], context['account'], *context['tags']])
    if len(term) >= 2 and term.casefold() in joined.casefold():
        score += 4.0
    return score


def _instant(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def memory_weight(memory: dict, context: dict, at: str) -> float:
    """Pure score: frequency × 180-day recency × context, discounted by counterevidence."""
    current = _instant(at)
    positive = 0.0
    for event in memory.get('occurrences', []):
        stamp = _instant(event['time'])
        days = max(0, (current - stamp).total_seconds() / 86400)
        positive += (0.25 + 0.75 * math.pow(0.5, days / 180)) * (1 + relevance(context, event['context'], memory['replacement']))
    negative = sum((1 + relevance(context, e['context'])) for e in memory.get('unchanged', [])
                   if relevance(context, e['context']) > 0)
    return round(positive / (1 + negative) * (0.1 if memory.get('denied') else 1), 6)


def memories(root: Path = ROOT, *, state: dict | None = None, public: PublicLexicon | None = None) -> list[dict]:
    state = state if state is not None else _state(root)
    public = public if public is not None else PublicLexicon()
    rows = {}
    for event in state['reviews'].values():
        for edit in event['changes']:
            key = (edit['original'], edit['replacement'])
            memory = rows.setdefault(key, {'original': key[0], 'replacement': key[1], 'occurrences': [], 'unchanged': []})
            memory['occurrences'].append({**{k: event[k] for k in ('task', 'item', 'review_sha256', 'engine', 'time', 'context')},
                                          **edit})
    output = []
    for (original, replacement), row in rows.items():
        forgotten = state['forgotten'].get(original, {})
        if forgotten.get('mode') == 'delete':
            continue
        row['denied'] = forgotten.get('mode') == 'deny'
        row['denied_at'] = forgotten.get('time')
        row.update(classify_change(original, replacement, public, state['words'], occurrences=row['occurrences']))
        row['can_suggest'] = row['can_suggest'] and not row['denied']
        needle = _comparison(original)[0]
        if needle:
            for event in state['reviews'].values():
                count = sum(span.count(needle) for span in event['unchanged'])
                if count:
                    row['unchanged'].append({k: event[k] for k in ('task', 'item', 'engine', 'time', 'context')})
        row.update(count=len(row['occurrences']), last_seen=max((e['time'] for e in row['occurrences']), key=_instant),
                   accounts=sorted({e['context']['account'] for e in row['occurrences'] if e['context']['account']}),
                   topics=sorted({tag for e in row['occurrences'] for tag in e['context']['tags']}))
        output.append(row)
    return sorted(output, key=lambda x: (x['original'], x['replacement']))


def is_term(word: str, public: PublicLexicon) -> bool:
    if word.casefold() in PLAIN_WORDS or not 2 <= len(word) <= 24:
        return False
    if re.fullmatch(r'[A-Za-z][A-Za-z0-9.+-]{1,23}', word):
        return any(c.isupper() for c in word)
    if re.fullmatch(r'[\u4e00-\u9fff]{2,12}', word):
        if any(word in public.category(c) for c in NAME_CATEGORIES):
            return True
        if len(word) >= 3 and any(word in public.category(c) for c in TERM_CATEGORIES):
            return True
        return word.endswith(('算法', '协议', '模型', '接口', '数据库', '综合征', '定理'))
    return False


def learned_terms(text: str, changes: list[dict], public: PublicLexicon) -> set[str]:
    candidates = []
    for match in re.finditer(r'[A-Za-z][A-Za-z0-9.+-]{1,23}|[\u4e00-\u9fff]{2,12}', text):
        if is_term(match[0], public):
            candidates.append((match.start(), match.end(), match[0]))
    # Explicit naming cues are evidence for an otherwise unknown proper name.
    for match in re.finditer(r'(?:邀请了|名叫|叫做|称为|简称)([\u4e00-\u9fffA-Za-z0-9]{2,12}?)(?=作为|的|是|[，。！？])', text):
        candidates.append((match.start(1), match.end(1), match[1]))
    for match in re.finditer(r'(?:这个|那个)([\u4e00-\u9fff]{2,8}?)(?=平台|模型|协议)', text):
        if match[1] not in PLAIN_WORDS:
            candidates.append((match.start(1), match.end(1), match[1]))
    # Public specialist terms may be embedded in a longer Chinese sentence.
    for edit in changes:
        a, b = edit['review_start'], edit['review_end']
        for start in range(max(0, a - 11), b):
            for end in range(max(start + 2, a + 1), min(len(text), start + 12) + 1):
                word = text[start:end]
                if start < b and end > a and re.fullmatch('[\u4e00-\u9fff]+', word) and is_term(word, public):
                    candidates.append((start, end, word))
    return {word for start, end, word in candidates if any(
        start < e['review_end'] and end > e['review_start'] for e in changes)}


def _review_event(task: dict, entry: dict, registered: str, root: Path) -> dict:
    item = next(x for x in task['results'] if x['id'] == entry['id'])
    result = read_json(inside(root, item['result']))
    for key, extension in (('text', 'txt'), ('markdown', 'md')):
        if digest(inside(root, item[key])) != result['output_sha256'][extension]:
            raise ValueError('原稿已改变，未学习：' + item['id'])
    basis = entry.get('source_basis', {})
    if basis.get('raw_sha256') and basis['raw_sha256'] != digest(inside(root, item['text'])):
        raise ValueError('校对稿依据已变化，未学习：' + item['id'])
    file = inside(root, entry.get('snapshot_file') or entry['file'])
    if digest(file) != entry['sha256']:
        raise ValueError('校对稿与登记哈希不符，未学习：' + item['id'])
    reviewed = file.read_text(encoding='utf-8-sig')
    raw = result.get('raw_text', result['text'])
    comparison = _character_compare(raw, reviewed)
    return {'task': task['id'], 'item': item['id'], 'review_sha256': entry['sha256'],
            'engine': result['engine'], 'time': registered, 'context': context_for(result['source']),
            'review_file': relative(root, file), 'raw_sha256': digest(inside(root, item['text'])),
            'display': result.get('display'), **comparison, 'reviewed': reviewed, 'raw': raw}


def _learn_events(state: dict, events: list[dict], public: PublicLexicon) -> dict:
    changes, added, skipped = [], 0, 0
    for event in events:
        key = '|'.join([event['task'], event['item'], event['review_sha256']])
        if key in state['reviews']:
            skipped += 1
            continue
        body = event.pop('reviewed')
        origin = {k: event[k] for k in ('task', 'item', 'review_sha256', 'engine', 'time', 'context')}
        for word in learned_terms(body, event['changes'], public):
            if word not in state['removed_words']:
                _add(state, word, {'kind': 'review', **origin, 'source': event['review_file']})
        event.update(compare(event.pop('raw'), body, public=public, words=state['words']))
        state['reviews'][key] = event
        changes.extend(event['changes'])
        added += 1
    return {'reviews_added': added, 'reviews_skipped': skipped, 'changes': changes}


def _summary(result: dict) -> str:
    changes = result['changes']
    suggestible = sum(e['can_suggest'] for e in changes)
    examples = '；'.join((e['original'] or '（空）') + ' → ' + (e['replacement'] or '（删除）') for e in changes[:5])
    return (f'本次记录 {len(changes)} 处文字改动，已记入纠错记忆；其中 {suggestible} 处可供提示，其余仅留记录。' +
            ('前几处：' + examples + '。' if examples else '') +
            (f'已学习过的 {result["reviews_skipped"]} 份校对稿未重复计数。' if result['reviews_skipped'] else ''))


def learn_registered(task: dict, entries: list[dict], root: Path = ROOT) -> dict:
    events = [_review_event(task, e, task['review']['registered'], root) for e in entries]
    public = PublicLexicon(auto_download=True)
    with artifact_lock(root / '.local/lexicon'):
        state = _state(root)
        result = _learn_events(state, events, public)
        _save(root, state)
    return {**result, 'tell_user': _summary(result) + ('' if public.status['ready'] else public.status['tell_user'])}


def _learn_titles(state: dict, root: Path, public: PublicLexicon) -> int:
    selected = config(root).get('vault_root')
    if not selected:
        return 0
    vault = Path(selected).expanduser().resolve()
    count = 0
    # Read names and the first Markdown title only. Never write to the vault.
    for path in sorted(vault.rglob('*.md')):
        if path.is_symlink() or not path.resolve().is_relative_to(vault) or any(p.startswith('.') for p in path.relative_to(vault).parts):
            continue
        title = path.stem
        with path.open(encoding='utf-8-sig') as handle:
            for _ in range(30):
                line = handle.readline()
                if line.startswith('# '):
                    title = line[2:].strip()
                    break
                if not line:
                    break
        if not is_term(title, public) or title in state['removed_words']:
            continue
        origin = {'kind': 'vault_title', 'source': path.relative_to(vault).as_posix(), 'title': title}
        if origin not in state['words'].get(title, {}).get('origins', []):
            _add(state, title, origin)
            count += 1
    return count


def learn(root: Path = ROOT) -> dict:
    from . import workflow
    events, failures = [], []
    for path in sorted((root / 'runs').glob('*/task.json')):
        task = workflow.load(path.parent.name, root)
        records = [h for h in task.get('content_history', []) if h.get('kind') == 'review']
        if task.get('review'):
            records.append(task['review'])
        for record in records:
            for entry in record['files']:
                try:
                    events.append(_review_event(task, entry, record['registered'], root))
                except (OSError, ValueError, KeyError) as exc:
                    failures.append({'task': task['id'], 'item': entry['id'], 'reason': str(exc)})
    public = PublicLexicon(auto_download=True)
    with artifact_lock(root / '.local/lexicon'):
        state = _state(root)
        result = _learn_events(state, events, public)
        titles = _learn_titles(state, root, public)
        _save(root, state)
    return {**result, 'status': 'incomplete' if failures else 'learned', 'failures': failures,
            'titles_added': titles, 'memories': memories(root, state=state, public=public),
            'tell_user': _summary(result) + ('' if public.status['ready'] else public.status['tell_user'])}


def forget(original: str, root: Path = ROOT, *, delete: bool = False) -> dict:
    with artifact_lock(root / '.local/lexicon'):
        state = _state(root)
        count = sum(row['original'] == original for row in memories(root, state=state))
        state['forgotten'][original] = {'mode': 'delete' if delete else 'deny', 'time': now()}
        _save(root, state)
    return {'status': 'forgotten', 'count': count,
            'tell_user': f'已{"删除" if delete else "否定"} {count} 条记忆；查询不再推荐，重复学习旧稿不会恢复。'}


def query(context: dict, root: Path = ROOT, *, at: str | None = None, limit: int = 50) -> dict:
    state, rows = _state(root), []
    public = PublicLexicon(auto_download=True)
    for word in state['words'].values():
        score = max((relevance(context, origin.get('context') or {
            'title': word['note'], 'description': origin['source']}, word['word']) for origin in word['origins']), default=0)
        if score > 0:
            rows.append({'kind': 'word', **word, 'score': round(score, 6)})
    for memory in memories(root, state=state, public=public):
        if not memory['can_suggest']:
            continue
        eligible = [e for e in memory['occurrences'] if not memory['requires_context'] or same_scene(context, e['context'])]
        score = max((relevance(context, e['context'], memory['replacement']) for e in eligible), default=0)
        if score > 0:
            rows.append({'kind': 'memory', **memory, 'score': memory_weight(memory, context, at or now())})
    rows.sort(key=lambda x: (-x['score'], x['kind'], x.get('word', x.get('original', '')), x.get('replacement', '')))
    return {'status': 'ready', 'context': context_for(context), 'results': rows[:max(0, limit)],
            'tell_user': '已按上下文列出相关词条与纠错记忆，仅供提示。' + ('' if public.status['ready'] else public.status['tell_user'])}


def same_scene(context: dict, origin: dict) -> bool:
    context, origin = context_for(context), context_for(origin)
    return bool((context['account'] and context['account'] == origin['account']) or
                set(context['tags']) & set(origin['tags']))


def add_parser(sub) -> None:
    group = sub.add_parser('lexicon', help='管理词库与纠错记忆，不改原稿、不向模型传词')
    commands = group.add_subparsers(dest='lexicon_command', required=True)
    for name, help_text in [('status', '查看公开词库状态'), ('download', '前台下载并校验公开词库'),
                            ('list', '列出个人词条'), ('learn', '学习已登记校对稿和知识库标题'),
                            ('memories', '列出全部纠错记忆')]:
        commands.add_parser(name, help=help_text)
    word = commands.add_parser('add', help='手动添加词条')
    word.add_argument('word')
    word.add_argument('--note', default='', help='词条说明')
    word.add_argument('--source', default='', help='来源校对任务或视频')
    commands.add_parser('remove', help='移除个人词条').add_argument('word')
    for name, help_text in [('import', '复制并导入 UTF-8 词表'), ('import-public', '手动导入公开词库文件夹')]:
        commands.add_parser(name, help=help_text).add_argument('file', type=Path)
    forget_parser = commands.add_parser('forget', help='否定或删除原文片段的纠错记忆')
    forget_parser.add_argument('original')
    forget_parser.add_argument('--delete', action='store_true', help='删除记忆，保留防止重复学习的标记')
    lookup = commands.add_parser('lookup', help='查询公开词库词频和类别')
    lookup.add_argument('word')
    lookup.add_argument('--category', action='append', help='限定类别，可重复')
    related = commands.add_parser('query', help='按视频上下文查询相关词与记忆')
    for field in ('title', 'description', 'account'):
        related.add_argument('--' + field, default='', help={'title': '标题', 'description': '简介', 'account': '账号名'}[field])
    related.add_argument('--tag', action='append', default=[], help='话题，可重复')
    related.add_argument('--limit', type=int, default=50, help='最多返回条数')


def dispatch(args, root: Path = ROOT) -> dict:
    command = args.lexicon_command
    if command == 'status':
        return public_status()
    if command in {'download', 'import-public'}:
        return download_public(source=args.file if command == 'import-public' else None)
    if command == 'add':
        return add(args.word, root, note=args.note, source=args.source)
    if command == 'remove':
        return remove(args.word, root)
    if command == 'import':
        return import_words(args.file, root)
    if command == 'forget':
        return forget(args.original, root, delete=args.delete)
    if command == 'lookup':
        return PublicLexicon(auto_download=True).lookup(args.word, args.category)
    if command == 'query':
        return query({'title': args.title, 'description': args.description, 'account': args.account, 'tags': args.tag}, root, limit=args.limit)
    if command == 'memories':
        return {'status': 'ready', 'memories': memories(root)}
    return {'list': list_words, 'learn': learn}[command](root)
