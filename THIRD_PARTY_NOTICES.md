# Third-party components / 第三方组件

The MIT license at the repository root applies to this project's own code and documentation. Dependencies retain their own licenses. The installation ZIP contains source, documentation, artwork and the Silero VAD model; Python, FFmpeg, Chromium and Python dependencies are installed separately.

根目录 MIT 许可适用于本项目自有代码与文档。依赖保留各自许可；安装包不内置 Python、FFmpeg、Chromium 或 Python 依赖的二进制文件。

| Component | Role | License / authoritative source |
|---|---|---|
| certifi | Mozilla root certificates for verified release downloads when Windows trust-store verification fails | [MPL-2.0](https://github.com/certifi/python-certifi/blob/master/LICENSE) |
| Playwright for Python | Headless Douyin page acquisition; a visible dedicated browser for explicitly requested login or verification | [Apache-2.0](https://github.com/microsoft/playwright-python/blob/main/LICENSE) |
| yt-dlp | Single-video extraction fallback | [Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE); see its [third-party licensing notes](https://github.com/yt-dlp/yt-dlp#license) for bundled components |
| greenlet | Playwright dependency | [MIT and PSF-derived portions](https://github.com/python-greenlet/greenlet/blob/master/LICENSE) |
| pyee | Playwright dependency | [MIT](https://github.com/jfhbrook/pyee/blob/main/LICENSE) |
| typing_extensions | Python typing support | [PSF-2.0](https://github.com/python/typing_extensions/blob/main/LICENSE) |
| Python | Local runtime | [PSF license](https://docs.python.org/3/license.html) |
| FFmpeg / ffprobe | Audio extraction, media checks and preparation of review frames | [LGPL/GPL depending on build](https://ffmpeg.org/legal.html); Windows installer uses the [Gyan.FFmpeg WinGet package](https://github.com/microsoft/winget-pkgs/tree/master/manifests/g/Gyan/FFmpeg) |
| Chromium | Playwright-managed browser | [Chromium and third-party notices](https://www.chromium.org/developers/how-tos/get-the-code/) included with its distribution |

Exact Python package versions are pinned in `scripts/runtime/requirements.lock.txt`.

## 识别后的疑点检查依赖

这些组件只在识别后使用。pypinyin 由安装器按锁定版本安装；jieba 只使用词典数据，按需下载，不安装其 Python 包，也不随源码 ZIP 或安装包分发词典。未加载额外的 `pypinyin-dict`。

| 组件 | 用途 | 固定版本、来源与许可 |
|---|---|---|
| pypinyin | 同音候选的拼音转换，仅忽略声调 | `0.55.0`；[官方项目](https://github.com/mozillazg/python-pinyin/tree/v0.55.0)、[发行包](https://pypi.org/project/pypinyin/0.55.0/)、[MIT 许可](https://github.com/mozillazg/python-pinyin/blob/v0.55.0/LICENSE.txt)，版权方为 mozillazg、闲耘。随包单字与词组数据分别来自 [pinyin-data](https://github.com/mozillazg/pinyin-data) 和 [phrase-pinyin-data](https://github.com/mozillazg/phrase-pinyin-data)，两者上游均声明 MIT。 |
| jieba 词典数据 | 判断通用词，并切分本视频上下文中的候选词 | `v0.42.1` 对应固定提交 `1e20c89b66f56c9301b0feed211733ffaa1bd72a`；[词典原文件](https://raw.githubusercontent.com/fxsjy/jieba/1e20c89b66f56c9301b0feed211733ffaa1bd72a/jieba/dict.txt)、[MIT 许可](https://github.com/fxsjy/jieba/blob/1e20c89b66f56c9301b0feed211733ffaa1bd72a/LICENSE)，版权方为 Sun Junyi。下载时同取上游 `LICENSE`；不使用其分词器或缓存。 |

词典 `jieba/dict.txt` 为 5,071,852 字节，SHA-256 为 `7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8`；许可文件为 1,075 字节，SHA-256 为 `18ba0984839f85853b29fadaf992f7dba8fd0ca0fbeae34de2b8735222dc7a37`。固定来源、GitHub Raw 与 jsDelivr 备用地址见 [assets/lexicons.json](assets/lexicons.json)，下载、校验与手动导入共用公开词库机制。

pypinyin 的词组表用于处理读音，不能兼任完整通用词表。jieba 词典覆盖更广但版本较早，缺词不等于错词；疑点必须同时满足候选和上下文约束，使用方式见[疑点清单](references/suspects.md)。

## 本地识别运行依赖

这些依赖由安装器按锁定版本单独安装，不将其二进制文件加入本项目安装 ZIP，也不适用本项目的 MIT 许可。

| 组件 | 用途 | 许可与来源 |
|---|---|---|
| sherpa-onnx、sherpa-onnx-core | Qwen3-ASR 与 Silero VAD 的本地 CPU 推理 | [Apache-2.0](https://github.com/k2-fsa/sherpa-onnx/blob/master/LICENSE)，[官方项目](https://github.com/k2-fsa/sherpa-onnx)；本步均锁定 2.0.1 |
| onnxruntime | 随 sherpa-onnx-core 分发的 ONNX 推理运行库，不另装 Python onnxruntime 包 | [MIT](https://github.com/microsoft/onnxruntime/blob/main/LICENSE)，[官方项目](https://github.com/microsoft/onnxruntime)；具体构建随锁定的 sherpa-onnx-core wheel |
| numpy | WAV 样本、能量计算与切段 | [BSD-3-Clause](https://github.com/numpy/numpy/blob/main/LICENSE.txt)，[官方项目](https://numpy.org/)；本步锁定 2.5.5，wheel 中附带组件另按其随包许可处理 |

## Source history / 来源历史

An earlier private, local-only workflow used two files from `a550752845-hue/douyin-video-transcript` at commit `692b064b50fe78f1194c6a243632a6b09e49d658`. That snapshot did not provide an explicit license. **Neither file is included in this public repository or its installation package.** The independently implemented adapter uses HTTP share pages and headless Playwright. The page loads its own visitor SDK at runtime, and the adapter sends an ordinary XHR from that page's context to retrieve post data; that SDK is not bundled by this project. The adapter contains no copy of a signature-generation algorithm, does not automatically solve CAPTCHA or use a third-party paid parsing service, and retains the separately installed yt-dlp fallback for individual videos. No upstream authorization is implied.

早期本地工作流使用过上述仓库中的两份文件，该快照未提供明确许可。公开版不包含这两份文件；下载适配器独立实现。无头页面在运行时加载网站自身的访客 SDK，适配器在页面上下文中发起普通 XHR 请求获取作品数据；本项目不打包该 SDK，不复制签名生成算法，不自动处理验证码，也不使用第三方付费解析服务。单条视频仍保留单独安装的 yt-dlp 回退；不声称已获得旧上游授权。

Doubao ASR is an external service governed by its provider's terms. Each user supplies their own account and API credentials. Douyin, DeepSeek, Codex, Claude, Gemini, Cursor and OpenCode names identify interoperability targets; this project is not an official product of those providers.

The hero illustration was generated for this project with OpenAI image generation. The workflow diagrams are project-authored SVGs. A configuration screenshot, when included, shows the project's own empty local dialog.

Personal-vault instruction adapters are independently implemented from the published [OpenAI AGENTS.md](https://developers.openai.com/codex/guides/agents-md/) and [Claude Code memory](https://code.claude.com/docs/en/memory) interfaces. No third-party implementation code is included. / 个人知识库宿主适配独立实现，参考官方入口约定，不包含第三方实现代码。

## 本地模型的来源与许可

### Qwen3-ASR-0.6B int8（按需下载，不随安装包分发）

- 上游模型：[Qwen/Qwen3-ASR-0.6B 官方模型卡](https://huggingface.co/Qwen/Qwen3-ASR-0.6B)，声明 Apache-2.0。
- ONNX 转换者：[zengshuishui/Qwen3-ASR-onnx](https://modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx)，模型卡声明 apache-2.0；本项目使用其中的 `model_0.6B` 推理文件及 `tokenizer` 文件。
- 打包与镜像维护者：csukuangfj2／k2-fsa，见 [Hugging Face 模型页](https://huggingface.co/csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25)与 [sherpa-onnx 官方模型 Release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)。hf-mirror 是同一 Hugging Face 路径的备用下载镜像。

下载包 `sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2` 本身没有 LICENSE 文件。这里如实分别记录上游、转换者和打包者，不把 sherpa-onnx 的代码许可或本项目的 MIT 许可当作转换包随附许可。文件大小、SHA-256、逐文件来源及整包来源固定在 [模型清单](assets/models.json)；不同来源必须通过相同哈希核验。

### Silero VAD（MIT，随安装包附带）

本项目随包分发 `assets/silero_vad.onnx`，643,854 B，SHA-256 为 `9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6`。文件取自 [sherpa-onnx 官方 Release](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx)，源头为 [snakers4/silero-vad](https://github.com/snakers4/silero-vad)。许可为 [MIT](https://github.com/snakers4/silero-vad/blob/master/LICENSE)，以下保留原版权行和许可全文。该许可仅对应 Silero VAD，不替代其他模型的许可。

```text
MIT License

Copyright (c) 2020-present Silero Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## THUOCL（清华开放中文词库，按需下载）

来源：[thunlp/THUOCL](https://github.com/thunlp/THUOCL/tree/a30ce79d895d01ab5132a5c74c29703ff7efb4cc)，固定提交 `a30ce79d895d01ab5132a5c74c29703ff7efb4cc`。本项目使用官方 `data/` 下的 11 类词表，原文件与 `LICENSE` 的大小、SHA-256、GitHub Raw 和 jsDelivr 地址见 [assets/lexicons.json](assets/lexicons.json)。这些数据按需下载到共用模型根目录，不进入源码或安装包。MIT 许可适用于 THUOCL 自身，不扩大到其他第三方组件；以下为该提交 [LICENSE](https://github.com/thunlp/THUOCL/blob/a30ce79d895d01ab5132a5c74c29703ff7efb4cc/LICENSE) 的全文。

```text
MIT License

Copyright (c) 2018 THUNLP

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
