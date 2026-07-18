# Mini Agent

[中文文档](README.zh-CN.md)

Mini Agent is a small, inspectable terminal coding agent for learning how an
agent loop works in practice. Give it a task in a repository and it can inspect
files, search code, propose or apply exact text changes, run bounded local
commands, and report what happened.

It is designed to make the important boundaries visible:

- a model requests typed Tools; the host validates and authorizes every call;
- all model-facing paths stay inside one Workspace;
- writes and Shell commands are controlled by a Permission Mode;
- Sessions, Tool Results, Plans, failures, and recovery state are persisted;
- streaming, cancellation, retries, context compaction, and interrupted work
  are handled explicitly.


## What can it do?

The Agent Loop can:

- read bounded UTF-8 text files with `read_file`;
- search repository text with literal or regular-expression search through
  `search_files`;
- apply exact, reviewable Add/Update/Delete text patches with `apply_patch`;
- create new files without overwriting an existing file with `create_file`;
- run bounded, non-interactive PowerShell or POSIX Shell commands with `shell`;
- store large Tool Results as immutable Artifacts and read them back when needed;
- follow scoped `AGENTS.md` instructions while keeping repository content below
  host safety rules;
- stream model output, ask for permission when needed, and leave an honest
  incomplete state when a stream or operation fails;
- retain Sessions as JSONL event histories so work can be listed and resumed;
- expose an explicit Plan Mode for complex tasks, disabled by default.

The built-in file Tools are Workspace-confined. Shell is permission-gated and
bounded, but it is not an operating-system sandbox or a container.

## Quick start

Requirements: Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), and an
API key for an OpenAI-compatible Provider.

Install the project from a checkout:

```console
uv sync --frozen
```

Set the API key in the environment. Mini Agent reads credentials only from
`MINI_AGENT_API_KEY`; it never reads an API key from TOML or CLI arguments.

Windows PowerShell:

```powershell
$env:MINI_AGENT_API_KEY = "your-provider-key"
```

macOS/Linux:

```bash
export MINI_AGENT_API_KEY="your-provider-key"
```

Run a one-shot task from the repository you want the Agent to inspect:

```console
mini-agent "Read the project structure and explain where the CLI starts"
```

The first non-option argument is treated as the task, so this is equivalent:

```console
mini-agent run "Find the authentication configuration and add a test"
```

Start an interactive Session by omitting the task:

```console
mini-agent
```

The default Workspace is the current directory. Select another repository with
`--workspace`:

```console
mini-agent --workspace ./my-repo "Run the tests and summarize failures"
```

## Common commands

```console
# Show command and option help; does not require an API key
mini-agent --help
mini-agent --version

# Create .mini-agent/config.toml and add runtime data to .gitignore
mini-agent init
mini-agent init --yes

# Inspect effective configuration and the source that supplied each value
mini-agent config show

# List durable Sessions in the selected Workspace
mini-agent sessions

# Continue an existing Session with a new task
mini-agent resume SESSION_ID "Continue by fixing the failing test"

# Print one redacted diagnostic record by its error ID
mini-agent doctor ERROR_ID
```

`init` is safe to run in a repository: it creates project defaults without
writing credentials and asks for confirmation unless `--yes` is supplied.
`config show`, `sessions`, and `doctor` are local inspection commands and do
not contact the model Provider.

Useful startup options are available on the default command and subcommands:

```console
mini-agent --workspace PATH
mini-agent --model MODEL
mini-agent --base-url https://provider.example/v1
mini-agent --permission-mode suggest|auto-edit|full-auto
mini-agent --plan-mode
mini-agent --no-plan-mode
```

The Provider uses the OpenAI-compatible Chat Completions streaming contract
with structured Tools. The default model is `gpt-4o-mini` and the default Base
URL is `https://api.openai.com/v1`; both can be changed as shown below.

## Interactive Session commands

Inside an interactive `mini-agent` Session, enter a task normally. The
following commands control the Session without sending a model request:

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

Configuration and Plan changes take effect on the next operation. Plan Mode is
off by default and can only be changed explicitly with `--plan-mode`,
`--no-plan-mode`, `/plan on`, or `/plan off`; repository text or task complexity
cannot enable it silently.

## Permissions and safety

The host validates paths, arguments, limits, and risk before a Tool can run.
The default `suggest` mode asks before writes and Shell commands. The three
modes are:

| Mode | Behavior |
| --- | --- |
| `suggest` | Ask before every write and Shell operation. |
| `auto-edit` | Allow ordinary file additions/updates; ask before Shell and other risky operations. |
| `full-auto` | Also allow recognized local read/build/test Shell commands; hard safety rules still apply. |

When confirmation is required, the terminal menu is:

```text
1 allow once
2 allow this exact normalized call for the Session
3 deny
4 cancel
```

Changing Tool arguments always requires a new decision. Sensitive files,
private keys, credential stores, `.mini-agent`, Workspace escapes, destructive
operations, network access, installation, interactive or detached Shell
behavior, and other hard hazards are not made safe by changing the permission
mode. `AGENTS.md`, CI files, lockfiles, and security policy are Protected Paths
whose writes require confirmation.

## Configuration

Project configuration lives at `.mini-agent/config.toml`. `mini-agent init`
creates a minimal example:

```toml
model = "gpt-4o-mini"
permission_mode = "suggest" # suggest, auto-edit, or full-auto
max_model_requests = 25
max_tool_calls = 50
max_active_seconds = 1800
context_window_tokens = 128000
response_reserve_tokens = 16000
artifact_threshold_bytes = 32768
instruction_file_bytes = 32768
instruction_chain_bytes = 131072
```

Values are applied in this order:

1. built-in defaults;
2. user TOML (`%APPDATA%/mini-agent/config.toml` on Windows or
   `~/.config/mini-agent/config.toml` on macOS/Linux);
3. project TOML;
4. environment variables;
5. CLI options;
6. explicit Session overrides.

Environment variables use the `MINI_AGENT_` prefix, for example
`MINI_AGENT_MODEL`, `MINI_AGENT_PERMISSION_MODE`, and
`MINI_AGENT_PROVIDER_BASE_URL`. API keys are the exception: use only
`MINI_AGENT_API_KEY`.

Configuration is strictly validated, and host safety ceilings prevent a source
from making a Turn unbounded. `config show` prints the effective non-secret
values and their winning sources. `provider_base_url` may be set in user TOML,
through the environment, or with `--base-url`; it cannot be set in project TOML.

## Sessions, failures, and recovery

Each Session is stored as UTF-8 JSONL under `.mini-agent/sessions`. The event
history is authoritative, so process exit does not erase the conversation.
Large results are kept as local Artifacts, and long conversations can be
compacted into structured summaries without deleting the original events.

If a process stops while a Tool is running, Mini Agent marks that Tool as
interrupted rather than assuming success or replaying it. On `resume`, it shows
the available evidence and offers `inspect`, `abandon`, `retry`, or `exit`.
Retry creates a newly validated Tool call; it is not an automatic replay of an
uncertain side effect. If `AGENTS.md` changed since the previous Session, the
change is disclosed during Resume.

Runtime diagnostics are redacted and written as rotating JSONL files under
`.mini-agent/logs`. Errors include an ID that can be inspected with:

```console
mini-agent doctor ERROR_ID
```

Normal completion returns exit code `0`, runtime failure `1`, configuration or
usage failure `2`, and forced interruption `130`.

## Development

Install the locked development environment and run the checks:

```console
uv sync --frozen --all-groups
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest
```

Build and smoke-test unpublished local artifacts:

```console
uv run --frozen python scripts/build_artifacts.py
uv run --frozen python scripts/smoke_artifacts.py
```

The build creates a pure-Python wheel, source distribution, and
`SHA256SUMS` under `dist/`. The Fake Model Provider is used by automated tests
and the artifact smoke journey; production CLI composition never silently
falls back to it when authentication is missing.

## License

Mini Agent is released under the [MIT License](LICENSE).
