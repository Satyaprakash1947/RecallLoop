from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from recallloop.extractor import ExtractionInterface, ExtractionValidationError
from recallloop.models import Source
from recallloop.store import MemoryStore
from conftest import CASES


REFERENCE = datetime(2026, 11, 21, 15, 30, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_golden_case(case):
    source = Source(transcript=case["transcript"], recorded_at=REFERENCE)
    interface = ExtractionInterface(reference_datetime=REFERENCE)
    if "expected_error" in case:
        with pytest.raises(ExtractionValidationError) as error:
            interface.extract(source, case["response"])
        assert case["expected_error"].casefold() in str(error.value).casefold()
        return

    result = interface.extract(source, case["response"])
    assert [commitment.status.value for commitment in result.commitments] == case["expected_statuses"]
    if case.get("expected_due_dates") is not None:
        assert [commitment.due_at.isoformat() if commitment.due_at else None for commitment in result.commitments] == case["expected_due_dates"]
    if case.get("expected_actor_confidence"):
        assert [c.actor_confidence.value for c in result.commitments] == case["expected_actor_confidence"]
    if case.get("assert_source_id"):
        assert result.source_id == source.source_id
    for commitment in result.commitments:
        assert commitment.evidence in source.transcript

    if case["id"] == "GL025":
        store = MemoryStore()
        store.save(source, result)
        assert store.transcript_for(result.commitments[0].commitment_id) == source


def test_correction_history_keeps_both_revisions_and_source():
    from recallloop.models import Commitment, CommitmentStatus, Extraction

    store = MemoryStore()
    first_source = Source(transcript="I will send it Monday.", recorded_at=REFERENCE)
    first = Commitment(statement="Send it Monday", status=CommitmentStatus.CONFIRMED, evidence="I will send it Monday.", actor="I")
    store.save(first_source, Extraction(source_id=first_source.source_id, commitments=[first]))

    correction_source = Source(transcript="Correction: I will send it Tuesday.", recorded_at=REFERENCE)
    corrected = Commitment(statement="Send it Tuesday", status=CommitmentStatus.CONFIRMED, evidence="I will send it Tuesday.", actor="I", supersedes=first.commitment_id)
    store.save(correction_source, Extraction(source_id=correction_source.source_id, commitments=[corrected]))

    assert len(store.commitments) == 2
    assert len(store.history) == 1
    assert store.history[0].source_id == correction_source.source_id
    assert store.transcript_for(first.commitment_id) == first_source
    assert store.transcript_for(corrected.commitment_id) == correction_source
