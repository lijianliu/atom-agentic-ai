# Atom Agent — Logging

Atom Agent has two independent logging systems:

1. **Local rotating file logger** — internal diagnostics (Python `logging`)
2. **GCS audit logger** — structured user-activity events (JSONL → GCS)

They serve different purposes and can be enabled independently.

---

## 1. Local Rotating File Logger

**File:** `agent/logging_config.py`

Standard Python `logging` with a `RotatingFileHandler` — similar to
log4j's `RollingFileAppender` with a size-based rotation policy.

### Log location

```
~/.config/atom-agentic-ai/logs/atom.log
```

The path is printed to the console when the agent starts.

### Rotation policy

| Setting          | Value  | log4j equivalent     |
|------------------|--------|----------------------|
| Max file size    | 20 MB  | `MaxFileSize`        |
| Backup count     | 4      | `MaxBackupIndex`     |
| Total disk cap   | 100 MB | (size × (count + 1)) |

When `atom.log` exceeds 20 MB, the handler rotates:

```
atom.log.4  → deleted
atom.log.3  → atom.log.4
atom.log.2  → atom.log.3
atom.log.1  → atom.log.2
atom.log    → atom.log.1
(new)       → atom.log
```

Total disk usage never exceeds **100 MB**.

### Log levels by handler

| Handler  | Min level   | What you see                              |
|----------|-------------|-------------------------------------------|
| **File** | `DEBUG`     | Everything — full operational trace       |
| **Console (stderr)** | `WARNING` | Only warnings and errors            |

### Console output behavior

By default, **only `WARNING` and above** messages appear on the console.
This keeps the interactive REPL clean — `DEBUG` and `INFO` messages are
silently captured in the log file for post-mortem analysis.

If you see a message printed to your terminal from the logger, it means
something noteworthy happened:

| Level      | Meaning                                        |
|------------|------------------------------------------------|
| `WARNING`  | Something unexpected but non-fatal (e.g. token refresh failed, env var misconfigured) |
| `ERROR`    | An operation failed (e.g. GCS flush failed) — the agent continues running |
| `CRITICAL` | A severe failure — the agent may not function correctly |

### File log format

```
2026-03-28 09:15:42 DEBUG    [atom.gcs_audit_logger] Fetched fresh gcloud access token (1024 chars)
2026-03-28 09:15:42 INFO     [atom.agent] Session started: abc-123
2026-03-28 09:16:01 WARNING  [atom.gcs_audit_logger] Token refresh failed — keeping old token: ...
```

### Console log format

```
WARNING  Token refresh failed — keeping old token: ...
ERROR    GCSLogger: flush to gs://bucket/path/... failed
```

### Usage in code

```python
from logging_config import get_logger

logger = get_logger(__name__)   # → atom.<module_name>

logger.debug("Low-level detail")       # file only
logger.info("Normal operation")        # file only
logger.warning("Something is off")     # file + console
logger.error("Something broke", exc_info=e)  # file + console
```

---

## 2. GCS Audit Logger

**File:** `agent/gcs_audit_logger.py`

A structured, async-safe logger that writes user-activity events as JSONL
to a Google Cloud Storage bucket. Designed for audit trails and analytics.

### Enabling

Set the `ATOM_AUDIT_LOG_GCS_PATH` environment variable:

```bash
export ATOM_AUDIT_LOG_GCS_PATH=gs://my-bucket/my-folder/my-prefix
```

- First segment after `gs://` → **bucket name** (e.g. `my-bucket`)
- Remaining path → **blob prefix** (e.g. `my-folder/my-prefix`)

If the variable is not set, the GCS logger is silently disabled.

### GCS blob path

```
gs://<bucket>/<prefix>/<YYYY-MM-DD>/<session-uuid>.jsonl
```

Example:
```
gs://hubble-ui/lijian-atom-audit-logging/ATOM_LOG_/2026-03-28/1aee8756-...jsonl
```

### Authentication

The logger uses `gcloud auth print-access-token` to obtain an OAuth2
access token from the currently active `gcloud` account. This means:

- It works with whatever account `gcloud` is configured to use
  (personal or service account).
- No service-account key files or `GOOGLE_APPLICATION_CREDENTIALS`
  env var required.
- Tokens are cached and automatically refreshed when older than
  `TOKEN_TTL_SECONDS` (default: 25 minutes; gcloud tokens
  are valid for ~60 minutes).

The `GCSClientFactory` is thread-safe. Callers should **never cache**
the returned client — always call `get_client()` to ensure a fresh token.

### Events logged

Each event is a single JSON line with at minimum:

```json
{
  "ts": "2026-03-28T09:15:42.123456+00:00",
  "session_id": "1aee8756-7002-48f3-be4d-e8477536d094",
  "event": "user_prompt",
  "prompt": "list all buckets"
}
```

Event types include:

| Event              | Description                        |
|--------------------|------------------------------------|
| `session_start`    | Agent session began                |
| `user_prompt`      | User entered a prompt              |
| `tool_call`        | Agent invoked a tool               |
| `agent_response`   | Agent returned a response          |
| `token_usage`      | LLM token consumption              |
| `session_end`      | Agent session ended (includes `started_at`) |

### Flush behavior

| Trigger                 | Mechanism                                   |
|-------------------------|---------------------------------------------|
| **Auto-flush**          | Every 50 pending events (`AUTO_FLUSH_EVERY`) |
| **Idle flush**          | After 60 s of inactivity (background asyncio task) |
| **Manual flush**        | `await logger.flush()`                       |
| **Session close**       | `await logger.close()` flushes + writes `session_end` |

Each flush **overwrites** the blob with the full session content
(all accumulated lines). This avoids GCS-level append complexity
and race conditions.

### Fire-and-forget guarantee

All GCS operations are wrapped in exception handlers. A flush failure
is logged as an `ERROR` (visible on console) but **never crashes the
agent**. Failed flushes retain their pending count and retry on the
next trigger.

### Dependencies

```bash
uv add google-cloud-storage
```

If `google-cloud-storage` is not installed, the logger disables itself
with a warning and the agent continues normally.
