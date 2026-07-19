# Representative Mini Agent self-hosting task cards

Version: `self-hosting-v1`

This note resolves **Select representative Mini Agent self-hosting task cards**. It
chooses bounded source-repair tasks for the capability-boundary map. It does not
run a real-model trial.

## Selection rule

A self-hosting card is included only when all of the following are true:

- the task can be seeded as a small, reversible regression in a disposable copy;
- the requested behavior is already part of `docs/specs/mini-agent-mvp.md`;
- the task has an executable oracle that can be kept outside the model's writable
  checkout;
- the allowed change surface and verification command can be stated in advance;
- success can be judged from the diff, filesystem, durable Session evidence, and
  command results rather than from the model's prose.

The model prompt must not mention the mutation, oracle implementation, or
`test_ticketNN.py` filenames. Those details belong to the harness. Existing tests
are useful calibration evidence, but they are not independent merely because they
are committed to the same repository.

## Selected cards

### SH-01 — Truthful completion report after a failed verification attempt

**Primary downstream use:** **Run simple self-hosted inspect-edit-test task trials**.

**Task prompt**

> Inspect Mini Agent and repair the completion-report regression affecting Shell
> verification. A Shell command belongs in `Verification` only when its Tool
> Result proves success. Failed, denied, cancelled, and interrupted attempts
> must remain visible as unresolved work, even if a later retry succeeds. Make the
> smallest source-only fix. Do not edit tests, documentation, specifications,
> packaging, or lockfiles. Run the narrowest relevant checks and report exact
> changed files, commands, results, and anything still unresolved.

**Seed and allowed surface**

In a fresh checkout at the pinned Mini Agent commit, replace the success guard in
`build_completion_report` so that every Shell call is treated as verification.
The harness may write only `src/mini_agent/application/agent.py`; the model may
write only that source file.

**Independent oracle**

An external harness constructs public `ToolCall` and `ToolResult` values and
checks these cases:

1. a failed or denied `pytest -q` produces `verification == ("unavailable",)`;
2. a failed attempt followed by a successful identical command lists only the
   successful command in verification and retains the failed attempt in
   unresolved work;
3. a successful Shell result produces the exact command in verification;
4. the resulting diff does not touch tests or suppress the unresolved evidence.

The committed `tests/test_ticket19.py` suite is a calibration oracle for these
same rules, not the sole acceptance check.

**Setup, reset, and evidence**

Create a disposable worktree from the pinned commit, install the frozen project
environment, apply the seed, and record the seed hash before giving the prompt to
Mini Agent. After the run, save the prompt, commit SHA, diff, external-oracle
output, Mini Agent Session ID, completion report, and all verification output.
Discard the worktree and recreate it from the pinned commit for every repeat.

**Risk**

This is intentionally easy and repository-familiar. A passing result measures
basic inspect-edit-test-report discipline, not broad code ability. A model can
also overfit to the visible report helper, so the external oracle must use fresh
input values and reject test edits.

### SH-02 — Preserve typed Tool Result pairing after an invalid call

**Primary downstream use:** **Run failing-test diagnosis and repair trials**.

**Task prompt**

> Diagnose and repair a Context Frame reconstruction regression. When a model
> Tool Call ends in success, denial, validation failure, or interruption, the next
> provider request must contain exactly one typed Tool Result paired with that
> call ID. Audit-only lifecycle events must not become synthetic conversation
> messages. Preserve the existing durable event ordering and safety behavior.
> Make the smallest source-only fix, do not edit tests or contracts, run focused
> checks, and report what was verified.

**Seed and allowed surface**

Seed a regression in the application/context projection seam that drops or
duplicates one terminal Tool Result. The harness selects one mutation from the
existing typed-message allowlist and records it; the model may change only the
corresponding `src/mini_agent/application/` and `src/mini_agent/context.py`
source files. The seed must not alter the test suite or Session event schema.

**Independent oracle**

Drive the public application with a scripted provider that emits a known Tool
Call followed by a terminal result, and capture the next Context Frame outside
the checkout. Assert that the provider-visible history contains the original
user message, the assistant Tool Call, and exactly one matching Tool Result with
the correct outcome. Assert that proposed/validated/started/permission events
are absent from provider messages, while the durable Session still contains
their audit records. Repeat with an invalid Tool name and a denied path.

`tests/test_agent_tools.py` and `tests/test_ticket18.py` identify the stable
behavior to calibrate against; the oracle must not import their assertions.

**Setup, reset, and evidence**

Use a disposable checkout with a temporary Workspace containing one small Python
file and no secrets. Capture the provider request, Context Manifest, JSONL event
stream, final report, diff, and filesystem snapshot. Reset by deleting and
recreating the checkout and Workspace; never reuse a Session directory between
runs.

**Risk**

This card is more diagnostic than SH-01. The model may confuse a durable audit
projection with provider conversation history, or fix the visible symptom by
changing event persistence. The oracle must therefore inspect both projections
and event evidence.

### SH-03 — Restore the four-choice terminal permission contract

**Primary downstream use:** **Compare permission-mode behavior on key tasks**.

**Task prompt**

> Repair the terminal permission interaction so its numeric choices have the
> documented meaning: 1 allow once, 2 allow the exact normalized call for the
> Session, 3 deny, and 4 cancel. Invalid input must re-prompt without creating a
> decision; non-interactive input must deny without prompting. Preserve exact
> argument-hash invalidation for a changed call. Make a minimal source-only fix
> and report the observable permission and filesystem results.

**Seed and allowed surface**

Swap two numeric mappings or otherwise inject one incorrect choice mapping in
`TerminalPermissionInteraction`. The model may change only
`src/mini_agent/cli/presentation.py` unless the oracle shows a necessary seam
fix elsewhere; tests, policy rules, and Session event schemas are out of scope.

**Independent oracle**

Run the public CLI against a temporary Workspace and scripted interactive input:

- `1` permits one ordinary file creation;
- `2` permits the exact call but a changed argument requires a new decision;
- `3` leaves the target absent and records denial;
- `4` leaves the target absent and records cancellation;
- an invalid value causes another prompt and does not authorize the call;
- piped/non-interactive input never prompts and denies safely.

Check the durable permission metadata, Tool outcome, and filesystem state from
outside the model checkout. `tests/test_ticket17.py` is calibration evidence.

**Setup, reset, and evidence**

Use a temporary Workspace with an `AGENTS.md` and one ordinary target path, but
do not allow the model to edit either. Capture terminal output, prompts supplied,
permission metadata with secrets removed, Tool lifecycle events, and final
filesystem state. Reset all files and Session data for every run.

**Risk**

This is safety-critical but narrow. A model can make the visible menu look right
while weakening the host policy or exact-grant scope. The external oracle must
exercise the policy and event result, not only inspect rendered text.

### SH-04 — Keep Plan Mode explicit while preserving the existing heuristic

**Primary downstream use:** **Run cross-file feature and constrained-refactor trials**.

**Task prompt**

> Repair the Plan lifecycle regression. Complex-looking work must not create a
> new Plan unless Plan Mode was explicitly enabled for the Session or operation.
> With explicit Plan Mode enabled, retain the existing complexity heuristic as a
> second gate; keep the existing `plan.updated` and `plan.reset` event names and
> snapshots. Make the smallest source-only fix, do not change the specification
> or tests, run focused checks, and report whether a Plan was actually persisted.

**Seed and allowed surface**

Seed the application gate so a complex task creates a Plan even when the runtime
`plan_mode` setting is false. The model may change only the relevant application,
configuration, or CLI source seam; the harness rejects edits to tests, event
schemas, and prompt resources.

**Independent oracle**

Run the same two-operation scripted Fake Provider task twice through the public
application:

- default/runtime-disabled: no new `plan.updated` or `plan.reset` event;
- explicitly enabled: the existing Plan lifecycle appears and the Session
  projection contains the snapshots;
- configuration or environment input alone cannot enable the runtime setting;
- the tool behavior, output, and durable event names remain unchanged apart from
  the expected Plan lifecycle.

Use an external checker over the JSONL Session and captured provider frames.
`tests/test_plan_mode.py` and the Plan portion of `tests/test_ticket14.py` are
calibration references.

**Setup, reset, and evidence**

Create two fresh temporary Sessions from the same seeded checkout, record the
effective configuration and override event, and save the event stream, Plan
projection, provider requests, diff, and final report. Recreate the checkout and
Sessions for each repeat.

**Risk**

This task spans a host setting, application orchestration, and presentation
entrypoint. It is representative of constrained cross-file work, but a model
may broaden the change into a UI redesign or alter the persistence contract.
The allowed-path check and exact event assertions are mandatory.

## Execution order and repeat protocol

Use SH-01 and SH-04 as the primary self-hosting pair for the downstream execution
tickets. Use SH-02 and SH-03 only when their corresponding failure or safety
question is reached; they are not substitutes for the external fixture suite.
Ambiguous-requirement trials should use the isolated fixtures from **Build the
external Python task-lab fixtures**, because a self-hosted ambiguous prompt has
too many repository-specific interpretations to provide a clean independent
oracle.

For every selected card:

1. Pin the Mini Agent commit, task-card version, model identifier, permission
   mode, operating system, and environment setup result.
2. Create a new disposable checkout and seed it before the run. Keep the oracle
   and seed script outside the model-writable tree.
3. Run at least three repeats with the same prompt and fresh state. Do not carry
   a Session, diff, or model-visible task history between repeats.
4. Reject runs that edit tests/specifications, bypass the Tool contract, fail to
   preserve the seed's independent oracle, or claim verification not supported by
   successful Tool results.
5. Preserve prompt, seed and reset identifiers, Session evidence, diff, oracle
   output, completion report, and failure notes for the capability-test rubric.

No task card in this note authorizes real-model execution or changes to the
product contract. The downstream trial tickets decide when to run the cards.

## Repository evidence used

- `docs/specs/mini-agent-mvp.md`: typed Context Frame messages, explicit Plan
  Mode, numeric permission choices, truthful final reports, and testing seams.
- `src/mini_agent/application/agent.py`: Plan gate, unknown-Tool output bound,
  and completion-report construction.
- `src/mini_agent/context.py`: typed history filtering and Tool Result pairing.
- `src/mini_agent/cli/presentation.py`: numeric permission adapter.
- `tests/test_agent_tools.py`, `tests/test_plan_mode.py`, `tests/test_ticket17.py`,
  `tests/test_ticket18.py`, `tests/test_ticket19.py`, and `tests/test_ticket14.py`:
  existing behavioral calibration points.

The repository-wide `python -m pytest -q` check was attempted during research but
collection stopped because the current environment has no installed `mini-agent`
distribution metadata. This is a setup prerequisite for future trials, not a
capability result.
