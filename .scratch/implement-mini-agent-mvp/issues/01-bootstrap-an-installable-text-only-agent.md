# 01 - Bootstrap an installable text-only Agent

**What to build:** A developer can install and launch Mini Agent, submit one task to a scripted Fake Model Provider, see a streamed text response, and finish a text-only Turn through the same domain and Application Port seams that later slices will extend.

**Blocked by:** None - can start immediately.

**Status:** ready-for-agent

- [ ] The project uses Python 3.12+, a source-layout `mini_agent` distribution, Hatchling, uv, and the agreed runtime dependencies without an Agent framework.
- [ ] `mini-agent`, `python -m mini_agent`, `--help`, and `--version` work without an API Key or Git repository.
- [ ] A scripted Fake Model Provider completes one asynchronous text-only Turn through provider-neutral messages and normalized Stream Events.
- [ ] Domain rules do not import terminal, HTTP, filesystem, or concrete Provider code; the first real Application Ports are explicit and replaceable.
- [ ] The CLI shows a minimal conversational exchange and never exposes diagnostic Phase or Actions fields.
- [ ] Deterministic Clock and ID Generator substitutes make the smoke journey repeatable offline.
