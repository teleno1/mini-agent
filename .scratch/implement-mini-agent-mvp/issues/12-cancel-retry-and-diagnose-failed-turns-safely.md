# 12 - Cancel, retry, and diagnose failed Turns safely

**What to build:** Users can cancel active work promptly, transient model failures retry only when safe, persistence uncertainty halts execution, and every failure is understandable through redacted correlated diagnostics.

**Blocked by:** 05 - Stream a real OpenAI-compatible text response; 08 - Run bounded Shell commands under Permission Policy; 09 - Complete bounded serial multi-Tool coding Turns.

**Status:** ready-for-agent

- [ ] Implement the complete stable Failure taxonomy with redacted details, retryability, user action, source, cause, and correlation IDs.
- [ ] Retry only agreed pre-output transient Provider failures within three total attempts, respecting Retry-After or jittered backoff and assigning a new request ID each time.
- [ ] Enforce layered Provider/Tool timeouts and never automatically retry partial streams, persistence errors, cancellation, or started side effects.
- [ ] First Ctrl+C acknowledges within one second, stops new scheduling, and allows five seconds for cleanup; a second forces exit 130 after best-effort recording.
- [ ] If a prerequisite or terminal Session Event cannot persist, stop the Agent Loop and never report durable success; diagnostic-log failure must not hide the primary Failure.
- [ ] Write redacted correlated rotating diagnostic logs and make `doctor` resolve an error ID without exposing secrets, prompts, or raw sensitive output.
- [ ] Tests cover retry budgets, cancellation in streaming/permission/Shell phases, forced interrupt, broken stdout, plain-text degradation, fsync failures, exit codes, and single terminal-state invariants.
