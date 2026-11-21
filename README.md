# RecallLoop commitment and recall engine

RecallLoop validates natural-language commitment extractions, stores source-backed memories in SQLite, searches them with FTS5, answers recall questions from transcript evidence, and runs explicitly approved actions through local mock tools. It does not connect to Agora, Murf, messaging providers, or ticketing services.

## Architecture

The code is split into four layers so each part has one clear job:

1. **Extraction** — `models.py` defines the Pydantic shapes; `extractor.py` parses and checks an LLM response against the transcript. The existing date resolver remains in `dates.py`.
2. **Lifecycle rules** — `lifecycle.py` defines legal state transitions and shared Kolkata deadline calculations. The repository records each transition in history.
3. **Storage** — `database.py` owns SQLite tables, foreign keys, FTS5, dependency edges, and transactions. It stores the original source transcript and timestamp with each memory. The older `MemoryStore` in `store.py` remains as a small in-memory example for original tests; new application code should use `SQLiteMemoryStore`.
4. **Retrieval and intelligence** — `retrieval.py` applies conservative answer rules and prepares overdue recovery suggestions. It never executes a suggestion.
5. **Action execution** — `action_service.py` applies approval policy and dispatches approved actions to the small tool interface in `action_tools.py`. `action_store.py` persists action state, idempotency, approvals, evidence references, and immutable events.
6. **HTTP API** — `api.py` maps JSON requests to storage, retrieval, and action services with FastAPI. `create_app(path)` makes it easy to use a separate database in development and tests.

### Beginner notes

- **SQLite** is a relational database stored in one local file. Each table holds one kind of thing; IDs connect related rows. For example, a memory row points to its source row with a foreign key, so a memory cannot refer to a transcript that does not exist.
- **Transactions** group writes into one all-or-nothing operation. If a correction fails halfway through, SQLite rolls it back instead of leaving half a correction behind.
- **FTS5** is SQLite's built-in full-text search extension. RecallLoop indexes the memory text, transcript, evidence quote, commitment statement, and resolved date. It returns ranked candidates; retrieval policy still decides whether the evidence is clear enough to answer.
- **Pydantic models** validate incoming data at the boundary. Invalid timestamps, unknown fields, unsupported confirmation language, and evidence quotes absent from their transcript are rejected before storage.
- **Protocols** in `retrieval.py` describe what the retrieval layer needs from a repository without tying it to SQLite internals. This makes it possible to replace the database later without rewriting answer policy.

## Evidence and lifecycle rules

- A stored memory always references an original transcript and its timezone-aware timestamp. Evidence quotes are checked against that transcript.
- A confirmed commitment requires explicit, non-conditional commitment language. A proposal, refusal, or vague statement is not silently turned into a promise.
- Actor and beneficiary identities must appear in the cited quote. Ambiguous and unknown identities remain unresolved.
- Supported dates resolve deterministically using the transcript time in `Asia/Kolkata`. Unsupported wording stays unresolved.
- Corrections create a new memory and commitment linked to the previous commitment. Older versions remain available and their memory lifecycle is marked `superseded`.
- Lifecycle transitions follow a state graph. Confirming a proposal requires acceptance evidence; recording a completion claim moves it only to `completed_pending_verification`; reaching `verified` requires a matching, named verification record.
- Lifecycle states are `proposed`, `confirmed`, `in_progress`, `overdue`, `recovery`, `completed_pending_verification`, `verified`, `cancelled`, and `disputed`. A common path is `proposed → confirmed → in_progress → overdue → recovery → completed_pending_verification → verified`; the code validates the allowed edges and records each change.
- Date-only deadlines remain due through the end of their local Kolkata date. Proposed and disputed items are excluded from automatic overdue marking. `mark_overdue_commitments` is explicit and repeat-safe.
- Ownership can be searched by responsible person. Records with unknown/ambiguous owners and records with no assigned actor are queryable separately; ambiguity is not resolved by guessing.
- Dependency edges require a quote from the dependent task's transcript. SQLite checks for cycles. A dependency is unblocked only when its prerequisite is verified; a completion claim pending verification still blocks it.
- Conflict detection reports duplicates, incompatible states, differing deadlines, and explicit revisions as separate records. It never merges them.
- Recovery recommendations are suggestions only, each marked `automatic_execution: false`. No email, escalation, or scheduling action is sent.
- Executable actions are separate records and have their own status lifecycle: `proposed`, `awaiting_approval`, `approved`, `executing`, `succeeded`, `failed`, `cancelled`, `verification_pending`, and `verified`. They do not change a commitment's status unless an explicitly approved `cancel_commitment` action calls the existing evidence-checked transition.
- Approvals save a fingerprint of the exact action type, description, target, parameters, commitment, and evidence links. Changing parameters clears the approval. Consequential failures require fresh approval before an explicit retry.
- Actions use a client-supplied idempotency key. Repeating the same request returns the same action; reusing the key for different content returns a conflict. Successful mock tools also deduplicate by that key.
- Tool success records that the mock operation ran. It does not mark the action verified or the underlying commitment complete. Verification is a separate state change requiring linked evidence and a verifier listed in `RECALLLOOP_AUTHORIZED_VERIFIERS`.
- Disputes add evidence and a history event; they do not erase the prior record. Verification records must point to an evidence quote that is present in its source transcript. A verification row records what the verifier judged; it does not independently prove real-world completion.
- `answer_from_memory` returns only a retrieved transcript quote, its source transcript, timestamp, memory ID, confidence, and retrieval status. Missing, disputed, conflicting, tied, or ambiguous evidence returns `insufficient_evidence`. Completion questions stay unanswered because this version does not store execution records.
- Duplicate submissions with identical source text and memory content return the existing memory ID and do not create another record.

## SQLite tables

`sources`, `people`, `memories`, `commitments`, `commitment_people`, `commitment_dependencies`, `evidence`, `verification_records`, `history_events`, action tables (`actions`, `action_events`, `action_evidence`, and `action_verifications`), and FTS5 table `memory_fts` are created on startup. Existing databases gain the acceptance-evidence and dependency-evidence columns in place. Foreign-key enforcement is enabled on each connection. The default database file is `recallloop.sqlite3` in the working directory; set `RECALLLOOP_DB_PATH` to change it.

## API

Start the local server from this directory:

```powershell
python -m uvicorn recallloop.api:app --reload
```

FastAPI's interactive API page is at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

| Method and path | Purpose |
|---|---|
| `POST /memories` | Store one memory and optional validated commitment with source evidence |
| `GET /memories/search?query=...` | Search text, evidence, transcript, task, and due date |
| `GET /commitments?responsible_person=...&ownership=ambiguous` | List commitments, optionally by responsible person or ownership state |
| `GET /commitments/overdue?reference_datetime=...` | Find eligible overdue commitments at a supplied aware timestamp |
| `GET /commitments/upcoming?reference_datetime=...&days=7` | Find deadlines in the upcoming window |
| `PATCH /commitments/{id}/status` | Apply a validated lifecycle transition with supporting source/evidence where needed |
| `GET /commitments/{id}` | Retrieve a commitment with its memory and source evidence |
| `GET /commitments/{id}/history` | Retrieve prior versions and correction/dispute events |
| `GET /commitments/{id}/dependencies` | Inspect blocking tasks and missed dependency deadlines |
| `GET /commitments/{id}/recovery-options` | Get non-executing recovery suggestions for an overdue commitment |
| `POST /recall` | Answer from retrieved evidence or return `insufficient_evidence` |
| `POST /actions` | Create a proposed action; consequential types wait for approval |
| `GET /actions?commitment_id=...&status=...` | List persisted actions |
| `GET /actions/{id}` | Retrieve an action and current result/error |
| `POST /actions/{id}/approve` | Approve the exact current action parameters |
| `POST /actions/{id}/execute` | Execute once through a registered mock/local tool |
| `POST /actions/{id}/cancel` | Cancel an action that has not started execution |
| `GET /actions/{id}/history` | Read append-only action lifecycle events |
| `POST /actions/{id}/verify` | Verify a successful action with evidence and an authorized verifier |

Example memory request:

```json
{
  "source": {
    "transcript": "I will send the agenda to Riya tomorrow.",
    "recorded_at": "2026-11-21T15:30:00+05:30",
    "timezone": "Asia/Kolkata",
    "speaker_id": "speaker-1"
  },
  "commitment": {
    "statement": "Send the agenda to Riya",
    "status": "confirmed",
    "evidence": "I will send the agenda to Riya tomorrow.",
    "actor": "I",
    "actor_confidence": "clear",
    "beneficiary": "Riya",
    "due_expression": "tomorrow"
  }
}
```

Example recall response contains `answer`, `source_transcript`, `source_timestamp`, `matching_memory_id`, `evidence_confidence`, and `retrieval_status`. If nothing supports the answer, the remaining source fields and ID are null and the status is `insufficient_evidence`.

For `PATCH /commitments/{id}/status`, send a JSON `status`. Evidence-bearing transitions also need a `source` object and an `evidence` quote copied from that transcript. To confirm a proposal, provide explicit acceptance wording; to enter `completed_pending_verification`, provide a completion claim; to enter `verified`, first record a supporting verification by a named verifier. The API rejects direct status changes to `overdue`; deadline monitoring calculates that state.

### Action approval and execution

The two mock tool implementations share an `ActionTool` interface. `MockNotificationTool` records draft messages, simulated sends, and recommendations. `MockTicketTool` creates, updates, and reassigns in-memory mock tickets. `CommitmentMutationTool` applies a locally approved cancellation through the existing commitment transition validator. No real external service is contacted.

`draft_message` and `record_recommendation` can execute from `proposed`. `send_message`, `reassign_technician`, `cancel_commitment`, `create_ticket`, and `update_ticket` start in `awaiting_approval`. Approval is tied to a SHA-256 fingerprint of the action parameters and references. The action store uses a SQLite unique key for idempotency; a repeated identical POST returns the existing action with HTTP 200, while a changed request under the same key returns HTTP 409.

Each execution is explicit. A retryable mock failure can be retried by a new execute request; consequential actions first need fresh approval. Timeouts and ambiguous tool errors are recorded as non-retryable so RecallLoop will not risk repeating an unknown side effect. Each attempt is added to `action_events`; current action fields provide a convenient snapshot but do not replace the event log.

To enable action or commitment verification, configure an allow-list before starting the API:

```powershell
$env:RECALLLOOP_AUTHORIZED_VERIFIERS = "reviewer-1,reviewer-2"
python -m uvicorn recallloop.api:app --reload
```

The default allow-list is empty. An authorized verifier still must provide existing evidence references and notes. Action verification updates only the action record; it never marks the commitment complete. Commitment completion must separately pass the commitment lifecycle's evidence and verifier checks.

Example send workflow:

```http
POST /actions
Content-Type: application/json

{"action_type":"send_message","description":"Send the repair update","target":"Riya","parameters":{"recipient":"Riya","message":"The technician is scheduled for Friday."},"idempotency_key":"repair-update-001"}
```

The response is initially `awaiting_approval`. Then explicitly approve and execute it:

```http
POST /actions/{action_id}/approve
Content-Type: application/json

{"approved_by":"operator-1"}
```

```http
POST /actions/{action_id}/execute
```

The mock notification tool returns a simulated delivery reference and records the recipient/message in process memory. SQLite persists the action result and event history. Restarting the app preserves action records, but resets mock message and ticket collections; this is deliberate because the tools are test doubles, not external services.

The database service also provides `find_commitments(person)`, `check_existing_commitment(person, task)`, `add_dependency(...)`, `detect_conflicts(...)`, `mark_disputed(...)`, and `record_verification(...)` for application code. These operations preserve their evidence and history in SQLite. The lifecycle update request accepts a `status`; evidence-bearing transitions also accept `source`, exact `evidence`, optional `detail`, and attribution fields. A `verified` transition additionally requires an existing `supports` verification record tied to completion-claim evidence and the same named verifier.

## Install, run, and test

Python 3.11 or newer is required. SQLite must be built with FTS5 support (included in standard recent Python distributions).

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest
```

The suite preserves the original 25 golden cases and adds coverage for persistence, retrieval, lifecycle transitions, deadline handling, dependencies, conflicts, recovery, and real HTTP action workflows using FastAPI `TestClient`. Run `python -m pytest` to execute everything. The run writes `reports/golden_test_report.md` with case-by-case results for all collected tests. The report reflects the most recent run.

## Deployment readiness (Render)

`render.yaml` describes a single-instance Render web service using the existing FastAPI application, a 1 GB persistent disk, and `/health` as its platform health check. The database path points inside that disk. This configuration uses Render's paid `starter` service plan because free Render web services cannot attach persistent disks; the service is intended for a small single-instance deployment, not horizontal scaling. Review the current plan and disk charges in Render before creating the service.

To deploy:

1. Push this project, including `render.yaml`, to a Git provider account connected to your Render account. The project must be at the repository root.
2. In the Render dashboard choose **New → Blueprint**, select that repository, and review the proposed `recallloop-api-prod` service and billing before applying it.
3. Wait for the deployment to become live. Render's generated URL is the application URL; append `/docs` for interactive API documentation and `/health` for the public health check.
4. Copy the generated `RECALLLOOP_API_KEY` from the service's environment settings into a trusted client. Send it as `X-API-Key` on API requests. The health endpoint, `/docs`, `/redoc`, and `/openapi.json` are public; memory, commitment, recall, and action routes require the key in production.

Example PowerShell request (replace the host with the deployed service URL and set the key in your shell):

```powershell
$env:RECALLLOOP_API_KEY = "<generated-key>"
$headers = @{ "X-API-Key" = $env:RECALLLOOP_API_KEY }
Invoke-RestMethod "https://<service>.onrender.com/health"
Invoke-RestMethod "https://<service>.onrender.com/memories/search?query=repair" -Headers $headers
```

The application rejects production startup when `RECALLLOOP_API_KEY` is missing. Do not put the key in a browser frontend or public source file: it grants access to all current API routes, including approval and execution operations. This is a single shared-key protection layer, not per-user authentication or role-based access control. The SQLite disk is attached to one service instance; do not scale this deployment to multiple instances. The tools remain local mocks and do not send real notifications or create tickets in an external system.

## Golden suite provenance

The earlier task referenced “RecallLoop Golden Test Suite v1.0”, but it was not included in the supplied workspace. `data/golden_test_cases.json` is an authored 25-case baseline based on the stated requirements, not a claim to reproduce an unavailable specification.
