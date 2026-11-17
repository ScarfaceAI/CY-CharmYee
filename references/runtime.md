# 运行命令与状态

旧任务、重复素材或旧稿修改先用只读 `resume`，普通任务按保存范围继续用 `run <task> --saved-scope`；离线换问题用 `reanalyze <task> --instruction ...`。详细命令与授权边界见[恢复与复用](continuation.md)。

默认使用随包 PowerShell 入口，不先打开浏览器识别。`<skill>` 取安装结果的 skill 路径，或当前 SKILL.md 的所在目录；`<task>` 取真实任务 ID。

```powershell
$dyRun = '<skill>\scripts\invoke.ps1'
& $dyRun doctor
```

该入口从本机 settings.json 选取 workspace 和它的 Python，调用本 Skill 相对路径的 brain.py；无需猜测旧电脑盘符。doctor 的 `execution_route` 应为 `bundled_local_engine`／`doubao_voice_api`，`web_transcription_fallback=false`；这确认程序路线，不代表云端已识别成功。环境缺失先运行 install.ps1 补齐，不转向网页识别。

## 首次介绍与配置状态

```powershell
& $dyRun welcome                  # 只读取中文介绍与本机状态
& $dyRun welcome --lang en        # 英文介绍
# Agent 已在聊天中展示完整介绍后，才登记本版本已展示：
& $dyRun welcome --acknowledge    # 英文可另加 --lang en
```

`welcome` 返回 `message`、9 组 `capabilities`、`readiness` 和 `onboarding`，不读取用户意图，也不调用语音识别。功能目录来自 `assets/capabilities.json`；Agent 按[首次介绍](onboarding.md)在聊天中说明全部组别，再结合用户当前目标继续。安装前入口不可用时先读指南介绍，安装后再核对状态。

`onboarding` 包含 `revision`、`due`、`guide_file`、`welcome_command`、相对工作目录的 `state_file` 和 `meaning`。`doctor`／`init` 也回传该状态；安装结果包含 `onboarding_reference`、`welcome_command`、`onboarding` 和 `next_action_for_agent`（某些提前失败阶段可能尚未取得本机状态）。仅在已于聊天实际展示介绍后执行 `--acknowledge`，它向工作目录 `.local/onboarding.json` 写入 `presented_revision`、时间和语言，表示介绍已展示，不代表理解、验收或授权；其他命令不会自动登记。

`readiness.existing_text` 表示可由 Agent 处理现有文字；`download_environment` 是素材获取依赖状态；`new_speech` 只反映本地媒体音轨工具与凭据是否具备试用条件，抖音媒体还需要获取环境。`knowledge_destination` 不代表写入授权，视觉核对以当前 Agent 工具为准；`cloud_access` 和 `douyin_login` 均未测试。凭据存在或本机环境就绪不代表真实云端识别、登录或任务成功。

## 媒体获取与识别

单条公开视频先尝试普通 HTTP 分享页，再在无登录态的无头页面调用网站自身加载的访客能力获取作品数据；必要时尝试本机已有专用浏览器状态，保留 yt-dlp 的单视频回退。1.3.0 本地候选曾用固定样本实测匿名获取、真实语音识别和画面校对，部分素材获取失败，且未做听音核对；详见[上一轮验收记录](../docs/verification/v1.3.0.md)，该记录不代表 1.3.1 的本轮结果。实际任务按获取和识别结果报告。

常规获取在后台运行，浏览器使用 `headless=True`，不弹窗抢焦点，不调用 Computer Use，不自动登录或处理验证码，也不使用第三方付费解析。浏览器进程可以复用，每条链接使用新的浏览器上下文。仅当可见阻挡容器明确要求登录或验证时才报告相应原因；标题、正文里的相关词或普通登录按钮不能证明被阻挡。只有用户明确要求登录、验证或界面排障时才运行 `login` 打开可见专用浏览器，登录和验证码由用户本人完成。

普通 `plan`／`run` 流程中，每个输入链接开始处理后，从发现到音轨验证的获取时限为 90 秒，最多同时运行 2 个后台获取进程；排队时间不计入时限。主页是一个输入时，主页发现及其多个作品的准备可能共用这 90 秒。付费豆包识别由父进程单独执行，不受获取时限中断；失败或结果未知的付费请求按下文规则处理。

需要博主全量分析或独立批量下载主页视频时，使用 [creator 任务](creator.md)：主页分页与逐作品获取分别执行，获取参数默认每项 300 秒、2 路并发；语音识别单独计时并在授权范围内执行。

批量链接任务中，某项音轨获取并验证完成后，可进入父进程的串行识别，同时继续获取其他素材；识别顺序可能与输入顺序不同。重复素材按 ID 去重，最终结果按原输入顺序编号，失败项保留位置。

语音识别默认由随包 asr.py 请求豆包语音 HTTP 接口；显式选择本地引擎时使用下面的 Qwen3-ASR。不会使用网页上传或网页字幕替代，也不自动录音；豆包付费重试遵守下文规则。

排查素材获取时可执行：

```powershell
& $dyRun inspect-source '<视频链接或本地媒体>' --anonymous-only
& $dyRun inspect-source '<视频链接或本地媒体>' --anonymous-only --download-audio
```

两种检查都不调用语音识别：不带 `--download-audio` 只检查元数据；加上该参数还会下载并验证音轨，但仍不代表云端识别成功。`--anonymous-only` 不使用已保存的账号或浏览器状态；去掉该参数才允许必要时尝试已有专用浏览器状态。依据输出及 `report_directory` 报告实际结果。此检查不用于证明作者主页分页完整。

## 本地识别模型

当前模型为 `qwen3-asr-0.6b-int8`，6 个推理文件合计 987,015,347 B，约 1 GB。Silero VAD 随安装包附带，不另行下载；运行时同样核对大小和 SHA-256。模型管理不改变默认引擎；需要本地识别时显式配置。

```powershell
& $dyRun models status
& $dyRun models download           # 后台下载，立即返回 JSON 状态
& $dyRun models download --wait    # 前台等待下载与校验完成
& $dyRun models cancel             # 只取消本模型的后台下载，保留部分文件
& $dyRun models import 'D:\下载\sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2'
& $dyRun models import 'D:\模型\sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25'
& $dyRun models remove             # 删除当前模型缓存，需要时可重新下载
```

所有子命令可加 `--model qwen3-asr-0.6b-int8`，省略时使用这个唯一模型。输出中的 `ready` 表示模型和 Silero VAD 均已校验；`files` 列出各文件的存在情况、大小、哈希核验与缓存使用情况；`progress` 包含已下载量、总量、当前文件、来源、错误、起止记录及进程号。`hint` 给出中文下一步说明。`doctor` 也报告本地模型状态，只读检查，不触发下载。

模型目录按以下顺序选择：

1. 环境变量 `CY_MODEL_DIR`，兼容旧前缀 `DYB_MODEL_DIR`，前者优先。
2. 共享状态目录中 `settings.json` 的 `model_dir` 字段。状态目录由 `CY_STATE_DIR`／`DYB_STATE_DIR` 或现有安装登记决定。
3. 默认 `%LOCALAPPDATA%\cy-charmyee\models`，不随旧品牌状态目录改变。

每个模型放在根目录内的同名子目录。临时改到其他磁盘可执行 `$env:CY_MODEL_DIR = 'D:\承意模型'`；长期使用可在共享设置中增加 `"model_dir": "D:\\承意模型"`，保留原有其他字段。换位置只改变后续查找位置，不自动搬走或删除旧缓存。核验结果记录文件大小、修改时间和哈希，大小与修改时间未变化时复用核验结果。

逐文件依次尝试 ModelScope、hf-mirror、Hugging Face；连接失败、超时、长时间无有效进展或哈希不匹配时换源，并留下原因。逐文件来源均失败后，才使用 [sherpa-onnx 官方 GitHub 整包](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2)兜底。下载只补缺失或损坏的文件，写入 `.part`，支持 HTTP Range 续传；服务器不支持 Range 时从头下载该文件。大小和 SHA-256 都通过后才原子改名为可用文件。开始前检查剩余空间；不足时说明还差多少字节。

后台下载以低优先级、无窗口方式运行。Windows 下会尝试脱离宿主作业对象；宿主不允许时，状态会说明后台可能随宿主结束，可改用 `--wait`。若进程已经退出而文件未完成，`status` 会提示再次运行 `download` 续传。`cancel` 核对进程号和创建时间后，只结束本模型登记的后台进程；前台下载用原终端的 `Ctrl+C` 中断。同一模型一次只允许一个下载或导入；进行中不能删除。

无法联网时，可以手动从上面的 GitHub 链接取得整包再 `import`，也可以导入已包含清单中 6 个文件的模型目录。整包先核对整包 SHA-256，只提取清单文件，拒绝绝对路径、`..`、链接和特殊文件；所有推理文件逐个核验后才放入缓存。导入保留来源文件，不执行包内内容。`remove` 只删除所选模型子目录中的缓存和进度，不删除随安装包附带的 Silero VAD；删除前应确认这个缓存已不需要。

## 本地识别引擎

当前默认仍是豆包。要给新任务启用本地识别，在工作目录的 `config.json` 合并以下字段，保留其他配置：

```json
{
  "asr_engine": "local",
  "asr_threads": 3,
  "asr_queue_timeout_seconds": 600,
  "asr_background_threshold_seconds": 120
}
```

`asr_engine` 只接受 `doubao` 或 `local`；省略时为 `doubao`。仍按 `plan` → `confirm` → `run` 执行。本地识别使用 CPU，不需要豆包凭据；音频不提交豆包。creator 的用量预览、授权和报价规则本步继续保留。

`asr_threads` 可以省略或设为 `null`：Windows 通过系统 API 读取物理核心数，默认取一半，至少 1、最多 4；读取失败时取逻辑核心数的四分之一，再应用同一上下限。显式配置必须是正整数，覆盖默认值。识别在无窗口、`BELOW_NORMAL`（低于正常）优先级的子进程中执行；同一进程按模型和线程数复用识别器，每段新建 stream。

本地引擎的 `run` 取得音轨后，用“尚未成功识别的音频秒数 × 本机速度系数”估算剩余识别时间。超过 `asr_background_threshold_seconds` 时，任务转为 `transcribing_in_background`，命令立即返回音频总时长、预计耗时、分片进度和中文 `tell_user`。阈值默认 120 秒，接受不小于 0 的有限秒数；等于阈值时仍在前台运行，调大阈值可让长录音留在前台。音轨提取仍须先完成；预计耗时不包含排队、获取素材和结果整理时间。豆包流程与 creator 的逐条处理入口不受这个阈值影响。

速度系数保存在模型根目录的 `asr-calibration.json`，按识别档案和线程数分别记录；没有实测数据时用 0.3。每个 WAV 分片成功识别后，子进程用“识别秒数 ÷ 分片音频秒数”更新系数，只计语音检测与解码处理，排队、模型文件校验和识别器创建（`create()`）的耗时不计入。分片记录中的 `model_load_seconds` 单独记录识别器创建耗时，静音未创建识别器时没有此字段；`recognition_seconds` 用于校准，`elapsed_seconds` 仍保留包含加载、排队及校验的总耗时。第一次直接采用实测值，之后按“旧值 × 0.8 ＋ 本次实测 × 0.2”滚动更新；中断的分片不参与校准。已有系数继续滚动更新，不自动清空历史样本。模型根目录需要可写，以便保存校准文件。

```powershell
& $dyRun run <任务>
& $dyRun status <任务>
& $dyRun cancel <任务>
& $dyRun run <任务>  # 取消或中断后续接
```

`status` 在识别期间返回已完成／总分片数、当前素材及分片序号（从 1 开始）、本次运行已用秒数、预计剩余秒数、后台进程是否存活和是否正在排队。分片指可续接的 WAV 分片，默认约 300 秒；其中的 VAD 短句不是独立续接点。预计剩余时间按本次启动时的系数计算，不包含未知的排队时间。只有任务仍停在 `transcribing_in_background` 或 `running`，且登记的本地识别进程已不存在或身份不匹配时，才返回 `interrupted`，提示再次 `run` 续接；这也包括前台进程被宿主结束的情况。后台正常收尾为 `incomplete`、`failed` 或 `waiting_model` 时，原样返回任务的状态、失败原因及 `tell_user`，不改报中断。识别成功后返回与 `show` 相同的最终结果摘要；通常是 `awaiting_review`，表示机器原稿已生成，仍待校对，并不冒充人工验收。

后台会执行同一条任务流程的剩余步骤，写出相同格式的 `result.json`、逐字稿和任务状态。后台运行中再次 `run` 只返回进度；任务级操作系统锁防止同一任务重复运行。`cancel` 只核对并结束本任务登记的后台进程，进程号和创建时间必须匹配，核对与结束使用同一个进程句柄；其持有的识别子进程也会随之结束。成功分片保留，任务记为 `cancelled`；再次 `run` 复用成功分片，按剩余时长重新分流。前台任务不能用 `cancel` 结束，需要在原终端中断。

后台主进程和识别子进程均为低优先级、无窗口。主进程尝试脱离宿主作业对象；宿主拒绝时，状态的 `background.job_breakaway` 为 `false`，`background_warning` 和 `tell_user` 如实说明后台可能随宿主退出。日志在任务目录的 `background.log`、`local-asr.log`，进程身份和进度分别在 `asr-run.json`、`asr-progress.json`。这些记录属于工作目录，不打包进源码或安装包。

同一模型根目录共用操作系统级文件锁，锁文件沿用 `.qwen3-asr-0.6b-int8.lock`，也与模型下载、导入和删除互斥。各工作区默认共用模型根目录，因此同一时间只有一个本地识别进程工作；配置 `CY_MODEL_DIR` 时，各会话应指向同一根目录以保持互斥。锁占用时输出中文排队提示；`asr_queue_timeout_seconds` 默认为 600 秒，可设为不小于 0 的有限秒数。超时只报错，不丢弃已完成分片；再运行即可继续。进程退出会由操作系统释放锁，不靠删除锁文件恢复。

本地 `run` 只要还有作品没有可用的缓存结果，就在准备音轨、估算耗时和转入后台之前检查模型。模型未就绪时，自动启动或续接 `models download` 后台下载，任务直接返回 `waiting_model`，包含中文 `tell_user`、`model_status` 和下载进度，不提取音轨、不启动后台识别；已有下载继续复用。所有作品都有可用缓存结果时，直接复用，不检查模型、不触发下载。模型就绪后再次 `run`，沿原任务继续。`doctor` 将 `sherpa_onnx` 实际导入是否成功列入依赖检查；模型有没有下载仍只作信息展示，不影响整体依赖状态。模型路径选择和导入方法见上一节。

新任务第一次进入识别流程时，把 `asr_engine`、`asr_profile` 固定在任务记录中，之后改工作区配置不会改动这项选择。本地档案以 `local-` 开头，后接模型文件哈希、VAD 文件哈希、VAD／切段／解码参数共同哈希的前 12 位。结果记录模型标识及文件哈希、sherpa-onnx 版本、线程数、参数和每段 `raw_text`；热词默认不传，预留每段 stream 的 `hotwords` 选项，实际值记录在结果里，不参与档案哈希。

没有识别档案记录的旧任务始终按豆包档案 `0426d7a84e24` 读取，保留已有 `-revision` 后缀，不因配置切到本地而重新识别。明确要求重新识别时，创建新任务并在 `confirm` 加 `--retranscribe`，沿现有规则另存版本。成功分片照常复用；本地失败或未完成的分片下次自动重跑，豆包仍要求核对后通过 `--retry-evidence` 明确重试。

输入沿用 `media.extract` 的 16 kHz、单声道、16 位 WAV。Silero VAD 先筛掉无语音部分，其 20 秒最长语音参数只是软限制；超过 15 秒的段会再次切分：优先选择前 15 秒内较长的低能量停顿，否则取 10～15 秒窗口能量最低的位置。拆分不丢样本、不重叠，每段最多 15 秒，时码按样本数计算。Qwen3-ASR 只有段级时间；普通 `export-srt`、`export-srt --max-chars 14` 和已登记校对稿的 `export-srt --reviewed` 均沿用现有导出流程，短行时码是段内分配，不代表模型给出了逐字时间。

## 版本检查与升级

```powershell
& $dyRun check-update          # 24 小时缓存；新任务先检查
& $dyRun check-update --force  # 用户明确要求立即检查时
& $dyRun update               # 只查询，不安装
& $dyRun update --apply       # 用户明确要求更新本 Skill 时
```

`check-update` 和每次 `kb context` 都会检查正式版状态，并返回当前版本、最新版本及 `should_notify`。套件在某一聊天中首次参与时，若有更高版本，只在该聊天提醒一次；同一聊天重复检查不重复提醒，新聊天会独立提醒。24 小时共享缓存只复用公开 Release 检查结果，不跨聊天抑制提醒。

优先传入真实宿主会话 ID：使用 brain 全局参数 `--session-id`，或本机已有的 `CY_SESSION_ID`、`CODEX_THREAD_ID`、`CODEX_SESSION_ID` 环境值。没有真实 ID 时不制造全局会话标识；Agent 在当前对话内对重复结果去重。命令返回 `should_notify=true` 只表示有可提醒版本，不证明用户已看到提醒。

`unavailable` 表示暂时不能联网检查，继续原任务；已有确认版本在刷新失败时会标为陈旧（`stale`，并可能带 `check_error`），仍可向用户说明这是缓存快照。设置 `CY_UPDATE_CHECK=0` 可以停用日常检查，显式升级仍会联网查询。只访问公开 GitHub Release 元数据，不发送文稿、Key 或登录状态。更新始终需要用户明确要求。

`update --apply` 重新查询最新正式版，下载官方 ZIP 及 SHA-256 文件，校验压缩包和内部完整清单，调用安装器更新当前 Skill 的真实目录。继续使用原本机设置和工作目录；旧程序留备份，Key、素材和任务数据不属于替换范围。安装失败且已生成有效旧程序备份时恢复旧程序，失败版本留在 `.trash`；依赖安装若中断，按实际错误修复后再试。查看 `update-result.json` 和返回的日志路径，不自行清空数据或强行删除更新锁。

`updated` 表示新版程序及本地入口验证通过，随后重读新版 Skill；`up_to_date` 表示无需更新。它们都不代表付费语音或云端权限已实测。源代码 Git checkout 使用维护发布流程，不能被自更新安装包覆盖。首次从 1.0.x 升级需先通过新版 ZIP 安装一次，1.1.0 起支持以上命令。

## 建立与执行任务

```powershell
& $dyRun plan '<链接、分享文案、本地媒体或 TXT 链接清单>'
# 已有文稿；正文不是链接清单：
& $dyRun plan-text '<UTF-8 文稿1.txt>' '<文稿2.txt>'
& $dyRun confirm <task> --purpose transcribe --evidence '<用户实际原话>'
# 分析或改稿时，把用户已给出的具体要求同时传入；不重新询问已明确的目标：
& $dyRun confirm <task> --purpose rewrite --evidence '<用户实际原话>' --instruction '<具体改稿要求>'
# 上面两条 confirm 按实际用途选择其一，分析时 purpose 使用 analyze。
& $dyRun run <task>
# 或仅使用匿名路径（二选一）：
# & $dyRun run <task> --anonymous-only
& $dyRun show <task>
```

`plan` 不获取媒体、不调用语音识别；入口会进行上述可关闭的版本检查。`run --anonymous-only` 不使用已保存的账号或浏览器状态。批量任务逐条隔离失败，保留成功结果和原始序号，交付时说明未成功的条目。明确指令与后续用途回复都可以作为真实 evidence，不需要机械等待第二次确认。`purpose` 为 `transcribe`、`analyze`、`rewrite`、`ingest`、`compile`；分析与改写还需 `--instruction '<实际要求>'`。多个交付目标可记录在 instruction 中，涉及入库时 purpose 选择 ingest 或 compile。

作者主页在 confirm 选择 `--latest N`、`--start YYYY-MM-DD --end YYYY-MM-DD` 或 `--all`。短链解析后才发现是主页时，只补范围。日期含起止日，置顶顺序不代表发布时间。

`--category reference`（默认外部参考）、`benchmark`（明确对标）、`own`（用户明确说明本人原稿）决定素材类别。

输入清单只接受逐行链接或媒体路径，不能嵌套清单。已有普通正文用 plan-text，多条独立文稿按顺序一起传入，统一批次交付；粘贴稿先原样保存 UTF-8 文件再导入。源文稿快照和结果不能被校对覆盖。

`plan-text` 仅接受 UTF-8 `.txt`／`.md`。已有 `.srt` 由 Agent 直接按[字幕处理说明](content.md#批量文字和已有字幕)读取、另存并检查时码，不通过 `plan-text` 假装已支持专用导入。

## Agent 接力

run 完成识别／导入后进入 `awaiting_review`。先读 task.results 的全部相关正文，找出数字、专名和内容冲突等疑点，按[当前会话的实际能力](review-capabilities.md)针对需要的范围用 `prepare-review` 准备材料并实际完成校对；用户明确只看已有材料时加 `--cached-only`，缺失则报告，不联网补取。另存每条正文文件，再依 results 顺序传递多个 `--file`。准备材料不会调用 ASR 或登记完成状态：

```powershell
& $dyRun register-review <task> --file '<校对1.txt>' --file '<校对2.txt>'
& $dyRun deliver <task> --kind review
# 改稿另存，后续指令作为证据：
& $dyRun register-spoken <task> --file '<口播1.txt>' --file '<口播2.txt>' --evidence '<实际改稿原话>'
& $dyRun deliver <task> --kind spoken
```

文件位于 workspace 内，内容只放正文。命令校验数量、顺序、源文件哈希与版本分离，不生成校对／改写内容；内容质量仍由 Agent 负责。

任务记录中的结果、正文、交付与归档路径相对 workspace 保存，命令输出附带 `workspace`，向用户报告时拼成完整路径。目录改名或移动后用 `init --register` 重新登记，用户输入、知识库目标和发布清单仍按实际路径核对。

其他参考中的 `$dyPython` 可取 doctor 的 `runtime_python`，`$dyBrain` 为 `<skill>\scripts\brain.py`。纯文稿处理尚未安装完整媒体环境时，可用已有 Python 执行 `brain.py --workspace '<实际工作目录>' plan-text ...`，无需安装浏览器或语音 Key。媒体任务默认仍使用上述已登记入口。

每批校对完成后，按[文稿协作与交付](content.md#校对检查记录)在任务目录保存对应版本的检查记录；压缩或明显删减时按同文的“压缩前后对照”使用 `scripts/compare_text.py` 计算并保存对照。常规处理直接完成，不增加确认轮次。

`deliver --kind raw` 用于明确需要原文的交付。合并稿使用 UTF-8 BOM、Windows 换行，每条一段；来源与日志不混进正文。新版文件以内容哈希区分，旧版保留。

分析稿单独保存 Markdown，再调用 `finish-analysis <task> --file '<分析稿.md>'`。用户已经授权分析而任务 purpose 兼含入库时，在任务目录留分析稿并继续对应入库阶段，不伪造 finish-analysis 的状态。

| 状态 | 接下来做什么 |
|---|---|
| needs_intent | 目标不清时问；已清楚则记录实际指令 |
| confirmed | 执行 run |
| awaiting_review | Agent 完成并登记校对 |
| ready_to_deliver | 生成并检查交付文件 |
| awaiting_analysis | 完成分析稿并登记 |
| awaiting_edit | 依当前协作阶段完成样稿或整批改稿 |
| ready_for_ingest / awaiting_compile | 按知识库参考完成成组归档／编译／实际发布 |
| incomplete / failed | 保留结果、说明失败；需要重试时取得实际重试指令 |
| completed / published | 核对实际交付或发布回执，说明未验证部分 |

## 复用与重试

续跑先在本地检查成功缓存，符合媒体内容／ID、识别参数和版本时直接复用，再为未就绪素材联网；已成功分片也会复用。用户明确重新识别时，confirm 加 `--retranscribe`，另存版本。

失败或结果未知的付费请求不自动重提；核对后用户明确要求重试，用 `run <task> --retry-evidence '<用户实际原话>'`。成功分片仍复用。

SRT 用 `export-srt <task>` 从已存时间生成机器原文字幕，写在识别结果旁，不重新调用识别；原始文本没有时间信息时不能编造 SRT。用途改变可以建立新任务并复用成功缓存，用户真实后续指令已经足以记录用途，不重复问一遍。

```powershell
& $dyRun export-srt <task> --reviewed                                    # 校对版字幕，沿用识别分句时间
& $dyRun export-srt <task> --reviewed --max-chars 14 --strip-end-punct   # 竖屏短视频：短行、去首尾逗号句号
```

校对登记后，`--reviewed` 把已登记的校对稿按字对齐回识别分句，人名、数字等更正进入字幕，时间轴沿用原分句。`--max-chars` 是每条字幕的字数上限（标点不计，4–80），超长句按标点和字数拆成多条，拆分点的时间按字数比例估算；`--strip-end-punct` 去掉每条首尾的逗号、句号等，保留问号和感叹号。这两个参数也可单独用于机器原文。新字幕按内容哈希另存到 `outputs/deliveries/<task>/`，不覆盖旧文件，也不改变任务状态。

只导出 `valid` 状态的校对稿；未登记、登记后又改过、文件缺失或依据过期的条目列在 `skipped`，不用机器原文顶替，按提示重新登记后再导出。校对稿与识别原文能对上的字不足 60% 时（多半是改写稿或登记错了条目）拒绝沿用时间轴；校对稿或识别原文只有标点、没有文字时记为 `unalignable`。被跳过的条目不影响同批其他条目导出。拆行优先落在标点、引号书名号、语气词之后或常见起词字之前；附近没有这类断点时才按字数拆。英文单词和数字不会被拆开，100,000 这类千分位逗号也不当作断句标点。交付前逐条看 `items[].check_cues`：`mostly_corrected` 表示这条大多是校对改过的字，`fast_reading` 表示读速超过每秒 12 字，二者都要抽查时间是否对齐；`split_by_length` 表示这条与下一条之间按字数硬拆，要看是否拆开了词语；确实拆开时向用户点明是第几条，方便在剪辑软件里手动调整。`dropped_segments` 是校对后已没有文字的原分句。向用户说明拆分时间属于估算，剪映等软件中的显示效果以实际导入为准。

普通 `dy-...` 与博主 `creator-...` 是不同任务：普通 `export-srt`、`archive`、`publish` 等命令不能直接接 `creator-...`。组合处理时检查实际可用的源转写及目标库规则；没有直接入口就由 Agent 按请求准备和核验独立文件，不伪造调用结果。具体见[博主任务组合说明](creator.md#与普通文稿任务组合)。
