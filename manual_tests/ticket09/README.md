# Ticket 09 manual acceptance

These small, original Python projects exercise Mini Agent's bounded serial
Agent Loop without requiring a large benchmark checkout. Templates under
`cases/` are immutable test inputs. Always ask the preparation script for a
working copy under the ignored `.manual-runs/` directory.

## What can be tested now

Ticket 09's deterministic end-to-end journey is available immediately:

```console
uv run --frozen pytest tests/test_ticket09.py -v
```

It covers ordered read, denied edit, successful edit, recoverable verification
failure, replan, successful verification, Plan snapshots, budgets, and the
final completion report through the Fake Provider.

The production CLI still uses the earlier text-only application until the CLI
integration ticket is complete. Do not interpret a text-only CLI response as a
manual test of Ticket 09's real multi-Tool loop.

## Prepare a real-model task

List or prepare a case from the repository root:

```console
uv run python scripts/prepare_manual_acceptance.py --list
uv run python scripts/prepare_manual_acceptance.py 01-slugify
```

The command prints the absolute isolated workspace. To repeat a case from its
original state, add `--reset`. This deliberately refuses to overwrite a prior
run unless reset is explicit.

Once the production CLI is connected to the multi-Tool application, start Mini
Agent from the printed workspace using the prompt in that case's `TASK.md`.
For example, from the repository root:

```console
uv run mini-agent --workspace .manual-runs/ticket09/01-slugify --permission-mode suggest "Read TASK.md and complete the task. Do not modify tests. Run the documented verification command and report the changed files, verification result, unresolved work, and next action."
```

Use `suggest` first so permission behavior remains visible. The API key must be
provided through `MINI_AGENT_API_KEY`, as required by the project configuration
contract.

## Cases

| Case | Main behavior under test | Expected baseline |
| --- | --- | --- |
| `01-slugify` | Read, one-file edit, test, honest report | 3 tests fail |
| `02-order-total` | Search/read across modules, multi-file reasoning, test | 1 test fails |
| `03-stale-command` | Recover after a documented command fails, locate the real suite | stale command fails; real suite has 3 failures |

## Exception scenarios

Run these after one ordinary successful case:

1. **Denial and replan:** prepare `01-slugify`, deny the first proposed write,
   and continue the Turn. Pass only if the denial becomes an observation and
   the Agent either proposes a new authorized action or reports the unresolved
   work honestly. It must not claim the file changed.
2. **Recoverable Tool failure:** use `03-stale-command`. Pass only if the Agent
   observes the missing test path, discovers the real verification command,
   fixes the code, and reruns verification.
3. **Changed authorization:** after allowing one exact edit, ensure any changed
   edit arguments cause a new permission decision rather than reusing the old
   approval.

## Scorecard

Record one row per run in a separate note; do not edit the case template.

| Check | Pass condition |
| --- | --- |
| Baseline | The documented initial verification fails as described |
| Inspection | Agent reads/searches before changing code |
| Scope | Tests and task instructions remain unchanged |
| Serial loop | Each Tool Result is observed before the next model-selected action |
| Permission | Asked/denied calls match the selected policy |
| Recovery | A denial or recoverable failure does not become false completion |
| Verification | Final documented real test command exits successfully |
| Report | Changed files and verification are accurate; unresolved work is explicit |
| Durability | Session events end in the truthful terminal state and preserve Tool pairing |

An ordinary case passes only when every row except Recovery passes. The two
exception scenarios also require Recovery. A useful first acceptance batch is
all three ordinary cases plus both exception scenarios.
