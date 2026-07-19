"""Independent oracle for the SH-02 typed Tool Result pairing trial.

The oracle imports the candidate checkout's source package but uses fresh
temporary workspaces, Sessions, and a scripted Provider outside the checkout.
It deliberately does not import the repository test suite.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory


def _events(
    *, call_id: str, name: str, arguments: dict[str, object], final: str
) -> tuple[object, ...]:
    from mini_agent.domain.streams import (
        ResponseCompleted,
        ResponseStarted,
        TextDelta,
        ToolCallArgumentDelta,
        ToolCallCompleted,
        ToolCallStarted,
        UsageReported,
    )

    encoded = json.dumps(arguments, separators=(",", ":"))
    return (
        ResponseStarted(request_id=f"request-{call_id}"),
        ToolCallStarted(tool_call_id=call_id, name=name),
        ToolCallArgumentDelta(tool_call_id=call_id, arguments=encoded),
        ToolCallCompleted(tool_call_id=call_id),
        UsageReported(input_tokens=3, output_tokens=2),
        ResponseCompleted(stop_reason="tool_calls"),
    ), (
        ResponseStarted(request_id=f"request-{call_id}-final"),
        TextDelta(text=final),
        UsageReported(input_tokens=4, output_tokens=2),
        ResponseCompleted(),
    )


def _message_shape(frame: object) -> dict[str, object]:
    from mini_agent.context import ContextFrame, ContextLayerName
    from mini_agent.domain.messages import AssistantMessage, ToolResultMessage, UserMessage

    if not isinstance(frame, ContextFrame):
        raise AssertionError("oracle expected a ContextFrame")
    history = [
        item.message
        for item in frame.messages
        if item.layer is ContextLayerName.HISTORY and item.message is not None
    ]
    typed = []
    for item in history:
        if isinstance(item, UserMessage):
            typed.append({"role": "user", "content": item.content})
        elif isinstance(item, AssistantMessage):
            typed.append(
                {
                    "role": "assistant",
                    "content": item.content,
                    "tool_call_ids": [call.tool_call_id for call in item.tool_calls],
                }
            )
        elif isinstance(item, ToolResultMessage):
            typed.append(
                {
                    "role": "tool",
                    "tool_call_id": item.tool_call_id,
                    "outcome": item.outcome,
                    "content": item.content,
                }
            )
        else:
            raise AssertionError(f"unexpected provider-visible history message: {type(item)!r}")
    current_user = next(
        item.content for item in frame.messages if item.layer is ContextLayerName.CURRENT_USER
    )
    return {
        "history": [{"role": "user", "content": current_user}, *typed],
        "manifest": frame.manifest.as_dict(),
    }


async def _run_case(workspace: Path, case: dict[str, object]) -> dict[str, object]:
    from mini_agent.adapters.clocks import DeterministicClock
    from mini_agent.adapters.ids import DeterministicIdGenerator
    from mini_agent.adapters.session_store import SessionStore
    from mini_agent.application.agent import AgentTurnApplication
    from mini_agent.context import ContextBuilder
    from mini_agent.domain.sessions import SessionEventType
    from mini_agent.providers.fake import ScriptedFakeModelProvider
    from mini_agent.tools.contracts import ToolRegistry
    from mini_agent.tools.files import ReadFileTool, SearchFilesTool
    from mini_agent.tools.workspace import Workspace

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
    clock = DeterministicClock(datetime(2026, 7, 19, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(workspace, clock=clock, id_generator=ids)
    first, second = _events(
        call_id=str(case["call_id"]),
        name=str(case["name"]),
        arguments=case["arguments"],
        final=str(case["final"]),
    )
    provider = ScriptedFakeModelProvider(responses=(first, second))
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(workspace),
        tool_registry=ToolRegistry([ReadFileTool(), SearchFilesTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        context_builder=ContextBuilder(str(workspace)),
    )
    result = await application.run(str(case["task"]))
    snapshot = store.read(result.session_id)
    if len(provider.requests) != 2:
        raise AssertionError(f"expected two provider requests, got {len(provider.requests)}")
    second_request = _message_shape(provider.requests[1])
    history = second_request["history"]
    expected_id = str(case["call_id"])
    tool_results = [item for item in history if item["role"] == "tool"]
    pairing_ok = [item["role"] for item in history] == ["user", "assistant", "tool"] and (
        len(tool_results) == 1 and tool_results[0]["tool_call_id"] == expected_id
    )
    audit_types = {
        SessionEventType.TOOL_PROPOSED.value,
        SessionEventType.TOOL_VALIDATED.value,
        SessionEventType.TOOL_STARTED.value,
    }
    event_types = [
        getattr(event.event_type, "value", event.event_type) for event in snapshot.events
    ]
    audit_ok = bool(audit_types.intersection(event_types))
    terminal_types = {
        SessionEventType.TOOL_COMPLETED.value,
        SessionEventType.TOOL_FAILED.value,
        SessionEventType.TOOL_INTERRUPTED.value,
    }
    terminal = [
        event
        for event in snapshot.events
        if getattr(event.event_type, "value", event.event_type) in terminal_types
    ]
    terminal_ok = len(terminal) == 1 and terminal[0].payload.get("tool_call_id") == expected_id
    return {
        "case": case["case_name"],
        "passed": pairing_ok and audit_ok and terminal_ok,
        "checks": {
            "typed_pairing": pairing_ok,
            "durable_audit": audit_ok,
            "durable_terminal": terminal_ok,
        },
        "session_id": result.session_id,
        "turn_id": result.turn_id,
        "provider_request_count": len(provider.requests),
        "provider_history": history,
        "manifest": second_request["manifest"],
        "event_types": event_types,
        "terminal_event": terminal[0].to_record(),
        "result_outcome": result.tool_results[0].outcome,
        "result_text": result.tool_results[0].content,
        "event_sha256": hashlib.sha256(
            (workspace / ".mini-agent" / "sessions" / result.session_id / "events.jsonl")
            .read_bytes()
        ).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    candidate = args.workspace.resolve()
    sys.path.insert(0, str(candidate / "src"))
    cases = (
        {
            "case_name": "successful-read",
            "call_id": "oracle-success",
            "task": "inspect the sample file",
            "name": "read_file",
            "arguments": {"path": "sample.py"},
            "final": "The file was inspected.",
        },
        {
            "case_name": "invalid-tool",
            "call_id": "oracle-invalid",
            "task": "diagnose the invalid operation",
            "name": "missing_tool",
            "arguments": {},
            "final": "The invalid operation was reported.",
        },
        {
            "case_name": "denied-path",
            "call_id": "oracle-denied",
            "task": "diagnose the denied path",
            "name": "read_file",
            "arguments": {"path": "../outside.txt"},
            "final": "The path was denied.",
        },
    )
    # The oracle workspace is outside the model-writable checkout.
    with TemporaryDirectory(prefix="mini-agent-sh02-oracle-") as temp:
        root = Path(temp)
        results = []
        for case in cases:
            try:
                results.append(asyncio.run(_run_case(root / case["case_name"], case)))
            except Exception as exc:  # noqa: BLE001 - record oracle evidence for a failed seed
                results.append(
                    {
                        "case": case["case_name"],
                        "passed": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    passed = all(item.get("passed", True) for item in results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {"oracle": "sh02-v1", "passed": passed, "cases": results},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"oracle": "sh02-v1", "passed": passed, "cases": len(results)}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
