from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401  # must precede douyin_text: sets the runtime path and a guarded default workspace
from douyin_text import asr, content, credentials, subtitles, vault, workflow
from douyin_text.common import digest, write_json
from support import SKILL_ROOT, WorkspaceCase

# Recognition of an 8-second clip at a natural speaking rate. The name, the game title and a number
# come back wrong, as they often do in real clips.
UTTERANCES = [("大家好，我是小汪。", 0, 1500), ("今天聊对至二。", 1500, 2700),
              ("这把枪的伤害是三十五，", 2900, 4800), ("记得点赞关注。", 5000, 6300), ("拜拜。", 6500, 7000)]
REVIEWED = "大家好，我是小王。\n今天聊对峙2，这把枪的伤害是35，记得点赞关注！拜拜。"


class Recognition(io.BytesIO):
    def __init__(self):
        super().__init__(json.dumps({"result": {"text": "".join(text for text, _, _ in UTTERANCES), "utterances": [
            {"text": text, "start_time": start, "end_time": end} for text, start, end in UTTERANCES]}}).encode())
        self.headers = {"X-Api-Status-Code": "20000000", "X-Tt-Logid": "test-log"}


def silence(path: Path, sample: bytes) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(sample * 16000 * 8)
    return path


def cues(path) -> list[tuple[str, str, str]]:
    blocks = Path(path).read_text(encoding="utf-8").strip().split("\n\n")
    parsed = []
    for number, block in enumerate(blocks, 1):
        index, timing, *text = block.split("\n")
        start, end = timing.split(" --> ")
        assert int(index) == number, block
        parsed.append((start, end, "\n".join(text)))
    return parsed


class ReviewedSubtitleTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        silence(self.wav, b"\x00\x00")
        write_json(self.root / "config.json", {"chunk_seconds": 300, "asr_timeout_seconds": 5,
                   "assistant_root": str(self.parent / "assistant"), "vault_root": str(self.parent / "vault")})

    def recognized(self, *media: Path) -> dict:
        task = workflow.plan([str(path) for path in media or (self.wav,)], self.root)
        workflow.confirm(task["id"], "transcribe", "转成文字，校对后导出字幕", root=self.root)
        with patch.object(credentials, "load", return_value={"api_key": "test-value"}), \
                patch.object(asr.request, "urlopen", side_effect=lambda *a, **k: Recognition()), \
                patch.object(vault, "publish", side_effect=AssertionError("unexpected vault publish")):
            return workflow.run(task["id"], self.root)

    def review(self, task: dict, *texts: str, items: list[str] | None = None) -> list[Path]:
        files = []
        for number, text in enumerate(texts, 1):
            path = self.root / "runs" / task["id"] / "review" / f"{number}-校对稿.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            files.append(path)
        content.register_content(task["id"], files, "review", self.root, item_ids=items)
        return files

    def test_reviewed_text_keeps_the_recognition_timing(self):
        task = self.recognized()
        review = self.review(task, REVIEWED)[0]
        before = {path: digest(path) for path in (review, self.path(task["results"][0]["text"]))}
        result = subtitles.export(task["id"], self.root, reviewed=True)
        self.assertEqual("exported", result["status"])
        self.assertEqual([
            ("00:00:00,000", "00:00:01,500", "大家好，我是小王。"),
            ("00:00:01,500", "00:00:02,700", "今天聊对峙2，"),
            ("00:00:02,900", "00:00:04,800", "这把枪的伤害是35，"),
            ("00:00:05,000", "00:00:06,300", "记得点赞关注！"),
            ("00:00:06,500", "00:00:07,000", "拜拜。"),
        ], cues(result["files"][0]))
        item = result["items"][0]
        self.assertEqual(5, item["changed_chars"])  # 汪→王, 至二→峙2, 三十五→35
        self.assertEqual([], item["check_cues"])
        self.assertEqual(f"runs/{task['id']}/review/1-校对稿.txt", item["review_file"])
        path = Path(result["files"][0])
        self.assertEqual(self.root / "outputs" / "deliveries" / task["id"], path.parent)
        self.assertEqual(f"字幕-校对版-01-中文录音-{item['sha256'][:10]}.srt", path.name)
        self.assertEqual(before, {path: digest(path) for path in before})
        self.assertFalse(list((self.root / "outputs" / task["results"][0]["id"]).rglob("*.srt")))
        self.assertEqual("ready_to_deliver", workflow.load(task["id"], self.root)["status"])
        self.assertEqual(result["files"], subtitles.export(task["id"], self.root, reviewed=True)["files"])

    def test_short_lines_share_their_sentence_time_and_drop_end_marks(self):
        task = self.recognized()
        self.review(task, REVIEWED)
        result = subtitles.export(task["id"], self.root, reviewed=True, max_chars=6, strip_end_punct=True)
        self.assertEqual([
            ("00:00:00,000", "00:00:00,643", "大家好"),
            ("00:00:00,643", "00:00:01,500", "我是小王"),
            ("00:00:01,500", "00:00:02,700", "今天聊对峙2"),
            ("00:00:02,900", "00:00:03,744", "这把枪的"),
            ("00:00:03,744", "00:00:04,800", "伤害是35"),
            ("00:00:05,000", "00:00:06,300", "记得点赞关注！"),
            ("00:00:06,500", "00:00:07,000", "拜拜"),
        ], cues(result["files"][0]))
        self.assertEqual("recognition_segments_split_by_characters", result["timing"])
        with self.assertRaisesRegex(ValueError, "4 到 80"):
            subtitles.export(task["id"], self.root, reviewed=True, max_chars=2)

    def test_machine_text_can_be_split_while_the_default_export_is_unchanged(self):
        task = self.recognized()
        default = Path(workflow.export_srt(task["id"], self.root)[0])
        self.assertEqual(self.path(task["results"][0]["result"]).with_name("transcript.srt"), default)
        self.assertEqual("1\n00:00:00,000 --> 00:00:01,500\n大家好，我是小汪。\n\n"
                         "2\n00:00:01,500 --> 00:00:02,700\n今天聊对至二。\n\n"
                         "3\n00:00:02,900 --> 00:00:04,800\n这把枪的伤害是三十五，\n\n"
                         "4\n00:00:05,000 --> 00:00:06,300\n记得点赞关注。\n\n"
                         "5\n00:00:06,500 --> 00:00:07,000\n拜拜。\n", default.read_text(encoding="utf-8"))
        result = subtitles.export(task["id"], self.root, max_chars=6)
        self.assertEqual("raw", result["kind"])
        self.assertTrue(Path(result["files"][0]).name.startswith("字幕-机器原文-01-"))
        self.assertEqual(["大家好，", "我是小汪。", "今天聊对至二。", "这把枪的", "伤害是三十五，", "记得点赞关注。", "拜拜。"],
                         [text for _, _, text in cues(result["files"][0])])
        self.assertEqual([], result["items"][0]["check_cues"])

    def test_items_without_a_valid_review_are_reported_not_filled_with_machine_text(self):
        task = self.recognized(self.wav, silence(self.parent / "第二段录音.wav", b"\x01\x00"))
        self.assertEqual(2, len(task["results"]))
        review = self.review(task, REVIEWED, items=[task["results"][0]["id"]])[0]
        result = subtitles.export(task["id"], self.root, reviewed=True)
        self.assertEqual("partial", result["status"])
        self.assertEqual([1], [item["number"] for item in result["items"]])
        self.assertEqual([(2, "not_registered")], [(item["number"], item["state"]) for item in result["skipped"]])
        self.assertEqual(1, len(list((self.root / "outputs" / "deliveries" / task["id"]).glob("*.srt"))))
        review.write_text(REVIEWED.replace("小王", "小汪"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "重新登记"):
            subtitles.export(task["id"], self.root, reviewed=True)

    def test_an_item_with_nothing_to_align_is_skipped_without_stopping_the_batch(self):
        task = self.recognized(self.wav, silence(self.parent / "第二段录音.wav", b"\x01\x00"))
        self.review(task, REVIEWED, "……")
        result = subtitles.export(task["id"], self.root, reviewed=True)
        self.assertEqual("partial", result["status"])
        self.assertEqual([1], [item["number"] for item in result["items"]])
        self.assertEqual([(2, "unalignable", "校对稿没有可对齐的文字")],
                         [(item["number"], item["state"], item["reason"]) for item in result["skipped"]])

    def test_a_rewrite_is_not_forced_onto_the_timeline(self):
        task = self.recognized()
        self.review(task, "这条视频主要讲武器选择：新手先练步枪，再按地图调整配件。")
        with self.assertRaisesRegex(ValueError, "不能沿用原时间轴"):
            subtitles.export(task["id"], self.root, reviewed=True)
        self.assertFalse((self.root / "outputs" / "deliveries" / task["id"]).exists())

    def test_text_without_recognition_timing_cannot_become_subtitles(self):
        source = self.parent / "已有文稿.txt"
        source.write_text("已有文稿正文。", encoding="utf-8")
        task = content.plan_text([source], self.root)
        workflow.confirm(task["id"], "transcribe", "校对已有文稿", root=self.root)
        task = workflow.run(task["id"], self.root)
        self.review(task, "已有文稿正文。")
        with self.assertRaisesRegex(ValueError, "没有分句时间"):
            subtitles.export(task["id"], self.root, reviewed=True)

    def test_cli_exports_reviewed_subtitles_and_keeps_the_default_output(self):
        task = self.recognized()
        self.review(task, REVIEWED)
        command = [sys.executable, "-B", "-X", "utf8", str(SKILL_ROOT / "scripts" / "brain.py"),
                   "--workspace", str(self.root), "export-srt", task["id"]]
        process = subprocess.run(command + ["--reviewed", "--max-chars", "12", "--strip-end-punct"],
                                 capture_output=True, timeout=120)
        self.assertEqual(0, process.returncode, process.stdout.decode("utf-8", "replace"))
        shown = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(("exported", "reviewed"), (shown["status"], shown["kind"]))
        self.assertEqual({"max_chars": 12, "strip_end_punct": True}, shown["style"])
        self.assertTrue((Path(shown["workspace"]) / shown["items"][0]["file"]).is_file())
        process = subprocess.run(command, capture_output=True, timeout=120)
        self.assertEqual(0, process.returncode, process.stdout.decode("utf-8", "replace"))
        shown = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual({"files", "workspace"}, set(shown))
        self.assertEqual("transcript.srt", Path(shown["files"][0]).name)


class AlignmentTests(unittest.TestCase):
    def test_removed_words_drop_their_sentence_and_added_words_join_a_neighbour(self):
        texts, report = subtitles.align([{"text": "嗯嗯嗯"}, {"text": "我们开始吧"}, {"text": "下课"}],
                                        "我们开始吧。下课了。")
        self.assertEqual(["", "我们开始吧。", "下课了。"], texts)
        self.assertEqual(1, report["changed_chars"])
        self.assertEqual(["好，开始吧"], subtitles.align([{"text": "开始吧"}], "好，开始吧")[0])

    def test_a_side_with_only_punctuation_names_what_cannot_be_aligned(self):
        with self.assertRaisesRegex(ValueError, "^识别原文只有标点"):
            subtitles.align([{"text": "……"}], "大家好")
        with self.assertRaisesRegex(ValueError, "^校对稿没有可对齐的文字"):
            subtitles.align([{"text": "大家好。"}], "……")

    def test_letter_case_and_full_width_digits_still_match(self):
        texts, report = subtitles.align([{"text": "我用iphone拍了３条"}], "我用 iPhone 拍了3条。")
        self.assertEqual(["我用 iPhone 拍了3条。"], texts)
        self.assertEqual(1.0, report["match_ratio"])

    def test_lines_never_split_english_words_or_numbers(self):
        lines = [line for line, _ in subtitles.lines("我们今天用iPhone15拍了一段3.5秒的视频，效果还不错吧？", 8)]
        self.assertTrue(any("iPhone15" in line for line in lines), lines)
        self.assertTrue(any("3.5秒" in line for line in lines), lines)
        self.assertTrue(all(subtitles.width(line) <= 8 for line in lines), lines)
        self.assertEqual([("他说：“好的。”", False), ("然后就走了", False)], subtitles.lines("他说：“好的。”然后就走了", 5))

    def test_thousands_separators_stay_inside_their_number(self):
        self.assertEqual([("我们买了", False), ("100,000台设备", False)], subtitles.lines("我们买了100,000台设备", 10))
        self.assertEqual(["我们买了", "100,000", "台设备"],
                         [subtitles.strip_marks(line) for line, _ in subtitles.lines("我们买了100,000台设备", 8)])
        self.assertEqual(["1,234,567.5%"], subtitles.TOKEN.findall("1,234,567.5%"))
        # Only three-digit groups join a number; other commas still separate.
        self.assertEqual(["1", ",", "0000", "和", "12", ",", "3"], subtitles.TOKEN.findall("1,0000和12,3"))

    def test_long_clauses_prefer_natural_breaks_and_flag_cuts_by_length(self):
        self.assertEqual([("今天给大家带来《对峙2》", False), ("里面最适合新手的三把枪。", False)],
                         subtitles.lines("今天给大家带来《对峙2》里面最适合新手的三把枪。", 14))
        self.assertEqual([("建议大家先在训练场压一压枪", False), ("再上实战。", False)],
                         subtitles.lines("建议大家先在训练场压一压枪再上实战。", 14))
        # Nothing marks a word boundary here, so the cut is made by length and reported.
        self.assertEqual([("新手拿它打中近距", True), ("离基本不会吃亏。", False)],
                         subtitles.lines("新手拿它打中近距离基本不会吃亏。", 14))

    def test_end_marks_go_but_tone_and_quotes_stay(self):
        self.assertEqual("真的吗？", subtitles.strip_marks("真的吗？"))
        self.assertEqual("太好了！", subtitles.strip_marks("，太好了！"))
        self.assertEqual("他说：“好的。”", subtitles.strip_marks("他说：“好的。”"))
        self.assertEqual("下次见", subtitles.strip_marks("下次见……"))


if __name__ == "__main__":
    unittest.main()
