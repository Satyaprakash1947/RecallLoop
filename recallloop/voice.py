"""Voice session integration and conservative conversation orchestration.

The Agora Platform owns real-time speech recognition and Murf playback. RecallLoop
only creates/stops sessions and keeps its credentials on the server.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from .models import VoiceConnectionState, VoiceSessionRecord

KOLKATA = ZoneInfo("Asia/Kolkata")


class VoiceConfigurationError(RuntimeError):
    pass


class VoiceProviderError(RuntimeError):
    pass


class VoiceProvider(Protocol):
    def start(self, channel: str, user_uid: int, idempotency_key: str, memory_session_id: UUID) -> str: ...
    def stop(self, provider_session_id: str) -> None: ...
    def user_tokens(self, channel: str, uid: int) -> tuple[str, str, str]: ...


class AgoraPlatformProvider:
    """Adapter for Agora's documented Python Platform SDK.

    A saved Agent Studio agent is required. Its Murf Falcon TTS configuration and
    provider secret live in Agora, never in browser code.
    """

    def __init__(self) -> None:
        self.app_id = os.getenv("AGORA_APP_ID", "").strip()
        self.app_certificate = os.getenv("AGORA_APP_CERTIFICATE", "").strip()
        self.agent_id = os.getenv("AGORA_AGENT_ID", "").strip()
        if not (self.app_id and self.app_certificate and self.agent_id):
            raise VoiceConfigurationError("Agora session requires AGORA_APP_ID, AGORA_APP_CERTIFICATE, and AGORA_AGENT_ID")
        try:
            from agora_agent import Agora, Area, SavedAgent
            from agora_token_builder import RtcTokenBuilder, RtmTokenBuilder
        except ImportError as exc:
            raise VoiceConfigurationError("Install the optional Agora voice dependencies to enable voice sessions") from exc
        self._client = Agora(area=Area.US, app_id=self.app_id, app_certificate=self.app_certificate)
        self._saved_agent = SavedAgent.get(self._client, self.agent_id)
        self._token_builder = RtcTokenBuilder
        self._rtm_token_builder = RtmTokenBuilder

    def start(self, channel: str, user_uid: int, idempotency_key: str, memory_session_id: UUID) -> str:
        try:
            session = self._saved_agent.create_session(
                channel=channel,
                remote_uids=[str(user_uid)],
                values={"recallloop_session_id": str(memory_session_id)},
                max_duration=3600,
                idle_timeout=300,
                data_policy={"retention": "none"},
                idempotency_key=idempotency_key,
            )
            return str(session.start())
        except Exception as exc:
            raise VoiceProviderError("Agora could not start the session") from exc

    def stop(self, provider_session_id: str) -> None:
        try:
            self._client.v3.sessions.stop(agent_session_id=provider_session_id)
        except Exception as exc:
            raise VoiceProviderError("Agora could not stop the session") from exc

    def user_tokens(self, channel: str, uid: int) -> tuple[str, str, str]:
        # Agora token builders expect an absolute Unix privilege expiry.
        expiry = int(time.time()) + 3600
        rtc = self._token_builder.buildTokenWithUid(
            self.app_id, self.app_certificate, channel, uid, role=1, privilegeExpiredTs=expiry)
        rtm = self._rtm_token_builder.buildToken(
            self.app_id, self.app_certificate, str(uid), role=1, privilegeExpiredTs=expiry)
        return rtc, rtm, self.app_id


class VoiceSessionStore:
    """SQLite session and transcript persistence, independent of memory records."""

    def __init__(self, database_path: str | Path):
        self.path = str(database_path)
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS voice_sessions (
                session_id TEXT PRIMARY KEY, agora_session_id TEXT UNIQUE,
                channel TEXT NOT NULL, user_uid INTEGER NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, stopped_at TEXT, error TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS voice_transcript (
                line_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES voice_sessions(session_id),
                role TEXT NOT NULL CHECK(role IN ('user','agent')),
                text TEXT NOT NULL, final INTEGER NOT NULL, created_at TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, channel: str, uid: int, agora_id: str, session_id: UUID | None = None) -> VoiceSessionRecord:
        now = datetime.now(KOLKATA)
        row = VoiceSessionRecord(session_id=session_id or uuid4(), agora_session_id=agora_id,
            channel=channel, user_uid=uid, status=VoiceConnectionState.RUNNING, created_at=now)
        with self._transaction() as db:
            db.execute("INSERT INTO voice_sessions VALUES(?,?,?,?,?,?,?,?)", (
                str(row.session_id), agora_id, channel, uid, row.status.value, now.isoformat(), None, None))
        return row

    def get(self, session_id: UUID | str) -> VoiceSessionRecord | None:
        with self._transaction() as db:
            item = db.execute("SELECT * FROM voice_sessions WHERE session_id=?", (str(session_id),)).fetchone()
        return self._record(item) if item else None

    def stop(self, session_id: UUID | str, *, error: str | None = None) -> VoiceSessionRecord | None:
        now = datetime.now(KOLKATA)
        with self._transaction() as db:
            db.execute("UPDATE voice_sessions SET status=?, stopped_at=?, error=? WHERE session_id=?",
                       (VoiceConnectionState.FAILED.value if error else VoiceConnectionState.STOPPED.value,
                        now.isoformat(), error, str(session_id)))
        return self.get(session_id)

    def append_transcript(self, session_id: UUID | str, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now(KOLKATA).isoformat()
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM voice_sessions WHERE session_id=?", (str(session_id),)).fetchone():
                raise KeyError("voice session not found")
            db.executemany("INSERT INTO voice_transcript(session_id,role,text,final,created_at) VALUES(?,?,?,?,?)",
                [(str(session_id), line["role"], line["text"], int(line.get("final", True)), now) for line in lines])
            rows = db.execute("SELECT role,text,final,created_at FROM voice_transcript WHERE session_id=? ORDER BY line_id",
                              (str(session_id),)).fetchall()
        return [dict(row) for row in rows]

    def transcript(self, session_id: UUID | str) -> list[dict[str, Any]]:
        with self._transaction() as db:
            rows = db.execute("SELECT role,text,final,created_at FROM voice_transcript WHERE session_id=? ORDER BY line_id",
                              (str(session_id),)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _record(row: sqlite3.Row) -> VoiceSessionRecord:
        return VoiceSessionRecord(session_id=row["session_id"], agora_session_id=row["agora_session_id"],
            channel=row["channel"], user_uid=row["user_uid"], status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            stopped_at=datetime.fromisoformat(row["stopped_at"]) if row["stopped_at"] else None,
            error=row["error"])


class VoiceSessionService:
    def __init__(self, store: VoiceSessionStore, provider: VoiceProvider):
        self.store, self.provider = store, provider

    def start(self, user_id: str) -> tuple[VoiceSessionRecord, str, str, str]:
        uid = (abs(hash(user_id)) % 2_000_000_000) + 1
        channel = f"recallloop-{uuid4().hex}"
        key = uuid4().hex
        local_session_id = uuid4()
        provider_id = self.provider.start(channel, uid, key, local_session_id)
        record = None
        try:
            record = self.store.create(channel, uid, provider_id, local_session_id)
            rtc_token, rtm_token, app_id = self.provider.user_tokens(channel, uid)
        except Exception:
            try:
                self.provider.stop(provider_id)
            except Exception:
                pass
            if record:
                self.store.stop(record.session_id, error="session token setup failed; provider stop was requested")
            raise
        return record, rtc_token, rtm_token, app_id

    def stop(self, session_id: UUID | str) -> VoiceSessionRecord:
        row = self.store.get(session_id)
        if not row:
            raise KeyError("voice session not found")
        if row.status == VoiceConnectionState.RUNNING and row.agora_session_id:
            self.provider.stop(row.agora_session_id)
        return self.store.stop(session_id)  # type: ignore[return-value]


class VoiceConversationOrchestrator:
    """Grounds recall answers through existing retrieval; never fabricates actions."""

    def __init__(self, retrieval, actions):
        self.retrieval, self.actions = retrieval, actions

    def respond(self, query: str) -> dict[str, Any]:
        answer = self.retrieval.answer_from_memory(query)
        if answer.retrieval_status != "insufficient_evidence":
            response = answer.answer
            response += " I have not found separate verification that the physical work was completed."
            return {"answer": response, "retrieval_status": answer.retrieval_status,
                    "evidence": answer.model_dump(mode="json"), "action": None,
                    "status": "evidence_retrieved"}
        clarification = "I could not find a clear, source-backed memory for that. Can you share a person, task, or phrase from the original discussion?"
        return {"answer": clarification, "retrieval_status": "insufficient_evidence",
                "evidence": answer.model_dump(mode="json"), "action": None,
                "status": "clarification_required"}

    def evidence_context(self, query: str) -> dict[str, Any]:
        """Return retrieved source plus existing commitment services for an agent tool."""
        answer = self.retrieval.answer_from_memory(query)
        base = answer.model_dump(mode="json")
        memory_id = answer.matching_memory_id
        if answer.retrieval_status == "insufficient_evidence" or not memory_id:
            # Completion questions intentionally have no direct answer from the
            # retrieval policy. For a unique matching task, pass its commitment
            # and source context to the agent while keeping the status explicitly
            # insufficient so a prior promise cannot be mistaken for completion.
            completion_query = any(word in query.casefold() for word in (
                "complete", "completed", "finish", "finished", "done", "delivered",
                "paid", "resolved", "fixed", "repaired", "has it", "was it",
            ))
            candidates = self.retrieval.search_memories(query) if completion_query else []
            if len(candidates) != 1:
                return {**base, "commitment": None, "history": None, "deadline": None,
                        "dependencies": None, "conflicts": [], "actions": [], "action_history": [],
                        "recovery_recommendations": [],
                        "completion_evidence_found": False}
            memory_id = candidates[0]["memory_id"]
        memory = self.retrieval.retrieve_memory_with_evidence(memory_id)
        commitment = memory.get("commitment") if memory else None
        details: dict[str, Any] = {**base, "memory": memory, "commitment": commitment,
                                  "history": None, "deadline": None, "dependencies": None,
                                  "conflicts": [], "actions": [], "action_history": [],
                                  "recovery_recommendations": [],
                                  "completion_evidence_found": False if answer.retrieval_status == "insufficient_evidence" else None}
        if commitment:
            commitment_id = commitment["commitment_id"]
            details["history"] = self.retrieval.get_commitment_history(commitment_id)
            details["deadline"] = self.retrieval.calculate_time_remaining(commitment_id)
            details["dependencies"] = self.retrieval.get_commitment_dependencies(commitment_id)
            details["conflicts"] = self.retrieval.detect_conflicts(commitment_id)
            actions = self.actions.list_actions(commitment_id=UUID(str(commitment_id)))
            details["actions"] = [action.model_dump(mode="json") for action in actions]
            details["action_history"] = [
                event.model_dump(mode="json")
                for action in actions for event in self.actions.get_action_history(action.action_id)
            ]
            recovery = self.retrieval.get_recovery_recommendations(commitment_id)
            details["recovery_recommendations"] = recovery.get("recommendations", [])
        return details
