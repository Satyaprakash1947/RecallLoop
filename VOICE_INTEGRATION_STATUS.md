# RecallLoop voice integration status

Updated: 2026-11-21

## Implemented in this repository

- Voice session start/stop service, persistent session state, and append-only transcript rows in the existing SQLite database.
- Server-side Agora Platform SDK adapter, RTC and RTM token generation, and short-lived browser session credentials. The App Certificate is never sent to JavaScript.
- Browser voice UI for microphone consent, Agora join/publish/playback, transcript view, text fallback, session cleanup, and action approval/rejection.
- Evidence context tool that calls the existing retrieval, commitment history, deadline, dependency, and recovery services.
- Transcript extraction endpoint that passes model output through the existing `ExtractionInterface`; each created memory has the final voice transcript as its source, and unsupported evidence returns a validation error.
- Mock technician availability roster and guarded ticket reassignment through the existing approved action workflow.
- Native Agora Murf TTS configuration path; agent speech comes over RTC, avoiding a second browser speech stream for voice turns.
- Render Free configuration retained without a paid plan or disk. SQLite data is ephemeral on that platform.

## Locally tested

Automated test status: **77 passed, 0 failed**, including the original golden suite (25/25). See `reports/golden_test_report.md`. Tests use a fake voice provider and local action tools; they do not contact Agora or Murf.

## Successfully tested against Agora

Not tested. The runtime has no Agora environment configuration and no authenticated vendor request has been made. The code is based on Agora's current Platform Python SDK and web SDK documentation linked below.

## Successfully tested against Murf

Not tested. Murf is configured as the saved Agora agent's TTS vendor, with Falcon through Agora's native Murf adapter. The Murf credential must be stored as an Agora secret; this application does not receive or expose the key.

## Requires account configuration

Set these secrets in the RecallLoop server environment: `AGORA_APP_ID`, `AGORA_APP_CERTIFICATE`, and `AGORA_AGENT_ID`. In Agora Agent Studio, configure ASR/LLM, Murf TTS with the Falcon model and voice, RTM transcript delivery, the required `recallloop_session_id` variable, and the authenticated RecallLoop custom tools. The `X-API-Key` tool header must use the production `RECALLLOOP_API_KEY`.

The provided vendor values were not copied into project files, shell history, Render configuration, or the browser. Configure secrets through environment/secret managers. Since vendor credentials were shared in chat, rotate them before enabling a public service.

## Mocked functionality

- Technician availability uses the configured synthetic `RECALLLOOP_MOCK_TECHNICIANS` roster. It is not connected to a real roster or ticketing system.
- Ticket reassignment executes only through `MockTicketTool` after user approval. Tool success does not verify that the real repair happened.
- Automated Agora/Murf behavior is replaced by a fake provider in tests.

## Not implemented / limitations

- No live authenticated Agora or Murf session has been executed here.
- No external ticket service, real technician availability, outbound notification, or autonomous completion verification.
- An agent must be saved and configured in Agora Agent Studio; the repository does not create that saved agent automatically.
- Agora custom-tool callbacks need a public HTTPS URL. A localhost callback cannot be reached by Agora.
- Render Free has no persistent disk; SQLite memories, action records, and transcripts can disappear after restart or deploy.
- The current API uses a single shared API key. It is suitable only for a trusted demo and is not per-user approval authorization.

## References

- [Agora Conversational AI Python Platform SDK](https://docs.agora.io/en/api-reference/api-ref/server-sdk/platform/python)
- [Agora backend and browser client guide](https://docs.agora.io/en/ai/build/build-server-client)
- [Agora Murf TTS adapter](https://docs.agora.io/en/ai/platform/models/tts/murf)
- [Agora Web SDK API reference](https://api-ref.agora.io/en/voice-sdk/web/4.x/index.html)
- [Agora Signaling Web SDK](https://docs.agora.io/en/api-reference/api-ref/signaling/web)
