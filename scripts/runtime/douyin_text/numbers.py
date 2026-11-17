"""Conservative, reversible display rules; never edit the recognition source."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

RULES_VERSION = '2'
DIGITS = dict(zip('零〇一二两三四五六七八九', '001223456789'))
NUMERAL = r'[零〇一二两三四五六七八九十百千万亿点]+'
TOKEN = re.compile(NUMERAL)
CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]')
LATIN_DIGIT = re.compile(r'[A-Za-z0-9]')

# Deliberately small and shipped with the code. A future lexicon can extend this list.
EXCEPTIONS = (
    '一心一意', '三心二意', '七上八下', '十全十美', '一五一十', '万无一失', '千方百计',
    '一刀切', '统一', '唯一', '万一', '一样', '一直', '一定', '一些', '一下', '一起', '一般',
    '百分之百', '成千上万', '十万火急', '十万大山', '万人空巷', '万众一心', '一毛不拔',
    '九牛一毛', '五光十色', '四面八方', '半斤八两', '百发百中', '三天打鱼', '两天晒网',
    '三天两头', '三番五次', '一举两得', '一分为二', '三长两短', '四分五裂', '五花八门',
    '六神无主', '七嘴八舌', '八仙过海', '九死一生', '十拿九稳', '百里挑一', '千钧一发',
    '一日三秋', '一日千里', '一千零一夜', '一百零八将', '双十一',
    '十年如一日', '十年寒窗', '十年树木', '十年磨一剑', '三十年河东', '三十年河西',
    '十万八千里', '十天半个月',
    '三国', '二郎神', '三体', '五指山', '四大天王',
)
STRICT_UNITS = tuple(sorted((
    '公里', '千米', '厘米', '毫米', '微米', '纳米', '米', '平方公里', '平方米', '平方厘米',
    '立方米', '立方厘米', '千克', '公斤', '毫克', '微克', '克', '吨', '斤', '两',
    '毫升', '微升', '升', '毫秒', '微秒', '秒钟', '分钟', '小时', '秒', '天',
    '摄氏度', '千瓦时', '千瓦', '瓦', '伏', '安培', '赫兹', '兆赫', '千焦', '焦耳', '卡路里',
), key=len, reverse=True))
COUNT_UNITS = tuple(sorted((
    '个人', '个人次', '人次', '人', '个', '次', '件', '条', '只', '位', '名', '台', '部', '本',
    '张', '份', '支', '把', '颗', '粒', '枚', '套', '组', '所', '间', '家', '辆', '架', '艘',
    '场', '局', '轮', '枪', '岁', '年', '元', '块钱', '美元', '欧元', '倍', '项', '种',
), key=len, reverse=True))
UNITS = tuple(sorted(set(STRICT_UNITS + COUNT_UNITS), key=len, reverse=True))


def _spell(value: int) -> str:
    """Canonical spelling below 10000, to reject elisions such as 一百八."""
    parts, zero = [], False
    for power, unit in ((1000, '千'), (100, '百'), (10, '十'), (1, '')):
        digit, value = divmod(value, power)
        if digit:
            if zero:
                parts.append('零')
            parts.append('零一二三四五六七八九'[digit] + unit)
            zero = False
        elif parts and value:
            zero = True
    text = ''.join(parts) or '零'
    return text[1:] if text.startswith('一十') else text


def _integer(text: str) -> int | None:
    if len(text) == 1 and text in DIGITS:
        return int(DIGITS[text])
    normalized = text.replace('两', '二').replace('〇', '零')
    value, pending = 0, None
    for char in normalized:
        if char in DIGITS:
            if pending not in (None, 0):
                return None  # adjacent digits are ambiguous outside years and model names
            pending = int(DIGITS[char])
        elif char in '十百千':
            value += (pending if pending is not None else 1) * {'十': 10, '百': 100, '千': 1000}[char]
            pending = None
        else:
            return None
    value += pending or 0
    if value >= 10000 or normalized != _spell(value):
        return None
    return value


def _number(text: str) -> str | None:
    whole, dot, fraction = text.partition('点')
    integer = _integer(whole)
    if integer is None or (dot and (not fraction or any(c not in DIGITS or c == '两' for c in fraction))):
        return None
    return str(integer) + ('.' + ''.join(DIGITS[c] for c in fraction) if dot else '')


def _quantity(text: str) -> str | None:
    """Use the same threshold for every unit, including the coefficient of 万/亿."""
    scale = text[-1:] if text.endswith(('万', '亿')) else ''
    value = _number(text[:-1] if scale else text)
    if value is not None and ('.' in value or int(value) >= 10):
        return value + scale
    return None


def _quantity_parts(token: str, after: str) -> tuple[str, str]:
    # 千 can belong to the unit: 三千克 is three kilograms, not 3000 grams.
    if token.endswith('千') and ('千' + after).startswith(STRICT_UNITS):
        token, after = token[:-1], '千' + after
    elif len(token) > 1 and token.endswith('两') and not after.startswith(UNITS) and not re.match('[A-Za-z]', after):
        token, after = token[:-1], '两' + after
    unit = next((unit for unit in UNITS if after.startswith(unit)), '')
    return token, unit


def _protected(text: str, extra: Iterable[str]) -> list[tuple[int, int]]:
    spans = []
    for word in (*EXCEPTIONS, *extra):
        if word:
            spans.extend((m.start(), m.end()) for m in re.finditer(re.escape(word), text))
    # Titles and explicitly lunar dates are not ordinary quantities. Do not guess proper names.
    for pattern in (r'《[^》]*》', r'〈[^〉]*〉', r'(?:农历|阴历)[^，。！？；\n]*',
                    r'(?:正|腊|冬)月[初十廿零一二三四五六七八九]+'):
        spans.extend((m.start(), m.end()) for m in re.finditer(pattern, text))
    return spans


def _number_edits(text: str, extra: Iterable[str]) -> list[dict]:
    protected = _protected(text, extra)
    edits = []

    def add(start, end, replacement, rule):
        if any(start < b and end > a for a, b in protected):
            return
        if start and (text[start - 1] in '第数几上约近' or text[start - 1] in '0123456789'):
            return
        if text[end:end + 1] in ('几', '多', '余', '来'):
            return
        if any(start < e['end'] and end > e['start'] for e in edits):
            return
        edits.append({'layer': 'number_conversion', 'start': start, 'end': end,
                      'original': text[start:end], 'replacement': replacement, 'rule': rule})

    for match in re.finditer(r'百分之(' + NUMERAL + ')', text):
        value = _number(match[1])
        if value is not None:
            add(match.start(), match.end(), value + '%', '百分比')
    for match in re.finditer('(' + NUMERAL + ')月(' + NUMERAL + ')(日|号)', text):
        month, day = _integer(match[1]), _integer(match[2])
        if month and 1 <= month <= 12 and day and 1 <= day <= (29 if month == 2 else 30 if month in (4, 6, 9, 11) else 31):
            add(match.start(), match.end(), f'{month}月{day}{match[3]}', '明确月日')
    for match in re.finditer('(' + NUMERAL + ')比(' + NUMERAL + ')', text):
        left, right = _integer(match[1]), _integer(match[2])
        if left is not None and right is not None and re.search(r'比分(?:是|为|来到|变为|变成|[:：\s])*$', text[:match.start()]):
            add(match.start(), match.end(), f'{left}:{right}', '明确比分')
    for match in re.finditer('(' + NUMERAL + ')(到|至)(' + NUMERAL + ')', text):
        right, unit = _quantity_parts(match[3], text[match.end():])
        if not unit and not right.endswith(('万', '亿')):
            continue
        end = match.start(3) + len(right) + len(unit)
        left_value, right_value = _quantity(match[1]), _quantity(right)
        if left_value is not None and right_value is not None:
            add(match.start(), end, left_value + match[2] + right_value + unit, '数量范围')
        # Reserve even a rejected range, so a later scalar/model rule cannot edit just one end.
        protected.append((match.start(), end))
    for match in TOKEN.finditer(text):
        token, start, end = match[0], match.start(), match.end()
        before, after = text[max(0, start - 1):start], text[end:]
        if before == '之' and text[:start].endswith('百分之'):
            continue
        quantity, unit = _quantity_parts(token, after)
        if quantity != token:
            value = _quantity(quantity)
            if value is not None:
                add(start, start + len(quantity), value, '计量')
            continue
        suffix_model = re.match('[A-Za-z]', after)
        prefix_model = (re.fullmatch('[A-Za-z]', before) and not unit
                        and not token.endswith(('万', '亿')))
        boundary = not after or after[0].isspace() or unicodedata.category(after[0]).startswith('P')
        if suffix_model or prefix_model:
            value = (''.join(DIGITS[c] for c in token) if all(c in DIGITS and c != '两' for c in token)
                     else _number(token))
            if value is not None and (suffix_model or len(token) >= 2 or boundary):
                add(start, end, value, '型号')
            continue
        if after.startswith('年') and len(token) == 4 and all(c in DIGITS and c != '两' for c in token):
            value = ''.join(DIGITS[c] for c in token)
            if value[0] != '0':
                add(start, end, value, '四位年份')
            continue
        if token.endswith(('万', '亿')):
            value = _quantity(token)
            if value is not None:
                add(start, end, value, '整万整亿')
            continue
        value = _quantity(token)
        if value is None:
            continue
        if after.startswith(STRICT_UNITS):
            add(start, end, value, '计量')
        elif after.startswith(COUNT_UNITS):
            add(start, end, value, '统计数量')
    return sorted(edits, key=lambda e: e['start'])


def _spacing_edits(text: str) -> list[dict]:
    boundaries = []
    for i in range(1, len(text)):
        left, right = text[i - 1], text[i]
        percent = left == '%' and i > 1 and text[i - 2].isascii() and text[i - 2].isdigit()
        if (CJK.fullmatch(left) and LATIN_DIGIT.fullmatch(right) or
                (LATIN_DIGIT.fullmatch(left) or percent) and CJK.fullmatch(right)):
            boundaries.append(i)
    # Merge overlapping contexts so each change is readable and can be replayed without overlap.
    groups = []
    for i in boundaries:
        if groups and i - 1 <= groups[-1][-1] + 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    edits = []
    for group in groups:
        start, end = group[0] - 1, group[-1] + 1
        replacement = ''.join((' ' if i in group else '') + text[i] for i in range(start, end))
        edits.append({'layer': 'cjk_spacing', 'start': start, 'end': end,
                      'original': text[start:end], 'replacement': replacement, 'rule': '中英文与数字间距'})
    return edits


def _replace(text: str, edits: list[dict]) -> str:
    for edit in reversed(edits):
        text = text[:edit['start']] + edit['replacement'] + text[edit['end']:]
    return text


def apply(text: str, *, number_conversion: bool = True, cjk_spacing: bool = True,
          extra_exceptions: Iterable[str] = ()) -> tuple[str, list[dict]]:
    """Offsets are zero-based, end-exclusive, in each layer's input (numbers, then spacing)."""
    for name, value in (('number_conversion', number_conversion), ('cjk_spacing', cjk_spacing)):
        if type(value) is not bool:
            raise ValueError(f'{name} 必须是布尔值 true 或 false。')
    changes = _number_edits(text, extra_exceptions) if number_conversion else []
    converted = _replace(text, changes)
    spaces = _spacing_edits(converted) if cjk_spacing else []
    return _replace(converted, spaces), changes + spaces
