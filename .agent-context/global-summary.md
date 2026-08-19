# Project summary

`sales-mobile-ingest` is a Windows-local ingestion boundary for Android call recordings. It reads a phone over normal MTP/Shell access, copies media without transcoding, validates and hashes it locally, and exposes a `ready/recordings` JSON-sidecar contract to downstream Sales AI.

Real recordings and local state belong only below the configurable data root and are ignored by Git. The first device target is an OPPO A6 Pro, but the architecture supports vendor adapters plus a generic Android path.
