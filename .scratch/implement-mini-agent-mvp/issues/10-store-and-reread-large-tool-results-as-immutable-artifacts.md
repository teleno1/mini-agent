# 10 - Store and reread large Tool Results as immutable Artifacts

**What to build:** Large Tool output remains available as an integrity-checked Session Artifact while the model receives only a bounded preview and can request controlled ranges when more evidence is needed.

**Blocked by:** 03 - Persist, list, and resume text-only Sessions; 06 - Let the Fake-driven Agent read and search a confined Workspace.

**Status:** ready-for-agent

- [ ] Crossing the configurable 32-KiB persistence threshold atomically writes an immutable Artifact before the terminal Tool event references it.
- [ ] References contain stable identity, Session-relative path, media type, byte count, SHA-256 digest, preview, and truncation state.
- [ ] The model cannot choose Artifact paths and normal model file Tools remain unable to access `.mini-agent` directly.
- [ ] A dedicated controlled Tool rereads bounded ranges without exceeding the absolute Tool response ceiling.
- [ ] Redact known credentials and sensitive environment values before either inline persistence or Artifact writing, retaining no unredacted copy.
- [ ] Failed Artifact writes cannot produce successful Tool Results; failed reference events leave detectable orphans without fabricating commitment.
- [ ] Integration tests cover exact threshold edges, integrity verification, ranged reread, path confinement, redaction, immutability, and orphan detection.
