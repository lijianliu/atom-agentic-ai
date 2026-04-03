# Logging V2 — Design Document

**Status:** Draft  
**Author:** TBD  
**Date:** 2026-04-02

---

## 1. Turn-by-Turn REPL Conversation Logging

### Goals

- **Human-readable** — No JSON escape hell when inspecting tool I/O
- **Debuggable** — Easy to replay/inspect what the LLM saw and returned
- **Grep-friendly** — `grep` and `cat` just work

### Problem

Tool inputs/outputs often contain code, JSON, multi-line text, special characters.
When serialized as JSON, readability dies:

```json
{"content": "def foo():\n    return \"bar\\nbaz\"", "error": null}
```

### Source of Truth: `pydantic_ai.Agent.iter()`

Everything yielded by pydantic_ai's `iter()` gets logged with full content.
See `agent/repl.py` for the current implementation.

**Node types from `agent.iter()`:**

| Node | Event/Part | Log Type | Description |
|------|------------|----------|-------------|
| `is_model_request_node` | `ThinkingPart` | `thinking` | Extended thinking content |
| `is_model_request_node` | `TextPart` | `text` | LLM text response |
| `is_model_request_node` | `ToolCallPart` | `tool-plan` | Tool calls LLM wants to make |
| `is_call_tools_node` | `ToolCallPart` | `tool-exec` | Tool execution + results |
| `is_end_node` | — | (not logged separately) | Turn complete |

This gives us a complete, replayable record of the LLM conversation.

### Solution: Heredoc-Style Files

Every LLM output (thinking, text, tool plan, tool exec) gets logged in chronological order:

```
{session_dir}/turn{T}.seq{S}.{type}.txt
```

Where:
- `T` = model request number (matches Usage #N, 3 chars, padded with `_`)
- `S` = sequence within that request (1-based, 3 chars, padded with `_`)
- `type` = `thinking` | `text` | `tool-plan` | `tool-exec`

### Log File Naming

```
t{T}.{S}.{type}.{label}.txt
```

| Component | Description |
|-----------|-------------|
| `T` | Turn number (model request #), 3 chars, padded with `_` |
| `S` | Sequence within turn, 3 chars, padded with `_` |
| `type` | `thinking`, `text`, `tool-plan`, `tool-exec` |
| `label` | 50-char description (alphanumeric + `_` only) |

**Examples:**
```
t__1.__1.text.Let_me_help_you_find_that_information.txt
t__1.__2.tool-plan.execute_command.git_clone_git_gecgithub.txt
t__1.__3.tool-plan.read_file.agent_repl_py.txt
t__1.__4.tool-exec.execute_command.git_clone_git_gecgithub.txt
t__1.__5.tool-exec.read_file.agent_repl_py.txt
t__2.__1.text.Based_on_the_results_I_found.txt
```

**Label rules:**
- Only `0-9`, `a-z`, `A-Z` allowed
- Other characters → `_`
- Multiple underscores collapsed
- Truncated to 50 chars
- Auto-generated:
  - text/thinking: from content
  - tool-plan/tool-exec: `{tool_name}.{args_preview}`

**Note:** Tool executions (`tool-exec`) are logged under the same turn that requested them.

Files sort naturally with `ls` — chronological order within and across turns.

### File Format

Use MIME multipart-style with a unique boundary per file:

```
Boundary: ----=_Part_7f3a9c2b1d4e8f0a6b2c9d5e3f1a8b7c
Timestamp: 2026-04-03T07:06:46.230Z
Tool: read_file
Call-ID: call_abc123
Turn: 1

----=_Part_7f3a9c2b1d4e8f0a6b2c9d5e3f1a8b7c
Content-Type: input

file_path: /path/to/file.py
start_line: 1
num_lines: 50

----=_Part_7f3a9c2b1d4e8f0a6b2c9d5e3f1a8b7c
Content-Type: output

def hello():
    """Say hello."""
    print("Hello, world!")
    return 42

----=_Part_7f3a9c2b1d4e8f0a6b2c9d5e3f1a8b7c--
```

### Format Rules

1. **First line declares boundary** — `Boundary: {boundary}`
2. **Header block** — Metadata as `Key: Value` lines (Timestamp, Tool, Call-ID, Turn)
3. **Blank line** — Separates header from parts
4. **Parts** — Each starts with `{boundary}` on its own line + `Content-Type:` header + blank line + body
5. **Final boundary** — Ends with `{boundary}--` (trailing `--` signals end)

### Boundary Generation

Generate once per file using the full UUID:
```python
boundary = f"----=_Part_{uuid.uuid4().hex}"
```

Example: `----=_Part_7f3a9c2b1d4e8f0a6b2c9d5e3f1a8b7c`

32 hex chars = 128 bits = astronomically unlikely to collide with actual content.

Since boundary is declared at top and unique per file, **content can contain anything** — no escaping needed.

### Why This Works

- **No escaping** — Content is literal, not JSON-encoded
- **Grep-friendly** — `grep -A 100 "Content-Type: output"` just works
- **Human-scannable** — Open in any text editor, instantly readable
- **Diff-friendly** — Easy to compare tool outputs across sessions
- **Standard format** — Follows MIME multipart conventions (RFC 2046)

### Edge Cases

| Scenario | Handling |
|----------|----------|
| Binary output | Base64 encode, add `Content-Encoding: base64` header |
| Tool error | Use `Content-Type: error` instead of `output` |
| No output | Omit output part or use empty body |

---

## 2. Directory Structure

### Goals

- **Single root** — All logs in one predictable location
- **Standard location** — Follow Unix conventions (`/var/log`)
- **Session isolation** — Each session gets its own folder

### Default Log Root

```
~/atom-agentic-ai/logs/
```

Overridable via `ATOM_LOG_DIR` environment variable.

### Folder Layout

```
~/atom-agentic-ai/logs/
├── {session_id}/                      # One folder per session
│   ├── turn__1.seq__1.thinking.txt    # Turn 1: LLM thinking
│   ├── turn__1.seq__2.text.txt        # Turn 1: LLM text
│   ├── turn__1.seq__3.tool-plan.txt   # Turn 1: LLM plans tools
│   ├── turn__1.seq__4.tool-exec.txt   # Turn 1: tool results
│   ├── turn__2.seq__1.tool-plan.txt   # Turn 2: LLM plans tools
│   ├── turn__2.seq__2.tool-exec.txt   # Turn 2: tool results
│   ├── turn__2.seq__3.tool-exec.txt   # Turn 2: more tool results
│   ├── turn__2.seq__4.text.txt        # Turn 2: LLM text
│   └── session.json                   # Conversation history + usage
├── {session_id}/
│   └── ...
└── atom.log                           # Rotating diagnostic log (Python logging)
```
```

### Session ID Format

```
{username}-{ISO8601_timestamp}
```

Example: `userid-2026-04-03T07-06-46.230Z`

- Generated **once** at startup
- Used everywhere: folder name, GCS path, session.json filename
- Timestamp uses `-` instead of `:` for filesystem safety

---

## 3. Open Questions

- [ ] Should we also keep a `tools-index.jsonl` for programmatic access?
- [ ] How does this interact with GCS audit logging? Duplicate or replace?
- [ ] Max file size limits? (Some tool outputs can be huge)
- [ ] Retention policy? Auto-cleanup after N days?

---

## References

- [RFC 2046 — MIME Multipart](https://datatracker.ietf.org/doc/html/rfc2046)
- [Current logging docs](./logging.md)
