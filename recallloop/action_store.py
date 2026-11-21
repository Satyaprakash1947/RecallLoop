"""Persistent action records, approvals, evidence links, and append-only history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4

from .models import (
    ActionCreate, ActionHistoryEvent, ActionRecord, ActionStatus, ActionType,
)


ACTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY,
  commitment_id TEXT REFERENCES commitments(commitment_id),
  action_type TEXT NOT NULL,
  description TEXT NOT NULL,
  target TEXT,
  parameters_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  approved_at TEXT,
  approved_by TEXT,
  executed_at TEXT,
  result_json TEXT,
  error TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,
  approval_fingerprint TEXT,
  requires_approval INTEGER NOT NULL,
  verification_required INTEGER NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  retryable INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS action_evidence (
  action_id TEXT NOT NULL REFERENCES actions(action_id),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  PRIMARY KEY(action_id,evidence_id)
);
CREATE TABLE IF NOT EXISTS action_events (
  event_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL REFERENCES actions(action_id),
  event_type TEXT NOT NULL,
  previous_status TEXT,
  new_status TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_events_action ON action_events(action_id, occurred_at);
CREATE TABLE IF NOT EXISTS action_verifications (
  verification_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL REFERENCES actions(action_id),
  verifier TEXT NOT NULL,
  notes TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_verification_evidence (
  verification_id TEXT NOT NULL REFERENCES action_verifications(verification_id),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  PRIMARY KEY(verification_id,evidence_id)
);
"""


class ActionConflict(ValueError):
    """Action cannot enter the requested state from its current state."""


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different action request."""


class MissingAction(KeyError):
    """Requested action does not exist."""


class SQLiteActionStore:
    def __init__(self, database_path: str | Path):
        self.path = str(database_path)
        with self._transaction(write=True) as connection:
            connection.executescript(ACTION_SCHEMA)

    @contextmanager
    def _transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def requires_approval(action_type: ActionType) -> bool:
        return action_type in {
            ActionType.SEND_MESSAGE, ActionType.REASSIGN_TECHNICIAN,
            ActionType.CANCEL_COMMITMENT, ActionType.CREATE_TICKET, ActionType.UPDATE_TICKET,
        }

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def _request_fingerprint(cls, request: ActionCreate) -> str:
        payload = request.model_dump(mode="json")
        return hashlib.sha256(cls._canonical(payload).encode("utf-8")).hexdigest()

    @classmethod
    def _approval_fingerprint(cls, record: ActionRecord) -> str:
        payload = {
            "commitment_id": str(record.commitment_id) if record.commitment_id else None,
            "action_type": record.action_type.value,
            "description": record.description,
            "target": record.target,
            "parameters": record.parameters,
            "evidence_references": sorted(str(item) for item in record.evidence_references),
            "verification_required": record.verification_required,
        }
        return hashlib.sha256(cls._canonical(payload).encode("utf-8")).hexdigest()

    def create_action(self, request: ActionCreate) -> tuple[ActionRecord, bool]:
        request = ActionCreate.model_validate(request.model_dump())
        fingerprint = self._request_fingerprint(request)
        requires_approval = self.requires_approval(request.action_type)
        initial_status = ActionStatus.AWAITING_APPROVAL if requires_approval else ActionStatus.PROPOSED
        now = self._now()
        action_id = uuid4()
        with self._transaction(write=True) as connection:
            existing = connection.execute("SELECT * FROM actions WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
            if existing:
                if existing["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("idempotency_key is already used by a different action request")
                return self._load_action(connection, existing["action_id"]), False
            if request.commitment_id:
                if not connection.execute("SELECT 1 FROM commitments WHERE commitment_id=?", (str(request.commitment_id),)).fetchone():
                    raise MissingAction("commitment not found")
            for evidence_id in request.evidence_references:
                evidence = connection.execute("SELECT memory_id FROM evidence WHERE evidence_id=?", (str(evidence_id),)).fetchone()
                if not evidence:
                    raise ValueError(f"evidence reference does not exist: {evidence_id}")
                if request.commitment_id:
                    linked = connection.execute("SELECT memory_id FROM commitments WHERE commitment_id=?", (str(request.commitment_id),)).fetchone()
                    if evidence["memory_id"] != linked["memory_id"]:
                        raise ValueError("action evidence must belong to its linked commitment")
            connection.execute(
                """INSERT INTO actions(action_id,commitment_id,action_type,description,target,parameters_json,status,
                   created_at,idempotency_key,request_fingerprint,requires_approval,verification_required)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(action_id), str(request.commitment_id) if request.commitment_id else None,
                 request.action_type.value, request.description, request.target,
                 self._canonical(request.parameters), initial_status.value, now.isoformat(), request.idempotency_key,
                 fingerprint, int(requires_approval), int(request.verification_required)),
            )
            for evidence_id in request.evidence_references:
                connection.execute("INSERT INTO action_evidence VALUES(?,?)", (str(action_id), str(evidence_id)))
            self._append_event(connection, action_id, "created", None, initial_status,
                {"request_fingerprint": fingerprint, "requires_approval": requires_approval}, now)
            return self._load_action(connection, action_id), True

    def _load_action(self, connection: sqlite3.Connection, action_id: UUID | str) -> ActionRecord:
        row = connection.execute("SELECT * FROM actions WHERE action_id=?", (str(action_id),)).fetchone()
        if not row:
            raise MissingAction("action not found")
        refs = connection.execute("SELECT evidence_id FROM action_evidence WHERE action_id=? ORDER BY evidence_id", (str(action_id),)).fetchall()
        return ActionRecord(
            action_id=row["action_id"], commitment_id=row["commitment_id"], action_type=row["action_type"],
            description=row["description"], target=row["target"], parameters=json.loads(row["parameters_json"]),
            status=row["status"], created_at=row["created_at"], approved_at=row["approved_at"],
            approved_by=row["approved_by"], executed_at=row["executed_at"],
            result=json.loads(row["result_json"]) if row["result_json"] else None, error=row["error"],
            idempotency_key=row["idempotency_key"], evidence_references=[r["evidence_id"] for r in refs],
            requires_approval=bool(row["requires_approval"]), verification_required=bool(row["verification_required"]),
            approval_fingerprint=row["approval_fingerprint"], attempt_count=row["attempt_count"], retryable=bool(row["retryable"]),
        )

    def get_action(self, action_id: UUID | str) -> ActionRecord | None:
        with self._transaction() as connection:
            row = connection.execute("SELECT action_id FROM actions WHERE action_id=?", (str(action_id),)).fetchone()
            return self._load_action(connection, action_id) if row else None

    def list_actions(self, commitment_id: UUID | str | None = None, status: ActionStatus | None = None) -> list[ActionRecord]:
        clauses, params = [], []
        if commitment_id:
            clauses.append("commitment_id=?")
            params.append(str(commitment_id))
        if status:
            clauses.append("status=?")
            params.append(ActionStatus(status).value)
        query = "SELECT action_id FROM actions" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at DESC"
        with self._transaction() as connection:
            rows = connection.execute(query, params).fetchall()
            return [self._load_action(connection, row["action_id"]) for row in rows]

    def _append_event(self, connection: sqlite3.Connection, action_id: UUID | str, event_type: str,
                      previous: ActionStatus | None, new: ActionStatus, details: dict, occurred_at: datetime | None = None) -> None:
        connection.execute("INSERT INTO action_events VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), str(action_id), event_type, previous.value if previous else None,
             new.value, (occurred_at or self._now()).isoformat(), self._canonical(details)))

    def get_action_history(self, action_id: UUID | str) -> list[ActionHistoryEvent]:
        with self._transaction() as connection:
            if not connection.execute("SELECT 1 FROM actions WHERE action_id=?", (str(action_id),)).fetchone():
                raise MissingAction("action not found")
            rows = connection.execute("SELECT * FROM action_events WHERE action_id=? ORDER BY occurred_at,rowid", (str(action_id),)).fetchall()
            return [ActionHistoryEvent(event_id=row["event_id"], action_id=row["action_id"], event_type=row["event_type"],
                previous_status=row["previous_status"], new_status=row["new_status"], occurred_at=row["occurred_at"],
                details=json.loads(row["details_json"])) for row in rows]

    def approve_action(self, action_id: UUID | str, approved_by: str) -> ActionRecord:
        with self._transaction(write=True) as connection:
            action = self._load_action(connection, action_id)
            if not action.requires_approval:
                raise ActionConflict("this action type does not require approval")
            allowed = action.status == ActionStatus.AWAITING_APPROVAL or (
                action.status == ActionStatus.FAILED and action.retryable)
            if not allowed:
                raise ActionConflict(f"cannot approve action in {action.status.value} state")
            fingerprint = self._approval_fingerprint(action)
            now = self._now()
            connection.execute("UPDATE actions SET status='approved',approved_at=?,approved_by=?,approval_fingerprint=?,error=NULL,retryable=0 WHERE action_id=?",
                (now.isoformat(), approved_by, fingerprint, str(action_id)))
            self._append_event(connection, action_id, "approved", action.status, ActionStatus.APPROVED,
                {"approved_by": approved_by, "approval_fingerprint": fingerprint}, now)
            return self._load_action(connection, action_id)

    def update_action_parameters(self, action_id: UUID | str, parameters: dict, target: str | None = None) -> ActionRecord:
        with self._transaction(write=True) as connection:
            action = self._load_action(connection, action_id)
            if action.status not in {ActionStatus.PROPOSED, ActionStatus.AWAITING_APPROVAL, ActionStatus.APPROVED, ActionStatus.FAILED} or action.attempt_count:
                raise ActionConflict("parameters cannot change after execution has begun")
            updated_request = ActionCreate(commitment_id=action.commitment_id, action_type=action.action_type,
                description=action.description, target=target if target is not None else action.target,
                parameters=parameters, idempotency_key=action.idempotency_key,
                evidence_references=action.evidence_references, verification_required=action.verification_required)
            old_status = action.status
            new_status = ActionStatus.AWAITING_APPROVAL if action.requires_approval else ActionStatus.PROPOSED
            new_fp = self._request_fingerprint(updated_request)
            connection.execute("""UPDATE actions SET parameters_json=?,target=?,status=?,approved_at=NULL,approved_by=NULL,
                approval_fingerprint=NULL,request_fingerprint=?,error=NULL,retryable=0 WHERE action_id=?""",
                (self._canonical(updated_request.parameters), updated_request.target, new_status.value, new_fp, str(action_id)))
            self._append_event(connection, action_id, "parameters_changed_approval_invalidated", old_status, new_status,
                {"approval_invalidated": old_status == ActionStatus.APPROVED, "new_request_fingerprint": new_fp})
            return self._load_action(connection, action_id)

    def cancel_action(self, action_id: UUID | str, cancelled_by: str, reason: str | None = None) -> ActionRecord:
        with self._transaction(write=True) as connection:
            action = self._load_action(connection, action_id)
            if action.status not in {ActionStatus.PROPOSED, ActionStatus.AWAITING_APPROVAL, ActionStatus.APPROVED, ActionStatus.FAILED}:
                raise ActionConflict(f"cannot cancel action in {action.status.value} state")
            connection.execute("UPDATE actions SET status='cancelled',approved_at=NULL,approved_by=NULL,approval_fingerprint=NULL,retryable=0 WHERE action_id=?",
                (str(action_id),))
            self._append_event(connection, action_id, "cancelled", action.status, ActionStatus.CANCELLED,
                {"cancelled_by": cancelled_by, "reason": reason})
            return self._load_action(connection, action_id)

    def claim_action(self, action_id: UUID | str) -> ActionRecord:
        invalidated = False
        with self._transaction(write=True) as connection:
            action = self._load_action(connection, action_id)
            if action.status in {ActionStatus.SUCCEEDED, ActionStatus.VERIFICATION_PENDING, ActionStatus.VERIFIED}:
                return action
            if action.status == ActionStatus.EXECUTING:
                raise ActionConflict("action is already executing")
            allowed = action.status == ActionStatus.APPROVED or (
                action.status == ActionStatus.PROPOSED and not action.requires_approval
            ) or (action.status == ActionStatus.FAILED and action.retryable and not action.requires_approval)
            if not allowed:
                if action.status == ActionStatus.FAILED and action.requires_approval and action.retryable:
                    raise ActionConflict("retrying this consequential action requires fresh approval")
                if action.requires_approval and action.status in {ActionStatus.PROPOSED, ActionStatus.AWAITING_APPROVAL}:
                    raise ActionConflict("action requires explicit approval before execution")
                raise ActionConflict(f"action cannot execute in {action.status.value} state")
            if action.requires_approval:
                current_fingerprint = self._approval_fingerprint(action)
                if not action.approval_fingerprint or action.approval_fingerprint != current_fingerprint:
                    new_status = ActionStatus.AWAITING_APPROVAL
                    connection.execute("UPDATE actions SET status=?,approved_at=NULL,approved_by=NULL,approval_fingerprint=NULL WHERE action_id=?",
                        (new_status.value, str(action_id)))
                    self._append_event(connection, action_id, "approval_invalidated", action.status, new_status,
                        {"reason": "action parameters no longer match approval"})
                    invalidated = True
            if not invalidated:
                next_attempt = action.attempt_count + 1
                connection.execute("UPDATE actions SET status='executing',attempt_count=?,error=NULL,result_json=NULL WHERE action_id=?",
                    (next_attempt, str(action_id)))
                self._append_event(connection, action_id, "execution_started", action.status, ActionStatus.EXECUTING,
                    {"attempt": next_attempt})
        if invalidated:
            raise ActionConflict("approval no longer matches the action; approve the current parameters")
        return self.get_action(action_id)

    def finish_action(self, action_id: UUID | str, result: dict | None = None,
                      error: str | None = None, retryable: bool = False) -> ActionRecord:
        with self._transaction(write=True) as connection:
            action = self._load_action(connection, action_id)
            if action.status != ActionStatus.EXECUTING:
                raise ActionConflict("action must be executing before a result can be recorded")
            now = self._now()
            if error is None:
                status = ActionStatus.VERIFICATION_PENDING if action.verification_required else ActionStatus.SUCCEEDED
                connection.execute("UPDATE actions SET status=?,executed_at=?,result_json=?,error=NULL,retryable=0 WHERE action_id=?",
                    (status.value, now.isoformat(), self._canonical(result or {}), str(action_id)))
                self._append_event(connection, action_id, "execution_succeeded", ActionStatus.EXECUTING, status,
                    {"result": result or {}, "attempt": action.attempt_count}, now)
            else:
                status = ActionStatus.FAILED
                connection.execute("""UPDATE actions SET status='failed',executed_at=?,error=?,retryable=?,approved_at=NULL,
                    approved_by=NULL,approval_fingerprint=NULL WHERE action_id=?""",
                    (now.isoformat(), error, int(retryable), str(action_id)))
                self._append_event(connection, action_id, "execution_failed", ActionStatus.EXECUTING, status,
                    {"error": error, "retryable": retryable, "attempt": action.attempt_count}, now)
            return self._load_action(connection, action_id)

    def verify_action(self, action_id: UUID | str, verifier: str, evidence_references: list[UUID], notes: str) -> ActionRecord:
        with self._transaction(write=True) as connection:
            action = self._load_action(connection, action_id)
            if action.status not in {ActionStatus.SUCCEEDED, ActionStatus.VERIFICATION_PENDING}:
                raise ActionConflict(f"only a successful action can be verified; current state is {action.status.value}")
            for evidence_id in evidence_references:
                evidence = connection.execute("SELECT memory_id FROM evidence WHERE evidence_id=?", (str(evidence_id),)).fetchone()
                if not evidence:
                    raise ValueError(f"verification evidence does not exist: {evidence_id}")
                if action.commitment_id:
                    linked = connection.execute("SELECT memory_id FROM commitments WHERE commitment_id=?", (str(action.commitment_id),)).fetchone()
                    if evidence["memory_id"] != linked["memory_id"]:
                        raise ValueError("verification evidence must belong to the linked commitment")
            verification_id = uuid4()
            now = self._now()
            connection.execute("INSERT INTO action_verifications VALUES(?,?,?,?,?)",
                (str(verification_id), str(action_id), verifier, notes, now.isoformat()))
            for evidence_id in evidence_references:
                connection.execute("INSERT INTO action_verification_evidence VALUES(?,?)", (str(verification_id), str(evidence_id)))
                connection.execute("INSERT OR IGNORE INTO action_evidence VALUES(?,?)", (str(action_id), str(evidence_id)))
            connection.execute("UPDATE actions SET status='verified' WHERE action_id=?", (str(action_id),))
            self._append_event(connection, action_id, "action_verified", action.status, ActionStatus.VERIFIED,
                {"verifier": verifier, "evidence_references": [str(ref) for ref in evidence_references], "notes": notes}, now)
            return self._load_action(connection, action_id)

    def get_verification_history(self, action_id: UUID | str) -> list[dict]:
        with self._transaction() as connection:
            if not connection.execute("SELECT 1 FROM actions WHERE action_id=?", (str(action_id),)).fetchone():
                raise MissingAction("action not found")
            rows = connection.execute("SELECT * FROM action_verifications WHERE action_id=? ORDER BY created_at,rowid", (str(action_id),)).fetchall()
            results = []
            for row in rows:
                evidence = connection.execute("SELECT evidence_id FROM action_verification_evidence WHERE verification_id=? ORDER BY evidence_id", (row["verification_id"],)).fetchall()
                results.append({"verification_id": row["verification_id"], "action_id": row["action_id"],
                    "verifier": row["verifier"], "notes": row["notes"], "created_at": row["created_at"],
                    "evidence_references": [item["evidence_id"] for item in evidence]})
            return results
