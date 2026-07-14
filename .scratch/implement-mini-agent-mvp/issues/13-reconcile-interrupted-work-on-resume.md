# 13 - Reconcile interrupted work on Resume

**What to build:** After a crash or forced exit, Resume identifies every uncertain Tool side effect, shows the available evidence, and lets the user inspect, abandon, retry as a new call, or exit without rewriting history.

**Blocked by:** 07 - Apply confirmed transactional file changes; 08 - Run bounded Shell commands under Permission Policy; 12 - Cancel, retry, and diagnose failed Turns safely.

**Status:** ready-for-agent

- [ ] Convert every durable `tool.started` without a terminal event into an interrupted outcome during validated Resume and never replay it automatically.
- [ ] Reconcile reads/searches through an optional new call, Patch through Checkpoint/current hashes/expected changes, and Shell through command, working directory, captured preview, and process evidence.
- [ ] Offer only inspect, abandon, retry as a newly validated and authorized call, or exit; never allow a guessed manual-success Tool Result.
- [ ] Distinguish confirmed cancellation/failure from uncertain interruption and preserve exactly one terminal Tool Result.
- [ ] Re-read current `AGENTS.md`, compare recorded hashes, persist and display instruction changes, and reset or replace incompatible Plan state.
- [ ] A changed or unknown higher Schema, corrupt event middle, active writer, or unresolved persistence failure prevents unsafe continuation.
- [ ] Crash-focused integration tests cover partial Patch, completed-but-unrecorded effect, still-running/unknown Shell process, changed instructions, and every recovery choice.
