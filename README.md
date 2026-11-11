# 微信直连 / wechat-direct

面向 Windows x64 的本机微信 MCP 插件，支持 Codex、Kimi Code、Gemini CLI、Claude Desktop、Claude Code，以及通过 OpenCode 或 Claude Code 使用 DeepSeek。通过本机数据库读取联系人、会话和消息，通过原生驱动按授权发送文字、表情、普通图片和文件，并支持本地定时任务。保留当前个人账号和聊天记录。

当前开发版 **2.5.0-dev.4**，适配微信 **4.2.0.52**。项目采用 [MIT 许可证](LICENSE)，第三方组件保留各自许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。这是独立项目，与腾讯或微信官方没有隶属关系。

## 当前状态

- 25 个 MCP 工具，支持普通图片、文件、定时任务、显式账号绑定、人工核对后的状态关闭、执行器控制与缓存管理。
- 109 个内置表情编码；单个图片/动图只从当前账号添加目录同步，并核对目录成员和原始摘要。
- 发送使用稳定 `operationKey` 防止重复提交。`unknown` / `submitted` 结果需查询原操作；`confirmed` 表示匹配到新的服务器记录，不代表已读。
- 微信核心文件摘要和账号绑定不匹配时拒绝操作。每个微信进程首次文字、表情、图片或文件提交前，分别执行不发送的对象准备检查。
- 微信 4.2.0.52 的核心摘要、文字及媒体原生布局和 109 个内置表情已核对，原绑定账号的读取通过。本机 Windows 智能应用控制拦截了未签名驱动，发送前自检及真实发送待验收。实机检查记录见 [验证范围](TEST_STATUS.md)。
- 数据库快照只应用校验通过的已提交 WAL；轮询游标记录各分片位置，避免同序号消息漏读或卡页。未变化分片复用表索引，减少重复解密无关数据库。

## 安装

使用适配当前微信版本的完整 Windows 安装包，或按下文从源码构建。运行环境需要 Python 3.12 x64 和 Node.js 22 或更高版本。已发布的安装包可在 [Releases](https://github.com/huaxueyu1105/wechat-direct/releases) 查找。

- **Codex** 用户解压后运行 `install.cmd`，在新对话选择“微信直连”。首次安装前正常登录微信，已有绑定可离线更新。安装器优先使用 Codex 提供的运行时。
- **其他客户端** 在解压目录运行 `node .\mcp-config.mjs <客户端>` 生成配置。默认仅查询，明确需要发送等操作时加 `--full-access`。 [各客户端配置方法](docs/clients.md)

## 从源码构建

环境：Windows x64、Python 3.12 x64、Node.js >= 22，以及 Zig 0.17.0。仅静态读取本机 `Weixin.dll` 并编译驱动；仓库不提供微信程序。

```powershell
# 在本开发版源码根目录执行。
py -3.12 -m pip install -r requirements.txt --target tools/python-deps
$env:PYTHONPATH = (Resolve-Path tools/python-deps).Path
$env:WECHAT_DIRECT_PYTHON = (py -3.12 -c 'import sys; print(sys.executable)')
node --test
py -3.12 -X utf8 -m unittest discover -s test -p '*_test.py'
py -3.12 -X utf8 tools/build_bridge.py --core 'C:\Program Files\Tencent\Weixin\4.2.0.52\Weixin.dll' --zig 'C:\Tools\zig\zig.exe'
New-Item -ItemType Directory -Path plugin/runtime/native -Force | Out-Null
Copy-Item native/direct-bridge-4.2.0.52.dll plugin/runtime/native/direct-bridge-v2.dll
Copy-Item native/direct-bridge-4.2.0.52.dll plugin/runtime/native/direct-bridge-v3.dll
py -3.12 -X utf8 tools/build_plugin.py
```

把 `--core`、`--zig` 改为实际文件路径。默认核心路径也可在 `config/native-target.json` 调整，核心 SHA-256 必须与版本配置一致。安装包输出至 `release/wechat-direct-2.5.0-dev.4-windows-x64.zip`。重新编译驱动后运行打包步骤，刷新 `runtime/support.json` 中的组件摘要。需要额外静态分析工具时，安装 `tools/requirements.txt`。

## 使用

可以请求“读取指定联系人的最近聊天”，或“按刚才的内容回复，合适时用我的表情”。完整给定正文按原文发送；代拟回复依赖用户明确授权，不会自行启动后台自动回复。

多账号登录时，用 `wechat_bind(account="<微信号或内部账号>")`，或 `node plugin/scripts/wechat.mjs bind --account '<微信号或内部账号>'` 显式选择。绑定前校验进程、对应数据库和账号自身记录；匹配失败保留原绑定。各账号使用独立的密钥和读取缓存。

内置和图片表情共用频率限制：每五条插件消息最多一次、中间至少四条纯文字、最近二十条不超过 20%。严肃、正式、争执、哀悼场景不用语境表情。图片/动图先预览和标注含义，含义不明确时只供用户指定发送。

普通图片用 `wechat_send_image`，接受不超过 20 MiB、四千万像素的静态 PNG/JPEG，不转成 GIF 或收藏表情。文件用 `wechat_send_file`，接受不超过 100 MiB 的本地文件并保留文件名与内容。这些是插件自身的保守上限。两者都需要 `recipientId`、文件绝对路径 `filePath` 和稳定 `operationKey`；发送前复制到私人缓存并核对内容。文件名或“出现一条附件消息”不足以确认成功；缺少可匹配的内容摘要时保持 `unknown`，只查询原操作。

## 定时发送

用 `wechat_schedule_create` 保存固定内容的任务，用 `wechat_schedules` 查看，用 `wechat_schedule_update` 暂停、恢复或取消。任务绑定账号；创建时使用稳定 UUID `taskId`，同编号重试返回原任务，不重复排程。支持文字、目录表情、普通图片和文件，不在执行时临时代拟内容。

- 一次性：`{"kind":"once","at":"2026-11-10T09:00:00+08:00"}`。
- 每日：`{"kind":"daily","time":"09:00"}`。
- 每周：`{"kind":"weekly","time":"09:00","weekdays":[0,2,4]}`，周一为 0。
- 默认 `timezone="Asia/Shanghai"`，也支持 UTC；其他 IANA 时区需要运行环境提供相应时区数据。

任务保存后需要单独启动执行器。它不依赖聊天窗口，但电脑必须处于可运行状态、微信已登录且版本与绑定账号匹配。在源码根目录执行：

```powershell
node plugin/scripts/scheduler.mjs --interval 15
# 在另一个终端查询或请求停止：
node plugin/scripts/scheduler.mjs --status
node plugin/scripts/scheduler.mjs --stop
```

`--once` 只检查一轮，可用于已有的 Windows 任务计划调用。执行器不自动注册开机任务；检查间隔、队列和微信响应都会影响实际发送时间，不保证秒级准时。停止执行器不会删除已保存任务。

`wechat_worker_status` / `wechat_worker_stop` 提供相同控制。状态包含进程、心跳、最近检查和错误；同一私人状态目录只允许一个执行器。停止请求等待当前调用结束，不能撤回已提交的消息，也不会强杀其他进程。

漏跑时，一次性任务在下次检查补一次；每日/每周任务只处理最近到期的一次，不批量补历史消息。已明确未提交的失败不会重试该次：一次性任务标为失败，循环任务下个周期继续。提交结果未知时仅查询原编号，并阻止该任务后续发送。暂停/取消不能撤回已经开始执行的消息。

图片和文件任务记录创建时的内容摘要；源文件应保留到执行时，修改或删除会阻止该次发送。任务记录与文件副本都在私人状态目录内，不随源码发布。

CLI 同样可创建任务：先写 UTF-8 JSON 文件，字段为 `taskId`、`payload`、`schedule`、`timezone`。`payload` 例如 `{"action":"send-file","recipientId":"<联系人内部账号>","filePath":"C:\\Reports\\daily.pdf"}`。然后执行：

```powershell
node plugin/scripts/wechat.mjs schedule-create --request-file 'C:\Requests\schedule.json'
node plugin/scripts/wechat.mjs schedule-list
node plugin/scripts/wechat.mjs schedule-update --task-id '<原 taskId>' --operation pause
```

私人状态保存在 `%LOCALAPPDATA%\CodexWeChatDirect`，包括账号绑定、数据库密钥缓存、消息读取缓存、表情素材、附件副本、定时任务和操作日志。请勿将该目录或实机报告提交至 GitHub。源码仓库不包含这些数据。每个微信进程最多保留 1024 个发送操作，达到上限后正常重启微信。

## 状态恢复与缓存管理

查询原发送时，若日志仅停留在尚未进入提交的 `reserved`，会恢复为 `not_submitted`。已进入提交但无法确认结果的记录继续保留为未知，不自动重发。人工在微信中核对后，才可调用 `wechat_send_close(operationKey, reviewed=true)` 解除该记录的后续发送阻塞；`closed` 表示停止自动追踪，**不表示已确认成功或失败**，同一编号仍不能重新提交。定时任务对应操作为 `wechat_schedule_update(operation="close-run", reviewed=true)`；一次性任务取消，循环任务保留下个周期，不重发已关闭的那一次。

可再生缓存默认按 **1024 MiB、7 天** 清理，优先回收较久未使用的数据库和附件副本。首次扫描或正在使用的分片、结果未知的附件可能使占用超过上限；这不是硬性磁盘配额。未变化分片的表索引可跨进程复用，原微信数据库、原附件、绑定、发送日志、任务和表情库均保留。

用 `wechat_cache_status` 查看，用 `wechat_cache_configure(maxMiB, maxAgeDays)` 调整后续操作的清理策略；立即清理用 `wechat_cache_clean`。`purge=true` 清除全部可清理副本，`includeKeys=true` 额外清理密钥缓存，下一次读取可能需重新提取。待确认或人工关闭但结果仍未知的附件继续受保护。

```powershell
node plugin/scripts/wechat.mjs cache-status
node plugin/scripts/wechat.mjs cache-configure --max-mib 1024 --max-age-days 7
node plugin/scripts/wechat.mjs cache-clean
```

## 工程目录

| 路径 | 用途 |
| --- | --- |
| `plugin/scripts/` | MCP 服务、CLI、数据库适配、发送状态与表情策略 |
| `plugin/skills/` | 插件使用说明 |
| `plugin/runtime/vendor/` | Apache-2.0 数据库读取模块 |
| `native/` | 原生驱动源码及客户端版本配置 |
| `tools/` | 编译、打包和静态分析工具 |
| `test/` | 不依赖真实账号的 Node / Python 测试 |
| `packaging/` | 安装器与安装说明 |

微信升级后需要重新适配。语音发送及自主后台回复尚未提供。参与开发见 [CONTRIBUTING.md](CONTRIBUTING.md)。
