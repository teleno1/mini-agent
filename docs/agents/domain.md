# Domain Docs

How engineering skills should consume this repository’s domain documentation.

## Before exploring

Read these files when they exist:

- `CONTEXT.md` at the repository root.
- `docs/adr/` entries relevant to the area being changed.

If these files do not exist, proceed silently. Domain-modeling workflows create them lazily when terminology or architectural decisions are resolved.

## Layout

This repository uses a single-context layout:

/
├── CONTEXT.md
├── docs/adr/
└── src/

## Vocabulary

Use terms defined in `CONTEXT.md` when naming domain concepts. Avoid synonyms that the glossary explicitly rejects.

If a necessary concept is absent, reconsider whether the project uses another term or note the gap for domain modeling.

## ADR conflicts

If proposed work contradicts an existing ADR, surface that conflict explicitly instead of silently overriding the decision.
