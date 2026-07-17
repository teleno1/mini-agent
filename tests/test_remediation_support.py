from __future__ import annotations

import json

from remediation_support import (
    SUPERSEDED_REMEDIATION_CONTRACTS,
    run_fake_cli_journey,
)

from mini_agent.domain.sessions import SessionEventType


def test_fake_cli_journey_exposes_public_context_and_durable_evidence(tmp_path) -> None:
    task = "Explain Mini Agent"

    journey = run_fake_cli_journey(tmp_path, task)

    assert journey.exit_code == 0
    assert f"|   > {task}" in journey.output
    assert "|   > Mini Agent is a small, inspectable coding agent." in journey.output
    assert journey.context_frames
    assert journey.context_frames[0].manifest.request_id
    assert [event.event_type for event in journey.events] == [
        SessionEventType.SESSION_CREATED,
        SessionEventType.TURN_STARTED,
        SessionEventType.USER_MESSAGE,
        SessionEventType.CONTEXT_MANIFEST_RECORDED,
        SessionEventType.MODEL_REQUEST_STARTED,
        SessionEventType.MODEL_REQUEST_COMPLETED,
        SessionEventType.ASSISTANT_MESSAGE,
        SessionEventType.TURN_COMPLETED,
    ]
    assert journey.manifests

    manifest_text = json.dumps(journey.manifests[0], ensure_ascii=False)
    assert task not in manifest_text
    assert "content" not in manifest_text
    assert "api_key" not in manifest_text


def test_remediation_baseline_names_superseded_production_expectations() -> None:
    assert SUPERSEDED_REMEDIATION_CONTRACTS == {
        "automatic-plan-creation",
        "word-based-permission-confirmation",
        "flat-transcript-formatting",
        "raw-lifecycle-events-as-user-messages",
    }
