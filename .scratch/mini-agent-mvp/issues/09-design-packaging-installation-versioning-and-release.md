# Design packaging, installation, versioning, and release workflow

Type: grilling
Status: resolved
Blocked by: 06

## Question

Which build backend, dependency and lock-file policy, version source, CLI entry point, installation paths, distribution artifacts, compatibility guarantees, and GitHub Release or PyPI workflow should make Mini Agent reproducibly installable without overbuilding the practice-project MVP?

## Answer

### Build and dependency toolchain

Use `pyproject.toml` as the only project metadata and tool-configuration entry point, Hatchling as the minimal PEP 517 build backend, and uv for development environments, dependency resolution, and locking. Commit `uv.lock`; CI installs all development groups in frozen mode. End users install a standard wheel and do not need uv as a runtime dependency.

Runtime dependencies are limited to Typer, Rich, Pydantic, and httpx. Test and development packages (`pytest`, `pytest-asyncio`, `ruff`, `mypy`, and build tooling) belong in the `dev` dependency group and are excluded from normal installs. Declare tested minimum direct versions and cap them before the next incompatible major release. The lock fixes exact CI transitive versions, while wheel metadata keeps compatible ranges for installers. Do not add a second environment manager or command-wrapper layer.

Require Python 3.12 or newer. Do not preemptively add helper libraries when the standard library keeps the design clear. Dependency upgrades update the lock and run both the locked suite and a minimum-supported direct-dependency check.

### Package identity, entry points, and version

Use distribution name `mini-agent`, import package `mini_agent`, and console command `mini-agent`. Register `mini-agent = "mini_agent.cli.app:main"` under `[project.scripts]` and provide `python -m mini_agent` as an equivalent development and diagnostic entry. Module import performs no configuration reads, Session creation, or network access; `--help` and `--version` work without credentials or a Git repository.

`[project].version` in `pyproject.toml` is the sole version source. Runtime reads installed metadata through `importlib.metadata`. Start at `0.1.0` and follow semantic versioning. A Release PR updates the version and changelog; its tag is exactly `vX.Y.Z`, and CI rejects disagreement between tag, project metadata, and artifacts. Do not derive versions dynamically from Git.

### Installation and runtime paths

The MVP must be deployable from a local wheel or Git checkout, not publicly published. Supported examples are `uv build` followed by `uv tool install <wheel>`, standard pip or pipx installation of a wheel, and `uv tool install git+https://github.com/teleno1/mini-agent.git`. Until a PyPI project exists, documentation must not present `uv tool install mini-agent` as an available command.

Package code and bundled prompt/Schema resources are installed read-only and accessed with `importlib.resources`; runtime never writes to `site-packages` or assumes a source checkout. User configuration uses the platform-standard config directory. Mutable Session, Artifact, Checkpoint, project log, and project configuration data live under the selected Workspace's `.mini-agent/`. Temporary atomic-write files stay in the target Session directory. `--workspace` accepts an explicit path; otherwise use the resolved current directory. Wheel, editable, Git, and source installs behave identically with respect to paths.

`.mini-agent/config.toml` and an optional explanatory README may be committed. Sessions, Artifacts, Checkpoints, logs, locks, and temporary files must be ignored. `mini-agent init` may create the minimal non-secret configuration and update `.gitignore` only with user confirmation. The Agent remains usable without initialization. It never commits files, and uninstallation never deletes user or project data.

### Artifacts and verification

Build one pure-Python `py3-none-any` wheel and one source distribution, plus `SHA256SUMS`. Include the package, `py.typed`, required bundled resources, README, LICENSE, and correct metadata. Exclude tests, `.scratch`, Sessions, logs, environment files, and secrets from the wheel. Verify both wheel and sdist in clean environments, including `--version`, `--help`, and a Fake Provider smoke journey. The sdist must build without Git metadata.

MVP completion requires reproducible construction and clean installation on Windows, macOS, and Linux; it does not require public upload. Platform installers, standalone executables, containers, signing, and SBOM generation remain outside this practice-project MVP.

### Compatibility policy

Support Python 3.12+ on Windows, macOS, and Linux. During `0.x`, internal Python modules and third-party plugin APIs are not stable. The current version reads its own Session Event Schema plus each older version for which an explicit safe migration exists, with at least the immediately previous released Schema supported for reading. Unknown newer Schemas are read-only, and migration is explicit and backed up. Forward readability by old binaries is not promised.

Configuration removal gets at least one minor-version deprecation warning. Document user-facing CLI migrations and breaking changes. Package metadata declares `Requires-Python`; unsupported Schema versions refuse writes.

### Optional release workflow

Daily work does not bump versions. At a demonstrable milestone, a short Release PR updates version and `CHANGELOG.md` using Added, Changed, Fixed, Security, and Breaking sections. After merge, a `vX.Y.Z` tag triggers the complete CI matrix and builds one artifact set retained as a GitHub Actions Artifact.

Creating a GitHub Release or publishing to PyPI is an optional manually approved follow-up. If PyPI is later enabled, use Trusted Publishing/OIDC rather than a long-lived token and publish the already tested artifacts; do not rebuild different files for different destinations. PyPI files are immutable and mistakes require a new version. TestPyPI may be used for a first manual rehearsal but is not a standing gate. No develop/release branches, nightlies, automated version increments, or generated changelogs are required.

### Licensing and project positioning

Use the MIT License. Package metadata includes description, README, license, author, repository, issues, changelog links, Python and OS classifiers, dependencies, entry point, and typing marker. The README clearly identifies Mini Agent as an independent learning project, not an Anthropic product and not a Claude Code compatibility implementation. Confirm dependency licenses are distributable under this project and that LICENSE metadata is present in built artifacts.
