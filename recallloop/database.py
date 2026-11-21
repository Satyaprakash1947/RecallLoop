"""SQLite persistence with per-operation transactions and foreign-key checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4

from .models import (
    Commitment,
    CommitmentStatus,
    IdentityConfidence,
    EvidenceRecord,
    Extraction,
    HistoryEvent,
    MemoryCreate,
    MemoryLifecycle,
    MemoryRecord,
    Source,
    VerificationRecord,
)
from .lifecycle import LifecycleTransitionError, effective_deadline, require_aware, validate_transition


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  transcript TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  timezone TEXT NOT NULL,
  speaker_id TEXT
);
CREATE TABLE IF NOT EXISTS people (
  person_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
  speaker_ref TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS memories (
  memory_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  lifecycle TEXT NOT NULL DEFAULT 'active',
  duplicate_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS commitments (
  commitment_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL UNIQUE REFERENCES memories(memory_id),
  statement TEXT NOT NULL,
  status TEXT NOT NULL,
  actor TEXT,
  actor_confidence TEXT NOT NULL,
  beneficiary TEXT,
  due_at TEXT,
  due_expression TEXT,
  supersedes TEXT REFERENCES commitments(commitment_id),
  notes TEXT,
  commitment_json TEXT NOT NULL,
  acceptance_evidence TEXT
);
CREATE TABLE IF NOT EXISTS commitment_people (
  commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
  person_id TEXT NOT NULL REFERENCES people(person_id),
  role TEXT NOT NULL,
  PRIMARY KEY(commitment_id, person_id, role)
);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  quote TEXT NOT NULL,
  evidence_type TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_records (
  verification_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  result TEXT NOT NULL CHECK(result IN ('supports', 'contradicts', 'inconclusive')),
  notes TEXT,
  verifier TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_events (
  event_id TEXT PRIMARY KEY,
  commitment_id TEXT REFERENCES commitments(commitment_id),
  memory_id TEXT REFERENCES memories(memory_id),
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  previous_status TEXT,
  new_status TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status);
CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source_id);
CREATE INDEX IF NOT EXISTS idx_commitment_people_person ON commitment_people(person_id);
CREATE TABLE IF NOT EXISTS commitment_dependencies (
  commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
  depends_on_id TEXT NOT NULL REFERENCES commitments(commitment_id),
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  evidence_quote TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(commitment_id, depends_on_id),
  CHECK(commitment_id <> depends_on_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  memory_id UNINDEXED, content, transcript, statement, evidence, due_date,
  tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
  DELETE FROM memory_fts WHERE memory_id = old.memory_id;
END;
"""


class SQLiteMemoryStore:
    """File-backed memory database. Each public write is one atomic transaction."""

    def __init__(self, path: str | Path, authorized_verifiers: set[str] | None = None):
        self.path = str(path)
        self.authorized_verifiers = ({name.casefold() for name in authorized_verifiers}
                                     if authorized_verifiers is not None else None)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._transaction(write=True) as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(commitments)")}
            if "acceptance_evidence" not in columns:
                connection.execute("ALTER TABLE commitments ADD COLUMN acceptance_evidence TEXT")
            dependency_columns = {row["name"] for row in connection.execute("PRAGMA table_info(commitment_dependencies)")}
            if "evidence_quote" not in dependency_columns:
                connection.execute("ALTER TABLE commitment_dependencies ADD COLUMN evidence_quote TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _dump(model) -> str:
        return model.model_dump_json()

    def create_memory(self, request: MemoryCreate) -> MemoryRecord:
        # Revalidate at the persistence boundary in case a nested model was changed
        # after its containing request was first validated.
        request = MemoryCreate.model_validate(request.model_dump())
        source = request.source
        commitment = request.commitment
        content = (request.content or (commitment.statement if commitment else "")).strip()
        quote = commitment.evidence if commitment else source.transcript
        duplicate_material = {
            "transcript": source.transcript,
            "recorded_at": source.recorded_at.isoformat(),
            "speaker_id": source.speaker_id,
            "timezone": source.timezone,
            "content": content,
            "commitment": ({
                "statement": commitment.statement,
                "status": commitment.status.value,
                "evidence": commitment.evidence,
                "actor": commitment.actor,
                "actor_confidence": commitment.actor_confidence.value,
                "beneficiary": commitment.beneficiary,
                "due_at": commitment.due_at.isoformat() if commitment.due_at else None,
                "due_expression": commitment.due_expression,
                "supersedes": str(commitment.supersedes) if commitment.supersedes else None,
                "notes": commitment.notes,
                "acceptance_evidence": commitment.acceptance_evidence,
            } if commitment else None),
        }
        duplicate_key = hashlib.sha256(json.dumps(duplicate_material, sort_keys=True).encode()).hexdigest()
        with self._transaction(write=True) as connection:
            existing = connection.execute("SELECT memory_id FROM memories WHERE duplicate_key=?", (duplicate_key,)).fetchone()
            if existing:
                return self._load_memory(connection, existing["memory_id"], duplicate=True)

            source_row = connection.execute("SELECT * FROM sources WHERE source_id=?", (str(source.source_id),)).fetchone()
            if source_row:
                if (source_row["transcript"] != source.transcript
                        or source_row["recorded_at"] != source.recorded_at.isoformat()
                        or source_row["timezone"] != source.timezone
                        or source_row["speaker_id"] != source.speaker_id):
                    raise ValueError("source_id already exists with different transcript metadata")
            else:
                connection.execute(
                    "INSERT INTO sources VALUES (?, ?, ?, ?, ?)",
                    (str(source.source_id), source.transcript, source.recorded_at.isoformat(), source.timezone, source.speaker_id),
                )

            memory_id = uuid4()
            created_at = self._now()
            connection.execute(
                "INSERT INTO memories(memory_id,source_id,content,created_at,lifecycle,duplicate_key) VALUES(?,?,?,?,?,?)",
                (str(memory_id), str(source.source_id), content, created_at, MemoryLifecycle.ACTIVE.value, duplicate_key),
            )
            if commitment:
                if commitment.supersedes:
                    prior = connection.execute(
                        "SELECT c.*,m.memory_id AS prior_memory_id FROM commitments c JOIN memories m USING(memory_id) WHERE commitment_id=?",
                        (str(commitment.supersedes),),
                    ).fetchone()
                    if prior is None:
                        raise ValueError("correction must reference an existing commitment")
                    connection.execute("UPDATE memories SET lifecycle='superseded' WHERE memory_id=?", (prior["prior_memory_id"],))
                connection.execute(
                    """INSERT INTO commitments(commitment_id,memory_id,statement,status,actor,actor_confidence,
                       beneficiary,due_at,due_expression,supersedes,notes,commitment_json,acceptance_evidence)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(commitment.commitment_id), str(memory_id), commitment.statement, commitment.status.value,
                     commitment.actor, commitment.actor_confidence.value, commitment.beneficiary,
                     commitment.due_at.isoformat() if commitment.due_at else None, commitment.due_expression,
                     str(commitment.supersedes) if commitment.supersedes else None, commitment.notes, self._dump(commitment),
                     commitment.acceptance_evidence),
                )
                self._link_people(connection, commitment, source)
                if commitment.supersedes:
                    connection.execute(
                        "INSERT INTO history_events VALUES(?,?,?,?,?,?,?,?,?)",
                        (str(uuid4()), str(commitment.supersedes), str(memory_id), str(source.source_id), "corrected",
                         source.recorded_at.isoformat(), prior["status"], "superseded",
                         f"Superseded by {commitment.commitment_id}: {commitment.statement}"),
                    )

            evidence_id = uuid4()
            connection.execute(
                "INSERT INTO evidence VALUES(?,?,?,?,?,?)",
                (str(evidence_id), str(memory_id), str(source.source_id), quote,
                 "commitment_quote" if commitment else "transcript", created_at),
            )
            connection.execute(
                "INSERT INTO memory_fts(memory_id,content,transcript,statement,evidence,due_date) VALUES(?,?,?,?,?,?)",
                (str(memory_id), content, source.transcript, commitment.statement if commitment else "", quote,
                 commitment.due_at.date().isoformat() if commitment and commitment.due_at else ""),
            )
            if commitment:
                self._append_history(connection, commitment.commitment_id, memory_id, source, "created", None,
                                     commitment.status.value, "Commitment record created")
            return self._load_memory(connection, str(memory_id))

    @staticmethod
    def _link_people(connection: sqlite3.Connection, commitment: Commitment, source: Source) -> None:
        speaker_person_id = None
        if source.speaker_id:
            connection.execute("INSERT OR IGNORE INTO people(person_id,display_name,speaker_ref) VALUES(?,?,?)",
                               (str(uuid4()), source.speaker_id, source.speaker_id))
            row = connection.execute("SELECT person_id FROM people WHERE speaker_ref=?", (source.speaker_id,)).fetchone()
            speaker_person_id = row["person_id"]
        identities = [(commitment.actor, "actor"), (commitment.beneficiary, "beneficiary")]
        pronouns = {"i", "me", "we", "us", "you", "someone", "unknown"}
        for name, role in identities:
            if not name:
                continue
            if name.casefold() == "i" and speaker_person_id:
                person_id = speaker_person_id
            elif name.casefold() not in pronouns and (role == "beneficiary" or commitment.actor_confidence.value == "clear"):
                person = connection.execute("SELECT person_id FROM people WHERE display_name=? COLLATE NOCASE", (name,)).fetchone()
                if person:
                    person_id = person["person_id"]
                else:
                    person_id = str(uuid4())
                    connection.execute("INSERT INTO people(person_id,display_name) VALUES(?,?)", (person_id, name))
            else:
                continue
            connection.execute("INSERT OR IGNORE INTO commitment_people VALUES(?,?,?)",
                               (str(commitment.commitment_id), person_id, role))

    @staticmethod
    def _append_history(connection, commitment_id, memory_id, source, event_type, previous, new, detail,
                        occurred_at: datetime | None = None):
        connection.execute(
            "INSERT INTO history_events VALUES(?,?,?,?,?,?,?,?,?)",
            (str(uuid4()), str(commitment_id) if commitment_id else None,
             str(memory_id) if memory_id else None, str(source.source_id), event_type,
             (occurred_at or source.recorded_at).isoformat(), previous, new, detail),
        )

    def _load_memory(self, connection: sqlite3.Connection, memory_id: str, duplicate: bool = False) -> MemoryRecord:
        row = connection.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        commitment_row = connection.execute("SELECT commitment_json FROM commitments WHERE memory_id=?", (memory_id,)).fetchone()
        evidence_rows = connection.execute("SELECT * FROM evidence WHERE memory_id=? ORDER BY created_at", (memory_id,)).fetchall()
        evidence = [EvidenceRecord(evidence_id=e["evidence_id"], memory_id=e["memory_id"], source_id=e["source_id"],
                                   quote=e["quote"], evidence_type=e["evidence_type"], created_at=e["created_at"])
                   for e in evidence_rows]
        return MemoryRecord(
            memory_id=row["memory_id"], source_id=row["source_id"], content=row["content"], created_at=row["created_at"],
            lifecycle=row["lifecycle"], commitment=Commitment.model_validate_json(commitment_row["commitment_json"]) if commitment_row else None,
            evidence=evidence, duplicate=duplicate,
        )

    def retrieve_memory_with_evidence(self, memory_id: UUID | str) -> dict | None:
        with self._transaction() as connection:
            record = connection.execute("SELECT * FROM memories WHERE memory_id=?", (str(memory_id),)).fetchone()
            if not record:
                return None
            model = self._load_memory(connection, str(memory_id))
            source = connection.execute("SELECT * FROM sources WHERE source_id=?", (record["source_id"],)).fetchone()
            verifications = connection.execute("SELECT * FROM verification_records WHERE memory_id=? ORDER BY created_at", (str(memory_id),)).fetchall()
            return {**model.model_dump(mode="json"), "source": dict(source),
                    "verification_records": [dict(item) for item in verifications]}

    @staticmethod
    def _fts_query(query: str) -> str:
        import re
        terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
        if not terms:
            return ""
        return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms[:24])

    def search_memories(self, query: str, limit: int = 20) -> list[dict]:
        match_query = self._fts_query(query)
        if not match_query:
            return []
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT m.memory_id, bm25(memory_fts,0.0,1.5,0.8,1.2,1.4,1.5) AS rank
                   FROM memory_fts JOIN memories m USING(memory_id)
                   WHERE memory_fts MATCH ? ORDER BY rank, m.created_at DESC LIMIT ?""",
                (match_query, max(1, min(limit, 100))),
            ).fetchall()
            results = []
            for row in rows:
                item = self.retrieve_memory_with_evidence(row["memory_id"])
                if item:
                    item["rank"] = row["rank"]
                    results.append(item)
            return results

    def find_commitments(self, person: str) -> list[dict]:
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT DISTINCT c.commitment_json,m.memory_id FROM commitments c
                   JOIN commitment_people cp USING(commitment_id) JOIN people p USING(person_id)
                   JOIN memories m USING(memory_id)
                   WHERE p.display_name=? COLLATE NOCASE OR p.speaker_ref=? ORDER BY m.created_at""",
                (person, person),
            ).fetchall()
            return [self.retrieve_memory_with_evidence(row["memory_id"]) for row in rows]

    def find_commitment(self, commitment_id: UUID | str) -> dict | None:
        with self._transaction() as connection:
            row = connection.execute("SELECT memory_id FROM commitments WHERE commitment_id=?", (str(commitment_id),)).fetchone()
            return self.retrieve_memory_with_evidence(row["memory_id"]) if row else None

    def list_commitments(self, responsible_person: str | None = None, ownership: str | None = None) -> list[dict]:
        sql = "SELECT DISTINCT c.memory_id FROM commitments c JOIN memories m USING(memory_id)"
        clauses = ["m.lifecycle IN ('active','disputed')"]
        params: list[str] = []
        if responsible_person:
            sql += " LEFT JOIN commitment_people cp USING(commitment_id) LEFT JOIN people p USING(person_id)"
            clauses.append("(c.actor=? COLLATE NOCASE OR p.display_name=? COLLATE NOCASE OR p.speaker_ref=?)")
            params.extend([responsible_person, responsible_person, responsible_person])
        if ownership == "unassigned":
            clauses.append("(c.actor IS NULL OR trim(c.actor)='')")
        elif ownership == "ambiguous":
            clauses.append("c.actor IS NOT NULL AND c.actor_confidence IN ('ambiguous','unknown')")
        elif ownership not in (None, ""):
            raise ValueError("ownership must be 'unassigned' or 'ambiguous'")
        sql += " WHERE " + " AND ".join(clauses) + " ORDER BY COALESCE(c.due_at,'9999'), m.created_at"
        with self._transaction() as connection:
            rows = connection.execute(sql, params).fetchall()
            return [self.retrieve_memory_with_evidence(row["memory_id"]) for row in rows]

    def get_ambiguous_commitments(self) -> list[dict]:
        return self.list_commitments(ownership="ambiguous")

    def get_unassigned_commitments(self) -> list[dict]:
        return self.list_commitments(ownership="unassigned")

    def transition_commitment_status(
        self,
        commitment_id: UUID | str,
        target: CommitmentStatus,
        source: Source | None = None,
        evidence_quote: str | None = None,
        detail: str | None = None,
        verifier: str | None = None,
        actor: str | None = None,
        actor_confidence: IdentityConfidence | None = None,
    ) -> dict:
        target = CommitmentStatus(target)
        if target == CommitmentStatus.OVERDUE:
            raise LifecycleTransitionError("overdue is computed by deadline monitoring, not set manually")
        evidence_required = {
            CommitmentStatus.CONFIRMED, CommitmentStatus.IN_PROGRESS,
            CommitmentStatus.COMPLETED_PENDING_VERIFICATION, CommitmentStatus.RECOVERY,
            CommitmentStatus.CANCELLED, CommitmentStatus.DISPUTED,
        }
        if target in evidence_required and (source is None or not evidence_quote):
            raise ValueError(f"transition to {target.value} requires a source transcript and evidence quote")
        if source and evidence_quote and evidence_quote not in source.transcript:
            raise ValueError("status evidence must be an exact substring of its source transcript")
        if bool(source) != bool(evidence_quote):
            raise ValueError("source and evidence quote must be supplied together")

        with self._transaction(write=True) as connection:
            row = connection.execute(
                """SELECT c.*,m.memory_id,m.source_id FROM commitments c JOIN memories m USING(memory_id)
                   WHERE c.commitment_id=?""", (str(commitment_id),),
            ).fetchone()
            if not row:
                raise KeyError("commitment not found")
            current = CommitmentStatus(row["status"])
            validate_transition(current, target)
            if target == CommitmentStatus.CONFIRMED:
                text = evidence_quote.casefold()
                pledges = ("i will", "i'll", "i shall", "i promise", "i commit", "we will", "we'll",
                           "i am going to", "i'm going to", "we are going to")
                disqualifiers = ("i will not", "i won't", "i cannot", "i can't", "maybe", "might", "perhaps",
                                 "if i", "if we", "not sure", "probably")
                if not any(phrase in text for phrase in pledges) or any(phrase in text for phrase in disqualifiers):
                    raise ValueError("confirmation requires explicit acceptance language in the evidence")
                if actor and actor.casefold() not in text:
                    raise ValueError("accepted responsible person must appear in the acceptance evidence")
            if target == CommitmentStatus.COMPLETED_PENDING_VERIFICATION:
                text = evidence_quote.casefold()
                completion_terms = ("completed", "finished", "done", "delivered", "submitted", "paid", "installed",
                                   "repaired", "sent", "emailed", "called", "fixed")
                future_terms = ("will ", "might ", "may ", "could ", "would ", "planning to ")
                negations = ("not completed", "not finished", "not done", "not sent", "not delivered", "not paid",
                    "not installed", "not repaired", "haven't finished", "have not finished", "didn't finish",
                    "haven't sent", "have not sent", "didn't send", "never sent", "wasn't done")
                if (not any(term in text for term in completion_terms) or any(term in text for term in future_terms)
                        or any(term in text for term in negations)):
                    raise ValueError("completed_pending_verification requires an explicit completion claim, not a future promise")
            if target == CommitmentStatus.VERIFIED:
                if self.authorized_verifiers is not None and (not verifier or verifier.casefold() not in self.authorized_verifiers):
                    raise PermissionError("verifier is not authorized by this RecallLoop instance")
                verification = connection.execute(
                    """SELECT 1 FROM verification_records v JOIN evidence e USING(evidence_id)
                       WHERE v.memory_id=? AND v.result='supports' AND v.verifier IS NOT NULL
                       AND v.verifier=? AND e.evidence_type='completion_claim' LIMIT 1""", (row["memory_id"], verifier),
                ).fetchone()
                if current != CommitmentStatus.COMPLETED_PENDING_VERIFICATION or not verification or not verifier:
                    raise ValueError("verified requires a supported verification record and named verifier")

            if source:
                self._save_source(connection, source)
                event_source_id = source.source_id
            else:
                event_source_id = UUID(row["source_id"])
            if source and evidence_quote:
                connection.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?)",
                    (str(uuid4()), row["memory_id"], str(source.source_id), evidence_quote,
                     "completion_claim" if target == CommitmentStatus.COMPLETED_PENDING_VERIFICATION else "status_transition", self._now()))

            commitment_model = Commitment.model_validate_json(row["commitment_json"])
            commitment_model.status = target
            if target == CommitmentStatus.CONFIRMED and evidence_quote:
                commitment_model.acceptance_evidence = evidence_quote
                if actor:
                    commitment_model.actor = actor
                    commitment_model.actor_confidence = actor_confidence or IdentityConfidence.CLEAR
                    self._link_people(connection, commitment_model, source)
            connection.execute(
                "UPDATE commitments SET status=?,commitment_json=?,acceptance_evidence=? WHERE commitment_id=?",
                (target.value, commitment_model.model_dump_json(), commitment_model.acceptance_evidence, str(commitment_id)),
            )
            if target == CommitmentStatus.DISPUTED:
                connection.execute("UPDATE memories SET lifecycle='disputed' WHERE memory_id=?", (row["memory_id"],))
            elif current == CommitmentStatus.DISPUTED and target == CommitmentStatus.CONFIRMED:
                connection.execute("UPDATE memories SET lifecycle='active' WHERE memory_id=?", (row["memory_id"],))
            self._append_history(connection, commitment_id, row["memory_id"],
                source or self._load_source(connection, event_source_id), "status_transition", current.value,
                target.value, detail or f"Status changed from {current.value} to {target.value}")
        result = self.find_commitment(commitment_id)
        return result

    @staticmethod
    def _load_source(connection: sqlite3.Connection, source_id: UUID) -> Source:
        row = connection.execute("SELECT * FROM sources WHERE source_id=?", (str(source_id),)).fetchone()
        if not row:
            raise KeyError("source transcript not found")
        return Source(source_id=row["source_id"], transcript=row["transcript"], recorded_at=row["recorded_at"],
                      timezone=row["timezone"], speaker_id=row["speaker_id"])

    def get_overdue_commitments(self, reference_datetime: datetime) -> list[dict]:
        reference = require_aware(reference_datetime)
        candidates = self.list_commitments()
        result = []
        eligible = {CommitmentStatus.CONFIRMED.value, CommitmentStatus.IN_PROGRESS.value,
                    CommitmentStatus.OVERDUE.value, CommitmentStatus.RECOVERY.value}
        for item in candidates:
            commitment = item.get("commitment") or {}
            due_at = commitment.get("due_at")
            if commitment.get("status") in eligible and due_at and effective_deadline(datetime.fromisoformat(due_at)) < reference:
                result.append(item)
        return result

    def get_upcoming_commitments(self, reference_datetime: datetime, days: int = 7) -> list[dict]:
        reference = require_aware(reference_datetime)
        horizon = reference + timedelta(days=max(0, days))
        eligible = {CommitmentStatus.CONFIRMED.value, CommitmentStatus.IN_PROGRESS.value,
                    CommitmentStatus.RECOVERY.value}
        result = []
        for item in self.list_commitments():
            commitment = item.get("commitment") or {}
            due_at = commitment.get("due_at")
            if not due_at or commitment.get("status") not in eligible:
                continue
            deadline = effective_deadline(datetime.fromisoformat(due_at))
            if reference <= deadline <= horizon:
                result.append(item)
        return result

    def calculate_time_remaining(self, commitment_id: UUID | str, reference_datetime: datetime | None = None) -> dict:
        reference = require_aware(reference_datetime or datetime.now(timezone.utc))
        item = self.find_commitment(commitment_id)
        if not item:
            raise KeyError("commitment not found")
        commitment = item["commitment"]
        if not commitment or not commitment.get("due_at"):
            return {"commitment_id": str(commitment_id), "due_at": None, "remaining_seconds": None,
                    "is_overdue": False, "status": "no_deadline"}
        due_at = effective_deadline(datetime.fromisoformat(commitment["due_at"]))
        seconds = (due_at - reference).total_seconds()
        return {"commitment_id": str(commitment_id), "due_at": due_at.isoformat(),
                "remaining_seconds": seconds, "is_overdue": seconds < 0, "status": commitment["status"]}

    def mark_overdue_commitments(self, reference_datetime: datetime) -> list[dict]:
        reference = require_aware(reference_datetime)
        changed: list[str] = []
        with self._transaction(write=True) as connection:
            rows = connection.execute(
                """SELECT c.*,m.memory_id,m.source_id FROM commitments c JOIN memories m USING(memory_id)
                   WHERE m.lifecycle='active' AND c.status IN ('confirmed','in_progress') AND c.due_at IS NOT NULL"""
            ).fetchall()
            for row in rows:
                due_at = effective_deadline(datetime.fromisoformat(row["due_at"]))
                if due_at >= reference:
                    continue
                validate_transition(CommitmentStatus(row["status"]), CommitmentStatus.OVERDUE)
                model = Commitment.model_validate_json(row["commitment_json"])
                model.status = CommitmentStatus.OVERDUE
                connection.execute("UPDATE commitments SET status='overdue',commitment_json=? WHERE commitment_id=?",
                                   (model.model_dump_json(), row["commitment_id"]))
                source = self._load_source(connection, UUID(row["source_id"]))
                self._append_history(connection, row["commitment_id"], row["memory_id"], source,
                    "deadline_overdue", row["status"], CommitmentStatus.OVERDUE.value,
                    f"Deadline passed at {due_at.isoformat()}; evaluated at {reference.isoformat()}", occurred_at=reference)
                changed.append(row["commitment_id"])
        return [self.find_commitment(commitment_id) for commitment_id in changed]

    def add_dependency(self, commitment_id: UUID | str, depends_on_id: UUID | str, evidence_quote: str) -> None:
        commitment_id, depends_on_id = str(commitment_id), str(depends_on_id)
        if commitment_id == depends_on_id:
            raise ValueError("a commitment cannot depend on itself")
        with self._transaction(write=True) as connection:
            dependent = connection.execute("SELECT memory_id,source_id FROM commitments JOIN memories USING(memory_id) WHERE commitment_id=?", (commitment_id,)).fetchone()
            dependency = connection.execute("SELECT commitment_id FROM commitments WHERE commitment_id=?", (depends_on_id,)).fetchone()
            if not dependent or not dependency:
                raise KeyError("both commitments must exist before adding a dependency")
            source = self._load_source(connection, UUID(dependent["source_id"]))
            if not evidence_quote or evidence_quote not in source.transcript:
                raise ValueError("dependency evidence must be an exact substring of the dependent task transcript")
            cycle = connection.execute(
                """WITH RECURSIVE reachable(id) AS (
                     SELECT depends_on_id FROM commitment_dependencies WHERE commitment_id=?
                     UNION SELECT d.depends_on_id FROM commitment_dependencies d JOIN reachable r ON d.commitment_id=r.id
                   ) SELECT 1 FROM reachable WHERE id=? LIMIT 1""", (depends_on_id, commitment_id),
            ).fetchone()
            if cycle:
                raise ValueError("dependency would create a circular dependency")
            connection.execute("""INSERT INTO commitment_dependencies
                (commitment_id,depends_on_id,source_id,evidence_quote,created_at) VALUES(?,?,?,?,?)""",
                (commitment_id, depends_on_id, dependent["source_id"], evidence_quote, self._now()))
            status = connection.execute("SELECT status FROM commitments WHERE commitment_id=?", (commitment_id,)).fetchone()["status"]
            self._append_history(connection, commitment_id, dependent["memory_id"], source, "dependency_added",
                                 status, status, f"Depends on commitment {depends_on_id}; evidence: {evidence_quote}",
                                 occurred_at=datetime.now(timezone.utc))

    def get_commitment_dependencies(self, commitment_id: UUID | str, reference_datetime: datetime | None = None) -> dict:
        reference = require_aware(reference_datetime or datetime.now(timezone.utc))
        with self._transaction() as connection:
            exists = connection.execute("SELECT 1 FROM commitments WHERE commitment_id=?", (str(commitment_id),)).fetchone()
            if not exists:
                raise KeyError("commitment not found")
            rows = connection.execute(
                """SELECT d.depends_on_id,d.evidence_quote,s.transcript AS evidence_transcript
                   FROM commitment_dependencies d JOIN sources s USING(source_id)
                   WHERE d.commitment_id=? ORDER BY d.created_at""",
                (str(commitment_id),),
            ).fetchall()
        dependencies = []
        for row in rows:
            item = self.find_commitment(row["depends_on_id"])
            commitment = item["commitment"]
            status = commitment["status"]
            due_at = commitment.get("due_at")
            missed_states = {CommitmentStatus.CONFIRMED.value, CommitmentStatus.IN_PROGRESS.value,
                             CommitmentStatus.OVERDUE.value, CommitmentStatus.RECOVERY.value}
            missed = bool(due_at and status in missed_states
                          and effective_deadline(datetime.fromisoformat(due_at)) < reference)
            dependencies.append({"commitment": item, "resolved": status == CommitmentStatus.VERIFIED.value,
                                 "missed_deadline": missed, "dependency_status": status,
                                 "evidence_quote": row["evidence_quote"],
                                 "evidence_transcript": row["evidence_transcript"]})
        return {"commitment_id": str(commitment_id), "blocked": any(not item["resolved"] for item in dependencies),
                "dependencies": dependencies}

    def get_blocked_commitments(self, reference_datetime: datetime) -> list[dict]:
        return [item for item in self.list_commitments()
                if self.get_commitment_dependencies(item["commitment"]["commitment_id"], reference_datetime)["blocked"]]

    def detect_conflicts(self, commitment_id: UUID | str | None = None) -> list[dict]:
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT c.commitment_id,c.statement,c.status,c.actor,c.actor_confidence,c.due_at,c.supersedes,
                          m.memory_id,m.lifecycle,c.commitment_json,s.speaker_id
                   FROM commitments c JOIN memories m USING(memory_id) JOIN sources s USING(source_id)
                   ORDER BY m.created_at"""
            ).fetchall()
        records = [dict(row) for row in rows]
        by_id = {row["commitment_id"]: row for row in records}
        conflicts: list[dict] = []
        for record in records:
            prior_id = record.get("supersedes")
            if prior_id and prior_id in by_id:
                conflicts.append({"type": "revised", "commitment_ids": [prior_id, record["commitment_id"]],
                                  "details": "A newer record explicitly supersedes the earlier commitment."})
        current_records = [row for row in records if row["lifecycle"] in {MemoryLifecycle.ACTIVE.value, MemoryLifecycle.DISPUTED.value}]
        for index, left in enumerate(current_records):
            for right in current_records[index + 1:]:
                if left["statement"].casefold().strip() != right["statement"].casefold().strip():
                    continue
                left_actor = (left["actor"] or "").casefold()
                right_actor = (right["actor"] or "").casefold()
                pronouns = {"i", "me", "we", "us"}
                if left_actor in pronouns or right_actor in pronouns:
                    same_identity = (left_actor == right_actor and left_actor in pronouns
                                     and left["speaker_id"] is not None
                                     and left["speaker_id"] == right["speaker_id"])
                else:
                    same_identity = bool(left_actor) and left_actor == right_actor
                same_person = (left["actor_confidence"] == right["actor_confidence"] == IdentityConfidence.CLEAR.value
                               and same_identity)
                if not same_person:
                    continue
                ids = [left["commitment_id"], right["commitment_id"]]
                if commitment_id and str(commitment_id) not in ids:
                    continue
                active_states = {CommitmentStatus.PROPOSED.value, CommitmentStatus.CONFIRMED.value,
                    CommitmentStatus.IN_PROGRESS.value, CommitmentStatus.OVERDUE.value, CommitmentStatus.RECOVERY.value,
                    CommitmentStatus.COMPLETED_PENDING_VERIFICATION.value}
                positive_states = active_states | {CommitmentStatus.VERIFIED.value}
                negative_states = {CommitmentStatus.DISPUTED.value, CommitmentStatus.NEGATED.value, CommitmentStatus.CANCELLED.value}
                contradictory = ((left["status"] in negative_states and right["status"] in positive_states)
                                 or (right["status"] in negative_states and left["status"] in positive_states))
                if contradictory:
                    conflicts.append({"type": "contradictory_records", "commitment_ids": ids,
                                      "details": f"Records have incompatible states: {left['status']} and {right['status']}"})
                same_deadline = (left["due_at"] and right["due_at"]
                    and datetime.fromisoformat(left["due_at"]).astimezone(timezone.utc)
                    == datetime.fromisoformat(right["due_at"]).astimezone(timezone.utc))
                if left["due_at"] and right["due_at"] and not same_deadline:
                    conflicts.append({"type": "conflicting_deadlines", "commitment_ids": ids,
                                      "deadlines": [left["due_at"], right["due_at"]]})
                if left["status"] in active_states and right["status"] in active_states:
                    conflicts.append({"type": "duplicate_active_commitments", "commitment_ids": ids,
                                      "details": left["statement"]})
        return [conflict for conflict in conflicts if not commitment_id or str(commitment_id) in conflict["commitment_ids"]]

    def get_commitment_history(self, commitment_id: UUID | str) -> list[dict]:
        with self._transaction() as connection:
            row = connection.execute("SELECT memory_id FROM commitments WHERE commitment_id=?", (str(commitment_id),)).fetchone()
            if not row:
                return []
            events = connection.execute(
                """WITH RECURSIVE revision_ids(commitment_id) AS (
                     SELECT commitment_id FROM commitments WHERE commitment_id=?
                     UNION ALL SELECT c.commitment_id FROM commitments c JOIN revision_ids r ON c.supersedes=r.commitment_id
                   ), revision_memories(memory_id) AS (
                     SELECT memory_id FROM commitments JOIN revision_ids USING(commitment_id)
                   )
                   SELECT h.*,s.transcript,s.recorded_at AS source_timestamp FROM history_events h
                   JOIN sources s USING(source_id)
                   WHERE h.commitment_id IN (SELECT commitment_id FROM revision_ids)
                      OR h.memory_id IN (SELECT memory_id FROM revision_memories)
                   ORDER BY h.occurred_at,h.rowid""",
                (str(commitment_id),),
            ).fetchall()
            versions = connection.execute(
                """WITH RECURSIVE revision_ids(commitment_id) AS (
                     SELECT commitment_id FROM commitments WHERE commitment_id=?
                     UNION ALL SELECT c.commitment_id FROM commitments c JOIN revision_ids r ON c.supersedes=r.commitment_id
                   )
                   SELECT c.commitment_json,m.memory_id,m.lifecycle,m.created_at,s.transcript,s.recorded_at AS source_timestamp
                   FROM revision_ids r JOIN commitments c USING(commitment_id)
                   JOIN memories m USING(memory_id) JOIN sources s USING(source_id) ORDER BY m.created_at""",
                (str(commitment_id),),
            ).fetchall()
            return [{"versions": [dict(v) for v in versions], "events": [dict(e) for e in events]}]

    def check_existing_commitment(self, person: str, task: str) -> list[dict]:
        candidates = self.find_commitments(person)
        terms = {word.casefold() for word in self._fts_query(task).replace('"', '').split(" OR ") if word}
        result = []
        for item in candidates:
            statement = item.get("commitment")
            if not statement:
                continue
            statement_terms = set(statement["statement"].casefold().split())
            if terms and any(term in statement_terms for term in terms):
                result.append(item)
        return result

    def mark_disputed(self, memory_id: UUID | str, source: Source, detail: str, evidence_quote: str) -> dict:
        if evidence_quote not in source.transcript:
            raise ValueError("dispute evidence must be an exact transcript substring")
        with self._transaction(write=True) as connection:
            memory = connection.execute("SELECT * FROM memories WHERE memory_id=?", (str(memory_id),)).fetchone()
            if not memory:
                raise KeyError("memory not found")
            self._save_source(connection, source)
            connection.execute("UPDATE memories SET lifecycle='disputed' WHERE memory_id=?", (str(memory_id),))
            evidence_id = uuid4()
            connection.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?)",
                               (str(evidence_id), str(memory_id), str(source.source_id), evidence_quote, "dispute", self._now()))
            commitment = connection.execute("SELECT commitment_id,status FROM commitments WHERE memory_id=?", (str(memory_id),)).fetchone()
            if commitment:
                current = CommitmentStatus(commitment["status"])
                validate_transition(current, CommitmentStatus.DISPUTED)
                model_row = connection.execute("SELECT commitment_json FROM commitments WHERE commitment_id=?", (commitment["commitment_id"],)).fetchone()
                model = Commitment.model_validate_json(model_row["commitment_json"])
                model.status = CommitmentStatus.DISPUTED
                connection.execute("UPDATE commitments SET status='disputed',commitment_json=? WHERE commitment_id=?",
                                   (model.model_dump_json(), commitment["commitment_id"]))
            self._append_history(connection, commitment["commitment_id"] if commitment else None, memory_id, source,
                                 "disputed", commitment["status"] if commitment else None, "disputed", detail)
        return self.retrieve_memory_with_evidence(memory_id)

    def _save_source(self, connection: sqlite3.Connection, source: Source) -> None:
        existing = connection.execute("SELECT transcript FROM sources WHERE source_id=?", (str(source.source_id),)).fetchone()
        if existing:
            metadata = connection.execute("SELECT * FROM sources WHERE source_id=?", (str(source.source_id),)).fetchone()
            if (metadata["transcript"] != source.transcript or metadata["recorded_at"] != source.recorded_at.isoformat()
                    or metadata["timezone"] != source.timezone or metadata["speaker_id"] != source.speaker_id):
                raise ValueError("source_id already exists with different transcript metadata")
        if not existing:
            connection.execute("INSERT INTO sources VALUES(?,?,?,?,?)",
                               (str(source.source_id), source.transcript, source.recorded_at.isoformat(), source.timezone, source.speaker_id))

    def record_verification(self, memory_id: UUID | str, evidence_id: UUID | str, result: str,
                            notes: str | None = None, verifier: str | None = None) -> VerificationRecord:
        if result not in {"supports", "contradicts", "inconclusive"}:
            raise ValueError("result must be supports, contradicts, or inconclusive")
        if result == "supports" and self.authorized_verifiers is not None and (
                not verifier or verifier.casefold() not in self.authorized_verifiers):
            raise PermissionError("verifier is not authorized by this RecallLoop instance")
        with self._transaction(write=True) as connection:
            row = connection.execute(
                """SELECT e.memory_id,e.quote,e.source_id,s.transcript FROM evidence e JOIN sources s USING(source_id)
                   WHERE evidence_id=?""", (str(evidence_id),),
            ).fetchone()
            if not row or row["memory_id"] != str(memory_id):
                raise ValueError("verification must reference evidence belonging to this memory")
            if row["quote"] not in row["transcript"]:
                raise ValueError("stored evidence does not match original transcript")
            verification = VerificationRecord(verification_id=uuid4(), memory_id=memory_id, evidence_id=evidence_id,
                result=result, notes=notes, verifier=verifier, created_at=datetime.now(timezone.utc))
            connection.execute("INSERT INTO verification_records VALUES(?,?,?,?,?,?,?)",
                (str(verification.verification_id), str(memory_id), str(evidence_id), result, notes, verifier, verification.created_at.isoformat()))
            commitment = connection.execute("SELECT commitment_id,status FROM commitments WHERE memory_id=?", (str(memory_id),)).fetchone()
            if commitment:
                source = self._load_source(connection, UUID(row["source_id"]))
                self._append_history(connection, commitment["commitment_id"], memory_id, source, "verification_recorded",
                    commitment["status"], commitment["status"], f"{result} evidence recorded by {verifier or 'unspecified verifier'}",
                    occurred_at=verification.created_at)
            return verification

