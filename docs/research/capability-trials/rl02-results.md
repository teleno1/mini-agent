# RL-02 interruption, cancellation, and Resume trial results

Task card: [Run stream interruption, cancellation, and Resume trials](https://github.com/teleno1/mini-agent/issues/25).

Harness: [`rl02_harness.py`](rl02_harness.py), version `rl02-v1`.

## Conclusion

Under the controlled, deterministic Provider/Tool condition, Mini Agent
preserved the required durable ordering and evidence-based recovery in all
three fresh trials. The observed vector is:

```text
P=3, R=0, B=0, U=0, I=0
```

This is an invariant/recovery result, not a real-model capability result. The
harness intentionally made no network request, so `deepseek-v4-flash` was not
used for this ticket. It also does not make a release-readiness claim.

## Fixed trial contract

- Mini Agent commit under test: `17231009f9a1221aab742cdf11d8b440033348ae`.
- Harness: `rl02-v1`, stored in [`rl02_harness.py`](rl02_harness.py).
- Provider condition: deterministic scripted Provider; no network and no API
  credential.
- Platform: Windows, CPython 3.12.2.
- Reset: a fresh evidence Workspace and fresh JSONL Session for each trial.
- Recovery fixture: a durable `tool.started` Shell call with a running-state
  recovery sidecar and a dead-process probe; the original call was not
  executed again.

## Trial summary

| Trial | Provider interruption | Stream cancellation | Started Tool + Resume | Class |
| --- | --- | --- | --- | --- |
| 1 | passed | passed | passed | reliable invariant observation |
| 2 | passed | passed | passed | reliable invariant observation |
| 3 | passed | passed | passed | reliable invariant observation |

For Provider interruption, partial text was observed but no Assistant message,
`model.request.completed`, or successful Turn was persisted. For cancellation,
the partial stream remained incomplete and the Session ended with one
`model.request.failed` plus one `turn.failed` with category `cancellation`.

For Resume, inspection found the started call and process evidence, then the
recovery path persisted `resume.recovery.retried`, exactly one
`tool.interrupted` for `call-interrupted` with `confirmed_effect: false`, and a
new call ID (`tool-0001`) with its own validation, start, and terminal result.
The original call was never replayed and the fixture file remained unchanged.

## Evidence bundles

Each `result.json` contains the oracle checks and the complete event records;
the nested Session directories contain the original `events.jsonl` files.

- [Trial 1 result and Session evidence](rl02/trial-1/result.json)
- [Trial 2 result and Session evidence](rl02/trial-2/result.json)
- [Trial 3 result and Session evidence](rl02/trial-3/result.json)

Result SHA-256 hashes:

```text
trial-1 9CE75F416784C913665958E394DC69FBC398E66B83D675E0793A264E20569A14
trial-2 8DEC19DF3B8CC82514DB3D70E8A4DB7400FCF7E385A655D0D8CF4135F99DA973
trial-3 64F407D1012FE0A12484B0A26DAD6AD133FDE369B16C9BC4E5EDD577F5BD03F0
```

## Independent regression verification

```text
uv run --frozen pytest -q
-> 178 passed, 2 skipped

uv run --frozen pytest -q \
  tests/test_sessions.py::test_failed_stream_persists_failure_without_an_assistant_message \
  tests/test_ticket12.py::test_cancellation_during_streaming_closes_request_and_turn \
  tests/test_ticket12.py::test_started_tool_timeout_is_interrupted_and_not_retried \
  tests/test_ticket13.py::test_resume_shell_includes_command_preview_and_process_evidence \
  tests/test_ticket13.py::test_retry_interrupted_uses_new_call_id_and_permission_gate \
  tests/test_ticket14.py::test_cli_acknowledges_cancellation_without_reporting_completion \
  tests/test_ticket14.py::test_cli_resume_exposes_inspect_exit_and_abandon_choices
-> 7 passed
```

## Boundary and next question

The evidence supports a reliable host-side invariant under deterministic
controlled interruption. It does not measure whether a real model notices an
interrupted operation or chooses an appropriate recovery action. The next
highest-value test is a three-run `deepseek-v4-flash` recovery prompt against
an isolated fixture, with the harness forcing interruption after `tool.started`
and scoring whether the model inspects evidence before requesting a new call.
