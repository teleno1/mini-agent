# 18 - Preserve unknown Tool calls as invalid results

**What to build:** When a Model Provider requests an unknown Tool, the Coding Agent records one protocol-linked invalid Tool Result and returns that observation to the model so it can recover. An ordinary model-selected name error must not become an internal Turn failure or execute any side effect.

**Blocked by:** None - can start immediately.

**Status:** ready-for-agent

- [ ] An unknown Tool name terminates its persisted Tool Call exactly once with outcome `invalid` and a stable validation category.
- [ ] No Tool implementation, permission confirmation, Workspace mutation, or Shell process is invoked for the unknown call.
- [ ] The invalid Tool Result remains linked to the original Tool Call ID and is included in the next derived Context Frame.
- [ ] The Agent Loop can accept a corrected Tool Call or a normal final response after the invalid observation without failing the Turn.
- [ ] Normal Turn execution and interrupted-work retry paths do not perform a second registry lookup that converts the invalid result into an exception.
- [ ] Deterministic Fake Provider tests verify durable event ordering, correction recovery, budget accounting, and absence of side effects for unknown Tool names.

## Completion evidence

Record concrete test names, event sequences, and side-effect assertions here before marking this ticket completed.
