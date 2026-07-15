"""Build and audit the local Mini Agent distributions."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from check_artifacts import verify_distribution

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def _run_uv_build(source: Path, output: Path, *targets: str) -> None:
    subprocess.run(
        ["uv", "build", *targets, "--out-dir", str(output)],
        cwd=source,
        check=True,
    )


def _clear_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_checksums(artifacts: list[Path]) -> None:
    lines = [f"{_sha256(path)}  {path.name}" for path in sorted(artifacts)]
    (DIST / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_without_git_metadata() -> None:
    """Prove the explicit Hatchling file list does not depend on Git."""

    with tempfile.TemporaryDirectory(prefix="mini-agent-sdist-") as temporary:
        source = Path(temporary) / "source"
        source.mkdir()
        for relative_path in ("LICENSE", "README.md", "pyproject.toml"):
            shutil.copy2(ROOT / relative_path, source / relative_path)
        shutil.copytree(ROOT / "src", source / "src")
        output = source / "dist"
        _run_uv_build(source, output, "--sdist")
        if len(list(output.glob("*.tar.gz"))) != 1:
            raise RuntimeError("Git-free source distribution build did not produce one sdist")


def main() -> None:
    _clear_directory(DIST)
    _run_uv_build(ROOT, DIST, "--wheel", "--sdist")

    artifacts = sorted((*DIST.glob("*.whl"), *DIST.glob("*.tar.gz")))
    if len(artifacts) != 2:
        raise RuntimeError("build must produce exactly one wheel and one source distribution")

    _write_checksums(artifacts)
    verify_distribution(DIST)
    _build_without_git_metadata()
    print(f"Built and verified {len(artifacts)} artifacts in {DIST}")


if __name__ == "__main__":
    main()
