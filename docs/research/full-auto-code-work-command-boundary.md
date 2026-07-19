# Full-Auto Code-Work Command Authorization Boundary

Date: 2026-07-19

## Question

How should Full-Auto recognize and authorize Code-Work Commands while keeping
the hard safety boundary intact?

## Evidence

- [OpenAI: Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)
  separates sandbox capability from approval policy; its workspace-write mode
  is workspace scoped, normally network-off, and the documented network proxy
  is allowlist-first when enabled.
- [Anthropic: Claude Code security](https://code.claude.com/docs/en/security)
  documents a complementary pattern: unmatched commands fail closed, network
  requests require approval, and previously allowlisted suspicious command
  injection still requires manual approval.
- [Anthropic: Claude Code settings](https://code.claude.com/docs/en/settings)
  shows exact command-shape rules such as `Bash(npm run lint)` and
  `Bash(npm run test *)`, together with deny rules for `curl` and secret paths.
- The repository's current `classify_shell_command` already performs the
  important ordering: sensitive target, syntactic ambiguity, command chaining,
  redirection, workspace escape, interpreters, network, installation,
  deletion, and Git writes are classified before its small recognized-local
  allowlist. `PermissionPolicyGate` auto-allows only `recognized-local` Shell
  calls in Full-Auto and hard-denies sensitive, boundary-escape, catastrophic,
  interactive, and detached hazards.

## Recommendation

Keep one deterministic, parse-before-authorize classifier and retain its
fail-closed default. A command is automatic only if it is one simple command
(no shell operator, redirection, substitution, variable expansion, or explicit
executable path), has a workspace-relative working directory, contains no
sensitive or escaping target, and exactly matches a local command form below.

| Class | Auto-authorize exact forms | Boundary |
| --- | --- | --- |
| Read-only | Existing listed readers and read-only `git` subcommands | No absolute/escaping/sensitive paths; no `find -exec`/`-delete`. |
| Test, lint, static analysis, formatting | `pytest`, `ruff check`, `ruff format --check`, `mypy`, and explicit project-local task forms | Arguments must contain no shell syntax, network, install, or write/deletion operation. Treat mutating formatting as an ordinary workspace write, not a Shell exception. |
| Build/package | Existing `make`, `ninja`, `cmake --build`, plus a finite project-tool allowlist | Permit only build/test/check targets and workspace-relative output paths. |
| Declared dependency resolution | A lockfile-aware resolver invocation for the repository's declared package manager, such as `uv sync --frozen` or an equivalent frozen/offline form | Permit only when its lock/manifest is present and unchanged for the Session; network remains off by default. A missing lock, a lock update, `install`, `add`, `remove`, global/user location, scripts, or arbitrary registry configuration requires confirmation. |
| Code generation | A finite list of repository-declared generator entry points and fixed safe subcommands | Generator config and output directories must be workspace-relative and validated before execution. Arbitrary `python -c`, `node -e`, `sh -c`, `powershell -Command`, package-runner hooks, and generated command strings remain confirmation-gated. |

## Always require focused confirmation

- Any command chain/pipeline, redirection, command substitution, variable or
  environment expansion, ambiguous quoting, explicit executable path, unknown
  executable, or unsupported argument shape.
- Network clients; network-enabled resolution of already declared dependencies
  can be a separate explicit confirmation, scoped to the resolver and approved
  registry destinations. It must not become a general network grant.
- Dependency changes, package installation outside the declared locked
  environment, lifecycle scripts, code-generation configurations outside the
  finite allowlist, and mutating formatter invocations.
- Git state changes other than read-only queries, or any operation that might
  write protected files.

## Always hard-deny

- Credential or sensitive-target access, workspace escape, catastrophic
  deletion/overwrite, interactive or detached jobs, Git commit/push and other
  publishing, and download-and-execute patterns.

Hard denial must take precedence over session grants and all automatic rules.
The existing ordering and `PermissionPolicyGate` already have this shape.

## Minimal implementation path

1. Preserve `ShellCommandClass` and `PermissionPolicyGate` as the sole
   authorization seam; do not add model-prompt exceptions or a broad shell
   allowlist.
2. Make the recognized-local table data-driven, with separately testable
   command, subcommand, argument, manifest/lockfile, and output-path validators.
3. Add command classes for declared-dependency resolution and declared code
   generation. Their validators must receive the validated Workspace context;
   string classification alone cannot prove a lockfile or generator target.
4. Keep the existing pre-classification hard denies. Add table-driven tests for
   every positive form and one adversarial variant per form (pipe, redirect,
   environment expansion, escape, sensitive path, unknown flag, and network).
5. Record the resulting class and rule in durable permission evidence, as the
   current policy already does.

## Domain-model check

The existing glossary's **Code-Work Command** is the right umbrella term. For
implementation, distinguish it from a **recognized Code-Work Command**: the
latter is a fully parsed, bounded command shape whose workspace and
declaration-dependent preconditions have been validated. Full-Auto authorizes
only the recognized subset; it is not a general permission to run commands.
