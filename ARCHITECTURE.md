# Architecture

```text
Windows Shell/MTP bridge (PowerShell, read-only phone)
  -> probe descriptors
  -> Python adapter classification
  -> inbox/recordings/.stage
  -> size check + SHA-256 + persistent state
  -> ready/recordings/media
  -> ready/recordings/JSON sidecar (commit signal)
```

The bridge owns Windows virtual-folder access only. Python owns configuration, classification, contract validation, staging semantics, durable state, logging, and CLI behavior. Downstream consumers see only the ready media/sidecar pair and never need a phone path or adapter rule.
