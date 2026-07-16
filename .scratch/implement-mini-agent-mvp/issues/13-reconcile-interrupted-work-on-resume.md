# 13 - Reconcile interrupted work on Resume

**What to build:** After a crash or forced exit, Resume identifies every uncertain Tool side effect, shows the available evidence, and lets the user inspect, abandon, retry as a new call, or exit without rewriting history.

**Blocked by:** 07 - Apply confirmed transactional file changes; 08 - Run bounded Shell commands under Permission Policy; 12 - Cancel, retry, and diagnose failed Turns safely.

**Status:** completed

- [x] Convert every durable `tool.started` without a terminal event into an interrupted outcome during validated Resume and never replay it automatically.
- [x] Reconcile reads/searches through an optional new call, Patch through Checkpoint/current hashes/expected changes, and Shell through command, working directory, captured preview, and process evidence.
- [x] Offer only inspect, abandon, retry as a newly validated and authorized call, or exit; never allow a guessed manual-success Tool Result.
- [x] Distinguish confirmed cancellation/failure from uncertain interruption and preserve exactly one terminal Tool Result.
- [x] Re-read current `AGENTS.md`, compare recorded hashes, persist and display instruction changes, and reset or replace incompatible Plan state.
- [x] A changed or unknown higher Schema, corrupt event middle, active writer, or unresolved persistence failure prevents unsafe continuation.
- [x] Crash-focused integration tests cover partial Patch, completed-but-unrecorded effect, still-running/unknown Shell process, changed instructions, and every recovery choice.

## Completion evidence

- `SessionStore.inspect_resume()` reconstructs uncertain Tool calls from authoritative `events.jsonl`; `reconcile_resume()` appends exactly one terminal interruption result per unresolved call and never replays it automatically.
- Read/search evidence records target, directory, query, glob/regex, availability, and hashes; Patch evidence records checkpoints, before/current/expected hashes, and partial/raced states; Shell evidence records redacted command, cwd, captured preview, PID, and process state.
- Resume choices are limited to inspect, abandon, retry, and exit. Retry closes the old uncertain work and `AgentTurnApplication.retry_interrupted()` creates fresh Tool IDs and routes new calls through normal validation and Permission Gate checks.
- Recovery events distinguish inspection, abandonment, retry, interruption, and `plan.reset`; confirmed cancellation/failure paths remain separate from uncertain interrupted work.
- Current instruction files are re-read, historical hashes compared, changes persisted/displayed, and incompatible active plans reset. Recovery sidecars are evidence-only; `events.jsonl` remains authoritative.
- Existing schema/corruption/active-writer protections plus malformed-sidecar and orphan-sidecar checks block unsafe continuation.
- `tests/test_ticket13.py` covers read inspect/exit, partial Patch, completed-but-unrecorded Patch, Shell process evidence, changed instructions, unsafe resume blockers, and retry with a fresh authorized call.
- Verification: `uv run --frozen pytest -q` → `125 passed, 2 skipped`; `uv run --frozen ruff format --check .`; `uv run --frozen ruff check src tests`; `uv run --frozen mypy`; `git diff --check`.
