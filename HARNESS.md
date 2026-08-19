# Acceptance harness

- `probe` must enumerate Windows portable devices without assuming a drive letter.
- `ingest --once` must use `inbox` staging, content hashing, deduplication, and JSON-last ready publication.
- `pytest` covers the contract, naming, nullable fields, deduplication, failure/recovery, candidate classification, and drive-independent data roots.
- Real E2E is PASS only if the connected phone yields a genuine call-recording sample copied into a gitignored data root, followed by a duplicate-free second ingest.
