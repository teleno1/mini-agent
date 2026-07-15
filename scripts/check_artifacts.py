"""Validate Mini Agent distribution contents and generated checksums."""

from __future__ import annotations

import hashlib
import re
import tarfile
import zipfile
from pathlib import Path

FORBIDDEN_PARTS = {".git", ".mini-agent", ".scratch", "tests"}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".pyc", ".pyo"}
VERSION_PATTERN = re.compile(r"mini_agent-(?P<version>[^-]+)")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_forbidden(name: str) -> bool:
    normalized = name.replace("\\", "/")
    parts = set(normalized.split("/"))
    basename = Path(normalized).name
    return (
        bool(parts & FORBIDDEN_PARTS)
        or basename == ".env"
        or basename.startswith(".env.")
        or Path(normalized).suffix.lower() in FORBIDDEN_SUFFIXES
    )


def _artifact_version(wheel: Path, sdist: Path) -> str:
    match = VERSION_PATTERN.match(wheel.name)
    if match is None or not sdist.name.startswith(f"mini_agent-{match.group('version')}"):
        raise RuntimeError("wheel and source distribution versions do not match")
    return match.group("version")


def _check_wheel(path: Path, version: str) -> None:
    if not path.name.endswith("-py3-none-any.whl"):
        raise RuntimeError("wheel must be a pure-Python py3-none-any artifact")

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        _reject_forbidden(names, path.name)
        required = {
            "mini_agent/__init__.py",
            "mini_agent/__main__.py",
            "mini_agent/py.typed",
            "README.md",
            "LICENSE",
            f"mini_agent-{version}.dist-info/METADATA",
            f"mini_agent-{version}.dist-info/WHEEL",
        }
        missing = required - set(names)
        if missing:
            raise RuntimeError(f"wheel is missing required files: {sorted(missing)}")

        wheel_metadata = archive.read(f"mini_agent-{version}.dist-info/WHEEL").decode()
        if "Root-Is-Purelib: true" not in wheel_metadata:
            raise RuntimeError("wheel is not marked as pure Python")

        project_metadata = archive.read(f"mini_agent-{version}.dist-info/METADATA").decode()
        for marker in ("License-File: LICENSE", "Description-Content-Type: text/markdown"):
            if marker not in project_metadata:
                raise RuntimeError(f"wheel metadata is missing {marker!r}")


def _check_sdist(path: Path, version: str) -> None:
    expected_prefix = f"mini_agent-{version}/"
    with tarfile.open(path) as archive:
        names = archive.getnames()
        _reject_forbidden(names, path.name)
        required = {
            f"{expected_prefix}LICENSE",
            f"{expected_prefix}README.md",
            f"{expected_prefix}pyproject.toml",
            f"{expected_prefix}src/mini_agent/py.typed",
        }
        missing = required - set(names)
        if missing:
            raise RuntimeError(f"source distribution is missing required files: {sorted(missing)}")


def _reject_forbidden(names: list[str], artifact_name: str) -> None:
    forbidden = sorted(name for name in names if _is_forbidden(name))
    if forbidden:
        raise RuntimeError(f"{artifact_name} contains forbidden files: {forbidden}")


def _check_checksums(directory: Path, artifacts: list[Path]) -> None:
    checksum_path = directory / "SHA256SUMS"
    if not checksum_path.is_file():
        raise RuntimeError("dist/SHA256SUMS was not generated")

    expected = {path.name: _sha256(path) for path in artifacts}
    actual: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or not digest or not name:
            raise RuntimeError("SHA256SUMS contains an invalid line")
        actual[name] = digest
    if actual != expected:
        raise RuntimeError("SHA256SUMS does not match the built artifacts")


def verify_distribution(directory: Path) -> None:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError("dist must contain exactly one wheel and one source distribution")

    version = _artifact_version(wheels[0], sdists[0])
    _check_wheel(wheels[0], version)
    _check_sdist(sdists[0], version)
    _check_checksums(directory, [wheels[0], sdists[0]])


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args()
    verify_distribution(args.directory)
    print(f"Verified distributions in {args.directory}")


if __name__ == "__main__":
    main()
