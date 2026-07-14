# Design prompt assembly and configuration precedence

Type: grilling
Status: resolved
Blocked by: 03, 04, 05

## Question

How should Mini Agent construct each ContextFrame from system policy, `AGENTS.md`, session state, Plan, Context Summary, recent messages, and Tool definitions, and what explicit precedence should environment variables, user configuration, project configuration, CLI flags, and in-session changes have without allowing lower-trust sources to override safety policy?

## Answer

### Context Frame layers

Build every Context Frame from typed, provenance-carrying layers rather than concatenating an opaque prompt:

1. Non-overridable system safety policy.
2. Mini Agent's core behavior contract and current Permission Policy.
3. Structured definitions for the Tools currently available.
4. The effective `AGENTS.md` instruction chain for the relevant Workspace paths.
5. Structured Session state: the latest valid Context Summary, current Plan, and any recovery or interruption notice.
6. Necessary conversation and Tool events after the Summary Boundary.
7. The current user message.

This order describes presentation, not a last-write-wins permission scheme. Lower-trust content never overrides a higher-trust safety constraint. Every layer retains its source, content hash, and estimated token count. Provider Adapters translate the canonical layers to a model API without changing their authority.

Tool Definitions use the Provider's machine-readable function/tool schema rather than duplicated prose. Context Summaries report historical facts but never become privileged instruction sources. Tool Results, Artifacts, source files, prior model output, and ordinary repository documentation remain untrusted task content even when they contain instruction-like language.

### Project instruction discovery

The Workspace-root `AGENTS.md` supplies project-wide instructions. For a Tool Call that targets a path, load additional `AGENTS.md` files from the Workspace root toward the target's parent directory. Nearer files may refine or override ordinary project conventions, but none may relax system safety, Workspace confinement, Protected Path rules, or the Permission Policy.

For a multi-path call, calculate effective instructions for each target. A material conflict blocks automatic execution and asks the user to resolve it. Do not load instructions outside the Workspace; resolve symbolic links before determining scope. Missing files are normal, while unreadable, oversized, or invalidly encoded files produce visible warnings rather than silent omission. Record the ordered relative paths and hashes actually used.

Only files named exactly `AGENTS.md` discovered by this algorithm enter the project-instruction layer. README files, code comments, fixtures, and other Markdown never gain instruction authority. `AGENTS.md` may govern coding and workflow conventions but cannot widen access, approve dangerous actions, request secrets, or alter system policy. It is a Protected Path; edits require explicit confirmation and affect only future Context Frames.

### Configuration precedence and trust classes

For ordinary user-tunable settings, precedence is:

```text
built-in default
< user configuration
< project configuration
< environment variable
< CLI argument
< explicit Session override
```

This chain is subordinate to immutable safety ceilings. Workspace confinement, Protected Paths, dangerous-command rules, and redaction cannot be loosened by configuration. Credentials and connection destinations follow stricter rules: the API Key comes only from `MINI_AGENT_API_KEY`; the Provider Base URL may come from user configuration, environment, or CLI, but never project configuration or `AGENTS.md`. Project configuration may select non-secret preferences such as model, context thresholds, and test commands.

Use strict TOML configuration at the platform-standard user config directory and `<workspace>/.mini-agent/config.toml` for the project. Environment variables use the `MINI_AGENT_` prefix. A missing file is normal; an existing file with invalid TOML, unknown keys, or wrong types fails with its source and field identified. The resulting immutable Effective Configuration retains each field's value, source, and any applied safety constraint. `mini-agent config show` displays values and provenance while revealing only whether a secret is set.

The MVP does not implement a Keychain or store an API Key in TOML. It reads the environment value into memory, redacts it from output and persistence, and removes unrelated secrets from Shell environments. A missing Key is reported before the first real request with setup guidance; users are not encouraged to paste and persist it in chat.

### Context budgeting

Never trim system safety, Permission Policy, the Tool Definitions actually in use, the current user message, or an unfinished Tool Call and its required protocol pair. Prefer the effective `AGENTS.md` chain, latest Context Summary, current Plan, and newest complete Turns.

When space is tight, replace large Tool Results with Artifact references, remove superseded Plans and repeated operational state, structurally compact history before the Summary Boundary, then omit irrelevant older dialogue already represented by durable history. If the frame still cannot fit, fail explicitly rather than dropping safety or the current request.

A single `AGENTS.md` defaults to a 32 KiB limit and the effective chain to 128 KiB; users may tighten those limits. Never silently truncate instructions because truncation can change their meaning. Context manifests keep per-layer estimates, while the ordinary CLI hides detailed token counts unless pressure or diagnostics make them useful.

### Session overrides

Explicit commands such as `/config model`, `/config permission`, and `/config verbosity` change only the current Session and append `session.config_changed` with non-secret old and new values. An override begins with the next request or Tool Call and does not mutate a Context Frame already sent. Model, temperature, permission mode, and presentation detail are allowlisted; a stricter permission mode applies immediately, while a less restrictive one requires explicit confirmation and remains capped by system safety.

Provider Base URL, API Key, Workspace root, and Session storage directory cannot change inside an active Session. `/config reset <field>` removes only the Session override. Resume restores the last valid Session overrides, then reapplies the current system policy and current project instructions.

### Audit and Provider mapping

Each model request records a Context Manifest: frame ID; layer type, source, hash, and token estimate; effective `AGENTS.md` paths and hashes; Effective Configuration hash; non-secret Provider and model parameters; Summary Boundary; and included message or Tool Event sequence ranges. It does not duplicate full prompts, project instructions, messages, or secrets. Built-in prompts are identified by application and prompt-template versions. Resume reports changed hashes rather than claiming byte-for-byte reproduction.

When supported, map system safety to `system` and core/project instructions to `developer`. A Provider with only a reliable `system` role receives a fixed, explicitly delimited merge of those layers. Tool Definitions always use structured function calling. A Provider without reliable system instructions or structured Tool Calls is incompatible with Agent mode; do not emulate execution by parsing free-form JSON or Shell text. Contract tests verify role mapping and tool/message pairing.

### Core prompt contract

The versioned core prompt stays small and stable. It defines the Coding Agent's responsibility, Workspace boundary, Agent Loop completion behavior, structured Tool use, Permission Policy obedience, honest observation and verification, error and Resume reporting, visible Plan behavior, final reporting, and the rule that hidden chain-of-thought is neither requested nor persisted. Project style, Provider details, concrete configuration values, Tool tutorials, and rules already enforceable by local code do not belong in it. Prompt changes require review and scenario regression but have an independent `prompt_version` rather than being coupled to every application release.
