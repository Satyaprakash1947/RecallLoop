"""Approval policy, controlled tool dispatch, retries, and action verification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import json
from typing import Iterable
from uuid import UUID

from .action_store import ActionConflict, SQLiteActionStore
from .action_tools import ActionTool, CommitmentMutationTool, MockNotificationTool, MockTicketTool, ToolExecutionError
from .database import SQLiteMemoryStore
from .models import ActionCreate, ActionRecord, ActionType


class VerifierNotAuthorized(PermissionError):
    """Verifier is not present in the configured allow-list."""


class ActionService:
    def __init__(self, action_store: SQLiteActionStore, memory_store: SQLiteMemoryStore,
                 tools: Iterable[ActionTool] | None = None, authorized_verifiers: set[str] | None = None,
                 timeout_seconds: float = 5.0):
        self.action_store = action_store
        self.memory_store = memory_store
        if tools is None:
            tools = (MockNotificationTool(), MockTicketTool(), CommitmentMutationTool(memory_store))
        self.tools: dict[ActionType, ActionTool] = {}
        for tool in tools:
            for action_type in tool.action_types:
                self.tools[action_type] = tool
        self.authorized_verifiers = {person.casefold() for person in (authorized_verifiers or set())}
        self.timeout_seconds = max(0.01, timeout_seconds)

    def create_action(self, request: ActionCreate) -> tuple[ActionRecord, bool]:
        return self.action_store.create_action(request)

    def list_actions(self, commitment_id: UUID | None = None, status=None) -> list[ActionRecord]:
        return self.action_store.list_actions(commitment_id, status)

    def get_action(self, action_id: UUID | str) -> ActionRecord | None:
        return self.action_store.get_action(action_id)

    def approve_action(self, action_id: UUID | str, approved_by: str) -> ActionRecord:
        return self.action_store.approve_action(action_id, approved_by)

    def cancel_action(self, action_id: UUID | str, cancelled_by: str, reason: str | None = None) -> ActionRecord:
        return self.action_store.cancel_action(action_id, cancelled_by, reason)

    def get_action_history(self, action_id: UUID | str):
        return self.action_store.get_action_history(action_id)

    def execute_action(self, action_id: UUID | str) -> ActionRecord:
        action = self.action_store.claim_action(action_id)
        # A completed/replay request returns the persisted state; tools are never
        # called a second time for an already successful action.
        if action.status.value != "executing":
            return action
        tool = self.tools.get(action.action_type)
        if tool is None:
            return self.action_store.finish_action(action.action_id, error="no tool is registered for this action type")

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recallloop-action")
        future = executor.submit(tool.execute, action)
        try:
            result = future.result(timeout=self.timeout_seconds)
            if not isinstance(result, dict):
                raise ToolExecutionError("tool response must be a JSON object")
            # Fail before recording success if a tool returned unserializable data.
            json.dumps(result)
        except FutureTimeout:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            return self.action_store.finish_action(action.action_id,
                error=f"tool execution timed out after {self.timeout_seconds:g} seconds", retryable=False)
        except ToolExecutionError as exc:
            executor.shutdown(wait=True)
            return self.action_store.finish_action(action.action_id, error=str(exc), retryable=exc.retryable)
        except Exception as exc:
            executor.shutdown(wait=True)
            return self.action_store.finish_action(action.action_id, error=f"tool execution failed: {exc}", retryable=False)
        else:
            executor.shutdown(wait=True)
            return self.action_store.finish_action(action.action_id, result=result)

    def verify_action(self, action_id: UUID | str, verifier: str, evidence_references: list[UUID], notes: str) -> ActionRecord:
        if verifier.casefold() not in self.authorized_verifiers:
            raise VerifierNotAuthorized("verifier is not authorized by this RecallLoop instance")
        return self.action_store.verify_action(action_id, verifier, evidence_references, notes)
