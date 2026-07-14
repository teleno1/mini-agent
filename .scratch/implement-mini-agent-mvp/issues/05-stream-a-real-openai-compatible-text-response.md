# 05 - Stream a real OpenAI-compatible text response

**What to build:** A configured user can run a text-only Turn against the supported OpenAI-compatible Chat Completions subset and see honest, bounded streaming behavior through the existing Provider-neutral seam.

**Blocked by:** 04 - Assemble trusted configuration, instructions, and Context Frames.

**Status:** ready-for-agent

- [ ] Implement the direct httpx Chat Completions adapter with configurable user-controlled Base URL and model plus structured-tool capability detection.
- [ ] Translate Provider chunks into the complete normalized Stream Event vocabulary with legal ordering and stable request/Tool Call IDs.
- [ ] Persist only a successfully closed aggregate; deltas remain ephemeral and a broken stream remains visibly incomplete.
- [ ] Discard partial Tool arguments and reject unknown or illegal Provider events as redacted protocol Failures.
- [ ] Apply bounded renderer backpressure, text-refresh coalescing, and plain-text fallback without changing aggregate content.
- [ ] Enforce connect, first-event, idle, and total request timeouts and map Provider responses into the stable Failure categories.
- [ ] Contract tests prove Fake and real adapters preserve role authority, Tool schema, message pairing, and partial-stream semantics without real calls in CI.
