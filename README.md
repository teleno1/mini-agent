# Mini Agent

Mini Agent is a small, inspectable terminal coding agent for learning how an
agent loop is put together. Production task execution requires a Provider API
key from the `MINI_AGENT_API_KEY` environment variable:

```console
set MINI_AGENT_API_KEY=your-provider-key
mini-agent "Explain Mini Agent"
```

The deterministic Fake Model Provider is composed explicitly by automated
tests and the artifact smoke journey; it is never selected by the production
CLI when authentication is absent.

Run without a task to enter an interactive Session. Each turn is stored as
UTF-8 JSONL under `.mini-agent/sessions`. List and continue Sessions with:

```console
mini-agent sessions
mini-agent resume SESSION_ID "Continue the task"
```

The supported views are `init`, `sessions`, `resume`, `config show`, and
`doctor`. In an interactive Session, `/config show`, `/config set key=value`,
`/config reset`, `/sessions`, and `/exit` are available. Set
`MINI_AGENT_API_KEY` to use the configured OpenAI-compatible Provider; the key
is never read from TOML or displayed by `config show`.

It is an independent educational project. It is not Claude Code, is not
Anthropic software, and does not promise Claude Code compatibility.

## Development

This project uses Python 3.12+, [uv](https://docs.astral.sh/uv/), and
[Hatchling](https://hatch.pypa.io/):

```console
uv sync
uv run pytest
uv run mini-agent --help
```

The locked development environment can be reproduced from a clean checkout:

```console
uv sync --frozen --all-groups
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest
```

Build and verify local, unpublished artifacts with:

```console
uv run --frozen python scripts/build_artifacts.py
uv run --frozen python scripts/smoke_artifacts.py
```

The build produces one pure-Python wheel, one source distribution, and a
`SHA256SUMS` file under `dist/`. Both artifacts are checked for the required
package metadata and can be installed without a source checkout.

The runtime dependencies are intentionally limited to Typer, Rich, Pydantic,
and httpx. Help, version, initialization, Session listing, configuration
inspection, and doctor diagnostics work without an API key. Session history is
authoritative in `events.jsonl`; listing metadata is rebuilt from that history.
