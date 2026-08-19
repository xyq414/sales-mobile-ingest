# Sales mobile ingest v1 scope

## Outcome

Create a durable, local Windows collection boundary:

`Android call recording (read-only MTP) -> validated local bytes -> recording contract v1`

## In scope

- Windows Shell/Portable Device discovery without drive-letter assumptions.
- Bounded candidate-directory search, OPPO v1 evidence rules, generic adapter guardrails.
- Atomic local staging, SHA-256 content identity, persistent deduplication, and JSON sidecars.
- `probe`, one-shot ingest, watch mode, Windows user-logon task scripts, tests, and private Git synchronization.

## Out of scope

Transcription, AI calls, CRM/contact matching, WeChat, cloud upload, media conversion, lifecycle deletion, and any mutation of the phone.
