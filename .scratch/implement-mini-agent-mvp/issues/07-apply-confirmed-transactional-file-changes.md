# 07 - Apply confirmed transactional file changes

**What to build:** The Agent can propose exact, reviewable file additions and edits, obtain the required permission, apply them transactionally, and report or roll back known failures without escaping the Workspace.

**Blocked by:** 04 - Assemble trusted configuration, instructions, and Context Frames; 06 - Let the Fake-driven Agent read and search a confined Workspace.

**Status:** ready-for-agent

- [ ] Implement exact Add, Update, and Delete patch operations with no fuzzy application, at most 10 files and 256 KiB per call.
- [ ] Implement single-file creation with UTF-8 bounds, optional parent creation, and strict no-overwrite behavior.
- [ ] Reject binary, sensitive, external-link, reparse, and path-race cases; recheck approved targets and argument hashes immediately before commit.
- [ ] Prepare same-filesystem temporary content, create Checkpoints, atomically replace targets, and roll back ordinary partial failures with explicit evidence.
- [ ] Implement suggest and auto-edit write behavior, always asking for delete and Protected Path writes through a focused confirmation interaction.
- [ ] Support allow once and exact-for-Session grants; any Tool, target, command, working-directory, or argument change invalidates the grant.
- [ ] Persist redacted Permission Decisions and normalize success, invalid, denied, failed, cancelled, and interrupted Tool Result outcomes into the agreed terminal events.
- [ ] Tests cover multi-file success, validation-before-write, rollback, create collision, protected resources, grant matching, and simulated interrupted commits.
