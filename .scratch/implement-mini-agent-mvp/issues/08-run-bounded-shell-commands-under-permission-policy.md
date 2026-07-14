# 08 - Run bounded Shell commands under Permission Policy

**What to build:** The Agent can run recognized local development commands with cross-platform time/output limits while unknown or risky commands require an explainable user decision and credentials remain unavailable to the child process.

**Blocked by:** 04 - Assemble trusted configuration, instructions, and Context Frames; 06 - Let the Fake-driven Agent read and search a confined Workspace.

**Status:** ready-for-agent

- [ ] Run PowerShell on Windows and a POSIX shell on macOS/Linux from a validated Workspace-relative working directory.
- [ ] Filter Provider credentials and unrelated sensitive environment values; reject model-provided environment overrides, interactive programs, and detached/background jobs.
- [ ] Enforce the agreed default and maximum timeouts, bounded stdout/stderr, exit-code/duration results, and process-group lifecycle.
- [ ] Implement exact explainable classification for recognized local read/build/test commands and ask for chaining, redirection, interpreters, network, installs, Git writes, deletion, and unknown executables.
- [ ] Enforce suggest, auto-edit, and full-auto behavior without presenting full-auto as unrestricted host access.
- [ ] Apply cooperative interrupt then platform-specific escalation; close output readers with the process and classify uncertain termination as interrupted.
- [ ] Contract and integration tests cover quoting ambiguity, working-directory bounds, environment filtering, output truncation, timeout, cancellation, process trees, and permission audit.
