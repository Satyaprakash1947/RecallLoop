"""Small synchronous tool contract and safe local-only mock implementations."""

from __future__ import annotations

import threading
import time
from typing import Protocol
from uuid import uuid4

from .database import SQLiteMemoryStore
from .models import ActionRecord, ActionStatus, ActionType, CommitmentStatus, Source


class ActionTool(Protocol):
    def execute(self, action: ActionRecord) -> dict: ...


class ToolExecutionError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class MockNotificationTool:
    """Records mock drafts, message sends, and recommendation notes in memory."""

    action_types = {ActionType.DRAFT_MESSAGE, ActionType.SEND_MESSAGE, ActionType.RECORD_RECOMMENDATION}

    def __init__(self, *, fail_next: bool = False, delay_seconds: float = 0):
        self.fail_next = fail_next
        self.delay_seconds = max(0, delay_seconds)
        self.sent_messages: list[dict] = []
        self.drafts: list[dict] = []
        self.recommendations: list[dict] = []
        self._results: dict[str, dict] = {}
        self._lock = threading.Lock()

    def execute(self, action: ActionRecord) -> dict:
        with self._lock:
            existing = self._results.get(action.idempotency_key)
            if existing:
                return {**existing, "idempotent_replay": True}
            if self.fail_next:
                self.fail_next = False
                raise ToolExecutionError("simulated notification failure before delivery", retryable=True)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if action.action_type == ActionType.RECORD_RECOMMENDATION:
            result = {"record_id": str(uuid4()), "recommendation": action.parameters["recommendation"], "recorded": True}
            destination = self.recommendations
        else:
            payload = {"recipient": action.parameters["recipient"], "message": action.parameters["message"],
                       "idempotency_key": action.idempotency_key}
            if action.action_type == ActionType.DRAFT_MESSAGE:
                result = {"draft_id": str(uuid4()), **payload, "sent": False}
                destination = self.drafts
            else:
                result = {"delivery_id": str(uuid4()), **payload, "sent": True}
                destination = self.sent_messages
        with self._lock:
            existing = self._results.get(action.idempotency_key)
            if existing:
                return {**existing, "idempotent_replay": True}
            destination.append(result)
            self._results[action.idempotency_key] = result
        return result


class MockTicketTool:
    """Creates and updates local mock tickets; no service connection is made."""

    action_types = {ActionType.CREATE_TICKET, ActionType.UPDATE_TICKET, ActionType.REASSIGN_TECHNICIAN}

    def __init__(self, *, fail_next: bool = False, delay_seconds: float = 0):
        self.fail_next = fail_next
        self.delay_seconds = max(0, delay_seconds)
        self.tickets: dict[str, dict] = {}
        self._results: dict[str, dict] = {}
        self._next_ticket = 1
        self._lock = threading.Lock()

    def execute(self, action: ActionRecord) -> dict:
        with self._lock:
            existing = self._results.get(action.idempotency_key)
            if existing:
                return {**existing, "idempotent_replay": True}
            if self.fail_next:
                self.fail_next = False
                raise ToolExecutionError("simulated ticket service failure before update", retryable=True)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        with self._lock:
            existing = self._results.get(action.idempotency_key)
            if existing:
                return {**existing, "idempotent_replay": True}
            if action.action_type == ActionType.CREATE_TICKET:
                ticket_ref = f"MOCK-{self._next_ticket:06d}"
                self._next_ticket += 1
                ticket = {"ticket_ref": ticket_ref, "title": action.parameters["title"],
                          "description": action.parameters.get("description", ""), "assignee": None}
                self.tickets[ticket_ref] = ticket
                result = {"ticket_ref": ticket_ref, "created": True}
            else:
                ticket_ref = action.parameters["ticket_ref"]
                ticket = self.tickets.get(ticket_ref)
                if ticket is None:
                    raise ToolExecutionError(f"mock ticket not found: {ticket_ref}")
                if action.action_type == ActionType.REASSIGN_TECHNICIAN:
                    ticket["assignee"] = action.parameters["technician"]
                else:
                    updates = action.parameters["updates"]
                    if not isinstance(updates, dict):
                        raise ToolExecutionError("ticket updates must be an object")
                    ticket.update(updates)
                result = {"ticket_ref": ticket_ref, "ticket": dict(ticket), "updated": True}
            self._results[action.idempotency_key] = result
            return result


class CommitmentMutationTool:
    """Applies explicitly approved commitment cancellation to the local database."""

    action_types = {ActionType.CANCEL_COMMITMENT}

    def __init__(self, memory_store: SQLiteMemoryStore):
        self.memory_store = memory_store

    def execute(self, action: ActionRecord) -> dict:
        if not action.commitment_id:
            raise ToolExecutionError("commitment_id is required for cancellation")
        try:
            source = Source.model_validate(action.parameters["source"])
            result = self.memory_store.transition_commitment_status(
                action.commitment_id, CommitmentStatus.CANCELLED, source=source,
                evidence_quote=action.parameters["evidence"], detail="Cancelled through approved action",
            )
        except Exception as exc:
            raise ToolExecutionError(str(exc)) from exc
        return {"commitment_id": str(action.commitment_id), "status": result["commitment"]["status"]}
