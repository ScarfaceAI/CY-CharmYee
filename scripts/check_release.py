"""Verify distributable content, local documentation links and the exact manifest."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from package import collect, make_manifest
from release_assets import version_tuple


# 只保留历史原文已有的 7 处英文链接；新文档和其他链接仍须检查。
HISTORICAL_EN_LINKS = {
    ("docs/releases/v1.5.0.md", "../../references/continuation.en.md"),
    ("docs/releases/v1.6.0.md", "../../references/personal-vault.en.md"),
    ("docs/releases/v1.6.0.md", "../../references/suite.en.md"),
    ("docs/releases/v1.6.0.md", "../../references/brand-migration.en.md"),
    ("docs/releases/v1.7.0.md", "../../references/personal-vault.en.md"),
    ("docs/releases/v1.7.0.md", "../../references/personal-domains.en.md"),
    ("docs/verification/workflow-continuity.md", "../../references/continuation.en.md"),
}


def check(root: Path) -> list[str]:
    errors = []
    version = (root / "VERSION").read_text().strip()
    try:
        version_tuple(version)
    except ValueError as exc:
        errors.append(str(exc))
    notes = root / "docs/releases" / f"v{version}.md"
    if not notes.is_file():
        errors.append("缺少 VERSION 对应版本的发布说明")
    else:
        notes_text = notes.read_text(encoding="utf-8-sig")
        if not notes_text.strip():
            errors.append("发布说明正文为空")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8-sig")
    if not re.search(r"^## " + re.escape(version) + r" - \d{4}-\d{2}-\d{2}$", changelog, re.M):
        errors.append("CHANGELOG.md has no dated entry for VERSION")
    payload = collect(root)
    current = json.loads((root / "bundle.json").read_text(encoding="utf-8-sig"))
    if current != make_manifest(root, version, payload):
        errors.append("bundle.json is stale; run scripts/package.py --manifest-only")
    skill = (root / "SKILL.md").read_text(encoding="utf-8-sig")
    frontmatter = skill.split("---", 2)[1] if skill.startswith("---") else ""
    if not re.search(r"^name: cy-charmyee$", frontmatter, re.M):
        errors.append("Skill name does not match the published directory")
    if not re.search(r"^description: .+", frontmatter, re.M):
        errors.append("Skill description is missing")
    for name, data in payload.items():
        path = root / name
        if path.suffix == ".py":
            try:
                ast.parse(data.decode("utf-8-sig"), filename=name)
            except SyntaxError as exc:
                errors.append(str(exc))
        if path.suffix == ".svg":
            ET.fromstring(data)
        if path.suffix.lower() in {".png", ".jpg", ".onnx"}:
            continue
        text = data.decode("utf-8-sig")
        if re.search(r"(?:gh[pousr]_[A-Za-z0-9]{25,}|sk-[A-Za-z0-9]{30,})", text):
            errors.append("Possible credential in " + name)
        if re.search(r"(?:[CEF]:[/\\]Users[/\\]admin|F:[/\\]codex work|E:[/\\]JT知识库)", text, re.I):
            errors.append("Machine-specific private path in " + name)
        if path.suffix != ".md":
            continue
        readable = re.sub(r"```.*?```", "", text, flags=re.S)
        links = re.findall(r"\[[^\]]*\]\(([^)\n]+)\)", readable)
        links += re.findall(r'(?:src|href)="([^"]+)"', readable)
        for link in links:
            link = link.strip("<> ").split("#")[0]
            if not link or urlsplit(link).scheme or link.startswith("//"):
                continue
            if (name, link) in HISTORICAL_EN_LINKS:
                continue
            target = (path.parent / unquote(link)).resolve()
            if not target.is_relative_to(root.resolve()) or not target.exists():
                errors.append(f"Broken local link in {name}: {link}")
    return errors


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    errors = check(root)
    print(json.dumps({"status": "failed" if errors else "passed", "errors": errors}, ensure_ascii=False))
    raise SystemExit(bool(errors))
