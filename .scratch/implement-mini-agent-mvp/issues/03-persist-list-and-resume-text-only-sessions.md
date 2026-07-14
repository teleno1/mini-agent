# 03 - Persist, list, and resume text-only Sessions

**What to build:** A text-only conversation survives process exit as an authoritative append-only Session, can be listed, and can be resumed without pretending transient runtime state was persisted.

**Blocked by:** 01 - Bootstrap an installable text-only Agent.

**Status:** ready-for-agent

- [ ] Store typed UTF-8 JSONL Session Events with schema version, stable IDs, contiguous sequence, causation, timestamps, and rebuildable projections.
- [ ] Persist user messages, model-request lifecycle, complete assistant messages, and Turn terminal events with exactly one exclusive Session writer.
- [ ] Append complete lines and flush/fsync required state before advancing to the next durable transition.
- [ ] Session listing uses rebuildable metadata, and Resume reconstructs state from events rather than serialized coroutines.
- [ ] Recovery truncates only a trailing partial JSON line with a warning and refuses mid-file corruption or sequence gaps.
- [ ] Current and explicitly supported old Schemas read through pure in-memory migration; unknown newer Schemas are read-only and cannot Resume or append.
- [ ] Tests cover locks, stale-lock evidence, projection rebuild, partial-tail repair, corruption refusal, and text-only Resume using real temporary files.
