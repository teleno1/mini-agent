# 15 - Audit the packaged MVP against the acceptance matrix

**What to build:** Maintainers receive objective evidence that the installed Mini Agent satisfies every agreed MVP capability on supported platforms, with any discovered implementation gap fixed without silently adding product scope.

**Blocked by:** 02 - Establish offline quality gates and distributable artifacts; 11 - Compact long Sessions without losing observable state; 13 - Reconcile interrupted work on Resume; 14 - Deliver the production conversational CLI; 16 - Fail production startup when Provider authentication is unavailable; 17 - Deny non-interactive permission prompts deterministically; 18 - Preserve unknown Tool calls as invalid results; 19 - Report only successful Shell verification; 20 - Normalize Provider usage after completed Tool Calls.

**Status:** ready-for-agent

- [ ] Trace every capability in the specification acceptance matrix to an automated test or an explicitly manual release-candidate check.
- [ ] Run and close gaps in unit, shared contract, integration, Fake Provider end-to-end, corruption, cancellation, recovery, and CLI semantic journeys.
- [ ] Produce passing Python 3.12 evidence on Windows/macOS/Linux and Python 3.13 evidence on Linux using frozen dependencies and no real-model calls.
- [ ] Inspect wheel and source distribution contents, verify clean installation and Git-free rebuild, and rerun help/version/Fake smoke from installed artifacts.
- [ ] Confirm permissions, Workspace confinement, durable-before-side-effect ordering, context compaction, interruption reconciliation, redaction, and exit-code invariants against the specification.
- [ ] Document the optional restricted-credential real-model checklist with Provider/model/date/Session/result fields and no sensitive logs; public release remains optional.
- [ ] Fix only acceptance gaps within the approved MVP; record any desired expansion as separate future work rather than broadening this ticket.
