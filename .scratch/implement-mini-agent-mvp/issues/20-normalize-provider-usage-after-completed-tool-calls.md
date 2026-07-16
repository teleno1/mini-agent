# 20 - Normalize Provider usage after completed Tool Calls

**What to build:** OpenAI-compatible streams that report usage in the same chunk as a `tool_calls` finish reason produce a legal normalized event order and complete the Agent Loop without an incomplete-stream failure.

**Blocked by:** 05 - Stream a real OpenAI-compatible text response; 09 - Complete bounded serial multi-Tool coding Turns.

**Status:** completed

- [x] Normalize completed Tool Calls before Provider usage when both become final in the same response.
- [x] Preserve text-only usage ordering and reject genuinely illegal post-stop Provider content.
- [x] Safely adapt internal `developer` instruction messages to the widely supported Provider `system` role without changing the Context Frame.
- [x] Provider contract regression covers the DeepSeek-style `finish_reason: tool_calls` plus `usage` chunk.
- [x] Agent Loop regression proves the normalized stream closes and proceeds to execute the Tool.
- [x] Targeted, full quality, and restricted real-model checks pass.

## Completion evidence

- `tests/test_provider.py::test_real_adapter_completes_tool_calls_before_same_chunk_usage` reproduces DeepSeek V4's same-chunk `finish_reason`, `usage`, nullable content, and reasoning fields, then asserts `ToolCallCompleted → UsageReported → ResponseCompleted`.
- `tests/test_provider.py::test_agent_loop_executes_tool_after_same_chunk_usage` drives the real adapter through `AgentTurnApplication`, executes `read_file`, returns its Tool Result to a second request, and completes with final assistant text.
- Existing real-adapter Context Frame contract tests assert the Provider-bound `developer → system` compatibility mapping while the Fake Provider continues to preserve the internal `developer` role.
- Restricted real-model check on 2026-07-16 with DeepSeek `deepseek-v4-flash`: `uv run --frozen mini-agent "能看到这个项目吗？请只做只读检查后回答。"` completed with exit code 0, nine successful read/search Tool Calls, one correctly denied non-interactive Shell call, and no incomplete stream. Session IDs and sensitive output are intentionally omitted from durable ticket evidence.
- Verification: `uv run --frozen pytest -q tests/test_provider.py` (14 passed); `uv run --frozen ruff format --check .`; `uv run --frozen ruff check .`; `uv run --frozen mypy`; `uv run --frozen pytest -q` (164 passed, 2 skipped); `git diff --check`.
