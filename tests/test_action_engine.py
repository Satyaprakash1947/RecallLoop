from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from recallloop.action_tools import MockNotificationTool, MockTicketTool
from recallloop.api import create_app
from recallloop.models import ActionStatus, Commitment, CommitmentStatus, IdentityConfidence, MemoryCreate, Source


NOW = datetime(2026, 12, 4, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def build_app(path, *, notification=None, ticket=None, authorized_verifiers=None):
    notification = notification or MockNotificationTool()
    ticket = ticket or MockTicketTool()
    app = create_app(path, authorized_verifiers=authorized_verifiers,
                     action_tools=[notification, ticket, app_cancellation_tool(path)])
    return app, notification, ticket


def app_cancellation_tool(path):
    # create_app owns the SQLiteMemoryStore instance for the same file.
    from recallloop.action_tools import CommitmentMutationTool
    from recallloop.database import SQLiteMemoryStore
    return CommitmentMutationTool(SQLiteMemoryStore(path))


def draft_action(key="draft-key", **overrides):
    body = {"action_type": "draft_message", "description": "Draft a reminder",
            "target": "Riya", "parameters": {"recipient": "Riya", "message": "Please confirm the repair window."},
            "idempotency_key": key}
    body.update(overrides)
    return body


def send_action(key="send-key", **overrides):
    body = {"action_type": "send_message", "description": "Send the approved repair update",
            "target": "Riya", "parameters": {"recipient": "Riya", "message": "The technician is scheduled for Friday."},
            "idempotency_key": key}
    body.update(overrides)
    return body


def approved(client, body, approver="operator-1"):
    created = client.post("/actions", json=body)
    assert created.status_code == 201
    action_id = created.json()["action_id"]
    response = client.post(f"/actions/{action_id}/approve", json={"approved_by": approver})
    assert response.status_code == 200
    return action_id


def test_action_creation_and_approval_policy(tmp_path):
    app, _, _ = build_app(tmp_path / "create.db")
    with TestClient(app) as client:
        draft = client.post("/actions", json=draft_action())
        assert draft.status_code == 201
        assert draft.json()["status"] == "proposed"
        send = client.post("/actions", json=send_action())
        assert send.status_code == 201
        assert send.json()["status"] == "awaiting_approval"
        assert send.json()["requires_approval"] is True


def test_consequential_execution_is_rejected_without_approval(tmp_path):
    app, notification, _ = build_app(tmp_path / "approval.db")
    with TestClient(app) as client:
        action_id = client.post("/actions", json=send_action()).json()["action_id"]
        response = client.post(f"/actions/{action_id}/execute")
        assert response.status_code == 409
        assert response.json()["detail"] == "action requires explicit approval before execution"
        assert notification.sent_messages == []
        assert client.get(f"/actions/{action_id}").json()["status"] == "awaiting_approval"


def test_mock_send_succeeds_once_and_is_idempotent(tmp_path):
    app, notification, _ = build_app(tmp_path / "send.db")
    with TestClient(app) as client:
        action_id = approved(client, send_action())
        response = client.post(f"/actions/{action_id}/execute")
        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"
        assert response.json()["result"]["recipient"] == "Riya"
        assert len(notification.sent_messages) == 1
        replay = client.post(f"/actions/{action_id}/execute")
        assert replay.status_code == 200
        assert len(notification.sent_messages) == 1


def test_simulated_failure_is_recorded_and_retry_needs_fresh_approval(tmp_path):
    app, notification, _ = build_app(tmp_path / "failure.db", notification=MockNotificationTool(fail_next=True))
    with TestClient(app) as client:
        action_id = approved(client, send_action())
        failed = client.post(f"/actions/{action_id}/execute")
        assert failed.status_code == 502
        assert failed.json()["status"] == "failed"
        assert failed.json()["retryable"] is True
        assert notification.sent_messages == []
        assert client.post(f"/actions/{action_id}/execute").status_code == 409
        assert client.post(f"/actions/{action_id}/approve", json={"approved_by": "operator-2"}).status_code == 200
        retried = client.post(f"/actions/{action_id}/execute")
        assert retried.status_code == 200
        assert retried.json()["status"] == "succeeded"
        assert len(notification.sent_messages) == 1


def test_same_idempotency_key_does_not_create_duplicate_and_rejects_changed_request(tmp_path):
    app, _, _ = build_app(tmp_path / "idempotency.db")
    with TestClient(app) as client:
        first = client.post("/actions", json=draft_action("same-key"))
        second = client.post("/actions", json=draft_action("same-key"))
        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["action_id"] == first.json()["action_id"]
        changed = draft_action("same-key", parameters={"recipient": "Riya", "message": "Different content"})
        assert client.post("/actions", json=changed).status_code == 409
        assert len(client.get("/actions").json()) == 1


def test_changed_parameters_invalidate_approval(tmp_path):
    app, _, _ = build_app(tmp_path / "changed.db")
    with TestClient(app) as client:
        action_id = approved(client, send_action())
        changed = app.state.action_store.update_action_parameters(action_id,
            {"recipient": "Riya", "message": "Updated details."})
        assert changed.status == ActionStatus.AWAITING_APPROVAL
        assert changed.approved_at is None
        assert client.post(f"/actions/{action_id}/execute").status_code == 409
        assert client.post(f"/actions/{action_id}/approve", json={"approved_by": "operator-2"}).status_code == 200
        assert client.post(f"/actions/{action_id}/execute").json()["result"]["message"] == "Updated details."
        events = client.get(f"/actions/{action_id}/history").json()
        assert any(event["event_type"] == "parameters_changed_approval_invalidated" for event in events)


def test_cancelled_action_cannot_execute_and_history_is_append_only(tmp_path):
    app, notification, _ = build_app(tmp_path / "cancel.db")
    with TestClient(app) as client:
        action = client.post("/actions", json=send_action()).json()
        action_id = action["action_id"]
        cancelled = client.post(f"/actions/{action_id}/cancel", json={"cancelled_by": "operator", "reason": "No longer needed"})
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert client.post(f"/actions/{action_id}/execute").status_code == 409
        assert notification.sent_messages == []
        events = client.get(f"/actions/{action_id}/history").json()
        assert [event["event_type"] for event in events] == ["created", "cancelled"]


def _create_commitment(app):
    transcript = "I will send the repair summary to Riya."
    source = Source(transcript=transcript, recorded_at=NOW, speaker_id="speaker-1")
    commitment = Commitment(statement="Send repair summary", status=CommitmentStatus.CONFIRMED,
        evidence=transcript, actor="I", actor_confidence=IdentityConfidence.CLEAR, beneficiary="Riya")
    return app.state.repository.create_memory(MemoryCreate(source=source, commitment=commitment))


def test_action_verification_requires_authorized_verifier_and_does_not_change_commitment(tmp_path):
    app, _, _ = build_app(tmp_path / "verify.db", authorized_verifiers={"reviewer-1"})
    with TestClient(app) as client:
        memory = _create_commitment(app)
        evidence_id = str(memory.evidence[0].evidence_id)
        body = send_action("verified-send", commitment_id=str(memory.commitment.commitment_id),
                           evidence_references=[evidence_id], verification_required=True)
        action_id = approved(client, body)
        completed = client.post(f"/actions/{action_id}/execute")
        assert completed.json()["status"] == "verification_pending"
        forbidden = client.post(f"/actions/{action_id}/verify", json={"verifier": "unlisted",
            "evidence_references": [evidence_id], "notes": "Checked the action receipt."})
        assert forbidden.status_code == 403
        verified = client.post(f"/actions/{action_id}/verify", json={"verifier": "reviewer-1",
            "evidence_references": [evidence_id], "notes": "Checked the action receipt."})
        assert verified.json()["status"] == "verified"
        assert app.state.repository.find_commitment(memory.commitment.commitment_id)["commitment"]["status"] == "confirmed"


def test_ticket_tool_failure_and_success(tmp_path):
    app, _, ticket = build_app(tmp_path / "ticket.db", ticket=MockTicketTool(fail_next=True))
    with TestClient(app) as client:
        body = {"action_type": "create_ticket", "description": "Open maintenance ticket",
                "parameters": {"title": "Generator repair", "description": "Generator does not start."},
                "idempotency_key": "ticket-repair"}
        action_id = approved(client, body)
        failed = client.post(f"/actions/{action_id}/execute")
        assert failed.status_code == 502
        assert failed.json()["retryable"] is True
        assert client.post(f"/actions/{action_id}/approve", json={"approved_by": "operator"}).status_code == 200
        success = client.post(f"/actions/{action_id}/execute")
        assert success.json()["status"] == "succeeded"
        assert success.json()["result"]["ticket_ref"] in ticket.tickets


def test_action_and_history_persist_after_app_restart(tmp_path):
    path = tmp_path / "restart.db"
    app, _, _ = build_app(path)
    with TestClient(app) as client:
        action_id = client.post("/actions", json=draft_action("persist-key")).json()["action_id"]
    reopened, _, _ = build_app(path)
    with TestClient(reopened) as client:
        action = client.get(f"/actions/{action_id}")
        assert action.status_code == 200
        assert action.json()["status"] == "proposed"
        assert [event["event_type"] for event in client.get(f"/actions/{action_id}/history").json()] == ["created"]


def test_commitment_verification_requires_configured_authorized_verifier(tmp_path):
    app, _, _ = build_app(tmp_path / "commitment-verifier.db", authorized_verifiers={"reviewer-1"})
    with TestClient(app) as client:
        memory = _create_commitment(app)
        completion = Source(transcript="I finished sending the repair summary.", recorded_at=NOW, speaker_id="speaker-1")
        commitment_id = str(memory.commitment.commitment_id)
        pending = client.patch(f"/commitments/{commitment_id}/status", json={
            "status": "completed_pending_verification", "source": completion.model_dump(mode="json"),
            "evidence": completion.transcript,
        })
        assert pending.status_code == 200
        claim_evidence = app.state.repository.find_commitment(commitment_id)["evidence"][-1]["evidence_id"]
        app.state.repository.record_verification(memory.memory_id, claim_evidence, "supports", verifier="reviewer-1")
        request = {"status": "verified", "verifier": "unauthorized", "detail": "Reviewed."}
        assert client.patch(f"/commitments/{commitment_id}/status", json=request).status_code == 403
        request["verifier"] = "reviewer-1"
        verified = client.patch(f"/commitments/{commitment_id}/status", json=request)
        assert verified.status_code == 200
        assert verified.json()["commitment"]["status"] == "verified"


def test_ticket_tool_supports_reassignment_after_ticket_creation(tmp_path):
    app, _, ticket = build_app(tmp_path / "ticket-update.db")
    with TestClient(app) as client:
        create_body = {"action_type": "create_ticket", "description": "Open repair ticket",
            "parameters": {"title": "Pump repair"}, "idempotency_key": "create-pump-ticket"}
        create_id = approved(client, create_body)
        created = client.post(f"/actions/{create_id}/execute").json()
        ticket_ref = created["result"]["ticket_ref"]
        reassign_body = {"action_type": "reassign_technician", "description": "Assign a technician",
            "parameters": {"ticket_ref": ticket_ref, "technician": "Technician-2"},
            "idempotency_key": "reassign-pump-ticket"}
        reassign_id = approved(client, reassign_body)
        response = client.post(f"/actions/{reassign_id}/execute")
        assert response.json()["status"] == "succeeded"
        assert ticket.tickets[ticket_ref]["assignee"] == "Technician-2"


def test_approved_cancellation_action_updates_commitment_with_source_evidence(tmp_path):
    app, _, _ = build_app(tmp_path / "cancel-commitment.db")
    with TestClient(app) as client:
        memory = _create_commitment(app)
        source = Source(transcript="I will not send the repair summary; cancel that commitment.",
                        recorded_at=NOW, speaker_id="speaker-1")
        body = {"action_type": "cancel_commitment", "description": "Cancel the report promise",
            "commitment_id": str(memory.commitment.commitment_id),
            "parameters": {"source": source.model_dump(mode="json"), "evidence": source.transcript},
            "idempotency_key": "cancel-report-promise"}
        action_id = approved(client, body)
        result = client.post(f"/actions/{action_id}/execute")
        assert result.json()["status"] == "succeeded"
        assert app.state.repository.find_commitment(memory.commitment.commitment_id)["commitment"]["status"] == "cancelled"


def test_http_endpoints_return_validation_and_not_found_codes(tmp_path):
    app, _, _ = build_app(tmp_path / "http-errors.db")
    with TestClient(app) as client:
        invalid = client.post("/actions", json={"action_type": "send_message", "description": "Incomplete",
            "parameters": {}, "idempotency_key": "invalid"})
        assert invalid.status_code == 422
        assert client.get("/actions/00000000-0000-0000-0000-000000000000").status_code == 404
        assert client.get("/actions/00000000-0000-0000-0000-000000000000/history").status_code == 404
