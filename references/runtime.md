# 运行命令与状态

单条抖音作品或本地音视频用本页。`<skill>` 取当前 Skill 目录，子 Skill 用自己的透传入口；`<task>` 取返回的真实任务 ID。

## 单条转写

```powershell
$dyRun = '<skill>\scripts\invoke.ps1'
& $dyRun quick '<作品链接、分享文案或本地音视频>' --evidence '<用户原话>'
```

省略 `--evidence` 时记录默认用途。`quick` 复用有效缓存，返回 `id`、`status`、正文和疑点清单路径。`awaiting_review` 只是机器稿已生成，继续按 [content.md](content.md) 校对与交付。

默认引擎是豆包，不打开网页上传识别。获取在后台运行；只有用户明确要求登录、验证或界面排障才开可见浏览器，登录和验证码由用户完成。配置缺失读 [setup.md](setup.md)／[doubao-setup.md](doubao-setup.md)。

多个输入、主页、合集或需选范围时返回 `needs_scope` 与 `next_command`，改走[完整流程](tasks.md#建立与执行任务)。已有文字直接处理；批量或持久化读[文稿入口](tasks.md#建立与执行任务)。旧任务、重复素材或旧稿修改先按 [continuation.md](continuation.md) 用只读 `resume`。

## tell_user

每次读取 JSON，`tell_user` 不为空就原样转告。统一出口汇集更新、模型下载进度、获取失败退路及命令自身说明；无须另写同类提醒。错误仍看 `error`，不能把提示当作成功回执。全局提醒的会话去重和无会话编号时的限制见[消息机制](tasks.md#统一的-tell_user)；不虚构会话 ID。

## 后台识别与续看

```powershell
& $dyRun status <task>
& $dyRun show <task>
& $dyRun run <task>     # 模型就绪、取消或中断后续接
& $dyRun cancel <task>  # 用户要求取消本任务后台识别时
```

本地引擎预计识别超过默认 120 秒时转后台；豆包和 creator 不使用该阈值。`status` 返回分片进度、当前素材、用时、预计剩余时间和进程状态；估时不含未知排队、获取和整理。

| 返回状态 | 接着做 |
|---|---|
| `transcribing_in_background`／`running` | 转告返回进度，用 `status` 续看；重复 `run` 只返回进度，不另开任务 |
| `waiting_model` | 按返回命令查 `models status`；模型就绪后 `run` 原任务；取消下载用 `models cancel` |
| `interrupted`／`cancelled` | 保留成功分片，原授权内 `run` 续接；用户主动取消后等其续做要求 |
| `awaiting_review` | 读正文与疑点清单，完成校对、检查记录、登记和交付 |
| `incomplete`／`failed` | 查看 `failures`、`error` 和 `show`；交付已有结果并说明缺口 |

`cancel` 保留成功分片；前台任务需在原终端中断。后台退出风险见 `background_warning` 和 `tell_user`。任务目录的日志为 `background.log`、`local-asr.log`，身份与进度为 `asr-run.json`、`asr-progress.json`。

## 失败与产物

失败先查实际状态，保留现场；不删除文件、任务、锁或凭据来“修复”。失败或结果未知的付费请求，先核对再取得用户明确重试指令，按[重试流程](tasks.md#复用与重试)执行。本地未完成分片可沿原授权续做。

命令返回 `workspace`，记录内相对路径均相对此目录；交付时拼成完整路径。`show` 可读完整结果列表；机器稿、结构化结果和疑点清单在结果目录，校对检查记在 `runs/<task>/checks/校对检查-vN.md`，交付用 `deliver` 返回的实际路径。先读结果再报告，不把 `completed` 当作内容验收。

## 版本检查与升级

用户明确要求升级时执行 `& $dyRun update --apply`；按结果报告，成功后重读返回的新版入口。手动检查、失败恢复、缓存与旧版兼容细节读 [updates.md](updates.md)。

## 按需参考

- 入口异常、获取排障或自检：读 [acquisition.md](acquisition.md)。
- 首次介绍及本机准备状态：读 [onboarding.md](onboarding.md#首次介绍与配置状态)。
- 模型管理、本地识别参数、热词和实现细节：读 [local-asr.md](local-asr.md)。
- 数字／间距配置或另存展示版本：读 [display.md](display.md)。
- 多输入、分析改写接力、字幕、付费重试：读 [tasks.md](tasks.md)。
