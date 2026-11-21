from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from recallloop.api import create_app
from recallloop.models import Commitment, CommitmentStatus, IdentityConfidence, MemoryCreate, Source
from recallloop.voice import VoiceConfigurationError, VoiceProviderError


class FakeVoiceProvider:
    app_id = "public-test-app"

    def __init__(self, *, fail_start=False, fail_stop=False):
        self.fail_start, self.fail_stop = fail_start, fail_stop
        self.started, self.stopped = [], []

    def start(self, channel, user_uid, idempotency_key, memory_session_id):
        if self.fail_start:
            raise VoiceProviderError("simulated session failure")
        self.started.append((channel, user_uid, idempotency_key, memory_session_id))
        return "agora-test-session"

    def stop(self, provider_session_id):
        if self.fail_stop:
            raise VoiceProviderError("simulated stop failure")
        self.stopped.append(provider_session_id)

    def user_tokens(self, channel, uid):
        return "rtc-test-token", "rtm-test-token", self.app_id


def test_voice_session_start_and_stop_over_http(tmp_path):
    provider = FakeVoiceProvider()
    app = create_app(tmp_path / "voice.db", voice_provider=provider)
    with TestClient(app) as client:
        started = client.post("/api/voice/session/start", json={"user_id": "test-user"})
        assert started.status_code == 200
        body = started.json()
        assert body["rtc_token"] == "rtc-test-token"
        assert body["rtm_token"] == "rtm-test-token"
        assert body["session"]["status"] == "running"
        assert body["session"]["channel"] == provider.started[0][0]
        stopped = client.post(f"/api/voice/session/{body['session']['session_id']}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"
        assert provider.stopped == ["agora-test-session"]


def test_agora_provider_errors_return_safe_http_errors(tmp_path):
    app = create_app(tmp_path / "voice-fail.db", voice_provider=FakeVoiceProvider(fail_start=True))
    with TestClient(app) as client:
        result = client.post("/api/voice/session/start", json={})
        assert result.status_code == 502
        assert result.json()["detail"] == "simulated session failure"


def test_failed_stop_remains_retryable(tmp_path):
    provider = FakeVoiceProvider(fail_stop=True)
    app = create_app(tmp_path / "voice-stop-retry.db", voice_provider=provider)
    with TestClient(app) as client:
        started = client.post("/api/voice/session/start", json={}).json()
        session_id = started["session"]["session_id"]
        failed = client.post(f"/api/voice/session/{session_id}/stop")
        assert failed.status_code == 502
        assert client.get(f"/api/voice/session/{session_id}").json()["status"] == "running"
        provider.fail_stop = False
        retried = client.post(f"/api/voice/session/{session_id}/stop")
        assert retried.status_code == 200
        assert retried.json()["status"] == "stopped"


def test_missing_vendor_credentials_fail_closed(tmp_path, monkeypatch):
    for key in ("AGORA_APP_ID", "AGORA_APP_CERTIFICATE", "AGORA_AGENT_ID"):
        monkeypatch.delenv(key, raising=False)
    app = create_app(tmp_path / "voice-config.db")
    with TestClient(app) as client:
        result = client.post("/api/voice/session/start", json={})
    assert result.status_code == 503
    assert "AGORA_APP_ID" in result.json()["detail"]


def test_transcript_survives_app_restart(tmp_path):
    path = tmp_path / "transcript.db"
    provider = FakeVoiceProvider()
    with TestClient(create_app(path, voice_provider=provider)) as client:
        started = client.post("/api/voice/session/start", json={}).json()
        session_id = started["session"]["session_id"]
        result = client.post(f"/api/voice/transcript/{session_id}", json={"lines":[
            {"role":"user","text":"What happened to the repair?","final":True},
            {"role":"agent","text":"I found one source transcript.","final":True},
        ]})
        assert result.status_code == 200
    with TestClient(create_app(path, voice_provider=provider)) as restarted:
        persisted = restarted.get(f"/api/voice/transcript/{session_id}")
        assert persisted.status_code == 200
        assert [line["role"] for line in persisted.json()["lines"]] == ["user", "agent"]


def test_voice_recall_uses_existing_evidence_and_completion_policy(tmp_path):
    app = create_app(tmp_path / "memory.db", voice_provider=FakeVoiceProvider())
    source = Source(transcript="Ravi said: I will repair the water leakage by 9 AM.",
                    recorded_at=datetime(2026, 11, 20, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    commitment = Commitment(statement="Repair the water leakage", status=CommitmentStatus.CONFIRMED,
        evidence=source.transcript, actor="Ravi", actor_confidence=IdentityConfidence.CLEAR,
        due_expression="9 AM", notes=None)
    # Relative time is intentionally omitted here: this test targets retrieval provenance.
    commitment.due_expression = None
    app.state.repository.create_memory(MemoryCreate(source=source, commitment=commitment))
    with TestClient(app) as client:
        answer = client.post("/api/voice/evidence", json={"query":"Ravi water leakage repair"})
        assert answer.status_code == 200
        assert answer.json()["retrieval_status"] != "insufficient_evidence"
        assert answer.json()["source_transcript"] == source.transcript
        completion = client.post("/api/voice/message", json={"message":"Is the repair completed?"})
        assert completion.status_code == 200
        assert completion.json()["retrieval_status"] == "insufficient_evidence"
        context = client.post("/api/voice/tools/recall", json={"query":"Is the water leakage repair completed?"})
        assert context.status_code == 200
        assert context.json()["retrieval_status"] == "insufficient_evidence"
        assert context.json()["completion_evidence_found"] is False
        assert context.json()["commitment"]["actor"] == "Ravi"


def test_voice_action_proposal_cannot_skip_approval(tmp_path):
    app = create_app(tmp_path / "action-proposal.db", voice_provider=FakeVoiceProvider())
    proposal = {"action_type":"reassign_technician", "description":"Reassign a sourced maintenance ticket",
        "target":"Technician name is explicitly provided", "parameters":{"ticket_ref":"MOCK-000010","technician":"Asha"},
        "idempotency_key":"voice-proposal-1"}
    with TestClient(app) as client:
        response = client.post("/api/voice/tools/action-proposal", json=proposal)
        assert response.status_code == 201
        assert response.json()["status"] == "awaiting_approval"
        action_id=response.json()["action_id"]
        execute=client.post(f"/actions/{action_id}/execute")
        assert execute.status_code == 409


def test_voice_extraction_validates_exact_transcript_before_memory_storage(tmp_path):
    app = create_app(tmp_path / "voice-extract.db", voice_provider=FakeVoiceProvider())
    with TestClient(app) as client:
        started = client.post("/api/voice/session/start", json={}).json()
        session_id = started["session"]["session_id"]
        transcript = "I will send the lab safety checklist to Riya by Friday."
        saved = client.post(f"/api/voice/transcript/{session_id}", json={"lines":[
            {"role":"user","text":transcript,"final":True}
        ]})
        assert saved.status_code == 200
        body = {"session_id":session_id,"content":"Lab checklist follow-up","extraction_response":{
            "commitments":[{"statement":"Send the lab safety checklist to Riya",
                "status":"confirmed","evidence":transcript,"actor":"I",
                "actor_confidence":"clear","beneficiary":"Riya","due_expression":"Friday"}],
            "unresolved":[]}}
        extracted = client.post(f"/api/voice/session/{session_id}/extract", json=body)
        assert extracted.status_code == 200, extracted.text
        assert extracted.json()["commitments_stored"] == 1
        memory = app.state.repository.retrieve_memory_with_evidence(extracted.json()["memory_ids"][0])
        assert memory["source"]["transcript"] == transcript

        invented = {**body,"extraction_response":{"commitments":[{
            "statement":"Send a report","status":"confirmed","evidence":"I will send the invented report",
            "actor":"I","actor_confidence":"clear"}],"unresolved":[]}}
        invalid = client.post(f"/api/voice/session/{session_id}/extract", json=invented)
        assert invalid.status_code == 422
