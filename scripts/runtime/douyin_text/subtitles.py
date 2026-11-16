"""Subtitle export from stored recognition timing, optionally carrying the registered review text.

Recognition stores sentence-level times. A reviewed transcript is placed back on those times by
aligning its letters and digits with the machine text; punctuation follows the character before it.
Nothing here calls speech recognition, changes task status or edits a registered text.
"""
from __future__ import annotations

import difflib
import hashlib
import math
import re
import unicodedata
from pathlib import Path

from . import content, workflow
from .common import ROOT, inside, read_json, relative, safe_name

# Below this share of directly matching characters the review is a different text (a rewrite or a
# file registered for another item) and is not forced onto the recognition timeline.
MIN_MATCH = 0.6
# Cues made mostly of corrections, or reading faster than people speak, are listed for a spot check.
EDITED_SHARE = 0.5
MAX_READING_RATE = 12.0  # characters per second
END_MARKS = set("，。、；：,.;:…—～~")
LEADING_MARKS = set("，。、；：,.;:")
BREAK_AFTER = set("，。！？；：、,.!?;:…")
CLOSING = set("”’」』）)】》〉]\"'")
OPENING = set("“‘「『（(【《〈[")
# Without a dictionary, prefer cuts that rarely split a word: after a particle or closing mark,
# before an opening mark or a common word-initial character, or where Chinese meets Latin text.
# Split lines are shown one after another, so uneven lengths cost less than a split word.
SOFT_BREAK_AFTER = set("的了着过吗呢吧啊呀哦嘛啦")
WORD_START = set("也还再又并或却很最更让被给跟所就没")
NATURAL_BREAK, SCRIPT_BREAK = 6.0, 3.0
# A grouped number such as 100,000 is one token, so its commas never end a clause or a line.
TOKEN = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])(?:\.[0-9]+)?%?"
                   r"|[A-Za-z0-9]+(?:[.'’\-][A-Za-z0-9]+)*%?|\s+|.", re.S)
CJK = "\u2e80-\u2fff\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
REASONS = {
    "not_registered": "还没有登记校对稿",
    "edited": "校对工作稿登记后又被修改；先读差异并重新登记，再导出",
    "missing": "登记的校对稿文件不存在",
    "stale": "识别原文依据已变化；复核后重新登记校对稿",
}


def is_text(char: str) -> bool:
    return unicodedata.category(char)[0] in "LN"


def width(text: str) -> int:
    """Letters and digits; punctuation and spaces do not count toward a line limit."""
    return sum(map(is_text, text))


def key(char: str) -> str:
    return unicodedata.normalize("NFKC", char).casefold()


def tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(f"(?<=[{CJK}]) (?=[{CJK}])", "", text)


def align(segments: list[dict], reviewed: str) -> tuple[list[str], dict]:
    """Cut the reviewed text at the recognition segment boundaries and report how well it matched."""
    raw, raw_owner = [], []
    for number, segment in enumerate(segments):
        for char in segment["text"]:
            if is_text(char):
                raw.append(key(char))
                raw_owner.append(number)
    positions = [index for index, char in enumerate(reviewed) if is_text(char)]
    if not raw:
        raise ValueError("识别原文只有标点，没有可对齐的文字")
    if not positions:
        raise ValueError("校对稿没有可对齐的文字")
    review = [key(reviewed[index]) for index in positions]
    owner, anchored = [0] * len(review), [False] * len(review)
    matcher = difflib.SequenceMatcher(None, raw, review, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(j2 - j1):
                owner[j1 + offset] = raw_owner[i1 + offset]
                anchored[j1 + offset] = True
        elif tag == "replace":
            # A correction such as 三十五 → 35 spreads over the characters it replaces.
            for offset in range(j2 - j1):
                owner[j1 + offset] = raw_owner[i1 + offset * (i2 - i1) // (j2 - j1)]
        elif tag == "insert":
            neighbour = raw_owner[i1 - 1] if i1 else raw_owner[0]
            for offset in range(j1, j2):
                owner[offset] = neighbour
        # "delete": machine characters the review removed have nothing to place.
    pieces: list[list[str]] = [[] for _ in segments]
    current, pointer = owner[0], 0
    for index, char in enumerate(reviewed):
        if pointer < len(positions) and positions[pointer] == index:
            current = owner[pointer]
            pointer += 1
        pieces[current].append(char)
    totals, exact = [0] * len(segments), [0] * len(segments)
    for number, matched in zip(owner, anchored):
        totals[number] += 1
        exact[number] += matched
    same = sum(anchored)
    return [tidy("".join(piece)) for piece in pieces], {
        "match_ratio": round(same / max(len(raw), len(review)), 4),
        "changed_chars": len(review) - same,
        "edited_segments": {number for number, total in enumerate(totals)
                            if total and exact[number] / total < EDITED_SHARE},
    }


def clauses(text: str) -> list[str]:
    tokens = TOKEN.findall(text)
    result, current, index = [], "", 0
    while index < len(tokens):
        current += tokens[index]
        index += 1
        if tokens[index - 1] in BREAK_AFTER:
            while index < len(tokens) and (tokens[index] in CLOSING or tokens[index].isspace()):
                current += tokens[index]
                index += 1
            result.append(current)
            current = ""
    return result + [current] if current else result


def break_bonus(before: str, after: str) -> float:
    if before in SOFT_BREAK_AFTER or before in CLOSING or after in OPENING or after in WORD_START:
        return NATURAL_BREAK
    return SCRIPT_BREAK if before.isascii() != after.isascii() else 0.0


def split_long(clause: str, limit: int) -> list[tuple[str, bool]]:
    """Balance an over-long clause over the fewest lines, never inside an English word or number.

    Each line carries whether the cut after it fell between two Chinese characters with no natural
    break nearby, which may split a word and deserves a look.
    """
    tokens = TOKEN.findall(clause)
    widths = [width(token) for token in tokens]
    parts, start = [], 0
    while sum(widths[start:]) > limit:
        remaining = sum(widths[start:])
        target = math.ceil(remaining / math.ceil(remaining / limit))
        best, best_score, used = None, None, 0
        for index in range(start + 1, len(tokens)):
            used += widths[index - 1]
            if used > limit:
                break
            if not used or tokens[index - 1] in OPENING or not (widths[index] or tokens[index] in OPENING):
                continue
            score = abs(used - target) - break_bonus(tokens[index - 1], tokens[index])
            if best_score is None or score < best_score:
                best, best_score = index, score
        if best is None:  # one token wider than the limit, such as a long English word
            best = start + 1
            while best < len(tokens) and not widths[best] and tokens[best] not in OPENING:
                best += 1
        by_length = (best < len(tokens) and not break_bonus(tokens[best - 1], tokens[best])
                     and is_text(tokens[best - 1][-1]) and is_text(tokens[best][0]))
        parts.append(("".join(tokens[start:best]), by_length))
        start = best
    parts.append(("".join(tokens[start:]), False))
    return [(part.strip(), by_length) for part, by_length in parts if part.strip()]


def lines(text: str, limit: int | None) -> list[tuple[str, bool]]:
    """Pack clauses into lines within the limit; each line carries split_long's cut flag."""
    if not limit or width(text) <= limit:
        return [(text, False)]
    result, current = [], ""
    for clause in clauses(text):
        if width(clause) > limit:
            if current.strip():
                result.append((current, False))
            current = ""
            result.extend(split_long(clause, limit))
        elif width(current + clause) <= limit:
            current += clause
        else:
            result.append((current, False))
            current = clause
    if current.strip():
        result.append((current, False))
    return [(line.strip(), by_length) for line, by_length in result if line.strip()]


def timed_lines(start: float, end: float, text: str,
                limit: int | None) -> list[tuple[float, float, str, bool]]:
    """Recognition has sentence times only, so split lines share the sentence by character count."""
    parts = lines(text, limit)
    weights = [max(width(part), 1) for part, _ in parts]
    total, span, used, result = sum(weights), end - start, 0, []
    for index, ((part, by_length), weight) in enumerate(zip(parts, weights)):
        begin = start + span * used / total
        used += weight
        result.append((begin, end if index == len(parts) - 1 else start + span * used / total, part, by_length))
    return result


def strip_marks(text: str) -> str:
    """Drop commas and full stops at either end of a line; question and exclamation marks keep the tone."""
    text = text.strip()
    while text and text[-1] in END_MARKS:
        text = text[:-1].rstrip()
    while text and text[0] in LEADING_MARKS:
        text = text[1:].lstrip()
    return text


def timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def render(cues: list[tuple[float, float, str]]) -> str:
    return "\n\n".join(f"{number}\n{timestamp(start)} --> {timestamp(end)}\n{text}"
                       for number, (start, end, text) in enumerate(cues, 1)) + "\n"


def export(task_id: str, root: Path = ROOT, *, reviewed: bool = False, max_chars: int | None = None,
           strip_end_punct: bool = False) -> dict:
    """Write one SRT per successful item into the task's delivery folder, named by content hash."""
    if max_chars is not None and not 4 <= max_chars <= 80:
        raise ValueError("每条字幕的字数上限应在 4 到 80 之间")
    task = workflow.load(task_id, root)
    if not task.get("results"):
        raise ValueError("尚无完整转写结果")
    content.originals(task, root)
    states = {row["id"]: row for row in content.inspect_content(task, "review", root)} if reviewed else {}
    folder = root / "outputs" / "deliveries" / task_id
    label = "校对版" if reviewed else "机器原文"
    items, skipped, files = [], [], []
    for number, item in zip(content.numbers(task, root), task["results"]):
        result = read_json(inside(root, item["result"]))
        segments = [x for x in result.get("segments", []) if str(x.get("text", "")).strip()]
        if not segments:
            skipped.append({"id": item["id"], "number": number, "state": "no_timing",
                            "reason": "识别结果没有分句时间，不能生成有效字幕"})
            continue
        texts, report, row = [x["text"] for x in segments], {}, {}
        if reviewed:
            row = states[item["id"]]
            if row["state"] != "valid":
                skipped.append({"id": item["id"], "number": number, "state": row["state"],
                                "reason": REASONS.get(row["state"]) or row.get("reason") or "校对稿暂不可用"})
                continue
            review_text = inside(root, row["file"]).read_text(encoding="utf-8-sig")
            try:
                texts, report = align(segments, review_text)
            except ValueError as error:  # one side has no words; the rest of the batch still exports
                skipped.append({"id": item["id"], "number": number, "state": "unalignable", "reason": str(error)})
                continue
            if report["match_ratio"] < MIN_MATCH:
                skipped.append({"id": item["id"], "number": number, "state": "misaligned",
                                "match_ratio": report["match_ratio"],
                                "reason": f"校对稿只有 {report['match_ratio']:.0%} 能与识别原文对上，"
                                          "可能是改写稿或登记错了条目，不能沿用原时间轴"})
                continue
        cues, check, dropped = [], [], []
        for index, (segment, text) in enumerate(zip(segments, texts)):
            pieces = timed_lines(segment["start"], segment["end"], text, max_chars) if text else []
            if strip_end_punct:
                pieces = [(start, end, strip_marks(part), cut) for start, end, part, cut in pieces]
            pieces = [piece for piece in pieces if width(piece[2])]  # a lone mark is not a subtitle
            if not pieces:
                dropped.append(index + 1)
                continue
            for start, end, part, by_length in pieces:
                cues.append((start, end, part))
                reasons = []
                if reviewed and index in report["edited_segments"]:
                    reasons.append("mostly_corrected")
                if reviewed and (end <= start or width(part) / (end - start) > MAX_READING_RATE):
                    reasons.append("fast_reading")
                if by_length:
                    reasons.append("split_by_length")
                if reasons:
                    check.append({"cue": len(cues), "reasons": reasons})
        if not cues:
            skipped.append({"id": item["id"], "number": number, "state": "empty", "reason": "没有可显示的字幕文字"})
            continue
        data = render(cues).encode("utf-8")
        checksum = hashlib.sha256(data).hexdigest()
        title = safe_name(str(result.get("source", {}).get("title") or item["id"]), 30)
        path = folder / f"字幕-{label}-{number:02d}-{title}-{checksum[:10]}.srt"
        content.immutable(path, data)
        files.append(str(path))
        entry = {"id": item["id"], "number": number, "file": relative(root, path), "sha256": checksum,
                 "cues": len(cues), "source_segments": len(segments), "check_cues": check}
        if reviewed:
            entry.update(review_file=row["file"], review_sha256=row["sha256"], match_ratio=report["match_ratio"],
                         changed_chars=report["changed_chars"])
        if dropped:
            entry["dropped_segments"] = dropped
        items.append(entry)
    if not items:
        raise ValueError("没有可导出的字幕：" + "；".join(f"第 {x['number']} 条{x['reason']}" for x in skipped))
    return {"status": "partial" if skipped else "exported", "kind": "reviewed" if reviewed else "raw",
            "files": files, "items": items, "skipped": skipped, "gaps": workflow.coverage_gaps(task, root),
            "style": {"max_chars": max_chars, "strip_end_punct": strip_end_punct},
            "timing": "recognition_segments" if max_chars is None else "recognition_segments_split_by_characters"}
