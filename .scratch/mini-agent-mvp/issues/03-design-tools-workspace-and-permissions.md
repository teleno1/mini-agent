# Design tool contracts, workspace confinement, and permissions

Type: grilling
Status: resolved
Blocked by: 02

## Question

What exact contracts should the read, search, patch, create-file, and shell tools expose, and how should the `suggest`, `auto-edit`, and `full-auto` policies classify, confirm, audit, or reject each call while enforcing cross-platform workspace confinement?

## Answer

### Shared Tool contract

Every Tool has a stable name and description, a Pydantic input model, side-effect category (`read`, `write`, or `execute`), cancellation and limit metadata, `risk(args, context) -> RiskAssessment`, and `execute(args, context) -> ToolResult`. Tools report facts and risk but never prompt or interpret the current permission mode. A separate Permission Engine combines the immutable normalized arguments, Risk Assessment, and Permission Policy to produce `allow`, `ask`, or `deny`.

### File and search tools

`read_file` accepts a Workspace-relative path plus optional starting line and line limit. It reads ordinary UTF-8 text (including UTF-8 BOM), returns at most 500 lines or 64 KiB with an explicit continuation point, and rejects directories, devices, binary files, sensitive targets, links outside the Workspace, and unsupported encodings.

`search_files` accepts a query, literal-or-regex mode, optional relative directory, and optional glob. It invokes `rg` through an argument vector without a Shell and falls back to Python when `rg` is absent. It ignores `.git`, `.mini-agent`, binary and sensitive targets, returns at most 200 matches or 64 KiB with relative path, line number, preview, and truncation metadata, and never follows links outside the Workspace.

Both tools are automatically allowed for safe Workspace targets in every mode. Sensitive and out-of-bound targets are denied rather than offered for confirmation.

### Patch and create tools

`apply_patch` uses a restricted text protocol with Add, Update, and Delete operations. Every path is Workspace-relative; update hunks require exact current context and never apply fuzzily. One call affects at most 10 files and accepts at most 256 KiB. Binary files, sensitive paths, links, and boundary escapes are rejected. The full patch is parsed, validated, risk-assessed, and authorized before any write.

A Patch Transaction provides logical all-or-nothing behavior, not database-style crash atomicity. New content is prepared and validated in same-filesystem temporary files, targets are atomically replaced one by one, and ordinary failures roll back through a Checkpoint. A process crash can leave a partial commit; recovery marks the call interrupted and reconciles actual files instead of retrying. Results include per-file line counts and a reviewable diff.

`create_file` accepts one relative path and UTF-8 content up to 256 KiB. It fails if the target exists, may create missing parent directories as part of the same Patch Transaction, and never overwrites. It exists as a convenient narrow operation even though `apply_patch` can add multiple files.

In `suggest`, all writes ask. In `auto-edit` and `full-auto`, ordinary additions and modifications are automatic. Deletions and writes to protected paths always ask.

### Shell tool

`shell` accepts a command string, optional Workspace-relative working directory, and optional timeout. It selects PowerShell on Windows and a POSIX shell on macOS/Linux. The default timeout is 120 seconds and the MVP maximum is 10 minutes. Cancellation and timeout terminate the process tree. Interactive programs, detached/background jobs, and arbitrary model-provided environment overrides are unsupported. The child inherits a filtered environment that does not expose provider credentials.

The Tool returns bounded stdout/stderr, exit code, and duration; at most 64 KiB enters model context while full output is stored as a session asset. Built-in file tools enforce path confinement, but Shell is explicitly not an OS sandbox.

Shell automation uses exact, explainable rules. `full-auto` may automatically run only complete recognized local read/build/test patterns such as `git status`, `git diff`, `git log`, `pytest`, `python -m pytest`, `ruff check`, and `mypy`. Pipelines, redirection, command substitution, command chaining, ambiguous quoting, deletion, Git history changes, network clients, installs, arbitrary interpreters, and unknown executables ask. Rules record their match reason.

### Permission modes

| Operation | `suggest` | `auto-edit` | `full-auto` |
| --- | --- | --- | --- |
| Safe read/search | allow | allow | allow |
| Ordinary add/update | ask | allow | allow |
| Delete or protected write | ask | ask | ask |
| Recognized local read/build/test Shell | ask | ask | allow |
| Network, install, Git write, arbitrary interpreter, redirection, unknown Shell | ask | ask | ask |
| Credential access, file boundary escape, catastrophic root/home deletion | deny | deny | deny |

`full-auto` therefore means low-risk local development automation, not unrestricted host authority.

### Workspace and protected resources

Mini Agent resolves and fixes the real Workspace root at startup. File tools accept only relative paths and reject absolute paths, drive changes, UNC paths, and escaping `..`. Existing targets are resolved through symlinks/junctions and checked with platform-correct case behavior. Reads may follow links only to Workspace-internal targets; writes and deletes reject any link/reparse component. New paths validate their nearest existing parent and recheck immediately before commit. Windows comparisons normalize drive and case; POSIX remains case-sensitive.

Hard-denied model targets include `.mini-agent/`, real `.env` files, private keys, cloud credentials, and system credential stores; `.env.example` and `.env.template` remain ordinary project files. `AGENTS.md`, `.git/`, CI/CD configuration, lockfiles, and security-policy files may be read, but writing always asks. Denials do not reveal sensitive content or unnecessary existence information.

### Authorization and audit

Evaluation order is: schema validation; path normalization and sensitive-target checks; immutable Risk Assessment; hard denies; exact Session allow; permission-mode default; user confirmation; final path recheck and approved-argument hash comparison. Any parameter change creates a new Tool Call and requires a new decision.

Confirmation supports `allow once` and `allow exact for session` only. The latter matches Tool plus exact normalized target path, or exact normalized Shell command plus working directory. The CLI displays the exact rule before approval; temporary rules disappear when the Session ends.

Every Tool Call records a redacted `PermissionDecision` Session Event containing Tool Call ID, tool, risk category, mode, decision, matched rule ID/reason, confirmation scope, normalized resource or command summary, argument hash, and timestamp. Raw contents, complete outputs, keys, and suspected secrets never enter audit fields.
