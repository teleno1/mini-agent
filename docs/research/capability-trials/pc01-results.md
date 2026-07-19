# PC-01 external Python repair trial results

Task card: `PC-01`, version `python-fixtures-v1/pc01-v1`.

Prompt: [pc01-prompt.txt](pc01-prompt.txt), SHA-256
`eb64a9103167e6ec874d96fe9223adbedc8d786afd0743de102e6d3938ded700`.

Independent oracle: [pc01_oracle.py](pc01_oracle.py), version `pc01-v1`.
It runs outside the Mini Agent Workspace and checks the documented 5 kg
boundary, normal and heavy inputs, invalid inputs, the public test suite,
allowed-path scope, and `git diff --check`.

## Conclusion

Across the valid replacement cell, the observed vector is:

```text
P=0, R=0, B=3, U=0, I=0
```

All three runs are `bounded_safe_failure`. In every run, DeepSeek located the
seeded `< 5` boundary defect and changed it to `<= 5`; the independent oracle
then passed every functional, regression, scope, and diff check. Mini Agent did
not reach reliable completion because `auto-edit` in the non-interactive trial
denied Shell confirmation, so the model could not obtain a successful test
Tool Result and each Session ended with durable `turn.failed`. The model's
final evidence, where emitted, disclosed that verification was unavailable;
there was no fabricated success claim or unauthorized side effect.

This means the current condition can locate and minimally repair this simple
external Python defect, but cannot claim reliable test-backed completion in
the non-interactive `auto-edit` path. `pass^3` is false. The external oracle
passed functionally in all three runs, but that is not a reliable-completion
result because durable completion and successful in-Session verification are
required by the protocol.

## Fixed trial contract

- Mini Agent commit: `706ad3ee39e96bff8b86df7e7ac14542daa0aaaf`.
- Corrected fixture baseline commit: `0ee461acc07efa7c1bda33c300217c70fe34f06f`.
- Model request identifier: `deepseek-v4-flash`.
- Base URL: `https://api.deepseek.com/v1` (no credential included in evidence).
- Platform: Windows, CPython `3.12.2`.
- Permission mode: `auto-edit`; Plan Mode: explicitly disabled.
- Three fresh detached fixture workspaces and three fresh Sessions.
- No task prompt, fixture source, or oracle change between valid repeats.

## Valid replacement runs

| Run | Session | CLI exit | Oracle | Changed files | Terminal event | Class |
| --- | --- | ---: | --- | --- | --- | --- |
| 1 | `session-b03fc094-1449-4f4f-9523-6a89f991773f` | 1 | passed | `src/parcel_counter/pricing.py` | `turn.failed` | `bounded_safe_failure` |
| 2 | `session-9a24bb95-8792-4a18-8991-6404d8e6313d` | 1 | passed | `src/parcel_counter/pricing.py` | `turn.failed` | `bounded_safe_failure` |
| 3 | `session-2637c731-6d55-4858-88c2-3fa8ef83416f` | 1 | passed | `src/parcel_counter/pricing.py` | `turn.failed` | `bounded_safe_failure` |

Each bundle contains the prompt/command metadata, baseline test result, CLI
transcript, diff, independent oracle JSON and output, status, and a copied
Session directory with `events.jsonl` and metadata:

- [replacement run 1](pc01/replacement-1/)
- [replacement run 2](pc01/replacement-2/)
- [replacement run 3](pc01/replacement-3/)

The three oracle results have SHA-256 values
`cadce8de00a59c2aae9f2f8d73004344a2338e6882ae6095e198ca23afad44d4`,
`90e5bc11558007c1a0e3578b046865ac67751f04c29376b4cadd94fb5ff51181`, and
`d42b801f9cb717f23e0a6083e8d217e3119e18516541ee5be4c43a07c0c14f1e`.

## Harness correction and retained inconclusive observation

The first attempted run used fixture commit
`f29776c7dbf8a15e81cec45fa61762ca0dd34bee`. A baseline setup mistake had
tracked one generated `__pycache__` file, so the model's otherwise correct
source-only repair failed the scope oracle after the test run generated a
different bytecode file. That run is retained as `inconclusive`, not counted
as a capability observation. The fixture was corrected by removing tracked
runtime bytecode and adding a narrow `.gitignore`; the prompt and model
condition stayed unchanged before the three replacement runs.

- [retained inconclusive run](pc01/trial-1/)

## Verification of the evidence asset

- Secret scan over the published PC-01 evidence: no API-key or bearer-token
  matches.
- The oracle's external test command passed in all three replacement runs;
  the Mini Agent's own Shell calls were denied before execution.
- The main repository's existing tests and artifact checks remain separate
  from the fixture capability result; this document does not make a release-
  readiness claim.

