"""In-memory reference store demonstrating provenance and immutable revisions."""

from datetime import datetime
from uuid import UUID

from .models import Commitment, Extraction, HistoryEvent, Source


class MemoryStore:
    def __init__(self):
        self.sources: dict[UUID, Source] = {}
        self.commitments: dict[UUID, Commitment] = {}
        self.history: list[HistoryEvent] = []
        self._commitment_sources: dict[UUID, UUID] = {}

    def save(self, source: Source, extraction: Extraction) -> None:
        if extraction.source_id != source.source_id:
            raise ValueError("extraction must reference the supplied source")
        self.sources[source.source_id] = source
        for commitment in extraction.commitments:
            if commitment.supersedes is not None and commitment.supersedes not in self.commitments:
                raise ValueError("correction must reference an existing commitment")
            self.commitments[commitment.commitment_id] = commitment
            self._commitment_sources[commitment.commitment_id] = source.source_id
            if commitment.supersedes:
                prior = self.commitments[commitment.supersedes]
                self.history.append(HistoryEvent(
                    commitment_id=prior.commitment_id,
                    source_id=source.source_id,
                    event_type="superseded",
                    occurred_at=source.recorded_at,
                    previous_status=prior.status,
                    new_status=commitment.status,
                    detail=f"Superseded by {commitment.commitment_id}",
                ))

    def transcript_for(self, commitment_id: UUID) -> Source:
        commitment = self.commitments[commitment_id]
        return self.sources[self._commitment_sources[commitment_id]]
