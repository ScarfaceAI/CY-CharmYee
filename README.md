<div align="center">
  <img src="assets/hero.png" alt="承意 · CY-CharmYee：承你所想，会你所意" width="100%">
  <h1>承意 · CY-CharmYee</h1>
  <p>承你所想，会你所意。</p>
  <p>在你电脑上运行的个人知识与决策伙伴。</p>
</div>

[![CI](https://github.com/ScarfaceAI/CY-CharmYee/actions/workflows/check.yml/badge.svg)](https://github.com/ScarfaceAI/CY-CharmYee/actions/workflows/check.yml)
[![License](https://img.shields.io/github/license/ScarfaceAI/CY-CharmYee)](https://github.com/ScarfaceAI/CY-CharmYee/blob/main/LICENSE)
[![Release](https://img.shields.io/github/v/release/ScarfaceAI/CY-CharmYee)](https://github.com/ScarfaceAI/CY-CharmYee/releases/latest)

在你电脑上运行的个人知识与决策伙伴，把你看到、听到、经历的变成随时能查的资料，做决定时调出相关依据，事后陪你复盘。

资料可以来自链接、音视频、已有文稿和日常对话；视频文稿只是资料入口之一。你可以建立或接入知识库，搜索转写稿、校对稿和知识库索引中的页面（含决策记录），记录重要决定，并在复盘时回看预期与实际。已登记的外部个人画像只读引用，新了解到的情况可记入承意自己的知识库并标注来源。

既有媒体流程和版本记录仍可查：[校对版字幕](docs/releases/v1.8.0.md)、[生活、工作、学习知识库](docs/releases/v1.7.0.md)、[品牌迁移](references/brand-migration.md)及[恢复与复用](references/continuation.md)。

承意共用一套安装、配置、工作目录和更新。平时直接说目标即可，也可指定以下入口：

| 名称 | Skill ID | 用途 |
|---|---|---|
| **CY-CharmYee** | `cy-charmyee` | 组合需求、配置、迁移和维护。 |
| **CY-vault** | `cy-vault` | 生活、工作、学习知识库、全文搜索、决策记录与复盘，以及已开启范围内的日常资料维护。 |
| **CY-transcribe** | `cy-transcribe` | 音视频转写、校对、字幕和导出。 |
| **CY-analyze** | `cy-analyze` | 内容与博主分析。 |
| **CY-download** | `cy-download` | 主页视频与发布文案下载。 |
| **CY-write** | `cy-write` | 口播改写、润色与压缩。 |
| **CY-knowledge** | `cy-knowledge` | 材料归档与知识主题编译。 |

显示名称使用 `CY-xxxx`，宿主读取的 Skill ID、目录和调用名称使用小写 `cy-xxxx`。子 Skill 无需分别配置，具体能否自动发现取决于宿主 Agent。详见[套件路由](references/suite.md)。

![从素材到交付的流程](assets/workflow.svg)

## 能做什么

### 资料入口

抖音链接、分享文案、作者主页、本地音视频和已有文稿都能成为资料入口。可转写、校对、分析或改写；机器原稿和校对稿分开保留。博主分析、批量下载和字幕处理等媒体能力见[博主任务指南](references/creator.md)及上方版本说明。

### 知识库

建立或接入知识库后，可在已开启的范围内查找资料、积累明确的新事实和可复用成果、整理主题；新建库不会自动扫描历史任务。`search <词>` 可搜索转写稿、校对稿和知识库索引中的页面（含决策记录），按相关程度返回出处与片段。个人资料若登记为外部画像，只读引用；新增情况记入承意自己的知识库并标注来源。详见[个人知识库指南](references/personal-vault.md)和[全文搜索说明](references/search.md)。

### 决策

每个决定可保存为知识库对应方向下的一份记录，写下问题、选项、选择、理由、预期、复盘日期和状态。到了复盘日期，下次你在对话里用承意时，Agent 会在开头提醒一次；承意不在后台运行，没开对话时不会提醒。复盘追加“预期和实际差在哪”“下次保持什么”“改进什么”。做新决定时可以先调出相关旧决定和资料；还没有知识库时，会引导你建立一个，空库也可以。详见[决策记录说明](references/decisions.md)。

安装后可以直接跟 Agent 说：

> 把这个抖音链接转成逐字稿，校对人名和数字，只交稿，不入库：［链接］

> 分析这个博主可访问的全部作品，按时间讲清选题和内容怎么变化：［主页链接］

> 把这条视频转成字幕，校对人名和数字后导出，每行不超过 14 个字、去掉句尾标点：［链接］

> 帮我建立个人知识库，从工作方向开始；把这份原文和校对稿存下来：［素材］

> 搜一下我以前关于“换岗”的转写稿、校对稿、知识页和决定，列出出处和片段。

> 记下这个决定：问题是……选项是……我选……理由是……我预计……在……复盘。

> 复盘上次换岗的决定，比较预期与实际，再记录下次保持什么和要改进什么。

> 我要决定是否换岗，先找出相关旧决定和知识库资料，再跟我一起梳理选项。

首次使用时，Agent 会介绍功能、示例、产出和语音配置需求；无需记命令。只发链接而没说用途时，它会问一个关键问题。分析会先在聊天中给结论、例子和范围，转写或改稿会先给正文或标明节选，再附完整文件。详见[首次介绍](references/onboarding.md)和[套件路由](references/suite.md)。

## 安装

**需要 Windows、可联网的环境，以及一个有本地文件和终端能力的 Agent。** 不需要提前安装 Python。当前默认用你自己的豆包语音服务识别音频；只有显式把 `asr_engine` 配置为 `local` 才用本地识别，[本地识别配置方法见此](references/local-asr.md#本地识别引擎)。豆包开通、计费和账户规则以服务页面为准。

### 方式一：交给 Agent

把下面这段发给 Agent。它可以从[官方 Release](https://github.com/ScarfaceAI/CY-CharmYee/releases/latest)获取承意安装包，也可以使用你提供的源码目录：

```text
请在我的 Windows 电脑安装并启用承意 CY-CharmYee（Skill ID：cy-charmyee）及其子 Skill。从 https://github.com/ScarfaceAI/CY-CharmYee/releases/latest 获取承意安装 ZIP 和对应 SHA-256，或使用我提供的源码根目录；核对 bundle.json 的 name 为 cy-charmyee。阅读 INSTALL.md，先在聊天中介绍功能与语音配置需求，再自动完成校验、安装和依赖配置，并在当前对话加载安装后的 SKILL.md。沿用已有工作区，不另建一套任务或凭据；旧 dyb 版本首次迁移请运行新安装器。豆包语音 Key 由我在本机窗口填写，不要让我把 Key 发到聊天里。抖音登录不是安装前置；只有我明确要求登录、验证或界面排障时才打开可见的抖音浏览器。
```

### 方式二：自己安装

1. 从[官方 Release](https://github.com/ScarfaceAI/CY-CharmYee/releases/latest)下载 `cy-charmyee-windows-v<版本>.zip` 和对应校验文件，核验 SHA-256 后解压；也可使用仓库源码根目录。1.6.0 起的包内根目录和产品 ID 均为 `cy-charmyee`，历史 `dyb-*` 包保留原名。
2. 双击 `install.cmd`。安装器会尝试自动准备 Python、运行依赖、FFmpeg 和浏览器环境。
3. 让 Agent 读取安装后的 `SKILL.md`，然后直接发送素材和要求。首次使用时它会介绍功能并检查本机状态。
4. 使用默认豆包识别时再配置自己的豆包语音 API Key；默认不弹配置窗口。显式配置[本地识别](references/local-asr.md#本地识别引擎)后，不需要豆包 Key。处理已有文稿、个人知识库或独立下载主页视频也不需要语音 Key；知识库在你选择开启时配置。

安装参数、默认目录和状态说明见[安装指南](INSTALL.md)。

### 更新已有安装

**从 `dyb-douyinbrain` 切换到承意 1.6.0，需要运行一次新安装器。** 下载并校验承意安装包后，让 Agent 使用其中的 `scripts/install.ps1`，沿用原 Windows 用户及自定义安装参数。安装器会查找已有登记；找到单一工作区时自动复用，存在多个时再选择。旧版更新器固定校验旧包名与产品标识，即使是已有更新功能的 1.1.0–1.5.0，也不能直接通过旧 `update --apply` 跨品牌升级。完成本地 `doctor` 与功能检查后，重新读取安装后的 `SKILL.md`，或开启新会话。

旧名称 `dyb-douyinbrain`、`dyb-*` 和更早的 `douyin-brain` 用作兼容入口。迁移保留已登记的工作目录、Key 和任务，并备份被替换的程序与配置；发现多个不同工作目录时列出候选项，不会自动合并。详见[安装与迁移说明](INSTALL.md)。

完成迁移后，新任务会检查最新正式版，有新版时提醒，不会擅自安装。需要更新时可以说“使用承意更新自己”，或在支持的宿主中调用 `$cy-charmyee 更新`。Agent 会在你明确要求后验证官方包并更新。检查暂时无法联网时不会阻断原任务；下载素材和调用云语音仍需要网络。详见[升级步骤](docs/RELEASING.md#已有用户如何升级)。

## 配置豆包语音

![豆包语音配置流程](assets/setup-flow.svg)

1. 注册或登录[火山引擎](https://signin.volcengine.com/auth/signup)，进入[语音服务开通页面](https://console.volcengine.com/speech/new/setting/activate?projectName=default)。
2. 在选定项目中开通「录音文件识别极速版」，查看实际额度和计费条件。
3. 在**同一个项目**的[语音 API Key 管理页面](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default)创建或复制 Key。
4. 双击 `配置豆包.cmd` 打开本机配置窗口并粘贴 Key；不用把 Key 发给 Agent。
5. 选择一小段本地录音完成真实转写，确认服务可用。真实识别可能消耗额度或产生费用。

页面链接默认进入 `default` 项目；如果服务在其他项目，先切换到实际项目。语音 Key 不是火山方舟聊天模型的 Key。更详细的步骤、旧版凭据和排障见[豆包语音配置教程](references/doubao-setup.md)。

配置窗口与本机教程的实际界面（未填写任何凭据）。点击配置窗口中的「手把手教程」可直接打开本机教程窗口，不依赖默认浏览器；安装目录还提供离线 HTML 版本。

<img src="assets/configuration-window.png" alt="本机 API Key 配置窗口" width="720">

<details><summary>查看本机配置教程窗口</summary>

![本机教程窗口](assets/configuration-guide.png)

</details>

## Agent 兼容

**一级：Claude Code、Codex。** 发布前在这两个 Agent 中做真实对话验收；本版本的真实会话验收尚未做。

**安装适配，社区支持：DeepSeek Harness、Gemini CLI、Cursor、OpenCode。** 能安装并共用同一套核心，不保证逐一验证。

Skill 使用通用 `SKILL.md` 格式，但自动发现方式取决于各 Agent 的配置和版本。以下是安装器的 `-Agent` 选择：

| Agent 或方式 | 安装选择 | 说明 |
|---|---|---|
| 自动选择 | `auto` | 默认共享 `.agents/skills`；检测到已有 Claude 配置时尝试桥接，保留同名独立安装。 |
| 共享 Skill 目录 | `shared` | 适合读取共享目录的 Agent。 |
| DeepSeek Harness | `deepseek` | 提供安装适配。 |
| Codex | `codex` | 一级 Agent。 |
| Claude Code | `claude` | 一级 Agent。 |
| Gemini CLI | `gemini` | 提供安装适配。 |
| Cursor | `cursor` | 提供安装适配。 |
| OpenCode | `opencode` | 提供安装适配。 |
| 全部或自定义 | `all`／`custom` | 使用共享目录并尝试 Claude 桥接，或通过 `-SkillsRoot` 指定目录。 |

有本地文件和终端能力的其他兼容 Agent，可读取 `SKILL.md` 并运行随包入口。使用方法和 `-Agent` 示例见 [INSTALL.md](INSTALL.md)。

## 数据与限制

资料保存在你电脑上的工作目录和知识库中，承意本身不上传保存或检索的知识资料。当前默认识别会把音频发送给豆包；只有显式将 `asr_engine` 配置为 `local` 才由本机模型识别，本地识别时音频不出电脑。你和 Agent 的对话，包括它读到的稿子和资料，按 Agent 服务商的规则处理；详见[数据、网络与使用限制](docs/limits.md)。

## 开源与贡献

承意自有代码由 [ScarfaceAI](https://github.com/ScarfaceAI) 以 [MIT License](LICENSE) 发布。安装时使用的第三方组件遵循各自许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；来源与历史见 [references/sources.md](references/sources.md)。欢迎阅读[贡献指南](CONTRIBUTING.md)、[维护与发布指南](docs/RELEASING.md)和[安全说明](SECURITY.md)。
