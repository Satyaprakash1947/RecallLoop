from datetime import datetime
from zoneinfo import ZoneInfo

from recallloop.api import RecallRequest, create_app
from recallloop.database import SQLiteMemoryStore
from recallloop.models import (
    Commitment,
    CommitmentStatus,
    IdentityConfidence,
    MemoryCreate,
    Source,
)
from recallloop.retrieval import RetrievalService


WHEN = datetime(2026, 12, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def source(text, speaker="speaker-7", when=WHEN):
    return Source(transcript=text, recorded_at=when, speaker_id=speaker)


def commitment(statement, evidence, **kwargs):
    return Commitment(statement=statement, status=kwargs.pop("status", CommitmentStatus.CONFIRMED),
                      evidence=evidence, actor=kwargs.pop("actor", "I"),
                      actor_confidence=kwargs.pop("actor_confidence", IdentityConfidence.CLEAR), **kwargs)


def save(store, transcript, statement, evidence=None, **kwargs):
    src = source(transcript)
    com = commitment(statement, evidence or transcript, **kwargs)
    return store.create_memory(MemoryCreate(source=src, commitment=com))


def test_database_persistence_after_reopening(tmp_path):
    path = tmp_path / "recall.db"
    first = SQLiteMemoryStore(path)
    record = save(first, "I will email the launch plan.", "Email launch plan")
    reopened = SQLiteMemoryStore(path)
    restored = reopened.retrieve_memory_with_evidence(record.memory_id)
    assert restored["commitment"]["statement"] == "Email launch plan"
    assert restored["source"]["transcript"] == "I will email the launch plan."


def test_exact_memory_retrieval_includes_original_quote_and_timestamp(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recall.db")
    record = save(store, "I will send Riya the budget on 2026-12-03.", "Send Riya the budget",
                  due_at=datetime(2026, 12, 3, tzinfo=ZoneInfo("Asia/Kolkata")), due_expression="2026-12-03")
    result = store.retrieve_memory_with_evidence(record.memory_id)
    assert result["evidence"][0]["quote"] == "I will send Riya the budget on 2026-12-03."
    assert result["source"]["recorded_at"] == WHEN.isoformat()


def test_retrieval_by_person(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recall.db")
    save(store, "I will send Riya the budget.", "Send Riya the budget", actor="Riya")
    assert len(store.find_commitments("Riya")) == 1


def test_retrieval_by_task(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recall.db")
    save(store, "I will send Riya the invoice.", "Send invoice to Riya", actor="Riya")
    assert store.check_existing_commitment("Riya", "send invoice")


def test_retrieval_by_due_date_uses_fts_index(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recall.db")
    save(store, "I will send Riya the budget on 2026-12-03.", "Send Riya the budget",
         due_at=datetime(2026, 12, 3, tzinfo=ZoneInfo("Asia/Kolkata")), due_expression="2026-12-03")
    results = store.search_memories("2026-12-03")
    assert results
    assert results[0]["commitment"]["due_at"].startswith("2026-12-03")


def test_missing_memory_returns_insufficient_evidence(tmp_path):
    service = RetrievalService(SQLiteMemoryStore(tmp_path / "recall.db"))
    result = service.answer_from_memory("What did I promise about the lighthouse?")
    assert result.retrieval_status == "insufficient_evidence"
    assert result.source_transcript is None


def test_ambiguous_memory_returns_insufficient_evidence(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recall.db")
    save(store, "I will send Riya the report today. Context one.", "Send Riya report", "I will send Riya the report today.", actor="Riya")
    save(store, "I will send Riya the report today. Context two.", "Send Riya report", "I will send Riya the report today.", actor="Riya")
    result = RetrievalService(store).answer_from_memory("When will Riya send the report?")
    assert result.retrieval_status == "insufficient_evidence"
    assert "ambiguous" in result.answer


def test_conflicting_records_are_not_collapsed_into_an_answer(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recall.db")
    save(store, "I will send Riya the report Monday.", "Send Riya report", "I will send Riya the report Monday.", actor="Riya")
    save(store, "I will send Riya the report Tuesday, 2026-12-08.", "Send Riya report", "I will send Riya the report Tuesday, 2026-12-08.", actor="Riya",
         due_at=datetime(2026, 12, 8, tzinfo=ZoneInfo("Asia/Kolkata")), due_expression="2026-12-08")
    result = RetrievalService(store).answer_from_memory("When will Riya send the report?")
    assert result.retrieval_status == "insufficient_evidence"
    assert "conflict" in result.answer


def test_correction_history_preserves_old_and_new_versions(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recall.db")
    first = save(store, "I will send Riya the report Monday.", "Send Riya report", actor="Riya")
    corrected_source = source("Correction: I will send Riya the report Tuesday.")
    corrected_commitment = commitment("Send Riya report Tuesday", "I will send Riya the report Tuesday.",
                                       actor="Riya", supersedes=first.commitment.commitment_id)
    corrected = store.create_memory(MemoryCreate(source=corrected_source, commitment=corrected_commitment))
    history = store.get_commitment_history(first.commitment.commitment_id)[0]
    assert len(history["versions"]) == 2
    assert any(item["event_type"] == "corrected" for item in history["events"])
    assert store.retrieve_memory_with_evidence(first.memory_id)["lifecycle"] == "superseded"
    assert store.retrieve_memory_with_evidence(corrected.memory_id)["source"]["transcript"] == corrected_source.transcript


def test_source_transcript_is_preserved_even_if_duplicate_submission_occurs(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recall.db")
    original = save(store, "I will call Riya tomorrow.", "Call Riya tomorrow", actor="Riya")
    repeated = save(store, "I will call Riya tomorrow.", "Call Riya tomorrow", actor="Riya")
    assert repeated.memory_id == original.memory_id
    assert repeated.duplicate is True
    assert len(store.search_memories("Riya tomorrow")) == 1


def test_completion_is_not_inferred_from_a_promise(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recall.db")
    save(store, "I will send Riya the report tomorrow.", "Send Riya report", actor="Riya")
    result = RetrievalService(store).answer_from_memory("Did I complete sending Riya the report?")
    assert result.retrieval_status == "insufficient_evidence"
    assert "No completion record" in result.answer


def test_dispute_and_verification_require_transcript_evidence(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recall.db")
    record = save(store, "I will send Riya the report.", "Send Riya report", actor="Riya")
    import pytest
    with pytest.raises(ValueError, match="exact transcript substring"):
        store.mark_disputed(record.memory_id, source("I did not say that."), "disputed", "Invented phrase")
    disputed_source = source("Correction: I will not send the report.")
    result = store.mark_disputed(record.memory_id, disputed_source, "Conflicting statement", disputed_source.transcript)
    assert result["lifecycle"] == "disputed"
    evidence_id = result["evidence"][-1]["evidence_id"]
    verification = store.record_verification(record.memory_id, evidence_id, "contradicts", verifier="reviewer")
    retrieved = store.retrieve_memory_with_evidence(record.memory_id)
    assert verification.result == "contradicts"
    assert len(retrieved["verification_records"]) == 1


def test_fastapi_routes_create_search_recall_commitment_and_history(tmp_path):
    app = create_app(tmp_path / "api.db")
    endpoints = {(route.path, method): route.endpoint for route in app.routes
                 if hasattr(route, "methods") for method in route.methods}
    body = {
        "source": {"transcript": "I will email Riya the agenda.", "recorded_at": WHEN.isoformat(), "speaker_id": "speaker-7"},
        "commitment": {"statement": "Email Riya agenda", "status": "confirmed", "evidence": "I will email Riya the agenda.",
                       "actor": "Riya", "actor_confidence": "clear"},
    }
    from recallloop.models import MemoryCreate
    created = endpoints[("/memories", "POST")](MemoryCreate.model_validate(body))
    commitment_id = created.commitment.commitment_id
    assert endpoints[("/memories/search", "GET")](query="Riya agenda")
    assert endpoints[("/commitments/{commitment_id}", "GET")](commitment_id=commitment_id)
    assert endpoints[("/commitments/{commitment_id}/history", "GET")](commitment_id=commitment_id)
    answer = endpoints[("/recall", "POST")](RecallRequest(query="What did Riya agree to do?"))
    assert answer.retrieval_status == "found"
