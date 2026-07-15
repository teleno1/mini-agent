# 02 - Establish offline quality gates and distributable artifacts

**What to build:** Every later slice inherits a reproducible offline quality gate, and maintainers can build and install typed Mini Agent artifacts on all supported platforms without publishing them.

**Blocked by:** 01 - Bootstrap an installable text-only Agent.

**Status:** completed

- [x] Commit the dependency lock and make frozen dependency installation succeed from a clean checkout.
- [x] Ruff formatting/linting, mypy, pytest, and the Fake Provider smoke journey run without credentials or real-model network calls.
- [x] CI covers Python 3.12 on Windows, macOS, and Linux plus Python 3.13 on Linux, and any required job failure blocks success.
- [x] Build one typed pure-Python wheel and one source distribution; both install cleanly and expose help, version, and the Fake Provider journey.
- [x] Distribution contents include required resources, typing marker, README, and MIT License while excluding tests, scratch/runtime data, secrets, and environment files.
- [x] The source distribution rebuilds without Git metadata and SHA-256 checksums are generated for artifacts.
