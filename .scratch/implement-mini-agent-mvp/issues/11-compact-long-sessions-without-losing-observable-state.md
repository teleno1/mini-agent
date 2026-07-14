# 11 - Compact long Sessions without losing observable state

**What to build:** A long coding Session can continue near the model context limit by compacting redundant data and producing a validated factual summary while retaining the original durable history.

**Blocked by:** 04 - Assemble trusted configuration, instructions, and Context Frames; 09 - Complete bounded serial multi-Tool coding Turns; 10 - Store and reread large Tool Results as immutable Artifacts.

**Status:** ready-for-agent

- [ ] Estimate input tokens conservatively, calibrate with Provider usage, and reserve the agreed response capacity including the small-window cap.
- [ ] Preserve safety policy, Permission Policy, active Tool Definitions, current user message, and unfinished Tool protocol pairs under all pressure.
- [ ] Micro-compact Artifact-backed results, superseded Plans, and re-derivable operational state before requesting a model summary.
- [ ] Generate the fixed structured Context Summary fields and validate types, evidence references, Artifact references, and monotonic Summary Boundary before activation.
- [ ] Assemble later requests from the latest valid summary plus relevant events after its boundary without deleting original Session Events or claiming hidden reasoning was retained.
- [ ] Record compaction lifecycle and fail the Turn clearly after three unsuccessful attempts instead of sending a known-oversized request.
- [ ] Tests cover threshold edges, Tool pairing, old-summary recompression, invalid/hallucinated references, failure recovery, and preservation of objectives, constraints, changes, failures, and next actions.
