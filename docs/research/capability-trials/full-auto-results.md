# Full-Auto code-capability trial results

Date: 2026-07-19

This report resolves the execution question in [Issue #30](https://github.com/teleno1/mini-agent/issues/30). It replaces the earlier non-interactive `auto-edit` comparison; its results are not mixed with that cell.

## Fixed cell

- Mini Agent commit under test: `0cc9386852aaf9e9a0d49b25da6b558fd0ab918a`
- Model: `deepseek-v4-flash`
- Provider: `https://api.deepseek.com/v1`
- Permission mode: `full-auto`
- Plan Mode: disabled
- Platform: Windows (`win32`), CPython 3.12
- Each run used a fresh workspace, fresh Session, unchanged task prompt, and the independent oracle outside the workspace.
- The original PC-01 fixture Git object was not present in the current checkout. Its public files were recovered exactly from retained prior Session evidence and committed as disposable fixture baseline `e7f89df5f10491efb3101aab003141171123ae7a`; this provenance caveat is retained rather than silently presenting a different hash as the historical fixture.
- RL-01/RL-02 fixture commit: `6f749dcc7298f3316af9f4a1730ad197e1b9946c`.

## Strict aggregation

| Card | Fresh runs | Oracle pass | Scope result | Session result | Reliable completion |
| --- | ---: | ---: | --- | --- | ---: |
| PC-01 | 3 | 3/3 | Only `src/parcel_counter/pricing.py` changed | Python verification was denied; Sessions ended with unresolved work or `turn.failed` | 0/3 |
| RL-01 | 3 | 3/3 | Only `src/reading_list/formatting.py` and `src/reading_list/store.py` changed | Python verification was denied; Sessions ended with unresolved work or `turn.failed` | 0/3 |
| RL-02 | 3 | 3/3 | Only `src/reading_list/formatting.py` and `src/reading_list/models.py` changed | Python verification was denied; Sessions ended with unresolved work or `turn.failed` | 0/3 |

## Interpretation

The model reliably selected the correct source changes in all nine valid runs, and every independent oracle passed. Full-Auto automatically authorized bounded local reads and source writes, but did not automatically authorize the requested Python interpreter-based verification. Therefore no card meets the strict definition of reliable completion: all nine runs are `bounded_safe_failure`, not success.

No tests, documentation, packaging, lockfiles, credentials, external paths, or unsafe side effects were changed in the trial workspaces. The external oracles ran after each Session and passed all required behavioral, visible-test, scope, and diff checks.

## Raw evidence

Raw Session JSONL and oracle outputs are retained locally under `.scratch/trial30/pc01-exact/`, `.scratch/trial30/rl01/`, and `.scratch/trial30/rl02/`. Each run contains one `.mini-agent/sessions/<session-id>/events.jsonl` and one `oracle.json`.
