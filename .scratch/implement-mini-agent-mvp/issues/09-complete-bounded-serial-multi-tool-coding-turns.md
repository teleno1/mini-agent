# 09 - Complete bounded serial multi-Tool coding Turns

**What to build:** A user can ask a real configured model to inspect code, make a confirmed change, run verification, adapt to denial or recoverable Tool failure, and finish with an honest report in one bounded serial Agent Turn.

**Blocked by:** 05 - Stream a real OpenAI-compatible text response; 07 - Apply confirmed transactional file changes; 08 - Run bounded Shell commands under Permission Policy.

**Status:** ready-for-agent

- [ ] Orchestrate repeated Context Frame, model, Tool authorization/execution, Tool Result, and continuation cycles through Application Ports.
- [ ] Execute multiple Tool Calls serially in model order, preserve provider-required pairing, and cancel not-yet-started calls only on user cancellation or host failure.
- [ ] Treat invalid input, denial, and recoverable Tool failure as structured observations so the model can adjust rather than falsely completing.
- [ ] Persist required transitions before the next possible side effect and stop immediately when durable state cannot advance.
- [ ] Enforce one active Turn, one active model request or Tool, 25 model requests, 50 Tool Calls, 30 active minutes, token usage, output, and retry budgets.
- [ ] Persist complete Plan snapshots with at most one in-progress step and omit Plans for simple work.
- [ ] Complete only on a normal no-Tool stop and report outcome, verification performed or unavailable, changed files, unresolved work, and next action.
- [ ] A deterministic end-to-end test proves read, edit, test, denial/replan, recoverable failure, and final report ordering through the Fake Provider.
