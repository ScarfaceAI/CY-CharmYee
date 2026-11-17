# 完整任务流程、接力与重试

多个输入、主页范围、纯文稿、分析改写、字幕导出或付费重试时读本页。单条默认校对只需[核心流程](runtime.md)和[文稿交付](content.md)。命令中的 `$dyRun` 取当前 Skill 的 `scripts/invoke.ps1`。

## 建立与执行任务

只转一条作品或一个文件，用 `quick` 串起现有的 `plan`、`confirm`（只转写）、`run`；UTF-8 TXT／Markdown 文稿走 `plan-text`，原样留底：

```powershell
& $dyRun quick '<作品链接、分享文案或本地文件>'
& $dyRun quick '<本地录音.wav>' --evidence '<用户原话>'
```

省略 `--evidence` 时记录默认用途说明。多个输入、主页、合集或需要选范围的链接返回 `needs_scope`、`next_command` 和中文说明，改用下方完整流程。成功时返回任务编号、状态、正文路径、疑点数量和清单路径；缓存沿原流程核对并复用，`reused=true` 时明确告知是旧结果。疑点未生成时 `missing` 标明缺口，数量为 0 不代表没有疑点。任务仍可用原命令续做、校对、导出。

长音频返回后台状态，并给出带任务编号的 `status`／`cancel`；等待模型时给出 `models status`、`models cancel` 和原任务的 `run`；获取失败时保留任务记录并给出下一步。

### 统一的 tell_user

JSON 统一出口保证 `tell_user` 始终存在且是字符串；没有要转告的话时为空，Agent 对不为空的内容原样转告。错误仍保留 `error`，同时给出中文解释；只有参数解析错误提示查看 `--help`。命令自己的话每次照常返回，再拼接三类全局提醒：更新提示、后台模型下载进度、获取失败的退路“在抖音 App 里保存到本地，再把文件拖给我。”。按完整句去重，每类只加简短说明，不截断命令自己的话。

更新检查沿用原缓存及会话机制，在 `quick`、`plan`、`plan-text`、`creator plan` 和 `kb context` 的 JSON 中提醒，不再写 stderr。更新按版本对、下载按模型、获取退路按固定话术在同一会话最多提醒一次；命令自己的疑点和等待提示不受影响。会话沿用 `--session-id`／`CY_SESSION_ID`／Codex 线程编号；没有真实会话编号或无法保存去重记录时，程序不能保证跨命令去重；仍按入口要求原样转告 `tell_user`，不虚构会话编号。全局提醒只加入输出，不写入任务话术，避免续做时重复提醒。

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

`plan-text` 仅接受 UTF-8 `.txt`／`.md`。已有 `.srt` 由 Agent 直接按[字幕处理说明](content-writing.md#批量文字和已有字幕)读取、另存并检查时码，不通过 `plan-text` 假装已支持专用导入。

## Agent 接力

run 完成识别／导入后进入 `awaiting_review`。本地新结果自动生成[疑点清单](suspects.md)，`run`、`show` 返回 `results[].suspects` 的文件路径与各类数量，任务级 `suspects` 汇总计数；旧结果可用 `suspects <task>` 补算。先读清单和相关正文，按[三类校对边界](content.md#校对边界)及[当前会话的实际能力](review-capabilities.md)，针对需要的范围用 `prepare-review` 准备材料并实际完成校对；用户明确只看已有材料时加 `--cached-only`，缺失则报告，不联网补取。另存每条正文文件，再依 results 顺序传递多个 `--file`。准备材料不会调用 ASR 或登记完成状态：

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

每批校对完成后，按[文稿协作与交付](content.md#校对检查记录)在任务目录保存对应版本的检查记录；压缩或明显删减时按[文稿改写的“压缩前后对照”](content-writing.md#压缩前后对照)使用 `scripts/compare_text.py` 计算并保存对照。常规处理直接完成，不增加确认轮次。

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
