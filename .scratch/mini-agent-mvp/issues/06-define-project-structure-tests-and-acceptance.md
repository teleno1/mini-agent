# Define project structure, tests, and MVP acceptance

Type: grilling
Status: resolved
Blocked by: 02, 03, 04, 05

## Question

What package/module boundaries, dependency directions, test seams, CI matrix, manual real-model scenarios, and observable acceptance criteria will prove the Mini Agent MVP is implementation-ready and complete without expanding beyond the agreed scope?

## Answer

### Package structure and dependency direction

Use one installable `mini_agent` package in a `src` layout rather than multiple distributable packages:

```text
src/mini_agent/
├── cli/              # Typer commands, Rich rendering, interactive input
├── application/      # Agent Loop, Turn orchestration, use cases and ports
├── domain/           # Pure messages, events, plans, permissions and rules
├── providers/        # Model Provider adapters
├── tools/            # File, search, patch and Shell adapters
├── sessions/         # JSONL, Artifacts, locking, recovery and projections
├── context/          # Context Frame assembly, estimation and compaction
├── instructions/     # AGENTS.md discovery and composition
└── config/           # Configuration loading and validation
```

`domain` depends on no terminal, HTTP, filesystem, or Provider implementation. `application` orchestrates domain behavior through ports and directly imports neither Rich, httpx, nor operating-system APIs. The CLI and infrastructure adapters depend inward on `application` and `domain`. Infrastructure modules do not freely import one another; cross-boundary workflows are coordinated by application use cases. The CLI owns presentation only, never Agent decisions.

### Application ports and test seams

Application-defined protocols cover only genuine external boundaries: `ModelProvider`, `ToolRegistry`, `PermissionGate`, `SessionStore`, `ContextBuilder`, `Compactor`, `InstructionLoader`, `Clock`, `IdGenerator`, `Workspace`, and `UserInteraction`. Avoid an interface for every class.

Tests use a scripted `FakeModelProvider`, in-memory `SessionStore`, temporary-directory `Workspace`, scripted `UserInteraction`, fake clock, and deterministic ID generator. Real filesystem behavior is exercised only where it is the subject of the test.

### Test layers

- Unit tests cover pure state transitions, permissions, path normalization, event projections, Plan invariants, configuration merging, and compaction thresholds.
- Contract tests run shared expectations against fakes and adapters for Provider streams, Tools, Session Event schemas, and structured compaction output.
- Integration tests use real temporary files for Tools, Patch rollback, JSONL, Session locking, tail repair, Artifacts, Resume, and `AGENTS.md` discovery, while retaining a fake model.
- A small number of CLI integration tests use Typer's runner and scripted interaction for critical user journeys. They assert semantic output and choices rather than broad ANSI snapshots.

No arbitrary coverage percentage is required. Every branch that protects permissions, state transitions, Workspace confinement, persistence integrity, or interruption recovery must be tested. Tests are offline, deterministic, and independent of developer-machine state.

### CI matrix and gates

Run Python 3.12 on Ubuntu, Windows, and macOS, plus Python 3.13 on Ubuntu as a forward-compatibility check. Every pull request runs locked development dependency installation, `ruff` lint and format checks, `mypy`, the offline pytest suite, wheel and source-distribution builds, installation of the wheel into a clean environment, `mini-agent --help`, and one Fake Provider smoke journey.

Tests require no API key or real model. Windows specifically exercises casing, separators, and process interruption; Unix platforms exercise symlinks and permission bits. Failure on any required matrix entry blocks merging.

### Manual real-model acceptance

Real-model calls are release-candidate checks, never CI requirements. Using a temporary Git repository and restricted test credential, manually verify: read-only exploration; confirmed patch plus tests; denial and replanning; hazardous Shell authorization; large-output Artifact creation; long-session compaction; forced exit and conservative Resume; changed `AGENTS.md`; rate limit, stream interruption, and invalid Tool Call handling; and one complete read-modify-test-report journey on each supported OS.

Record date, Provider, model, scenario, Session ID, result, and failure notes without credentials or sensitive full logs.

### Observable MVP acceptance

The MVP is complete when an installed wheel exposes a working interactive `mini-agent` command; streams responses; runs bounded file, search, patch, and Shell Tools under Workspace and Permission Policy constraints; validates and audits Tool Calls; shows Plans for complex work; applies effective `AGENTS.md`; writes continuous JSONL events and Artifacts; resumes interrupted Sessions without replaying uncertain Tools; compacts context without losing observable task state; reports Provider and Tool failures honestly; lists and resumes Sessions; and finishes with outcome, verification, changed files, and unresolved work.

Acceptance measures architecture behavior and safety boundaries, not whether a particular model solves every arbitrary coding task.

### Definition of done

Each implementation change must match its recorded contract; test happy, denial, failure, and applicable interruption paths; use deterministic dependencies; evolve schemas and fixtures together; and update user documentation for visible behavior. Every new Tool includes input schema, risk description, permission behavior, result bounds, and contract tests. `ruff`, `mypy`, offline pytest, package build, and wheel smoke checks must pass. A release candidate also requires the recorded manual real-model checklist; a one-off successful model run never replaces automated checks.
