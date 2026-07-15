# 03 - Persist, list, and resume text-only Sessions

**What to build:** A text-only conversation survives process exit as an authoritative append-only Session, can be listed, and can be resumed without pretending transient runtime state was persisted.

**Blocked by:** 01 - Bootstrap an installable text-only Agent.

**Status:** completed

- [x] Store typed UTF-8 JSONL Session Events with schema version, stable IDs, contiguous sequence, causation, timestamps, and rebuildable projections.
- [x] Persist user messages, model-request lifecycle, complete assistant messages, and Turn terminal events with exactly one exclusive Session writer.
- [x] Append complete lines and flush/fsync required state before advancing to the next durable transition.
- [x] Session listing uses rebuildable metadata, and Resume reconstructs state from events rather than serialized coroutines.
- [x] Recovery truncates only a trailing partial JSON line with a warning and refuses mid-file corruption or sequence gaps.
- [x] Current and explicitly supported old Schemas read through pure in-memory migration; unknown newer Schemas are read-only and cannot Resume or append.
- [x] Tests cover locks, stale-lock evidence, projection rebuild, partial-tail repair, corruption refusal, and text-only Resume using real temporary files.

## Completion evidence

- Typed JSONL events, stable IDs, contiguous sequences, causation, timestamps, projections, and durable lifecycle ordering: `tests/test_sessions.py::test_text_turn_is_durable_and_resumes_from_rebuilt_messages` and `tests/test_sessions.py::test_failed_stream_persists_failure_without_an_assistant_message`.
- Exclusive writer, active/stale lock evidence, and stale-lock recovery: `tests/test_sessions.py::test_one_exclusive_writer_reports_active_and_stale_lock_evidence`.
- Rebuildable listing metadata and metadata-cache independence: `tests/test_sessions.py::test_listing_rebuilds_when_disposable_metadata_is_missing`.
- Text-only CLI listing and Resume: `tests/test_cli.py::test_cli_lists_and_resumes_a_durable_session`.
- Trailing partial-tail repair and corruption/sequence-gap refusal: `tests/test_sessions.py::test_only_a_trailing_partial_json_line_is_repaired` and `tests/test_sessions.py::test_mid_file_corruption_and_sequence_gaps_refuse_recovery`.
- Old-schema in-memory migration and newer-schema read-only inspection: `tests/test_sessions.py::test_newer_schema_is_read_only_and_old_schema_migrates_in_memory`.
- Full verification: `uv run pytest` (16 passed), `uv run ruff check src tests`, `uv run mypy`, and `git diff --check`.
