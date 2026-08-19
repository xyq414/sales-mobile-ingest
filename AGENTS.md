This file is a project-local overlay. It adds project-specific context only. It does not override the user's global Codex AGENTS.md core workflow. If this file conflicts with the global core workflow, follow the global core workflow and treat the conflicting local rule as stale.

# sales-mobile-ingest

- Purpose: read-only Android/MTP call-recording collection into a local, versioned recording contract.
- Entry point: `python -m sales_mobile_ingest probe`, `ingest --once`, and `watch`.
- Data root: CLI `--data-root`, `config.local.json`, `SALES_MOBILE_INGEST_DATA_ROOT`, then the per-user Documents default; never assume a drive letter.
- Primary validation: `python -m pytest` and an MTP probe on the local Windows PC. Phone files are read-only.
- Do not add real recordings, customer metadata, device dumps, or local configuration to Git.

Global workflow, engineering behavior, final report style, Git habits, prompt format, and safety limits are inherited from the user's global Codex AGENTS.md.
