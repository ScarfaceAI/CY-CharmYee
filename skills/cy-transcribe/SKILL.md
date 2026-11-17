---
name: cy-transcribe
description: Transcribe and proofread Douyin videos or local audio/video, handle related subtitles, and export transcripts. Explicit invocation also supports existing text and subtitles.
license: MIT
---

# CY-transcribe · 转写与校对

提取、校对抖音或本地音视频文字，处理相关字幕与导出；显式调用也可处理已有文稿和字幕。

先运行 `& '<当前子 Skill>/scripts/invoke.ps1' guide`。成功后读取 JSON 的 `references.suite`；错误或非零退出时报告并停止，不猜共享路径。后续命令用同一入口透传。

- 单条链接或音视频：依次读 `references.runtime`、`references.content`，再读共享目录的 `references/review-capabilities.md`，执行 `quick` 和默认校对、登记、交付。豆包仍是默认引擎；保留机器原文，实际看图／听音与未解疑点按文稿指南记录。无需再读总入口。
- 已有文字不调用识别；已有 SRT 另存并保留未要求修改的结构，读共享目录的 `references/content-writing.md`。识别字幕导出读 `references/tasks.md` 的“复用与重试”；校对版用 `export-srt --reviewed`，短行可加 `--max-chars`／`--strip-end-punct`。
- 聊天先交正文，再附文件。不因转写自动分析、改写或入库；同时提出的其他目标按共用规则组合。
