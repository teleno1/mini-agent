"""Bounded, read-only Workspace file and repository search Tools."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mini_agent.tools.contracts import (
    RiskAssessment,
    SideEffectCategory,
    ToolLimits,
    ToolOutcome,
    ToolResult,
)
from mini_agent.tools.workspace import BinaryTargetError, Workspace, WorkspaceError, WorkspacePathError


MAX_LINES = 500
MAX_BYTES = 64 * 1024
MAX_SEARCH_MATCHES = 200


class ReadFileInput(BaseModel):
    """Line/range input accepted by ``read_file``."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    max_lines: int = Field(default=MAX_LINES, ge=1, le=MAX_LINES)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    start_byte: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def normalize_range(self) -> ReadFileInput:
        if self.line_start is not None and self.start_line != 1 and self.line_start != self.start_line:
            raise ValueError("start_line and line_start disagree")
        if self.line_end is not None and self.end_line is not None and self.line_end != self.end_line:
            raise ValueError("end_line and line_end disagree")
        start = self.line_start or self.start_line
        end = self.line_end if self.line_end is not None else self.end_line
        if end is not None and end < start:
            raise ValueError("end line must not precede start line")
        if self.line_start is not None or self.line_end is not None:
            return self.model_copy(update={"start_line": start, "end_line": end})
        return self


class SearchFilesInput(BaseModel):
    """Bounded literal/regex repository search input."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    query: str = Field(min_length=1, validation_alias="query")
    directory: str = "."
    glob: str | None = None
    regex: bool = False
    max_matches: int = Field(default=MAX_SEARCH_MATCHES, ge=1, le=MAX_SEARCH_MATCHES)
    max_bytes: int = Field(default=MAX_BYTES, ge=1, le=MAX_BYTES)
    case_sensitive: bool = True

    @model_validator(mode="after")
    def validate_pattern(self) -> SearchFilesInput:
        if not self.query.strip():
            raise ValueError("query cannot be blank")
        if self.glob is not None and not self.glob.strip():
            raise ValueError("glob cannot be blank")
        if self.glob is not None:
            portable = self.glob.replace("\\", "/")
            if portable.startswith(("/", "//")) or any(part == ".." for part in portable.split("/")):
                raise ValueError("glob must remain relative to the Workspace")
        if self.regex:
            try:
                re.compile(self.query, 0 if self.case_sensitive else re.IGNORECASE)
            except re.error as exc:
                raise ValueError("regex is invalid") from exc
        return self


class _WorkspaceTool:
    side_effect = SideEffectCategory.READ
    limits = ToolLimits.bounded(timeout_seconds=30.0, max_output_bytes=MAX_BYTES)

    def _invalid(self, call_id: str, name: str, message: str) -> ToolResult:
        from mini_agent.tools.contracts import ToolCall

        return ToolResult.failed(
            ToolCall(tool_call_id=call_id, name=name, arguments={}),
            outcome=ToolOutcome.INVALID,
            category="tool-validation",
            code="invalid-input",
            message=message,
        )


class ReadFileTool(_WorkspaceTool):
    """Read a bounded UTF-8 text range from a confined Workspace target."""

    name = "read_file"
    description = "Read a bounded UTF-8 text range from a Workspace-relative file."
    input_model: ClassVar[type[BaseModel]] = ReadFileInput

    def assess(self, arguments: BaseModel) -> RiskAssessment:
        request = _as_read_input(arguments)
        return RiskAssessment(
            side_effect=self.side_effect,
            resources=(request.path,),
            summary="read text from one Workspace file",
        )

    async def execute(self, workspace: Workspace, arguments: BaseModel) -> ToolResult:
        request = _as_read_input(arguments)
        call_id = getattr(arguments, "tool_call_id", "read-file")
        try:
            target = workspace.resolve_read(request.path)
            raw = workspace.read_text_bytes(target)
            return _read_range(target.relative_path, raw, request)
        except BinaryTargetError:
            return self._failure(call_id, "binary", "binary Workspace content is not readable")
        except WorkspacePathError as exc:
            return self._failure(call_id, exc.code, str(exc))
        except WorkspaceError:
            return self._failure(call_id, "read", "Workspace file could not be read")
        except UnicodeDecodeError:
            return self._failure(call_id, "binary", "binary Workspace content is not readable")
        except OSError:
            return self._failure(call_id, "read", "Workspace file could not be read")

    def _failure(self, call_id: str, code: str, message: str) -> ToolResult:
        from mini_agent.tools.contracts import ToolCall

        return ToolResult.failed(
            ToolCall(tool_call_id=str(call_id), name=self.name, arguments={}),
            category="tool-execution",
            code=code,
            message=message,
        )


class SearchFilesTool(_WorkspaceTool):
    """Search bounded text matches without invoking a Shell Tool."""

    name = "search_files"
    description = "Search Workspace text using a literal or regular expression query."
    input_model: ClassVar[type[BaseModel]] = SearchFilesInput

    def assess(self, arguments: BaseModel) -> RiskAssessment:
        request = _as_search_input(arguments)
        return RiskAssessment(
            side_effect=self.side_effect,
            resources=(request.directory,),
            summary="search text under a Workspace directory",
        )

    async def execute(self, workspace: Workspace, arguments: BaseModel) -> ToolResult:
        request = _as_search_input(arguments)
        call_id = getattr(arguments, "tool_call_id", "search-files")
        try:
            target = workspace.resolve_read(request.directory, directory=True)
        except WorkspacePathError as exc:
            return self._failure(call_id, exc.code, str(exc))
        try:
            if shutil.which("rg"):
                matches = await asyncio.to_thread(self._search_with_rg, workspace, target.path, request)
            else:
                matches = await asyncio.to_thread(self._search_with_python, workspace, target.path, request)
            return ToolResult(
                tool_call_id=str(call_id),
                tool_name=self.name,
                outcome=ToolOutcome.SUCCESS,
                data={
                    "query": request.query,
                    "directory": target.relative_path,
                    "matches": matches.items,
                    "match_count": len(matches.items),
                    "truncated": matches.truncated,
                    "continuation": matches.continuation,
                },
            )
        except ValueError:
            return self._failure(call_id, "regex", "search pattern is invalid")
        except subprocess.TimeoutExpired:
            return self._failure(call_id, "timeout", "repository search exceeded its time limit")
        except OSError:
            return self._failure(call_id, "search", "repository search could not be completed")

    def _failure(self, call_id: str, code: str, message: str) -> ToolResult:
        from mini_agent.tools.contracts import ToolCall

        return ToolResult.failed(
            ToolCall(tool_call_id=str(call_id), name=self.name, arguments={}),
            category="tool-execution",
            code=code,
            message=message,
        )

    def _search_with_rg(self, workspace: Workspace, root: Path, request: SearchFilesInput) -> _SearchResult:
        executable = shutil.which("rg")
        if executable is None:
            return self._search_with_python(workspace, root, request)
        relative_root = root.relative_to(workspace.root).as_posix()
        args = [
            executable,
            "--no-heading",
            "--line-number",
            "--column",
            "--color",
            "never",
            "--binary-files",
            "without-match",
            "--max-count",
            str(request.max_matches),
        ]
        if not request.regex:
            args.append("--fixed-strings")
        if not request.case_sensitive:
            args.append("--ignore-case")
        if request.glob is not None:
            args.extend(["--glob", request.glob])
        args.extend(["--", request.query, relative_root])
        completed = subprocess.run(
            args,
            cwd=workspace.root,
            check=False,
            capture_output=True,
            timeout=self.limits.timeout_seconds,
            shell=False,
        )
        if completed.returncode not in (0, 1):
            raise OSError("rg search failed")
        return _parse_rg_output(completed.stdout, request)

    def _search_with_python(self, workspace: Workspace, root: Path, request: SearchFilesInput) -> _SearchResult:
        flags = 0 if request.case_sensitive else re.IGNORECASE
        matcher = re.compile(request.query, flags) if request.regex else None
        matches: list[dict[str, Any]] = []
        used_bytes = 0
        truncated = False
        files = [root] if root.is_file() else _iter_files(workspace, root)
        for path in files:
            relative = path.relative_to(workspace.root).as_posix()
            if request.glob is not None and not _glob_match(relative, request.glob):
                continue
            if workspace.is_ignored(relative):
                continue
            try:
                target = workspace.resolve_read(relative)
                raw = workspace.read_text_bytes(target)
                text = raw.decode("utf-8-sig")
            except (WorkspaceError, UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                found = matcher.search(line) if matcher is not None else _literal_search(line, request.query, request.case_sensitive)
                if found is None:
                    continue
                item = {
                    "path": relative,
                    "line": line_number,
                    "column": found.start() + 1,
                    "text": line,
                }
                encoded_size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 1
                if len(matches) >= request.max_matches or used_bytes + encoded_size > request.max_bytes:
                    truncated = True
                    break
                matches.append(item)
                used_bytes += encoded_size
            if truncated:
                break
        return _SearchResult(tuple(matches), truncated, _next_search_continuation(matches) if truncated else None)


class _SearchResult:
    def __init__(self, items: tuple[dict[str, Any], ...], truncated: bool, continuation: dict[str, Any] | None) -> None:
        self.items = items
        self.truncated = truncated
        self.continuation = continuation


def _read_range(relative: str, raw: bytes, request: ReadFileInput) -> ToolResult:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    start = request.start_line
    end = request.end_line or len(lines)
    if start > len(lines) and lines:
        selected: list[str] = []
    else:
        selected = []
    next_line: int | None = None
    next_byte: int | None = None
    line_byte_offset = 0
    for line_number, line in enumerate(lines, start=1):
        encoded_line = line.encode("utf-8")
        if line_number < start:
            line_byte_offset += len(encoded_line)
            continue
        if line_number > end or len(selected) >= request.max_lines:
            if line_number <= end:
                next_line, next_byte = line_number, line_byte_offset
            break
        current_bytes = sum(len(item.encode("utf-8")) for item in selected)
        remaining = MAX_BYTES - current_bytes
        if len(encoded_line) <= remaining:
            selected.append(line)
        else:
            piece = _decode_prefix(encoded_line, remaining)
            selected.append(piece)
            next_line = line_number
            next_byte = line_byte_offset + len(piece.encode("utf-8"))
            break
        line_byte_offset += len(encoded_line)
    content = "".join(selected)
    last_line = start + len(selected) - 1 if selected else start - 1
    truncated = next_line is not None
    continuation = None
    if truncated:
        continuation = {"path": relative, "start_line": next_line, "start_byte": next_byte}
    return ToolResult(
        tool_call_id="read-file",
        tool_name="read_file",
        outcome=ToolOutcome.SUCCESS,
        data={
            "path": relative,
            "content": content,
            "start_line": start,
            "end_line": last_line,
            "total_lines": len(lines),
            "truncated": truncated,
            "continuation": continuation,
        },
    )


def _decode_prefix(raw: bytes, limit: int) -> str:
    if limit <= 0:
        return ""
    prefix = raw[:limit]
    while prefix:
        try:
            return prefix.decode("utf-8")
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return ""


def _as_read_input(arguments: BaseModel) -> ReadFileInput:
    return arguments if isinstance(arguments, ReadFileInput) else ReadFileInput.model_validate(arguments)


def _as_search_input(arguments: BaseModel) -> SearchFilesInput:
    return arguments if isinstance(arguments, SearchFilesInput) else SearchFilesInput.model_validate(arguments)


def _literal_search(line: str, query: str, case_sensitive: bool) -> re.Match[str] | None:
    if case_sensitive:
        index = line.find(query)
    else:
        index = line.lower().find(query.lower())
    if index < 0:
        return None
    return re.match(re.escape(line[index:]), line[index:])


def _parse_rg_output(raw: bytes, request: SearchFilesInput) -> _SearchResult:
    text = raw.decode("utf-8", errors="replace")
    items: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for line in text.splitlines():
        path, separator, remainder = line.partition(":")
        if not separator:
            continue
        line_number_text, separator, remainder = remainder.partition(":")
        column_text, separator, content = remainder.partition(":")
        if not separator:
            continue
        try:
            line_number = int(line_number_text)
            column = int(column_text)
        except ValueError:
            continue
        item = {"path": path.replace("\\", "/"), "line": line_number, "column": column, "text": content}
        size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 1
        if len(items) >= request.max_matches or used + size > request.max_bytes:
            truncated = True
            break
        items.append(item)
        used += size
    return _SearchResult(tuple(items), truncated, _next_search_continuation(items) if truncated else None)


def _next_search_continuation(items: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    if not items:
        return None
    last = items[-1]
    return {"path": last["path"], "start_line": last["line"]}


def _iter_files(workspace: Workspace, root: Path):
    for directory, directories, filenames in __import__("os").walk(root, followlinks=False):
        directory_path = Path(directory)
        directories[:] = [
            name
            for name in directories
            if name not in {".git", ".mini-agent"}
            and not workspace.is_ignored((directory_path / name).relative_to(workspace.root).as_posix())
        ]
        for filename in filenames:
            yield directory_path / filename


def _glob_match(value: str, pattern: str) -> bool:
    return fnmatchcase(value, pattern) or (pattern.startswith("**/") and fnmatchcase(value, pattern[3:]))
