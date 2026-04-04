# Logging V2 — Design Document

**Status:** Draft  
**Author:** TBD  
**Date:** 2026-04-02

---

## 1. Turn-by-Turn REPL Conversation Logging

### Hierarchy

```
Session > Query > Turn > Sequence
```

| Level | Description | Example |
|-------|-------------|---------|
| **Session** | One REPL session (folder) | `userid-2026-04-03T07-06-46.230Z/` |
| **Query** | One user prompt | `q01` |
| **Turn** | One model request within a query | `t01`, `t02` |
| **Sequence** | One logged item within a turn | `s01`, `s02` |

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
| `is_model_request_node` | `ToolCallPart` | `plan` | Tool calls LLM wants to make |
| `is_call_tools_node` | `ToolCallPart` | `exec` | Tool execution + results |
| `is_end_node` | — | (not logged separately) | Turn complete |

This gives us a complete, replayable record of the LLM conversation.

### Solution: Heredoc-Style Files

Every LLM output (thinking, text, tool plan, tool exec) gets logged in chronological order:

```
{session_dir}/q{QQ}.t{TT}.s{SS}.{type}.txt
```

Where:
- `QQ` = query number (user prompt #), 2 digits, zero-padded
- `TT` = turn number (model request # within query), 2 digits, zero-padded
- `SS` = sequence within that turn, 2 digits, zero-padded
- `type` = `thinking` | `text` | `plan` | `exec`

### Log File Naming

```
q{QQ}.t{TT}.s{SS}.{type}.{label}.txt
```

| Component | Description |
|-----------|-------------|
| `QQ` | Query number (user prompt #), 2 digits, zero-padded |
| `TT` | Turn number (model request #), 2 digits, zero-padded |
| `SS` | Sequence within turn, 2 digits, zero-padded |
| `type` | `thinking`, `text`, `plan`, `exec` |
| `label` | 50-char description (alphanumeric + `_` only) |

**Examples:**
```
q01.t01.s01.text.Let_me_help_you_find_that_information.txt
q01.t01.s02.plan.execute_command.git_clone_git_gecgithub.txt
q01.t01.s03.plan.read_file.agent_repl_py.txt
q01.t01.s04.exec.execute_command.git_clone_git_gecgithub.txt
q01.t01.s05.exec.read_file.agent_repl_py.txt
q01.t02.s01.text.Based_on_the_results_I_found.txt
q02.t01.s01.thinking.Let_me_analyze_the_code.txt
q02.t01.s02.text.Here_is_what_I_found.txt
```

**Label rules:**
- Only `0-9`, `a-z`, `A-Z` allowed
- Other characters → `_`
- Multiple underscores collapsed
- Truncated to 50 chars
- Auto-generated:
  - text/thinking: from content
  - plan/exec: `{tool_name}.{args_preview}`

**Note:** Tool executions (`exec`) are logged under the same turn that requested them.

Files sort naturally with `ls` — chronological order within and across queries/turns.

### File Format

Use MIME multipart-style with a unique boundary per file:

```
Boundary: ----=_Part_7f3a9c2b1d4e8f0a6b2c9d5e3f1a8b7c
Timestamp: 2026-04-03T07:06:46.230Z
Query: 1
Turn: 1
Tool: read_file
Call-ID: call_abc123

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
2. **Header block** — Metadata as `Key: Value` lines (Timestamp, Query, Turn, Tool, Call-ID)
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

### Console Output Labels

The console uses the same hierarchy in usage labels:

```
📊 [Usage Query 1 Turn 1] | $0.05 | 1,234 in → 567 out | 2 tools
📊 [Usage Query 1] | $0.12 | ...    (query-level total)
📊 [Session 3 queries, 8 reqs] | $0.45 | ...    (session-level total)
```

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
├── {session_id}/                                      # One folder per session
│   ├── q01.t01.s01.thinking.txt                       # Query 1, Turn 1: LLM thinking
│   ├── q01.t01.s02.text.txt                           # Query 1, Turn 1: LLM text
│   ├── q01.t01.s03.plan.txt                           # Query 1, Turn 1: LLM plans tools
│   ├── q01.t01.s04.exec.txt                           # Query 1, Turn 1: tool results
│   ├── q01.t02.s01.plan.txt                           # Query 1, Turn 2: LLM plans tools
│   ├── q01.t02.s02.exec.txt                           # Query 1, Turn 2: tool results
│   ├── q01.t02.s03.exec.txt                           # Query 1, Turn 2: more tool results
│   ├── q01.t02.s04.text.txt                           # Query 1, Turn 2: LLM text
│   ├── q02.t01.s01.text.txt                           # Query 2, Turn 1: LLM text
│   └── session.json                                   # Conversation history + usage
├── {session_id}/
│   └── ...
└── atom.log                                           # Rotating diagnostic log (Python logging)
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
