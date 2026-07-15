from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from mini_agent.tools.contracts import (
    SideEffectCategory,
    ToolCall,
    ToolOutcome,
    ToolRegistry,
)
from mini_agent.tools.files import ReadFileInput, ReadFileTool, SearchFilesInput, SearchFilesTool
from mini_agent.tools.workspace import (
    BinaryTargetError,
    SensitiveTargetError,
    Workspace,
    WorkspacePathError,
)


def test_registry_exposes_typed_read_and_search_definitions_and_preserves_call_id(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    registry = ToolRegistry([ReadFileTool(), SearchFilesTool()])

    definitions = registry.definitions()
    assert [definition.name for definition in definitions] == ["read_file", "search_files"]
    assert all(definition.side_effect is SideEffectCategory.READ for definition in definitions)
    assert definitions[0].limits.max_output_bytes == 64 * 1024

    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    call = ToolCall(id="call-read-42", name="read_file", arguments={"path": "note.txt"})
    result = asyncio.run(registry.execute(workspace, call))
    assert result.tool_call_id == "call-read-42"
    assert result.tool_name == "read_file"
    assert result.outcome is ToolOutcome.SUCCESS


@pytest.mark.parametrize(
    "target",
    [
        "/etc/passwd",
        "\\\tmp\\outside.txt",
        "C:\\outside.txt",
        "C:outside.txt",
        "\\\\server\\share\\outside.txt",
        "..\\outside.txt",
        "nested/../../outside.txt",
        "\\\\?\\C:\\outside.txt",
        "NUL.txt",
    ],
)
def test_workspace_rejects_absolute_drive_unc_traversal_and_device_paths(
    tmp_path: Path, target: str
) -> None:
    workspace = Workspace(tmp_path)

    with pytest.raises(WorkspacePathError):
        workspace.resolve_read(target)


def test_workspace_allows_internal_link_but_rejects_external_link(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    inside = tmp_path / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    internal_link = tmp_path / "inside-link.txt"
    external = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    external.write_text("do not disclose", encoding="utf-8")
    external_link = tmp_path / "outside-link.txt"
    try:
        internal_link.symlink_to(inside)
        external_link.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("the current platform does not permit temporary symlinks")

    assert workspace.resolve_read("inside-link.txt").path == inside.resolve()
    with pytest.raises(WorkspacePathError) as error:
        workspace.resolve_read("outside-link.txt")
    assert error.value.code == "outside"
    assert "do not disclose" not in str(error.value)


def test_workspace_denies_sensitive_and_binary_targets_but_allows_env_template(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / ".env").write_text("API_KEY=secret", encoding="utf-8")
    (tmp_path / ".env.example").write_text("API_KEY=", encoding="utf-8")
    (tmp_path / "private.pem").write_text("private key", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"not text")
    (tmp_path / "data").write_bytes(b"header\x00secret")

    with pytest.raises(SensitiveTargetError):
        workspace.resolve_read(".env")
    with pytest.raises(SensitiveTargetError):
        workspace.resolve_read("private.pem")
    assert workspace.resolve_read(".env.example").path.name == ".env.example"
    with pytest.raises(BinaryTargetError):
        workspace.resolve_read("image.png")
    with pytest.raises(BinaryTargetError):
        workspace.read_text_bytes(workspace.resolve_read("data"))


@pytest.mark.asyncio
async def test_read_file_handles_bom_ranges_and_line_continuation(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    path = tmp_path / "bom.txt"
    path.write_bytes("\ufeffone\ntwo\nthree\n".encode("utf-8"))
    tool = ReadFileTool()

    ranged = await tool.execute(
        workspace,
        ReadFileInput(path="bom.txt", start_line=2, end_line=2),
    )
    assert ranged.data["content"] == "two\n"
    assert ranged.data["start_line"] == 2
    assert ranged.data["end_line"] == 2
    assert ranged.data["truncated"] is False

    long_path = tmp_path / "long.txt"
    long_path.write_text("x" * 100 + "\nlast\n", encoding="utf-8")
    first = await tool.execute(
        workspace,
        ReadFileInput(path="long.txt", max_bytes=10),
    )
    assert first.data["content"] == "x" * 10
    continuation = first.data["continuation"]
    assert continuation == {"path": "long.txt", "start_line": 1, "start_byte": 10}

    second = await tool.execute(
        workspace,
        ReadFileInput(
            path=continuation["path"],
            start_line=continuation["start_line"],
            start_byte=continuation["start_byte"],
            max_bytes=10,
        ),
    )
    assert second.data["content"] == "x" * 10

    many_lines = tmp_path / "many.txt"
    many_lines.write_text("".join(f"line-{index}\n" for index in range(1, 503)), encoding="utf-8")
    limited = await tool.execute(workspace, ReadFileInput(path="many.txt"))
    assert limited.data["end_line"] == 500
    assert limited.data["continuation"]["start_line"] == 501


@pytest.mark.asyncio
async def test_read_file_failure_is_bounded_and_does_not_reveal_sensitive_content(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / ".env").write_text("TOP_SECRET=do-not-return", encoding="utf-8")

    result = await ReadFileTool().execute(workspace, ReadFileInput(path=".env"))

    assert result.outcome is ToolOutcome.FAILED
    assert result.error is not None
    assert "TOP_SECRET" not in result.text
    assert "do-not-return" not in result.text


@pytest.mark.asyncio
async def test_search_python_fallback_supports_aliases_regex_glob_ignore_and_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.py").write_text("prefix needle here\nother\n", encoding="utf-8")
    (source / "main.log").write_text("needle ignored\n", encoding="utf-8")
    ignored = tmp_path / "ignored"
    ignored.mkdir()
    (ignored / "skip.py").write_text("needle ignored\n", encoding="utf-8")
    monkeypatch.setattr("mini_agent.tools.files.shutil.which", lambda _: None)

    literal = await SearchFilesTool().execute(
        workspace,
        SearchFilesInput(pattern="needle", path=".", glob="*.py"),
    )
    assert literal.outcome is ToolOutcome.SUCCESS
    assert literal.data["match_count"] == 1
    assert literal.data["matches"][0] == {
        "path": "src/main.py",
        "line": 1,
        "column": 8,
        "text": "prefix needle here",
    }

    regex = await SearchFilesTool().execute(
        workspace,
        SearchFilesInput(query=r"needle\s+here", regex=True, directory="src"),
    )
    assert regex.data["match_count"] == 1


@pytest.mark.asyncio
async def test_search_rg_is_direct_and_result_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "main.py").write_text("needle\n", encoding="utf-8")
    observed: dict[str, object] = {}

    def fake_run(args, *, cwd, check, capture_output, timeout, shell):
        observed.update(
            args=args,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            timeout=timeout,
            shell=shell,
        )
        return subprocess.CompletedProcess(
            args,
            0,
            b"main.py:1:1:needle\nmain.py:2:1:needle\nmain.py:3:1:needle\n",
            b"",
        )

    monkeypatch.setattr("mini_agent.tools.files.shutil.which", lambda _: "rg")
    monkeypatch.setattr("mini_agent.tools.files.subprocess.run", fake_run)

    result = await SearchFilesTool().execute(
        workspace,
        SearchFilesInput(query="needle", max_results=2),
    )

    assert observed["shell"] is False
    assert observed["cwd"] == workspace.root
    assert "--" in observed["args"]
    assert result.data["match_count"] == 2
    assert result.data["truncated"] is True


@pytest.mark.asyncio
async def test_search_rejects_outside_directory_without_disclosing_target(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    result = await SearchFilesTool().execute(
        workspace,
        SearchFilesInput(query="needle", directory="../outside"),
    )

    assert result.outcome is ToolOutcome.FAILED
    assert result.error is not None
    assert "outside" not in result.text
