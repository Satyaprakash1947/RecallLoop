"""Validated domain models. Extraction records always carry source provenance."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class CommitmentStatus(StrEnum):
    CONFIRMED = "confirmed"
    PROPOSED = "proposed"
    IN_PROGRESS = "in_progress"
    OVERDUE = "overdue"
    RECOVERY = "recovery"
    COMPLETED_PENDING_VERIFICATION = "completed_pending_verification"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    UNCERTAIN = "uncertain"
    NEGATED = "negated"
    CANCELLED = "cancelled"


class IdentityConfidence(StrEnum):
    CLEAR = "clear"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class MemoryLifecycle(StrEnum):
    ACTIVE = "active"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"


class EvidenceConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    NONE = "none"


class Source(StrictModel):
    """Original transcript envelope; timestamps must include a timezone."""

    source_id: UUID = Field(default_factory=uuid4)
    transcript: Annotated[str, Field(min_length=1)]
    recorded_at: datetime
    timezone: str = "Asia/Kolkata"
    speaker_id: str | None = None

    @model_validator(mode="after")
    def timestamp_has_zone(self):
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        return self


class Commitment(StrictModel):
    commitment_id: UUID = Field(default_factory=uuid4)
    statement: Annotated[str, Field(min_length=1)]
    status: CommitmentStatus
    evidence: Annotated[str, Field(min_length=1)]
    actor: str | None = None
    actor_confidence: IdentityConfidence = IdentityConfidence.UNKNOWN
    beneficiary: str | None = None
    due_at: datetime | None = None
    due_expression: str | None = None
    supersedes: UUID | None = None
    notes: str | None = None
    acceptance_evidence: str | None = None

    @model_validator(mode="after")
    def dated_fields_are_consistent(self):
        if self.due_at is not None and (self.due_at.tzinfo is None or self.due_at.utcoffset() is None):
            raise ValueError("due_at must be timezone-aware")
        if self.due_at is not None and not self.due_expression:
            raise ValueError("due_expression is required when due_at is resolved")
        return self


class Extraction(StrictModel):
    extraction_id: UUID = Field(default_factory=uuid4)
    source_id: UUID
    commitments: list[Commitment] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class Execution(StrictModel):
    """Execution is separate from extraction: extracting never performs an action."""

    execution_id: UUID = Field(default_factory=uuid4)
    commitment_id: UUID
    state: str = "not_started"
    attempted_at: datetime | None = None
    outcome: str | None = None


class HistoryEvent(StrictModel):
    """Immutable revision event, also anchored to its originating transcript."""

    event_id: UUID = Field(default_factory=uuid4)
    commitment_id: UUID
    source_id: UUID
    event_type: str
    occurred_at: datetime
    previous_status: CommitmentStatus | None = None
    new_status: CommitmentStatus | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def event_timestamp_has_zone(self):
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return self


class MemoryCreate(StrictModel):
    """API input for one source-backed memory, optionally a commitment."""

    source: Source
    content: str | None = None
    commitment: Commitment | None = None

    @model_validator(mode="after")
    def has_memory_content(self):
        if not (self.content and self.content.strip()) and self.commitment is None:
            raise ValueError("provide content or a commitment")
        if self.commitment and self.commitment.evidence not in self.source.transcript:
            raise ValueError("commitment evidence must be an exact transcript substring")
        if self.commitment:
            managed_states = {
                CommitmentStatus.IN_PROGRESS, CommitmentStatus.OVERDUE, CommitmentStatus.RECOVERY,
                CommitmentStatus.COMPLETED_PENDING_VERIFICATION, CommitmentStatus.VERIFIED,
                CommitmentStatus.DISPUTED,
            }
            if self.commitment.status in managed_states:
                raise ValueError(f"{self.commitment.status.value} must be reached through a lifecycle transition")
            evidence = self.commitment.evidence.casefold()
            for field_name in ("actor", "beneficiary"):
                identity = getattr(self.commitment, field_name)
                if identity and identity.casefold() not in evidence:
                    raise ValueError(f"{field_name} must be grounded in the cited evidence")
            if self.commitment.status == CommitmentStatus.CONFIRMED:
                unsure = ("maybe", "might", "perhaps", "if i", "if we", "i could", "i won't", "i will not",
                          "i can't", "i cannot", "not sure", "probably", "i would")
                pledges = ("i will", "i'll", "i shall", "i promise", "i commit", "i am going to", "i'm going to",
                           "we will", "we'll", "we are going to")
                if any(term in evidence for term in unsure) or not any(term in evidence for term in pledges):
                    raise ValueError("confirmed status requires explicit, non-conditional commitment evidence")
                if self.commitment.acceptance_evidence is None:
                    self.commitment.acceptance_evidence = self.commitment.evidence
            if self.commitment.due_expression:
                if self.commitment.due_expression not in self.source.transcript:
                    raise ValueError("due_expression must appear in the original transcript")
                from .dates import resolve_date
                resolved = resolve_date(self.commitment.due_expression, self.source.recorded_at, self.source.timezone)
                if resolved is None and self.commitment.due_at is not None:
                    raise ValueError("unsupported date expression cannot have a resolved due_at")
                if resolved is not None:
                    if self.commitment.due_at is not None and self.commitment.due_at != resolved:
                        raise ValueError("due_at conflicts with deterministic date resolution")
                    self.commitment.due_at = resolved
            elif self.commitment.due_at is not None:
                raise ValueError("due date cannot be supplied without an explicit transcript expression")
        return self


class EvidenceRecord(StrictModel):
    evidence_id: UUID
    memory_id: UUID
    source_id: UUID
    quote: str
    evidence_type: str = "transcript_quote"
    created_at: datetime


class VerificationRecord(StrictModel):
    verification_id: UUID
    memory_id: UUID
    evidence_id: UUID
    result: str
    notes: str | None = None
    verifier: str | None = None
    created_at: datetime


class MemoryRecord(StrictModel):
    memory_id: UUID
    source_id: UUID
    content: str
    created_at: datetime
    lifecycle: MemoryLifecycle = MemoryLifecycle.ACTIVE
    commitment: Commitment | None = None
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    duplicate: bool = False


class RecallAnswer(StrictModel):
    answer: str
    source_transcript: str | None = None
    source_timestamp: datetime | None = None
    matching_memory_id: UUID | None = None
    evidence_confidence: EvidenceConfidence = EvidenceConfidence.NONE
    retrieval_status: str = "insufficient_evidence"


class CommitmentStatusUpdate(StrictModel):
    status: CommitmentStatus
    source: Source | None = None
    evidence: str | None = None
    detail: str | None = None
    verifier: str | None = None
    actor: str | None = None
    actor_confidence: IdentityConfidence | None = None


class ActionType(StrEnum):
    DRAFT_MESSAGE = "draft_message"
    SEND_MESSAGE = "send_message"
    REASSIGN_TECHNICIAN = "reassign_technician"
    CANCEL_COMMITMENT = "cancel_commitment"
    RECORD_RECOMMENDATION = "record_recommendation"
    CREATE_TICKET = "create_ticket"
    UPDATE_TICKET = "update_ticket"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    VERIFICATION_PENDING = "verification_pending"
    VERIFIED = "verified"


class ActionCreate(StrictModel):
    commitment_id: UUID | None = None
    action_type: ActionType
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    target: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)]
    evidence_references: list[UUID] = Field(default_factory=list)
    verification_required: bool = False

    @model_validator(mode="after")
    def action_parameters_are_sufficient(self):
        required_by_type = {
            ActionType.DRAFT_MESSAGE: ("recipient", "message"),
            ActionType.SEND_MESSAGE: ("recipient", "message"),
            ActionType.REASSIGN_TECHNICIAN: ("ticket_ref", "technician"),
            ActionType.CANCEL_COMMITMENT: (),
            ActionType.RECORD_RECOMMENDATION: ("recommendation",),
            ActionType.CREATE_TICKET: ("title",),
            ActionType.UPDATE_TICKET: ("ticket_ref", "updates"),
        }
        missing = [key for key in required_by_type[self.action_type] if key not in self.parameters]
        if missing:
            raise ValueError(f"{self.action_type.value} requires parameters: {', '.join(missing)}")
        for key in required_by_type[self.action_type]:
            value = self.parameters[key]
            if key != "updates" and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"action parameter '{key}' must be a non-empty string")
        if self.action_type == ActionType.CANCEL_COMMITMENT:
            if self.commitment_id is None or "source" not in self.parameters or "evidence" not in self.parameters:
                raise ValueError("cancel_commitment requires commitment_id, source, and evidence parameters")
            source = Source.model_validate(self.parameters["source"])
            evidence = self.parameters["evidence"]
            if not isinstance(evidence, str) or evidence not in source.transcript:
                raise ValueError("cancellation evidence must be an exact substring of the action source transcript")
            if not any(word in evidence.casefold() for word in ("cancel", "won't", "will not", "no longer")):
                raise ValueError("cancellation evidence must explicitly support cancellation")
        if self.action_type == ActionType.UPDATE_TICKET and not isinstance(self.parameters.get("updates"), dict):
            raise ValueError("update_ticket parameter 'updates' must be an object")
        if len(set(self.evidence_references)) != len(self.evidence_references):
            raise ValueError("evidence_references must be unique")
        return self


class ActionRecord(StrictModel):
    action_id: UUID
    commitment_id: UUID | None = None
    action_type: ActionType
    description: str
    target: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: ActionStatus
    created_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None
    executed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    idempotency_key: str
    evidence_references: list[UUID] = Field(default_factory=list)
    requires_approval: bool
    verification_required: bool = False
    approval_fingerprint: str | None = None
    attempt_count: int = 0
    retryable: bool = False


class ActionApproval(StrictModel):
    approved_by: Annotated[str, Field(min_length=1, max_length=200)]


class ActionVerification(StrictModel):
    verifier: Annotated[str, Field(min_length=1, max_length=200)]
    evidence_references: Annotated[list[UUID], Field(min_length=1)]
    notes: Annotated[str, Field(min_length=1, max_length=2000)]

    @model_validator(mode="after")
    def evidence_is_unique(self):
        if len(set(self.evidence_references)) != len(self.evidence_references):
            raise ValueError("evidence_references must be unique")
        return self


class ActionCancellation(StrictModel):
    cancelled_by: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Annotated[str | None, Field(max_length=1000)] = None


class ActionHistoryEvent(StrictModel):
    event_id: UUID
    action_id: UUID
    event_type: str
    previous_status: ActionStatus | None = None
    new_status: ActionStatus
    occurred_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class VoiceConnectionState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class VoiceSessionRecord(StrictModel):
    session_id: UUID
    agora_session_id: str | None = None
    channel: str
    user_uid: int
    status: VoiceConnectionState
    created_at: datetime
    stopped_at: datetime | None = None
    transcript_memory_id: UUID | None = None
    error: str | None = None


class VoiceSessionStart(StrictModel):
    user_id: Annotated[str, Field(min_length=1, max_length=128)] = "browser-user"


class VoiceTranscriptLine(StrictModel):
    role: str
    text: Annotated[str, Field(min_length=1, max_length=5000)]
    final: bool = True

    @model_validator(mode="after")
    def role_is_supported(self):
        if self.role not in {"user", "agent"}:
            raise ValueError("transcript role must be user or agent")
        return self


class VoiceTranscriptUpdate(StrictModel):
    lines: Annotated[list[VoiceTranscriptLine], Field(max_length=200)]


class VoiceMessageRequest(StrictModel):
    message: Annotated[str, Field(min_length=1, max_length=5000)]
    session_id: UUID | None = None


class VoiceExtractionRequest(StrictModel):
    session_id: UUID
    extraction_response: dict[str, Any]
    content: str | None = None


class VoiceActionProposal(StrictModel):
    commitment_id: UUID | None = None
    action_type: ActionType
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    target: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)]
    evidence_references: list[UUID] = Field(default_factory=list)
    verification_required: bool = False
