"""FastAPI transport layer. Domain policies live in retrieval.py."""

from __future__ import annotations

import hmac
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from .action_service import ActionService, VerifierNotAuthorized
from .action_store import ActionConflict, IdempotencyConflict, MissingAction, SQLiteActionStore
from .action_tools import ActionTool
from .database import SQLiteMemoryStore
from .extractor import ExtractionInterface, ExtractionValidationError
from .models import (ActionApproval, ActionCancellation, ActionCreate, ActionHistoryEvent, ActionRecord, ActionStatus,
                     ActionType, ActionVerification, CommitmentStatusUpdate, MemoryCreate, MemoryRecord, RecallAnswer, StrictModel,
                     Source, VoiceExtractionRequest, VoiceMessageRequest, VoiceSessionStart, VoiceTranscriptUpdate)


class RecallRequest(StrictModel):
    query: str

class VoiceRecallToolRequest(StrictModel):
    query: str


class VoiceActionToolRequest(ActionCreate):
    """Strict ActionCreate payload; tool calls cannot execute or self-approve."""
from .retrieval import RetrievalService
from .voice import (AgoraPlatformProvider, VoiceConfigurationError, VoiceConversationOrchestrator,
                    VoiceProviderError, VoiceSessionService, VoiceSessionStore)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(database_path: str | Path | None = None, *, authorized_verifiers: set[str] | None = None,
               action_tools: Iterable[ActionTool] | None = None, action_timeout_seconds: float = 5.0,
               voice_provider=None) -> FastAPI:
    path = database_path or os.environ.get("RECALLLOOP_DB_PATH", "recallloop.sqlite3")
    configured_verifiers = authorized_verifiers
    if configured_verifiers is None:
        configured_verifiers = {name.strip() for name in os.environ.get("RECALLLOOP_AUTHORIZED_VERIFIERS", "").split(",") if name.strip()}
    repository = SQLiteMemoryStore(path, authorized_verifiers=configured_verifiers)
    service = RetrievalService(repository)
    action_store = SQLiteActionStore(path)
    action_service = ActionService(action_store, repository, tools=action_tools,
                                   authorized_verifiers=configured_verifiers,
                                   timeout_seconds=action_timeout_seconds)
    app = FastAPI(title="RecallLoop Memory API", version="0.4.0")
    app.state.repository = repository
    app.state.retrieval = service
    app.state.action_store = action_store
    app.state.actions = action_service
    app.state.voice_store = VoiceSessionStore(path)
    app.state.voice_provider = voice_provider
    app.state.voice = None
    app.state.voice_orchestrator = VoiceConversationOrchestrator(service, action_service)

    api_key = os.environ.get("RECALLLOOP_API_KEY", "").strip()
    deployment_environment = os.environ.get("RECALLLOOP_ENV", "development").casefold()
    if deployment_environment == "production" and not api_key:
        raise RuntimeError("RECALLLOOP_API_KEY must be configured in production")

    @app.middleware("http")
    async def require_api_key(request: Request, call_next):
        # Keep health and schema pages public for platform health checks and API discovery.
        public_paths = {"/", "/voice.js", "/styles.css", "/health", "/docs", "/redoc", "/openapi.json"}
        if api_key and request.url.path not in public_paths:
            provided = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(provided, api_key):
                return Response(status_code=401, content='{"detail":"invalid or missing API key"}',
                                media_type="application/json", headers={"WWW-Authenticate": "ApiKey"})
        return await call_next(request)

    @app.get("/health", tags=["operations"])
    def health_check():
        try:
            with sqlite3.connect(str(path), timeout=2) as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ok"}

    @app.get("/")
    def voice_home():
        if not (WEB_DIR / "index.html").exists():
            raise HTTPException(status_code=404, detail="voice interface not installed")
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/voice.js")
    def voice_script():
        return FileResponse(WEB_DIR / "voice.js", media_type="text/javascript")

    @app.get("/styles.css")
    def voice_styles():
        return FileResponse(WEB_DIR / "styles.css", media_type="text/css")

    if api_key:
        original_openapi = app.openapi

        def openapi_with_api_key():
            schema = original_openapi()
            schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
            schemes["ApiKeyAuth"] = {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            for route, operations in schema.get("paths", {}).items():
                if route != "/health":
                    for operation in operations.values():
                        if isinstance(operation, dict) and "responses" in operation:
                            operation["security"] = [{"ApiKeyAuth": []}]
            return schema

        app.openapi = openapi_with_api_key

    @app.post("/memories", response_model=MemoryRecord)
    def create_memory(request: MemoryCreate):
        try:
            return repository.create_memory(request)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/memories/search")
    def search_memories(query: str = Query(min_length=1, max_length=500)):
        return service.search_memories(query)

    @app.get("/commitments")
    def list_commitments(responsible_person: str | None = None, ownership: str | None = Query(default=None, pattern="^(unassigned|ambiguous)$")):
        return service.list_commitments(responsible_person, ownership)

    @app.get("/commitments/overdue")
    def overdue_commitments(reference_datetime: datetime | None = None):
        reference = reference_datetime or datetime.now(ZoneInfo("Asia/Kolkata"))
        return service.get_overdue_commitments(reference)

    @app.get("/commitments/upcoming")
    def upcoming_commitments(reference_datetime: datetime | None = None, days: int = Query(default=7, ge=0, le=365)):
        reference = reference_datetime or datetime.now(ZoneInfo("Asia/Kolkata"))
        return service.get_upcoming_commitments(reference, days)

    @app.patch("/commitments/{commitment_id}/status")
    def update_commitment_status(commitment_id: UUID, request: CommitmentStatusUpdate):
        try:
            return service.transition_commitment_status(commitment_id, request.status, source=request.source,
                evidence_quote=request.evidence, detail=request.detail, verifier=request.verifier,
                actor=request.actor, actor_confidence=request.actor_confidence)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/commitments/{commitment_id}/dependencies")
    def commitment_dependencies(commitment_id: UUID, reference_datetime: datetime | None = None):
        try:
            return service.get_commitment_dependencies(commitment_id, reference_datetime)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/commitments/{commitment_id}/recovery-options")
    def recovery_options(commitment_id: UUID, reference_datetime: datetime | None = None):
        try:
            return service.get_recovery_recommendations(commitment_id, reference_datetime)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/commitments/{commitment_id}")
    def get_commitment(commitment_id: UUID):
        result = repository.find_commitment(commitment_id)
        if not result:
            raise HTTPException(status_code=404, detail="commitment not found")
        return result

    @app.get("/commitments/{commitment_id}/history")
    def get_commitment_history(commitment_id: UUID):
        result = service.get_commitment_history(commitment_id)
        if not result:
            raise HTTPException(status_code=404, detail="commitment not found")
        return result

    @app.post("/recall", response_model=RecallAnswer)
    def recall(body: RecallRequest):
        return service.answer_from_memory(body.query)

    @app.post("/api/voice/session/start")
    def start_voice_session(body: VoiceSessionStart):
        try:
            provider = app.state.voice_provider or AgoraPlatformProvider()
            manager = VoiceSessionService(app.state.voice_store, provider)
            app.state.voice = manager
            record, rtc_token, rtm_token, app_id = manager.start(body.user_id)
            return {"session": record, "app_id": app_id, "rtc_token": rtc_token,
                    "rtm_token": rtm_token, "uid": record.user_uid, "channel": record.channel}
        except VoiceConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except VoiceProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="voice session setup failed") from exc

    @app.post("/api/voice/session/{session_id}/stop")
    def stop_voice_session(session_id: UUID):
        manager = app.state.voice
        if manager is None:
            try:
                manager = VoiceSessionService(app.state.voice_store, AgoraPlatformProvider())
            except VoiceConfigurationError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            return manager.stop(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except VoiceProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/voice/session/{session_id}")
    def get_voice_session(session_id: UUID):
        session = app.state.voice_store.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="voice session not found")
        return session

    @app.get("/api/voice/transcript/{session_id}")
    def get_voice_transcript(session_id: UUID):
        if not app.state.voice_store.get(session_id):
            raise HTTPException(status_code=404, detail="voice session not found")
        return {"session_id": session_id, "lines": app.state.voice_store.transcript(session_id)}

    @app.post("/api/voice/transcript/{session_id}")
    def save_voice_transcript(session_id: UUID, body: VoiceTranscriptUpdate):
        try:
            lines = app.state.voice_store.append_transcript(session_id, [line.model_dump() for line in body.lines])
            return {"session_id": session_id, "lines": lines}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/voice/session/{session_id}/extract")
    def extract_voice_memory(session_id: UUID, body: VoiceExtractionRequest):
        if session_id != body.session_id:
            raise HTTPException(status_code=422, detail="path and body session_id must match")
        session = app.state.voice_store.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="voice session not found")
        transcript_rows = app.state.voice_store.transcript(session_id)
        transcript = "\n".join(row["text"] for row in transcript_rows if row["final"])
        if not transcript.strip():
            raise HTTPException(status_code=422, detail="session has no final transcript to extract")
        timestamp = datetime.fromisoformat(transcript_rows[0]["created_at"])
        source = Source(transcript=transcript, recorded_at=timestamp, timezone="Asia/Kolkata")
        try:
            extraction = ExtractionInterface(reference_datetime=timestamp).extract(source, body.extraction_response)
            memories = []
            if extraction.commitments:
                for commitment in extraction.commitments:
                    memories.append(repository.create_memory(MemoryCreate(
                        source=source, content=body.content or commitment.statement, commitment=commitment)))
            elif body.content and body.content.strip():
                memories.append(repository.create_memory(MemoryCreate(source=source, content=body.content)))
            else:
                raise ExtractionValidationError("no commitment or memory content was supplied")
            return {"source_id": source.source_id, "extraction_id": extraction.extraction_id,
                    "unresolved": extraction.unresolved,
                    "memory_ids": [memory.memory_id for memory in memories],
                    "commitments_stored": len(extraction.commitments)}
        except (ExtractionValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/voice/message")
    def voice_message(body: VoiceMessageRequest):
        if body.session_id and not app.state.voice_store.get(body.session_id):
            raise HTTPException(status_code=404, detail="voice session not found")
        result = app.state.voice_orchestrator.respond(body.message)
        if body.session_id:
            app.state.voice_store.append_transcript(body.session_id, [
                {"role": "user", "text": body.message, "final": True},
                {"role": "agent", "text": result["answer"], "final": True},
            ])
        return result

    @app.post("/api/voice/evidence")
    def voice_evidence(body: VoiceRecallToolRequest):
        """Read-only evidence preview for the browser transcript panel."""
        return app.state.voice_orchestrator.evidence_context(body.query)

    @app.post("/api/voice/tools/recall")
    def voice_recall_tool(body: VoiceRecallToolRequest):
        """HTTPS custom-tool callback for an Agora Agent Studio configured agent."""
        return app.state.voice_orchestrator.evidence_context(body.query)

    @app.post("/api/voice/tools/action-proposal", status_code=201)
    def voice_action_tool(body: VoiceActionToolRequest, response: Response):
        """Create an action proposal only; the caller cannot approve or execute it."""
        try:
            action, created = action_service.create_action(body)
            if not created:
                response.status_code = 200
            return action
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except MissingAction as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/voice/tools/technicians")
    def available_mock_technicians():
        tool = action_service.tools.get(ActionType.REASSIGN_TECHNICIAN)
        if tool is None or not hasattr(tool, "list_available_technicians"):
            raise HTTPException(status_code=503, detail="technician availability is not configured")
        return {"source": "local_mock_roster", "technicians": tool.list_available_technicians()}

    @app.post("/api/voice/action/{action_id}/approve")
    def approve_voice_action(action_id: UUID, body: ActionApproval):
        try:
            return action_service.approve_action(action_id, body.approved_by)
        except MissingAction as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/voice/action/{action_id}/reject")
    def reject_voice_action(action_id: UUID, body: ActionCancellation):
        try:
            return action_service.cancel_action(action_id, body.cancelled_by,
                                                body.reason or "Rejected in voice interface")
        except MissingAction as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/actions", response_model=ActionRecord, status_code=201)
    def create_action(request: ActionCreate, response: Response):
        try:
            action, created = action_service.create_action(request)
            if not created:
                response.status_code = 200
            return action
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except MissingAction as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/actions", response_model=list[ActionRecord])
    def list_actions(commitment_id: UUID | None = None, status: ActionStatus | None = None):
        return action_service.list_actions(commitment_id, status)

    @app.get("/actions/{action_id}", response_model=ActionRecord)
    def get_action(action_id: UUID):
        action = action_service.get_action(action_id)
        if not action:
            raise HTTPException(status_code=404, detail="action not found")
        return action

    @app.post("/actions/{action_id}/approve", response_model=ActionRecord)
    def approve_action(action_id: UUID, request: ActionApproval):
        try:
            return action_service.approve_action(action_id, request.approved_by)
        except MissingAction as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/actions/{action_id}/execute", response_model=ActionRecord)
    def execute_action(action_id: UUID, response: Response):
        try:
            action = action_service.execute_action(action_id)
            if action.status == ActionStatus.FAILED:
                response.status_code = 502
            return action
        except MissingAction as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/actions/{action_id}/cancel", response_model=ActionRecord)
    def cancel_action(action_id: UUID, request: ActionCancellation):
        try:
            return action_service.cancel_action(action_id, request.cancelled_by, request.reason)
        except MissingAction as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/actions/{action_id}/history", response_model=list[ActionHistoryEvent])
    def action_history(action_id: UUID):
        try:
            return action_service.get_action_history(action_id)
        except MissingAction as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/actions/{action_id}/verify", response_model=ActionRecord)
    def verify_action(action_id: UUID, request: ActionVerification):
        try:
            return action_service.verify_action(action_id, request.verifier,
                                                request.evidence_references, request.notes)
        except MissingAction as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except VerifierNotAuthorized as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ActionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app


app = create_app()
