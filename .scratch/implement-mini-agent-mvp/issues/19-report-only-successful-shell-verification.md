# 19 - Report only successful Shell verification

**What to build:** Completion Reports distinguish verification actually performed successfully from attempted commands that were denied, failed, timed out, cancelled, or left uncertain. Users receive an honest outcome, unresolved work, and relevant next action without failed commands being presented as verification evidence.

**Blocked by:** None - can start immediately.

**Status:** ready-for-agent

- [ ] A Shell command appears in Completion Report verification only when its Tool Result outcome is successful.
- [ ] Denied, invalid, failed, timed-out, cancelled, and interrupted Shell attempts are excluded from verification and represented in unresolved work with their observable outcome.
- [ ] When no successful verification exists, the report states that verification is unavailable and recommends the relevant safe next action.
- [ ] Mixed journeys report successful verification separately from unsuccessful attempts without losing either observation.
- [ ] Changed files and overall outcome do not imply verified success when every attempted verification command was unsuccessful.
- [ ] Domain/application and semantic CLI tests cover successful, denied, failed, timed-out, cancelled, interrupted, and mixed Shell results.

## Completion evidence

Record concrete test names and rendered Completion Report assertions here before marking this ticket completed.
