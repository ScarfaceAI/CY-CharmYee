from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import support  # noqa: F401
import check_release
import package


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.notes = "docs/releases/v9.9.9.md"
        for name, body in {
            "VERSION": "9.9.9\n",
            "CHANGELOG.md": "# 更新记录\n\n## 9.9.9 - 2026-11-17\n\n仅保留中文文档。\n",
            "SKILL.md": "---\nname: cy-charmyee\ndescription: 测试入口\n---\n",
            "INSTALL.md": "# 安装说明\n",
            "LICENSE": "测试许可\n",
            "scripts/install.ps1": "# 测试安装入口\n",
            "scripts/brain.py": "# 测试运行入口\n",
            self.notes: "仅保留中文文档。\n",
        }.items():
            self.write(name, body)

    def write(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def check(self):
        package.write_manifest(self.root, "9.9.9")
        return check_release.check(self.root)

    def test_chinese_body_needs_no_language_headings(self):
        self.assertEqual([], self.check())

    def test_missing_current_version_notes_is_reported(self):
        (self.root / self.notes).unlink()
        self.write("docs/releases/v9.9.8.md", "上一版本的发布说明。\n")
        self.assertEqual(["缺少 VERSION 对应版本的发布说明"], self.check())

    def test_empty_or_whitespace_notes_is_reported(self):
        for body in ("", " \t\r\n", "\ufeff \n"):
            with self.subTest(body=repr(body)):
                self.write(self.notes, body)
                self.assertEqual(["发布说明正文为空"], self.check())

    def test_existing_historical_english_links_are_preserved(self):
        for name in ("docs/releases/v1.5.0.md", "docs/verification/workflow-continuity.md"):
            self.write(name, "历史记录：[恢复指南](../../references/continuation.en.md)。\n")
        self.assertEqual([], self.check())

    def test_new_english_document_links_are_still_rejected(self):
        self.write("README.md", "[恢复指南](references/continuation.en.md)\n")
        self.write(self.notes, "发布说明：[恢复指南](../../references/continuation.en.md)。\n")
        self.assertCountEqual([
            "Broken local link in README.md: references/continuation.en.md",
            f"Broken local link in {self.notes}: ../../references/continuation.en.md",
        ], self.check())

    def test_other_broken_links_in_historical_documents_are_still_rejected(self):
        name = "docs/releases/v1.5.0.md"
        self.write(name, "[中文指南](../../references/missing.md)\n"
                         "[套件指南](../../references/suite.en.md)\n")
        self.assertEqual([
            f"Broken local link in {name}: ../../references/missing.md",
            f"Broken local link in {name}: ../../references/suite.en.md",
        ], self.check())
