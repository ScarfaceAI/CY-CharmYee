# 版本检查与升级细节

用户要求检查、升级或排查更新时读本页。命令中的 `$dyRun` 取当前 Skill 的 `scripts/invoke.ps1`。

## 版本检查与升级

```powershell
& $dyRun check-update          # 手动检查；使用 24 小时缓存
& $dyRun check-update --force  # 用户明确要求立即检查时
& $dyRun update               # 只查询，不安装
& $dyRun update --apply       # 用户明确要求更新本 Skill 时
```

`check-update` 和每次 `kb context` 都会检查正式版状态，并返回当前版本、最新版本及 `should_notify`。套件在某一聊天中首次参与时，若有更高版本，只在该聊天提醒一次；同一聊天重复检查不重复提醒，新聊天会独立提醒。24 小时共享缓存只复用公开 Release 检查结果，不跨聊天抑制提醒。

优先传入真实宿主会话 ID：使用 brain 全局参数 `--session-id`，或本机已有的 `CY_SESSION_ID`、`CODEX_THREAD_ID`、`CODEX_SESSION_ID` 环境值。没有真实 ID 时不制造全局会话标识；Agent 在当前对话内对重复结果去重。命令返回 `should_notify=true` 只表示有可提醒版本，不证明用户已看到提醒。

`unavailable` 表示暂时不能联网检查，继续原任务；已有确认版本在刷新失败时会标为陈旧（`stale`，并可能带 `check_error`），仍可向用户说明这是缓存快照。设置 `CY_UPDATE_CHECK=0` 可以停用日常检查，显式升级仍会联网查询。只访问公开 GitHub Release 元数据，不发送文稿、Key 或登录状态。更新始终需要用户明确要求。

`update --apply` 重新查询最新正式版，下载官方 ZIP 及 SHA-256 文件，校验压缩包和内部完整清单，调用安装器更新当前 Skill 的真实目录。继续使用原本机设置和工作目录；旧程序留备份，Key、素材和任务数据不属于替换范围。安装失败且已生成有效旧程序备份时恢复旧程序，失败版本留在 `.trash`；依赖安装若中断，按实际错误修复后再试。查看 `update-result.json` 和返回的日志路径，不自行清空数据或强行删除更新锁。

`updated` 表示新版程序及本地入口验证通过，随后重读新版 Skill；`up_to_date` 表示无需更新。它们都不代表付费语音或云端权限已实测。源代码 Git checkout 使用维护发布流程，不能被自更新安装包覆盖。首次从 1.0.x 升级需先通过新版 ZIP 安装一次，1.1.0 起支持以上命令。
