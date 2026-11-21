"""Search and evidence-only recall policies, kept separate from SQLite details."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol
from uuid import UUID

from .models import EvidenceConfidence, MemoryCreate, MemoryRecord, RecallAnswer, Source


class MemoryRepository(Protocol):
    def create_memory(self, request: MemoryCreate) -> MemoryRecord: ...
    def search_memories(self, query: str, limit: int = 20) -> list[dict]: ...
    def find_commitments(self, person: str) -> list[dict]: ...
    def get_commitment_history(self, commitment_id: UUID | str) -> list[dict]: ...
    def retrieve_memory_with_evidence(self, memory_id: UUID | str) -> dict | None: ...
    def check_existing_commitment(self, person: str, task: str) -> list[dict]: ...
    def list_commitments(self, responsible_person: str | None = None, ownership: str | None = None) -> list[dict]: ...
    def get_overdue_commitments(self, reference_datetime: datetime) -> list[dict]: ...
    def get_upcoming_commitments(self, reference_datetime: datetime, days: int = 7) -> list[dict]: ...
    def calculate_time_remaining(self, commitment_id: UUID | str, reference_datetime: datetime | None = None) -> dict: ...
    def mark_overdue_commitments(self, reference_datetime: datetime) -> list[dict]: ...
    def get_commitment_dependencies(self, commitment_id: UUID | str, reference_datetime: datetime | None = None) -> dict: ...
    def get_blocked_commitments(self, reference_datetime: datetime) -> list[dict]: ...
    def detect_conflicts(self, commitment_id: UUID | str | None = None) -> list[dict]: ...
    def transition_commitment_status(self, commitment_id: UUID | str, target, **kwargs) -> dict: ...


class RetrievalService:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository

    def search_memories(self, query: str) -> list[dict]:
        return self.repository.search_memories(query)

    def find_commitments(self, person: str) -> list[dict]:
        return self.repository.find_commitments(person)

    def get_commitment_history(self, commitment_id: UUID | str) -> list[dict]:
        return self.repository.get_commitment_history(commitment_id)

    def retrieve_memory_with_evidence(self, memory_id: UUID | str) -> dict | None:
        return self.repository.retrieve_memory_with_evidence(memory_id)

    def check_existing_commitment(self, person: str, task: str) -> list[dict]:
        return self.repository.check_existing_commitment(person, task)

    def list_commitments(self, responsible_person: str | None = None, ownership: str | None = None) -> list[dict]:
        return self.repository.list_commitments(responsible_person, ownership)

    def get_overdue_commitments(self, reference_datetime: datetime) -> list[dict]:
        return self.repository.get_overdue_commitments(reference_datetime)

    def get_upcoming_commitments(self, reference_datetime: datetime, days: int = 7) -> list[dict]:
        return self.repository.get_upcoming_commitments(reference_datetime, days)

    def calculate_time_remaining(self, commitment_id: UUID | str, reference_datetime: datetime | None = None) -> dict:
        return self.repository.calculate_time_remaining(commitment_id, reference_datetime)

    def mark_overdue_commitments(self, reference_datetime: datetime) -> list[dict]:
        return self.repository.mark_overdue_commitments(reference_datetime)

    def transition_commitment_status(self, commitment_id: UUID | str, target, **kwargs) -> dict:
        return self.repository.transition_commitment_status(commitment_id, target, **kwargs)

    def get_commitment_dependencies(self, commitment_id: UUID | str, reference_datetime: datetime | None = None) -> dict:
        return self.repository.get_commitment_dependencies(commitment_id, reference_datetime)

    def get_blocked_commitments(self, reference_datetime: datetime) -> list[dict]:
        return self.repository.get_blocked_commitments(reference_datetime)

    def detect_conflicts(self, commitment_id: UUID | str | None = None) -> list[dict]:
        return self.repository.detect_conflicts(commitment_id)

    def get_recovery_recommendations(self, commitment_id: UUID | str,
                                     reference_datetime: datetime | None = None) -> dict:
        record = self.repository.find_commitment(commitment_id)
        if not record:
            raise KeyError("commitment not found")
        commitment = record.get("commitment") or {}
        state = commitment.get("status")
        timing = self.repository.calculate_time_remaining(commitment_id, reference_datetime)
        eligible = {"confirmed", "in_progress", "overdue", "recovery"}
        if state not in eligible or not timing["is_overdue"]:
            return {"commitment_id": str(commitment_id), "recommendations": [], "automatic_execution": False}

        recommendations = []
        actor = commitment.get("actor")
        confidence = commitment.get("actor_confidence")
        if actor and confidence == "clear" and actor.casefold() not in {"i", "we", "someone"}:
            recommendations.append({"action": f"Contact {actor} to confirm status and agree on recovery.", "reason": "The named responsible person is identified in the commitment record."})
        elif not actor:
            recommendations.append({"action": "Identify and assign an accountable owner before planning recovery.", "reason": "No responsible person is recorded."})
        else:
            recommendations.append({"action": "Contact the responsible speaker to confirm ownership and current status.", "reason": "The identity is not specific enough to name a person."})
        task = commitment.get("statement", "").casefold()
        if any(term in task for term in ("repair", "technician", "plumbing", "electrical", "maintenance")):
            recommendations.append({"action": "Consider finding an alternative technician.", "reason": "The task appears to involve technical repair or maintenance."})
        recommendations.append({"action": "Request a revised deadline and record the response as new evidence.", "reason": "The stored deadline has passed."})
        recommendations.append({"action": "Escalate to an authorized owner if escalation is required and within your authority.", "reason": "An overdue task may need an authorized decision."})
        return {"commitment_id": str(commitment_id), "recommendations": recommendations, "automatic_execution": False}

    def answer_from_memory(self, query: str) -> RecallAnswer:
        hits = self.search_memories(query)
        if not hits:
            return self._insufficient("No matching source-backed memory was found.")

        # This version has no execution-recording interface. A promise is not proof
        # of completion, so completion questions must remain unanswered.
        completion_words = {"complete", "completed", "finish", "finished", "done", "delivered", "paid"}
        if completion_words.intersection(term.casefold() for term in re.findall(r"[\w]+", query)):
            return self._insufficient("No completion record is stored; a commitment alone cannot prove the task was completed.")

        terms = {term.casefold() for term in re.findall(r"[\w]+", query) if len(term) > 1}
        stopwords = {"what", "when", "where", "who", "which", "did", "does", "do", "was", "were",
                     "is", "are", "the", "a", "an", "i", "we", "you", "he", "she", "they", "to",
                     "for", "about", "tell", "me", "please", "recall", "remember", "promise", "promised"}
        useful_terms = terms - stopwords
        relevant = []
        for hit in hits:
            commitment = hit.get("commitment") or {}
            evidence_quotes = [entry["quote"] for entry in hit.get("evidence", [])]
            searchable = " ".join((hit.get("content", ""), commitment.get("statement", ""), *evidence_quotes)).casefold()
            if not useful_terms or any(term in searchable for term in useful_terms):
                relevant.append(hit)
        if not relevant:
            return self._insufficient("Search results do not contain enough matching evidence to answer.")

        # A later explicit correction replaces the earlier version for the answer,
        # while both versions remain persisted and searchable as history.
        superseded_ids = {str(hit["commitment"]["supersedes"]) for hit in relevant
                          if hit.get("commitment") and hit["commitment"].get("supersedes")}
        current = [hit for hit in relevant if not hit.get("commitment") or str(hit["commitment"]["commitment_id"]) not in superseded_ids]
        if current:
            relevant = current

        if any(hit.get("lifecycle") == "disputed" for hit in relevant):
            return self._insufficient("The matching memory is disputed; the available evidence does not settle it.")

        commitments = [hit["commitment"] for hit in relevant if hit.get("commitment")]
        if len(commitments) > 1:
            tasks: dict[str, list[dict]] = {}
            for commitment in commitments:
                tasks.setdefault(commitment["statement"].casefold().strip(), []).append(commitment)
            conflict = any(len(group) > 1 and len({(c["status"], c.get("due_at")) for c in group}) > 1
                           for group in tasks.values())
            if conflict:
                return self._insufficient("Matching records conflict; there is no single supported answer.")
            if any(len(group) > 1 for group in tasks.values()):
                return self._insufficient("Several records describe the same task; the intended occurrence is ambiguous.")
            ranked = sorted(relevant, key=lambda item: item.get("rank", 0))
            if len(ranked) > 1 and abs(ranked[0].get("rank", 0) - ranked[1].get("rank", 0)) < 1e-9:
                return self._insufficient("Several memories match equally well; the intended record is ambiguous.")

        hit = min(relevant, key=lambda item: item.get("rank", 0))
        transcript = hit.get("source", {}).get("transcript")
        timestamp = hit.get("source", {}).get("recorded_at")
        evidence = hit.get("evidence", [])
        quote = evidence[0]["quote"] if evidence else None
        if not transcript or not quote or quote not in transcript:
            return self._insufficient("The matching record has no verifiable quote in its original transcript.")
        if timestamp:
            timestamp = datetime.fromisoformat(timestamp)
        answer = f'The transcript says: “{quote}”'
        return RecallAnswer(answer=answer, source_transcript=transcript, source_timestamp=timestamp,
                            matching_memory_id=hit["memory_id"],
                            evidence_confidence=EvidenceConfidence.HIGH if hit.get("commitment") else EvidenceConfidence.MEDIUM,
                            retrieval_status="found")

    @staticmethod
    def _insufficient(reason: str) -> RecallAnswer:
        return RecallAnswer(answer=reason, retrieval_status="insufficient_evidence")
