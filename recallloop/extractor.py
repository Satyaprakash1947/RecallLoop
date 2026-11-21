"""Validate an LLM-produced JSON extraction without executing commitments."""

import json
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from .dates import resolve_date
from .models import Commitment, CommitmentStatus, Extraction, Source


class ExtractionValidationError(ValueError):
    """Raised when an LLM response is invalid or unsupported by its transcript."""


class ExtractionInterface:
    def __init__(self, reference_datetime: datetime | None = None):
        self.reference_datetime = reference_datetime

    def extract(self, source: Source, llm_response: str | dict[str, Any]) -> Extraction:
        """Parse, schema-check, and source-check a response.

        Required input shape: {"commitments": [...], "unresolved": [...]}.
        A quote of the original transcript is mandatory for every commitment.
        """
        try:
            payload = json.loads(llm_response) if isinstance(llm_response, str) else llm_response
            if not isinstance(payload, dict):
                raise TypeError("top-level response must be a JSON object")
            payload = {**payload, "source_id": str(source.source_id)}
            extraction = Extraction.model_validate(payload)
        except (json.JSONDecodeError, TypeError, ValidationError) as exc:
            prefix = "invalid JSON response: " if isinstance(exc, json.JSONDecodeError) else ""
            raise ExtractionValidationError(prefix + str(exc)) from exc

        seen: set[tuple[str, str]] = set()
        for commitment in extraction.commitments:
            if commitment.evidence not in source.transcript:
                raise ExtractionValidationError("commitment evidence must be an exact transcript substring")
            duplicate_key = (commitment.statement.casefold().strip(), commitment.evidence.casefold().strip())
            if duplicate_key in seen:
                raise ExtractionValidationError("duplicate commitment in one extraction")
            seen.add(duplicate_key)
            evidence_lower = commitment.evidence.casefold()
            for field_name in ("actor", "beneficiary"):
                identity = getattr(commitment, field_name)
                if identity and identity.casefold() not in evidence_lower:
                    raise ExtractionValidationError(f"{field_name} must be grounded in the cited evidence")
            self._validate_confirmation(commitment, source.transcript)
            self._resolve_due_date(commitment, source)
        return extraction

    @staticmethod
    def _validate_confirmation(commitment: Commitment, transcript: str) -> None:
        evidence = commitment.evidence.lower()
        if commitment.status == CommitmentStatus.CONFIRMED:
            # These constructions convey doubt, refusal, condition, or mere suggestion.
            disqualifiers = (
                "maybe", "might", "perhaps", "i could", "i can maybe", "if i", "if we",
                "should i", "should we", "could you", "i won't", "i will not", "i can't",
                "i cannot", "not sure", "probably", "i would", "we could", "let me see",
            )
            if any(phrase in evidence for phrase in disqualifiers):
                raise ExtractionValidationError("uncertain, conditional, suggested, or negated evidence cannot be confirmed")
            if not any(phrase in evidence for phrase in ("i will", "i'll", "i shall", "i promise", "i commit", "i am going to", "i'm going to", "we will", "we'll", "we are going to")):
                raise ExtractionValidationError("confirmed status requires explicit commitment language in the evidence")

    def _resolve_due_date(self, commitment: Commitment, source: Source) -> None:
        if commitment.due_expression is None:
            if commitment.due_at is not None:
                raise ExtractionValidationError("due date cannot be supplied without an explicit transcript expression")
            return
        if commitment.due_expression not in source.transcript:
            raise ExtractionValidationError("due_expression must appear in the original transcript")
        reference = self.reference_datetime or source.recorded_at
        resolved = resolve_date(commitment.due_expression, reference, source.timezone)
        if resolved is None:
            if commitment.due_at is not None:
                raise ExtractionValidationError("unsupported date expression cannot have a resolved due_at")
            return
        if commitment.due_at is not None and commitment.due_at != resolved:
            raise ExtractionValidationError("provided due_at conflicts with deterministic date resolution")
        commitment.due_at = resolved
