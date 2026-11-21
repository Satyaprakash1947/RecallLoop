from datetime import datetime
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from recallloop.api import create_app
from recallloop.database import SQLiteMemoryStore
from recallloop.lifecycle import LifecycleTransitionError
from recallloop.models import (
    Commitment, CommitmentStatus, CommitmentStatusUpdate, IdentityConfidence, MemoryCreate, Source,
)
from recallloop.retrieval import RetrievalService


IST = ZoneInfo("Asia/Kolkata")
BASE = datetime(2026, 12, 2, 10, 0, tzinfo=IST)


def make_source(transcript: str, recorded_at: datetime = BASE) -> Source:
    return Source(transcript=transcript, recorded_at=recorded_at, speaker_id="speaker-1")


def create_commitment(store, statement="Send the report", *, status=CommitmentStatus.CONFIRMED,
                      actor="Riya", confidence=IdentityConfidence.CLEAR, due=None, transcript=None):
    transcript = transcript or f"I will {statement.casefold()} for Riya."
    source = make_source(transcript)
    commitment = Commitment(statement=statement, status=status, evidence=transcript, actor=actor,
                            actor_confidence=confidence, due_expression=due)
    return store.create_memory(MemoryCreate(source=source, commitment=commitment))


def create_dated(store, *, status=CommitmentStatus.CONFIRMED, date="2026-12-01",
                 statement="Repair the generator", actor="Riya", confidence=IdentityConfidence.CLEAR):
    transcript = f"I will {statement.casefold()} for Riya by {date}."
    source = make_source(transcript)
    commitment = Commitment(statement=statement, status=status, evidence=transcript, actor=actor,
                            actor_confidence=confidence, due_expression=date)
    return store.create_memory(MemoryCreate(source=source, commitment=commitment))


def test_lifecycle_transitions_store_acceptance_and_history(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "life.db")
    proposed = create_commitment(store, status=CommitmentStatus.PROPOSED,
                                 transcript="I could send the report if I have time.", actor="I")
    commitment_id = proposed.commitment.commitment_id
    acceptance = make_source("I accept responsibility. I will send the report by Friday as Riya.")
    accepted = store.transition_commitment_status(commitment_id, CommitmentStatus.CONFIRMED,
        source=acceptance, evidence_quote="I will send the report by Friday as Riya.", actor="Riya")
    assert accepted["commitment"]["status"] == "confirmed"
    assert accepted["commitment"]["acceptance_evidence"] == "I will send the report by Friday as Riya."
    assert accepted["commitment"]["actor"] == "Riya"

    progress_source = make_source("I started working on the report.")
    in_progress = store.transition_commitment_status(commitment_id, CommitmentStatus.IN_PROGRESS,
        source=progress_source, evidence_quote=progress_source.transcript)
    assert in_progress["commitment"]["status"] == "in_progress"
    history = store.get_commitment_history(commitment_id)[0]["events"]
    assert [event["new_status"] for event in history if event["event_type"] == "status_transition"] == ["confirmed", "in_progress"]


def test_invalid_lifecycle_transition_is_rejected(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "invalid.db")
    proposed = create_commitment(store, status=CommitmentStatus.PROPOSED,
                                 transcript="I might send the report.", actor="I")
    with pytest.raises(LifecycleTransitionError, match="invalid commitment transition"):
        store.transition_commitment_status(proposed.commitment.commitment_id, CommitmentStatus.VERIFIED)
    with pytest.raises(LifecycleTransitionError, match="computed by deadline monitoring"):
        store.transition_commitment_status(proposed.commitment.commitment_id, CommitmentStatus.OVERDUE)
    with pytest.raises(ValueError, match="source transcript and evidence"):
        store.transition_commitment_status(proposed.commitment.commitment_id, CommitmentStatus.CONFIRMED)


def test_completion_claim_stays_pending_until_supported_verification(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "verify.db")
    record = create_commitment(store)
    cid = record.commitment.commitment_id
    future = make_source("I will send the report tomorrow.")
    with pytest.raises(ValueError, match="explicit completion claim"):
        store.transition_commitment_status(cid, CommitmentStatus.COMPLETED_PENDING_VERIFICATION,
            source=future, evidence_quote=future.transcript)
    claim = make_source("I finished sending the report; please verify it.")
    pending = store.transition_commitment_status(cid, CommitmentStatus.COMPLETED_PENDING_VERIFICATION,
        source=claim, evidence_quote=claim.transcript)
    assert pending["commitment"]["status"] == "completed_pending_verification"
    with pytest.raises(ValueError, match="supported verification record"):
        store.transition_commitment_status(cid, CommitmentStatus.VERIFIED)
    # Verifying only the original promise cannot establish that it was completed.
    store.record_verification(record.memory_id, pending["evidence"][0]["evidence_id"], "supports", verifier="reviewer-1")
    with pytest.raises(ValueError, match="supported verification record"):
        store.transition_commitment_status(cid, CommitmentStatus.VERIFIED, verifier="reviewer-1")
    evidence_id = pending["evidence"][-1]["evidence_id"]
    store.record_verification(record.memory_id, evidence_id, "supports", verifier="reviewer-1")
    verified = store.transition_commitment_status(cid, CommitmentStatus.VERIFIED, verifier="reviewer-1")
    assert verified["commitment"]["status"] == "verified"
    events = store.get_commitment_history(cid)[0]["events"]
    assert any(event["event_type"] == "verification_recorded" for event in events)


def test_overdue_and_upcoming_respect_date_only_deadlines_and_kolkata_timezone(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "deadlines.db")
    overdue = create_dated(store, date="2026-12-01")
    upcoming = create_dated(store, date="2026-12-04", statement="Deliver the replacement")
    proposed = create_dated(store, date="2026-12-01", statement="Maybe inspect the site", status=CommitmentStatus.PROPOSED)
    disputed = create_dated(store, date="2026-12-01", statement="Confirm disputed repair")
    dispute_source = make_source("Riya disputes the repair commitment.")
    store.mark_disputed(disputed.memory_id, dispute_source, "Owner dispute", dispute_source.transcript)
    # 2026-12-02 18:30 UTC is 2026-12-03 00:00 in Kolkata.
    reference = datetime(2026, 12, 2, 18, 30, tzinfo=ZoneInfo("UTC"))
    service = RetrievalService(store)
    assert {row["memory_id"] for row in service.get_overdue_commitments(reference)} == {str(overdue.memory_id)}
    assert {row["memory_id"] for row in service.get_upcoming_commitments(reference)} == {str(upcoming.memory_id)}
    assert proposed.memory_id not in {row["memory_id"] for row in service.get_overdue_commitments(reference)}
    assert disputed.memory_id not in {row["memory_id"] for row in service.get_overdue_commitments(reference)}
    with pytest.raises(ValueError, match="timezone-aware"):
        service.get_overdue_commitments(datetime(2026, 12, 3))


def test_due_date_is_not_overdue_until_end_of_local_due_day(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "due-day.db")
    record = create_dated(store, date="2026-12-02")
    before_end = datetime(2026, 12, 2, 23, 59, tzinfo=IST)
    remaining = store.calculate_time_remaining(record.commitment.commitment_id, before_end)
    assert remaining["is_overdue"] is False
    assert store.get_overdue_commitments(before_end) == []


def test_mark_overdue_changes_only_confirmed_past_due_records_and_preserves_history(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "mark.db")
    confirmed = create_dated(store)
    proposed = create_dated(store, status=CommitmentStatus.PROPOSED, statement="Maybe inspect pump")
    disputed = create_dated(store, statement="Disputed check")
    dispute_source = make_source("Riya disputes the overdue check.")
    store.mark_disputed(disputed.memory_id, dispute_source, "Disputed", dispute_source.transcript)
    marked = store.mark_overdue_commitments(datetime(2026, 12, 2, 12, 0, tzinfo=IST))
    assert [item["commitment"]["commitment_id"] for item in marked] == [str(confirmed.commitment.commitment_id)]
    assert store.find_commitment(confirmed.commitment.commitment_id)["commitment"]["status"] == "overdue"
    assert store.find_commitment(proposed.commitment.commitment_id)["commitment"]["status"] == "proposed"
    assert store.find_commitment(disputed.commitment.commitment_id)["commitment"]["status"] == "disputed"
    history = store.get_commitment_history(confirmed.commitment.commitment_id)[0]["events"]
    assert any(event["event_type"] == "deadline_overdue" for event in history)


def test_responsible_person_ambiguous_and_unassigned_views(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "owners.db")
    named = create_commitment(store, statement="Email the customer", actor="Riya")
    ambiguous = create_commitment(store, statement="Prepare site notes", actor="Someone",
        confidence=IdentityConfidence.AMBIGUOUS, transcript="I will prepare site notes; someone owns the follow-up.")
    unassigned = create_commitment(store, statement="Review the checklist", actor=None)
    assert len(store.list_commitments(responsible_person="Riya")) == 1
    assert store.get_ambiguous_commitments()[0]["memory_id"] == str(ambiguous.memory_id)
    assert store.get_unassigned_commitments()[0]["memory_id"] == str(unassigned.memory_id)
    assert named.memory_id != unassigned.memory_id


def test_dependencies_block_until_verified_and_report_missed_deadline(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "deps.db")
    dependent = create_commitment(store, statement="Install the replacement",
        transcript="I will install the replacement for Riya after the pump repair.")
    prerequisite = create_dated(store, statement="Repair the pump", date="2026-12-01")
    store.add_dependency(dependent.commitment.commitment_id, prerequisite.commitment.commitment_id,
                         "after the pump repair")
    reference = datetime(2026, 12, 3, 10, 0, tzinfo=IST)
    state = store.get_commitment_dependencies(dependent.commitment.commitment_id, reference)
    assert state["blocked"] is True
    assert state["dependencies"][0]["missed_deadline"] is True
    assert state["dependencies"][0]["resolved"] is False

    claim = make_source("I completed the pump repair.")
    pending = store.transition_commitment_status(prerequisite.commitment.commitment_id,
        CommitmentStatus.COMPLETED_PENDING_VERIFICATION, source=claim, evidence_quote=claim.transcript)
    store.record_verification(prerequisite.memory_id, pending["evidence"][-1]["evidence_id"], "supports", verifier="reviewer")
    store.transition_commitment_status(prerequisite.commitment.commitment_id, CommitmentStatus.VERIFIED, verifier="reviewer")
    assert store.get_commitment_dependencies(dependent.commitment.commitment_id, reference)["blocked"] is False


def test_dependency_cycle_is_rejected_and_blocked_tasks_are_detected(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "cycle.db")
    first = create_commitment(store, statement="Prepare component",
        transcript="I will prepare the component after installation for Riya.")
    second = create_commitment(store, statement="Install component",
        transcript="I will install the component after preparation for Riya.")
    store.add_dependency(first.commitment.commitment_id, second.commitment.commitment_id, "after installation")
    assert str(first.memory_id) in {item["memory_id"] for item in store.get_blocked_commitments(BASE)}
    with pytest.raises(ValueError, match="circular dependency"):
        store.add_dependency(second.commitment.commitment_id, first.commitment.commitment_id, "after preparation")
    with pytest.raises(ValueError, match="cannot depend on itself"):
        store.add_dependency(first.commitment.commitment_id, first.commitment.commitment_id, "after installation")


def test_conflicts_report_deadlines_duplicates_contradictions_and_revisions(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "conflicts.db")
    first = create_dated(store, date="2026-12-04", statement="Send the proposal")
    second = create_dated(store, date="2026-12-05", statement="Send the proposal")
    kinds = {item["type"] for item in store.detect_conflicts(first.commitment.commitment_id)}
    assert {"conflicting_deadlines", "duplicate_active_commitments"} <= kinds

    disputed = create_commitment(store, statement="Send the proposal", actor="Riya")
    dispute_source = make_source("Riya disputes the proposal commitment.")
    store.mark_disputed(disputed.memory_id, dispute_source, "Contradictory record", dispute_source.transcript)
    assert "contradictory_records" in {item["type"] for item in store.detect_conflicts(first.commitment.commitment_id)}

    corrected_source = make_source("Correction: I will send the proposal Friday, says Riya.")
    corrected_commitment = Commitment(statement="Send the proposal", status=CommitmentStatus.CONFIRMED,
        evidence=corrected_source.transcript, actor="Riya", actor_confidence=IdentityConfidence.CLEAR,
        supersedes=first.commitment.commitment_id)
    corrected = store.create_memory(MemoryCreate(source=corrected_source, commitment=corrected_commitment))
    assert any(item["type"] == "revised" for item in store.detect_conflicts(first.commitment.commitment_id))
    versions = store.get_commitment_history(first.commitment.commitment_id)[0]["versions"]
    assert len(versions) == 2
    assert store.find_commitment(disputed.commitment.commitment_id)["commitment"]["status"] == "disputed"
    assert corrected.commitment.supersedes == first.commitment.commitment_id


def test_recovery_options_are_suggestions_and_never_run_actions(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "recovery.db")
    record = create_dated(store, statement="Repair the generator", date="2026-12-01")
    service = RetrievalService(store)
    result = service.get_recovery_recommendations(record.commitment.commitment_id,
        datetime(2026, 12, 3, 10, 0, tzinfo=IST))
    actions = [item["action"] for item in result["recommendations"]]
    assert result["automatic_execution"] is False
    assert any("Contact Riya" in action for action in actions)
    assert any("alternative technician" in action for action in actions)
    assert any("revised deadline" in action for action in actions)
    assert any("authorized owner" in action for action in actions)


def test_intelligence_api_routes_are_registered_and_callable(tmp_path):
    app = create_app(tmp_path / "intelligence-api.db")
    routes = {(route.path, method): route.endpoint for route in app.routes
              if hasattr(route, "methods") for method in route.methods}
    required = {
        ("/commitments", "GET"), ("/commitments/overdue", "GET"),
        ("/commitments/upcoming", "GET"), ("/commitments/{commitment_id}/status", "PATCH"),
        ("/commitments/{commitment_id}/dependencies", "GET"),
        ("/commitments/{commitment_id}/recovery-options", "GET"),
    }
    assert required <= routes.keys()
    assert routes[("/commitments", "GET")](ownership=None) == []
    assert routes[("/commitments/overdue", "GET")](BASE) == []
    assert routes[("/commitments/upcoming", "GET")](BASE, days=7) == []
    record = create_commitment(app.state.repository)
    cid = record.commitment.commitment_id
    progress = make_source("I started the work today.")
    updated = routes[("/commitments/{commitment_id}/status", "PATCH")](cid,
        CommitmentStatusUpdate(status=CommitmentStatus.IN_PROGRESS, source=progress, evidence=progress.transcript))
    assert updated["commitment"]["status"] == "in_progress"
    assert routes[("/commitments/{commitment_id}/dependencies", "GET")](cid, reference_datetime=BASE)["blocked"] is False
    assert routes[("/commitments/{commitment_id}/recovery-options", "GET")](cid, reference_datetime=BASE)["recommendations"] == []


def test_existing_task002_database_schema_is_upgraded_in_place(tmp_path):
    path = tmp_path / "older.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
      CREATE TABLE commitments (
        commitment_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL UNIQUE, statement TEXT NOT NULL,
        status TEXT NOT NULL, actor TEXT, actor_confidence TEXT NOT NULL, beneficiary TEXT,
        due_at TEXT, due_expression TEXT, supersedes TEXT, notes TEXT, commitment_json TEXT NOT NULL
      );
      CREATE TABLE commitment_dependencies (
        commitment_id TEXT NOT NULL, depends_on_id TEXT NOT NULL, source_id TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(commitment_id, depends_on_id)
      );
    """)
    connection.close()

    SQLiteMemoryStore(path)
    connection = sqlite3.connect(path)
    commitment_columns = {row[1] for row in connection.execute("PRAGMA table_info(commitments)")}
    dependency_columns = {row[1] for row in connection.execute("PRAGMA table_info(commitment_dependencies)")}
    connection.close()
    assert "acceptance_evidence" in commitment_columns
    assert "evidence_quote" in dependency_columns
