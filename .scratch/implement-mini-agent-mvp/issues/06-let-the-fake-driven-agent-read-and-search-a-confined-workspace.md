# 06 - Let the Fake-driven Agent read and search a confined Workspace

**What to build:** A Fake-driven coding Turn can request bounded file reads and repository searches while the host confines every target to the selected Workspace and records a complete Tool lifecycle.

**Blocked by:** 03 - Persist, list, and resume text-only Sessions.

**Status:** in-progress

- [ ] Define the shared typed Tool, Risk Assessment, Tool Call, Tool Result, registry, and lifecycle contracts without UI or permission prompting inside Tools.
- [ ] Resolve one real Workspace root and reject absolute, drive-changing, UNC, traversal, device, binary, sensitive, and out-of-bound targets with platform-correct comparisons.
- [ ] Permit reads through links only when the resolved target remains inside the Workspace; never reveal sensitive content through denial details.
- [ ] `read_file` implements UTF-8/BOM handling, line/range continuation, and the agreed 500-line/64-KiB limits.
- [ ] `search_files` supports literal/regex, directory, and glob; uses `rg` without a Shell when present, safely falls back to Python, and honors result limits and ignored targets.
- [ ] Safe reads/searches are automatically authorized and their proposed, validated, started, and terminal events yield exactly one Tool Result per persisted Tool Call.
- [ ] Unit, contract, and temporary-Workspace integration tests cover Windows/POSIX path cases, symlinks/reparse behavior, truncation, and Fake-driven model adaptation to failures.
