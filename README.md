# RecallLoop commitment and recall engine

RecallLoop validates natural-language commitment extractions, stores source-backed memories in SQLite, searches them with FTS5, answers recall questions from transcript evidence, and runs explicitly approved actions through local mock tools. The voice interface uses Agora Conversational AI Platform sessions and can use Agora's native Murf TTS adapter; these vendor paths require separate account setup and are not represented as live-tested here.

## Architecture

The code is split into four layers so each part has one clear job:

1. **Extraction** — `models.py` defines the Pydantic shapes; `extractor.py` parses and checks an LLM response against the transcript. The existing date resolver remains in `dates.py`.
2. **Lifecycle rules** — `lifecycle.py` defines legal state transitions and shared Kolkata deadline calculations. The repository records each transition in history.
3. **Storage** — `database.py` owns SQLite tables, foreign keys, FTS5, dependency edges, and transactions. It stores the original source transcript and timestamp with each memory. The older `MemoryStore` in `store.py` remains as a small in-memory example for original tests; new application code should use `SQLiteMemoryStore`.
4. **Retrieval and intelligence** — `retrieval.py` applies conservative answer rules and prepares overdue recovery suggestions. It never executes a suggestion.
5. **Action execution** — `action_service.py` applies approval policy and dispatches approved actions to the small tool interface in `action_tools.py`. `action_store.py` persists action state, idempotency, approvals, evidence references, and immutable events.
6. **HTTP API** — `api.py` maps JSON requests to storage, retrieval, and action services with FastAPI. `create_app(path)` makes it easy to use a separate database in development and tests.
7. **Voice interface** — `voice.py` starts and stops Agora sessions, persists session transcript events, and routes evidence lookup through the existing retrieval service. `web/` is a small browser client; it receives short-lived RTC/RTM tokens, never the App Certificate.

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
| `POST /api/voice/session/start` | Start an Agora session and return browser RTC/RTM credentials |
| `POST /api/voice/session/{id}/stop` | Stop Agora and persist stopped state |
| `GET /api/voice/session/{id}` | Read persisted voice session state |
| `GET /api/voice/transcript/{id}` | Read transcript lines captured from the Agora client |
| `POST /api/voice/transcript/{id}` | Append validated transcript lines |
| `POST /api/voice/session/{id}/extract` | Validate an agent extraction against this session's exact final transcript, then persist source-linked memories |
| `POST /api/voice/message` | Text fallback; answer through evidence-based retrieval |
| `POST /api/voice/evidence` | Read-only evidence preview used by the browser |
| `POST /api/voice/tools/recall` | Authenticated Agora custom tool; returns sourced memory, commitment, history, deadlines, dependencies, and recovery options |
| `GET /api/voice/tools/technicians` | Read the synthetic mock technician roster |
| `POST /api/voice/tools/action-proposal` | Create an action through the existing action service; does not approve or execute it |
| `POST /api/voice/action/{id}/approve` | Approve the exact action request from the voice UI |
| `POST /api/voice/action/{id}/reject` | Cancel/reject an unexecuted action |

### Voice setup and operation

The browser interface is served at `/`. In production it asks for `X-API-Key` in a password field; the key stays in page memory and is never written to browser storage. `POST /api/voice/session/start` creates the agent session server-side, then returns the App ID, channel, UID, and expiring RTC/RTM tokens needed by the browser. The App Certificate and Murf key are not returned. Microphone capture, channel join, audio playback, RTM transcripts, and cleanup use the Agora browser SDK. Speaking while the agent is talking is the interruption mechanism; Agora controls the turn-taking behavior. If microphone permission is denied or connection setup fails, the page displays a recoverable error and the text input remains available.

Agora's Conversational AI Platform currently documents its Python Platform SDK as preview. Install the dependencies in `requirements.txt`. Create a saved Conversational AI agent in the Agora Console/Agent Studio and set `AGORA_APP_ID`, `AGORA_APP_CERTIFICATE`, and its saved ID in `AGORA_AGENT_ID` on the RecallLoop server. The selected implementation uses App ID + App Certificate token authentication; it does not need Agora Customer ID/Secret. Agora's Platform docs currently show US routing, so `AgoraPlatformProvider` uses the US area.

Configure the saved agent with:

1. A cascaded speech pipeline: choose an ASR language and an available LLM/provider in Agora. The LLM must be configured to use the tools below for memory or action questions.
2. TTS vendor **Murf**, credential mode **BYOK**, with its API key saved in an Agora secret set. Configure the chosen Murf voice ID, locale, and Falcon model (`FALCON` in Agora's Murf adapter; the Murf API model family is Falcon 2.1), with 24 kHz output. Agora's Murf integration sends streamed audio over the RTC session, so RecallLoop does not also play a second Murf stream in the browser.
3. Enable RTM/Signaling transcript delivery and set the data channel to `rtm`. The saved-agent tool instructions should say: “For claims about prior discussions, call `recall_memory` first. Cite only returned source transcript, timestamp, status, and evidence. If no record matches, ask a question rather than filling gaps. If completion evidence is missing, say you cannot verify completion, cite the earlier commitment if supplied, and do not claim the physical work is done or definitely incomplete. For reassignment, call `available_mock_technicians` and ask the user to name one of those options. Call `propose_action` only with exact ticket/commitment details supported by retrieved records. Never approve or execute an action; wait for the user.”
4. Add authenticated custom tools using the deployed HTTPS origin and the `X-API-Key` header. Store the API key as an Agora secret/credential and do not paste it into a public tool description.

| Agent tool | Method and URL | Input |
|---|---|---|
| `recall_memory` | `POST https://<host>/api/voice/tools/recall` | `{"query":"..."}` |
| `available_mock_technicians` | `GET https://<host>/api/voice/tools/technicians` | none |
| `propose_action` | `POST https://<host>/api/voice/tools/action-proposal` | Valid `ActionCreate` JSON, including unique `idempotency_key` |
| `save_transcript_memory` | `POST https://<host>/api/voice/session/{session_id}/extract` | `{"session_id":"...","content":"...","extraction_response":{"commitments":[...],"unresolved":[]}}` |

The API-key middleware protects these callbacks too. Local text recall works without Agora credentials. A real Agora callback requires a public HTTPS origin; `localhost` is not reachable by Agora's servers. The UI's text fallback uses browser speech synthesis only for text-entered questions. For microphone turns, Agora/Murf supplies audio while the browser shows transcript text, preventing duplicate speech. If Agora or Murf fails, the UI reports the error; text retrieval remains available. The direct Murf WebSocket API is not separately called because Agora's documented native Murf adapter already streams speech over RTC.

Declare a required string session variable named `recallloop_session_id` in the saved agent so the platform passes the local session identifier to its custom tool context. The server injects this value when creating each Agora session. The extraction callback builds the source from the persisted final transcript and validates every commitment quote, attribution, certainty, and date with the existing `ExtractionInterface`; invalid or fabricated quotes return HTTP 422. A saved transcript with no commitment can be stored with `content` and an empty `commitments` array. This is an explicit tool call: ordinary conversation is not silently turned into a stored memory.

The default technician names are synthetic mock options for local demos, not a statement about real people or their live availability. Set `RECALLLOOP_MOCK_TECHNICIANS` to the mock names you want to allow. Ticket changes still run through `MockTicketTool`, require an approved action, and do not establish physical completion. The example cannot reassign a real maintenance ticket.

For local operation, copy `.env.example` to `.env` (never commit `.env`), fill the Agora settings only when you want to test vendor sessions, then run:

```powershell
Copy-Item .env.example .env
python -m pip install -r requirements.txt
python -m uvicorn recallloop.api:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), enter `RECALLLOOP_API_KEY` if configured, and allow microphone access. To exercise only the text and memory path, run without Agora credentials and use the text box. For a vendor integration test, configure a saved agent with the tools above and use a reachable HTTPS deployment.

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

`render.yaml` describes a single-instance Render Free web service and `/health` as its platform health check. It has no persistent disk or paid service plan. **SQLite files on Render Free are ephemeral and can be lost on restart, deploy, or instance replacement.** Use local SQLite for development only; persistent production memory needs a separately approved persistent database service. The Agora Platform Preview and provider usage may have their own account requirements and costs; this repository does not provision or verify them.

To deploy:

1. Push this project, including `render.yaml`, to a Git provider account connected to your Render account. The project must be at the repository root.
2. In the Render dashboard choose **New → Blueprint**, select that repository, and review the proposed `recallloop-api-prod` service and billing before applying it.
3. Wait for the deployment to become live. Render's generated URL is the application URL; append `/docs` for interactive API documentation and `/health` for the public health check.
4. Add `AGORA_APP_ID`, `AGORA_APP_CERTIFICATE`, and `AGORA_AGENT_ID` in the Render service environment if using voice. Add the `AGORA_APP_CERTIFICATE` only as a secret. Configure Agora Agent Studio's authenticated tools to use the service URL and the generated `RECALLLOOP_API_KEY` as `X-API-Key`.
5. Copy the generated `RECALLLOOP_API_KEY` from the service's environment settings into the trusted browser UI or service client. Send it as `X-API-Key` on API requests. The static voice interface and health/docs pages are public; memory, commitment, recall, voice, and action routes require the key in production.

Example PowerShell request (replace the host with the deployed service URL and set the key in your shell):

```powershell
$env:RECALLLOOP_API_KEY = "<generated-key>"
$headers = @{ "X-API-Key" = $env:RECALLLOOP_API_KEY }
Invoke-RestMethod "https://<service>.onrender.com/health"
Invoke-RestMethod "https://<service>.onrender.com/memories/search?query=repair" -Headers $headers
```

The application rejects production startup when `RECALLLOOP_API_KEY` is missing. This deployment uses one shared API key, not per-user authentication or role-based access control; voice-interface users can approve actions available to the deployment. Use it only for a trusted demo until role-based access exists. Render Free SQLite is ephemeral: all memories, actions, and voice transcripts can be lost on service restart or redeploy. Do not treat this free deployment as durable memory storage. Mock notifications and tickets do not contact external services.

## Golden suite provenance

The earlier task referenced “RecallLoop Golden Test Suite v1.0”, but it was not included in the supplied workspace. `data/golden_test_cases.json` is an authored 25-case baseline based on the stated requirements, not a claim to reproduce an unavailable specification.
