# 微信直连 / wechat-direct

面向 Windows x64 的本机微信 Codex 插件。通过本机数据库读取联系人、会话和消息，通过原生驱动按授权发送文字、内置表情和当前账号添加的单个表情。保留当前个人账号和聊天记录。

当前插件版本 **2.4.2**，适配微信 **4.2.0.13**。项目采用 [MIT 许可证](LICENSE)，第三方组件保留各自许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。这是独立项目，与腾讯或微信官方没有隶属关系。

## 当前状态

- 13 个 MCP 工具：状态、联系人、会话、消息、轮询、文字发送、语境回复、表情来源、表情分页、含义标注、表情发送、回复策略、发送结果查询。
- 109 个内置表情编码；单个图片/动图只从当前账号添加目录同步，并核对目录成员和原始摘要。
- 发送使用稳定 `operationKey` 防止重复提交。`unknown` / `submitted` 结果需查询原操作；`confirmed` 表示匹配到新的服务器记录，不代表已读。
- 微信核心文件摘要和账号绑定不匹配时拒绝操作。每个微信进程首次文字/图片提交前执行不发送的对象准备检查。
- 4.2.0.13 的联系人、聊天读取及文字/图片对象准备已在开发机验证；**当前版本的真实文字和图片发送尚未完成验收**。详见 [验证范围](TEST_STATUS.md)。

## 安装

从本仓库 Releases 下载 Windows x64 安装包，解压后运行 `install.cmd`。需要已安装 Codex 桌面应用及 Python 3.12 x64、Node.js 22 或更高版本；安装器优先查找 Codex 提供的运行时。

首次安装前正常登录微信；已有绑定可离线更新。运行时也可通过 `WECHAT_DIRECT_PYTHON`、`WECHAT_DIRECT_NODE` 显式指定。安装后在新对话选择“微信直连”。安装和更新不发送测试消息。若尚无 Release，可按下文从源码构建。

## 从源码构建

环境：Windows x64、Python 3.12 x64、Node.js >= 22，以及 Zig 0.17.0。仅静态读取本机 `Weixin.dll` 并编译驱动；仓库不提供微信程序。

```powershell
git clone https://github.com/huaxueyu1105/wechat-direct.git
cd wechat-direct
py -3.12 -m pip install -r requirements.txt --target tools/python-deps
$env:PYTHONPATH = (Resolve-Path tools/python-deps).Path
node --test
py -3.12 -X utf8 -m unittest discover -s test -p '*_test.py'
py -3.12 -X utf8 tools/build_bridge.py --core 'C:\Program Files\Tencent\Weixin\4.2.0.13\Weixin.dll' --zig 'C:\Tools\zig\zig.exe'
New-Item -ItemType Directory -Path plugin/runtime/native -Force | Out-Null
Copy-Item native/direct-bridge-4.2.0.13.dll plugin/runtime/native/direct-bridge-v2.dll
Copy-Item native/direct-bridge-4.2.0.13.dll plugin/runtime/native/direct-bridge-v3.dll
py -3.12 -X utf8 tools/build_plugin.py
```

把 `--core`、`--zig` 改为实际文件路径。默认核心路径也可在 `config/native-target.json` 调整；核心 SHA-256 必须与版本配置一致。安装包输出至 `release/wechat-direct-2.4.2-windows-x64.zip`。需要额外的静态分析工具时，安装 `tools/requirements.txt`。

## 使用

可以请求“读取指定联系人的最近聊天”，或“按刚才的内容回复，合适时用我的表情”。完整给定正文按原文发送；代拟回复依赖用户明确授权，不会自行启动后台自动回复。

内置和图片表情共用频率限制：每五条插件消息最多一次、中间至少四条纯文字、最近二十条不超过 20%。严肃、正式、争执、哀悼场景不用语境表情。图片/动图先预览和标注含义，含义不明确时只供用户指定发送。

私人状态保存在 `%LOCALAPPDATA%\CodexWeChatDirect`，包括账号绑定、数据库密钥缓存、消息读取缓存、表情素材和操作日志。请勿将该目录或实机报告提交至 GitHub。源码仓库不包含这些数据。每个微信进程最多保留 1024 个发送操作，达到上限后正常重启微信。

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

微信升级后需要重新适配。一般图片附件、文件和语音发送尚未提供。参与开发见 [CONTRIBUTING.md](CONTRIBUTING.md)。
