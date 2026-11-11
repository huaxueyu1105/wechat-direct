# 在其他 AI 客户端连接微信直连

在运行微信的同一台 Windows x64 电脑上，解压 **2.5.0-dev.4** 的完整安装包。需要微信 4.2.0.52、Node.js 22+ 和 Python 3.12 x64。直接从解压目录运行 `mcp-config.mjs` 即可使用本地 MCP，无需运行用于注册 Codex 的 `install.cmd`。从源码运行时，先按 README 构建安装包，或使用同样构建好的 `plugin/` 运行时。

## 生成配置

在完整安装包的解压目录打开 PowerShell，按客户端运行其中一条命令。若 Python 3.12 x64 没有加入 PATH，也没有可用的 `py -3.12`，可以先设置 `$env:WECHAT_DIRECT_PYTHON = 'C:\Python312\python.exe'`，路径换成实际位置。

```powershell
node .\mcp-config.mjs kimi
node .\mcp-config.mjs gemini
node .\mcp-config.mjs claude-desktop
node .\mcp-config.mjs claude-code
node .\mcp-config.mjs deepseek
```

在源码根目录运行时，把 `.\mcp-config.mjs` 换成 `.\packaging\mcp-config.mjs`。生成器只输出 JSON。合并配置时只添加或替换 `wechat_direct` 这一项，保留同一文件里的其他设置。配置使用解压位置的绝对路径，移动目录后重新生成。

| 客户端 | 配置和检查 |
| --- | --- |
| Kimi Code CLI | 合并到 `~/.kimi-code/mcp.json`，旧版 Kimi CLI 使用 `~/.kimi/mcp.json`。新会话中用 `/mcp` 查看连接，旧版可用 `kimi mcp test wechat_direct`。 [Kimi Code MCP 文档](https://moonshotai.github.io/kimi-code/zh/customization/mcp.html) |
| Gemini CLI | 合并到 `~/.gemini/settings.json`，重启后运行 `gemini mcp list` 或在会话中输入 `/mcp list`。 [Gemini CLI MCP 文档](https://geminicli.com/docs/tools/mcp-server/) |
| Claude Desktop | 在设置的 Developer 页面打开本地配置，也可编辑 `%APPDATA%\Claude\claude_desktop_config.json`。合并后完全退出并重启 Claude Desktop。 [本地 MCP 配置文档](https://modelcontextprotocol.io/docs/develop/connect-local-servers) |
| Claude Code | 将输出保存为 UTF-8 JSON，如 `wechat-direct.mcp.json`。运行 `claude --mcp-config .\wechat-direct.mcp.json`，会话中用 `/mcp` 检查连接。 [Claude Code MCP 文档](https://code.claude.com/docs/en/mcp) |
| DeepSeek | 将 `deepseek` 的输出合并到 OpenCode 项目目录的 `opencode.json`。OpenCode 1.x 使用 `mcp.wechat_direct`，2.x 生成时加 `--opencode-v2`，使用 `mcp.servers.wechat_direct`。运行 `opencode mcp list` 检查连接，再在 OpenCode 中用 `/connect` 选择 DeepSeek、填写 API Key 并选择模型。 [OpenCode MCP 文档](https://opencode.ai/docs/mcp-servers/) · [DeepSeek 官方接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/opencode/) |

例如，可用以下 PowerShell 命令生成并启动一次 Claude Code 会话。DeepSeek 也可以使用相同的 Claude Code MCP 配置，再按官方说明将 Claude Code 的模型提供商设为 DeepSeek。 [DeepSeek 接入 Claude Code](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)

```powershell
node .\mcp-config.mjs claude-code | Set-Content -Encoding utf8 .\wechat-direct.mcp.json
claude --mcp-config .\wechat-direct.mcp.json
```

## 查询和发送

外部客户端配置默认只开放查询，例如微信状态、联系人、最近会话、聊天记录、发送结果和定时任务状态。先在客户端请求“查看微信直连状态”。如果尚未绑定账号，在解压目录运行以下命令，填写自己要绑定的微信号。已绑定的 Codex 用户默认共用 `%LOCALAPPDATA%\CodexWeChatDirect` 下的账号绑定、发送记录和任务。

```powershell
node .\plugins\wechat-desktop\scripts\wechat.mjs bind --account '<你的微信号>'
```

需要发送、切换账号、同步表情、管理定时任务等操作时，生成配置时加 `--full-access`，然后重新加载客户端的 MCP 配置。仅在明确授权后发送。同一逻辑发送必须沿用同一 `operationKey`，状态未知时只查询原操作。生成配置和检查连接均不会发送消息或启动定时执行器。

可用 `node .\mcp-config.mjs kimi --check` 检查 Node、Python 和本地 MCP 握手及工具列表，不需要登录模型账号或读取微信数据。配置适用于能启动本地 stdio MCP 的客户端。使用云模型时，客户端可能会把调用所返回的联系人、聊天等内容提交给该模型服务，请按需要缩小查询范围。
