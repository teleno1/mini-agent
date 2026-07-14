# Mini Agent

Mini Agent is a small, inspectable terminal coding agent for learning how an
agent loop is put together. The first slice runs entirely offline with a
scripted Fake Model Provider:

```console
mini-agent "Explain Mini Agent"
```

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

The runtime dependencies are intentionally limited to Typer, Rich, Pydantic,
and httpx. No API key or Git repository is needed for the offline Fake
Provider journey.
