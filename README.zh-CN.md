# Mini Agent

[English](README.md)

Mini Agent 是一个小型、可阅读、可检查的终端编码 Agent，适合用来学习
Agent Loop 在真实项目中是如何工作的。

你可以在一个代码仓库中给它一个任务，让它读取文件、搜索代码、提出或应用
精确的文本修改、运行受限的本地命令，并说明最终做了什么。

它把 Agent 中最重要的边界明确展示出来：

- 模型只能请求有类型约束的 Tool，真正执行前由宿主程序校验和授权；
- 所有面向模型的文件路径都限制在一个 Workspace 内；
- 文件写入和 Shell 命令受 Permission Mode 控制；
- Session、Tool 结果、Plan、失败状态和恢复状态都会持久化；
- 流式输出、取消、重试、上下文压缩和中断恢复都有明确处理。

## 它能做什么？

Agent Loop 内置以下能力：

- 使用 `read_file` 读取有大小和行数限制的 UTF-8 文本文件；
- 使用 `search_files` 按字面量或正则表达式搜索仓库文本；
- 使用 `apply_patch` 应用精确、可审查的新增/修改/删除文本补丁；
- 使用 `create_file` 创建新文件，并拒绝覆盖已有文件；
- 使用 `shell` 运行有超时和输出限制的非交互 PowerShell 或 POSIX 命令；
- 将较大的 Tool 结果保存为不可变 Artifact，需要时再读取；
- 按路径加载作用域明确的 `AGENTS.md` 指令，同时保持宿主安全规则优先；
- 流式显示模型输出，需要时请求权限；流或操作失败时明确标记为未完成；
- 将 Session 保存为 JSONL，之后可以列出并继续之前的工作；
- 通过显式 Plan Mode 为复杂任务展示可见计划，默认关闭。

内置文件 Tool 会限制在 Workspace 内。Shell 虽然有权限控制和资源限制，
但它不是操作系统级沙箱，也不是容器。

## 快速开始

运行要求：Python 3.12 或更高版本、[uv](https://docs.astral.sh/uv/)，以及
一个兼容 OpenAI 接口的 Provider API Key。

在项目副本中安装：

```console
uv sync --frozen
```

将 API Key 设置为环境变量。Mini Agent 只从 `MINI_AGENT_API_KEY` 读取凭据，
不会从 TOML 或命令行参数读取 API Key。

Windows PowerShell：

```powershell
$env:MINI_AGENT_API_KEY = "your-provider-key"
```

macOS/Linux：

```bash
export MINI_AGENT_API_KEY="your-provider-key"
```

进入目标代码仓库，运行一次性任务：

```console
mini-agent "阅读项目结构，并说明 CLI 从哪里启动"
```

第一个非选项参数会被视为任务，因此下面的写法等价：

```console
mini-agent run "找到认证配置，并为它补充一个测试"
```

不提供任务即可进入交互式 Session：

```console
mini-agent
```

默认 Workspace 是当前目录。也可以指定其他仓库：

```console
mini-agent --workspace ./my-repo "运行测试并总结失败原因"
```

## 常用命令

```console
# 查看帮助和版本；不需要 API Key
mini-agent --help
mini-agent --version

# 创建 .mini-agent/config.toml，并将运行时数据加入 .gitignore
mini-agent init
mini-agent init --yes

# 查看最终生效的配置及其来源
mini-agent config show

# 列出当前 Workspace 中持久化的 Session
mini-agent sessions

# 继续一个已有 Session
mini-agent resume SESSION_ID "继续修复失败的测试"

# 根据错误 ID 查看一条脱敏后的诊断记录
mini-agent doctor ERROR_ID
```

`init` 不会写入凭据，默认会请求确认，使用 `--yes` 可以直接确认。
`config show`、`sessions` 和 `doctor` 都是本地检查命令，不会调用模型 Provider。

常用启动选项：

```console
mini-agent --workspace PATH
mini-agent --model MODEL
mini-agent --base-url https://provider.example/v1
mini-agent --permission-mode suggest|auto-edit|full-auto
mini-agent --plan-mode
mini-agent --no-plan-mode
```

Provider 使用兼容 OpenAI 的 Chat Completions 流式接口和结构化 Tool。
默认模型是 `gpt-4o-mini`，默认 Base URL 是
`https://api.openai.com/v1`，两者都可以通过上面的方式修改。

## 交互式 Session 命令

在交互式 `mini-agent` Session 中，直接输入任务即可。以下命令用于控制
Session，不会发送模型请求：

```text
/help
/config show
/config set model=gpt-4o-mini
/config set permission_mode=auto-edit
/config reset
/plan on
/plan off
/sessions
/exit
```

配置和 Plan 的修改会在下一次操作时生效。Plan Mode 默认关闭，只能通过
`--plan-mode`、`--no-plan-mode`、`/plan on` 或 `/plan off` 显式修改；仓库
中的文本或任务复杂度不能偷偷开启它。

## 权限与安全

Tool 执行前，宿主程序会校验路径、参数、资源限制和风险。默认的 `suggest`
模式会在文件写入和 Shell 操作前请求确认。

| 模式 | 行为 |
| --- | --- |
| `suggest` | 每次文件写入和 Shell 操作都请求确认。 |
| `auto-edit` | 普通文件新增/修改自动执行；Shell 和其他风险操作请求确认。 |
| `full-auto` | 额外允许已识别的本地读取/构建/测试命令；硬性安全规则仍然生效。 |

需要确认时，终端菜单为：

```text
1 仅允许这一次
2 在当前 Session 中允许这个完全相同的标准化调用
3 拒绝
4 取消
```

只要 Tool 参数发生变化，就必须重新决策。敏感文件、私钥、凭据存储、
`.mini-agent`、Workspace 越界、破坏性删除、网络访问、安装依赖、交互式或
后台 Shell 行为等风险，不会因为切换权限模式而自动变安全。

`AGENTS.md`、CI 配置、锁文件和安全策略属于 Protected Path，修改它们始终
需要显式确认。

## 配置

项目配置文件位于 `.mini-agent/config.toml`。`mini-agent init` 会创建一个
最小示例：

```toml
model = "gpt-4o-mini"
permission_mode = "suggest" # suggest、auto-edit 或 full-auto
max_model_requests = 25
max_tool_calls = 50
max_active_seconds = 1800
context_window_tokens = 128000
response_reserve_tokens = 16000
artifact_threshold_bytes = 32768
instruction_file_bytes = 32768
instruction_chain_bytes = 131072
```

配置来源按以下顺序生效，后者覆盖前者：

1. 内置默认值；
2. 用户 TOML：Windows 为 `%APPDATA%/mini-agent/config.toml`，macOS/Linux 为
   `~/.config/mini-agent/config.toml`；
3. 项目 TOML；
4. 环境变量；
5. CLI 选项；
6. 显式的 Session 覆盖值。

环境变量使用 `MINI_AGENT_` 前缀，例如 `MINI_AGENT_MODEL`、
`MINI_AGENT_PERMISSION_MODE` 和 `MINI_AGENT_PROVIDER_BASE_URL`。
API Key 是例外：只能使用 `MINI_AGENT_API_KEY`。

配置会被严格校验，宿主安全上限会防止配置把一次 Turn 变成无限运行。
`config show` 会显示最终生效的非敏感配置和对应来源。`provider_base_url`
可以在用户 TOML、环境变量或 `--base-url` 中设置，但不能写入项目 TOML。

## Session、失败与恢复

每个 Session 都以 UTF-8 JSONL 形式保存到 `.mini-agent/sessions`。事件历史是
权威记录，因此进程退出不会抹掉对话。较大的结果会保存为本地 Artifact，较长
的对话可以压缩为结构化摘要，但原始事件不会被删除。

如果进程在 Tool 执行期间停止，Mini Agent 会将该 Tool 标记为 interrupted，
不会假设它成功，也不会自动重放。执行 `resume` 时，它会展示可用证据，并提供
`inspect`、`abandon`、`retry` 和 `exit` 选项。

`retry` 会创建一个重新校验和授权的新 Tool 调用，不是对不确定副作用的自动重放。
如果上次 Session 之后 `AGENTS.md` 发生变化，恢复时也会明确提示。

运行时诊断会脱敏，并以轮转 JSONL 文件写入 `.mini-agent/logs`。错误消息中的
错误 ID 可以这样查看：

```console
mini-agent doctor ERROR_ID
```

退出码约定如下：正常完成为 `0`，运行时失败为 `1`，配置或用法错误为 `2`，
强制中断为 `130`。

## 开发

安装锁定的开发环境并运行检查：

```console
uv sync --frozen --all-groups
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest
```

构建并执行未发布的本地制品冒烟测试：

```console
uv run --frozen python scripts/build_artifacts.py
uv run --frozen python scripts/smoke_artifacts.py
```

构建会在 `dist/` 下生成纯 Python wheel、源码分发包和 `SHA256SUMS`。
自动化测试和制品冒烟测试使用 Fake Model Provider；生产 CLI 在缺少认证时
不会偷偷切换到 Fake Provider。

## 许可证

Mini Agent 使用 [MIT License](LICENSE) 发布。
