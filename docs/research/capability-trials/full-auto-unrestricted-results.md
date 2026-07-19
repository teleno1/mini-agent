# Full-Auto unrestricted-command trial results

Date: 2026-07-19

This is a follow-up experiment for [Issue #30](https://github.com/teleno1/mini-agent/issues/30). It is separate from the earlier bounded Full-Auto result and uses the same prompts, fixtures, independent oracles, and three-run aggregation.

## Experimental boundary

The disposable runner `.scratch/trial30/unrestricted_full_auto_runner.py` monkey-patched the permission gate only for this process. In `full-auto`, every Shell execution request bypassed confirmation with rule `full-auto-experimental-all-shell`. The Shell tool still enforced Workspace path validation, credential filtering, process bounds, timeout/output limits, and hard rejection of boundary escape, sensitive targets, catastrophic deletion, interactive processes, and detached processes. The repository's default Full-Auto policy and product source were not changed.

This is therefore an unrestricted permission-confirmation experiment, not a removal of host safety controls.

## Fixed cell

- Mini Agent commit under test: `0cc9386852aaf9e9a0d49b25da6b558fd0ab918a`
- Model: `deepseek-v4-flash`
- Provider: `https://api.deepseek.com/v1`
- Permission mode: `full-auto` with the disposable experimental runner
- Plan Mode: disabled
- Platform: Windows (`win32`), CPython 3.12
- PC-01 fixture: `e7f89df5f10491efb3101aab003141171123ae7a`
- RL-01/RL-02 fixture: `6f749dcc7298f3316af9f4a1730ad197e1b9946c`
- Every accepted run used a fresh workspace and Session. The contaminated preliminary RL-01 copy was excluded; clean reruns are under `trial30-unrestricted-v2`.

## Strict three-run aggregation

| Card | Fresh runs | Oracle pass | Scope/result summary | Session terminal states | Strict reliable completion |
| --- | ---: | ---: | --- | --- | ---: |
| PC-01 | 3 | 3/3 | Correct one-file source fix in all runs | 3 `turn.completed`, but each report retained unresolved observations | 0/3 |
| RL-01 | 3 | 2/3 | Run 1 corrupted output encoding and created `fix_fmt.py`/`update_files.py`; runs 2–3 passed | 3 `turn.failed` | 0/3 |
| RL-02 | 3 | 2/3 | Runs 1–2 passed; run 3 streamed no edit and oracle failed | 2 `turn.failed`, 1 `turn.completed` | 0/3 |

The strict reliable-completion column requires an independent oracle pass, exact scope, successful requested verification, and a normal truthful Session completion. No card reached that bar: PC-01's final oracle passed three times but all three Session reports retained unresolved observations; RL-01 and RL-02 also had failed or incomplete Sessions.

## Observations

- The original interpreter restriction was removed for this experiment: PC-01 ran `python -m unittest discover -s tests -v` successfully in all three runs; RL-01 and RL-02 also reached Python-based verification where the Session progressed far enough.
- PC-01 model behavior was stable: all three runs made only the intended `pricing.py` boundary fix, and the oracle passed.
- RL-01 was not stable under unrestricted Shell. In clean run 1, the model repeatedly attempted ad-hoc Python file rewrites after patch failures, created helper files, corrupted the display-label encoding, and failed visible tests and scope checks. Clean runs 2 and 3 passed the oracle.
- RL-02 produced correct source changes in runs 1 and 2. Run 3 ended during the model response after inspection, with no changes; the oracle correctly rejected it.
- The host still rejected commands containing detached-process/chaining hazards even with the experimental permission bypass. Those rejections are execution-layer hard safety controls, not the former Full-Auto confirmation gate.

Conclusion: removing the command-confirmation restriction allows interpreter verification and can improve completion, but it does not produce reliable end-to-end behavior across these cards. It also exposes materially riskier model behavior, including source corruption and generated helper files. The default bounded Full-Auto policy should remain unchanged.

## Raw evidence

- PC-01: `.scratch/trial30-unrestricted/pc01-exact/run-{1,2,3}/`
- RL-01: `.scratch/trial30-unrestricted-v2/rl01/run-{1,2,3}/`
- RL-02: `.scratch/trial30-unrestricted-v2/rl02/run-{1,2,3}/`
- Each accepted run contains `.mini-agent/sessions/<session-id>/events.jsonl` and `oracle.json`.
