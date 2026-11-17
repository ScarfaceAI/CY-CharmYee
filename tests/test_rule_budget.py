"""单条默认校对的规则预算与 Markdown 标题锚点；只读本地文件。"""
from __future__ import annotations

from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
BUDGET = 20_000
# 已安装并完成首次介绍；一条新作品／本地音视频，默认校对并交付。
# 两条路径都包含实际能力校对；正文与任务疑点清单属于产物，不是规则文件。
# 配置、首次介绍、已开启知识库、失败重试等条件分支不属于这条日常路径。
# 全文件计数，不按本次只读的行数打折。新增必读引用时同步更新这两份清单。
READING_PATHS = {
    "总入口": (
        "SKILL.md",
        "references/suite.md",
        "references/runtime.md",
        "references/content.md",
        "references/review-capabilities.md",
    ),
    "cy-transcribe": (
        "skills/cy-transcribe/SKILL.md",
        "references/suite.md",
        "references/runtime.md",
        "references/content.md",
        "references/review-capabilities.md",
    ),
}


def without_fences(text: str) -> str:
    """代码示例中的标题和链接不是文档锚点；保留行号。"""
    result = []
    fence = None
    lines = text.splitlines()
    if lines and lines[0] == "---":
        for end in range(1, len(lines)):
            if lines[end] in {"---", "..."}:
                lines[:end + 1] = [""] * (end + 1)
                break
    for line in lines:
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                    and not marker[2].strip()):
                fence = None
            result.append("")
        elif marker:
            fence = marker[1]
            result.append("")
        else:
            result.append(line)
    return "\n".join(result)


def heading_anchors(text: str) -> set[str]:
    """GitHub 标题：小写、去标点、空格换连字符，重复标题添加序号。"""
    anchors = set()
    previous = ""
    for line in without_fences(text).splitlines():
        heading = re.match(r"^ {0,3}#{1,6}[ \t]+(.+?)\s*$", line)
        title = None
        if heading:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[1])
        elif previous.strip() and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", line):
            title = previous.strip()
        if title is not None:
            base = "".join(c for c in title.lower() if c.isalnum() or c in " -_").replace(" ", "-")
            anchor, number = base, 0
            while anchor in anchors:
                number += 1
                anchor = f"{base}-{number}"
            anchors.add(anchor)
        previous = line
    return anchors


def relative_fragments(text: str):
    """提取行内、引用式及 HTML 链接；只返回相对链接的非空片段。"""
    for number, line in enumerate(without_fences(text).splitlines(), 1):
        links = re.findall(r"\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)", line)
        links += re.findall(r"^ {0,3}\[[^\]]+\]:\s*(<[^>\n]+>|\S+)", line)
        links += re.findall(r'''(?:href|src)=["']([^"']+)["']''', line)
        for raw in links:
            link = raw.strip("<>")
            parsed = urlsplit(link)
            if parsed.fragment and not parsed.scheme and not parsed.netloc and not parsed.path.startswith("/"):
                yield number, link, unquote(parsed.path), unquote(parsed.fragment)


class RuleBudgetTests(unittest.TestCase):
    def test_single_video_reading_paths_below_budget(self):
        for name, paths in READING_PATHS.items():
            with self.subTest(entry=name):
                sizes = {path: len((ROOT / path).read_bytes()) for path in paths}
                self.assertLess(sum(sizes.values()), BUDGET, f"{name} 超出 UTF-8 字节预算：{sizes}")

    def test_relative_link_fragments_match_target_headings(self):
        paths = [ROOT / name for name in (
            "SKILL.md", "README.md", "INSTALL.md", "CONTRIBUTING.md",
            "SOURCE_OF_TRUTH.md", "docs/RELEASING.md",
        )]
        paths += sorted((ROOT / "skills").glob("*/SKILL.md"))
        paths += sorted((ROOT / "references").glob("*.md"))
        cache = {}
        errors = []
        for source in paths:
            for line, link, relative, fragment in relative_fragments(source.read_text(encoding="utf-8-sig")):
                target = (source.parent / relative).resolve() if relative else source.resolve()
                # 目标文件存在性由 scripts/check_release.py 检查，这里只补标题锚点。
                if not target.is_file():
                    continue
                if target not in cache:
                    cache[target] = heading_anchors(target.read_text(encoding="utf-8-sig"))
                if fragment not in cache[target]:
                    errors.append(f"{source.relative_to(ROOT).as_posix()}:{line}: {link}")
        self.assertEqual([], errors, "无对应标题的相对锚点：\n" + "\n".join(errors))

    def test_heading_rules_ignore_code_and_number_duplicate_titles(self):
        text = ("---\nname: example\nlicense: MIT\n---\n"
                "# 中文 ABC_1：`Quick`！\n## 重复\n## 重复\n## 重复-1\n"
                "```powershell\n# 代码标题\n```\n~~~md\n# 另一示例\n~~~\n"
                "Setext 标题\n----\n### 尾部 ###\n")
        self.assertEqual({"中文-abc_1quick", "重复", "重复-1", "重复-1-1", "setext-标题", "尾部"},
                         heading_anchors(text))

    def test_fragment_links_include_same_file_and_encoded_chinese(self):
        text = ('[本页](#校对) [另页](<other.md#%E6%A0%A1%E5%AF%B9>)\n'
                '[外部](https://example.com/a#不存在) [协议相对](//example.com/#不存在)\n'
                '[引用]: other.md#校对 "标题"\n<a href="other.md#校对">入口</a>\n'
                '```md\n[示例](missing.md#无效)\n```\n')
        self.assertEqual([("", "校对"), ("other.md", "校对"), ("other.md", "校对"), ("other.md", "校对")],
                         [(path, anchor) for _, _, path, anchor in relative_fragments(text)])


if __name__ == "__main__":
    unittest.main()
