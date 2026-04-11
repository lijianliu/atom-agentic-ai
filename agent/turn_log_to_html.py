"""Convert turn logs (MIME multipart format) to a readable HTML page.

Turn logs are session/query/turn/sequence conversation logs created by
TurnLogger. Each file is named:
    q{QQ}.t{TT}.s{SS}.{type}.{label}.txt

Where:
    QQ = query number (2 digits)
    TT = turn number (2 digits)
    SS = sequence number (2 digits)
    type = thinking | text | plan | exec
    label = 50-char description

For detailed documentation about this tool, features, and usage examples,
see: docs/turn-log-html.md

Usage:
    python agent/turn_log_to_html.py /path/to/session-dir/
    python agent/turn_log_to_html.py /path/to/session-dir/ -o my_report.html
    uv run agent/turn_log_to_html.py /path/to/session-dir/ --open
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any


def parse_filename(filename: str) -> dict[str, Any] | None:
    """Parse turn log filename into components.
    
    Returns:
        dict with keys: query, turn, sequence, type, label
        or None if filename doesn't match pattern.
    """
    # q01.t02.s03.exec.tool_name_args.txt
    pattern = r"^q(\d+)\.t(\d+)\.s(\d+)\.(thinking|text|plan|exec)\.(.+)\.txt$"
    match = re.match(pattern, filename)
    if not match:
        return None
    
    return {
        "query": int(match.group(1)),
        "turn": int(match.group(2)),
        "sequence": int(match.group(3)),
        "type": match.group(4),
        "label": match.group(5).replace("_", " "),
    }


def parse_mime_content(content: str) -> dict[str, Any]:
    """Parse MIME multipart content from a turn log file.
    
    Returns:
        dict with keys: headers (dict), parts (list of (content_type, body) tuples)
    """
    lines = content.split("\n")
    
    # Extract headers (everything before the first blank line)
    headers: dict[str, str] = {}
    i = 0
    while i < len(lines) and lines[i].strip():
        line = lines[i].strip()
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip()] = value.strip()
        i += 1
    
    # Extract boundary
    boundary = headers.get("Boundary", "")
    
    # Parse parts
    parts: list[tuple[str, str]] = []
    current_type = ""
    current_body_lines: list[str] = []
    
    for line in lines[i:]:
        if line.strip() == boundary or line.strip() == f"{boundary}--":
            # Save previous part
            if current_type and current_body_lines:
                body = "\n".join(current_body_lines).strip()
                parts.append((current_type, body))
            current_type = ""
            current_body_lines = []
        elif line.startswith("Content-Type:"):
            current_type = line.split(":", 1)[1].strip()
        elif current_type:  # collecting body
            current_body_lines.append(line)
    
    return {"headers": headers, "parts": parts}


def load_session_logs(session_dir: Path) -> list[dict[str, Any]]:
    """Load all turn log files from a session directory.
    
    Returns:
        List of entries, each containing:
        - file_info: parsed filename (query, turn, sequence, type, label)
        - headers: MIME headers
        - parts: list of (content_type, body) tuples
    """
    entries = []
    
    for file_path in session_dir.glob("*.txt"):
        file_info = parse_filename(file_path.name)
        if not file_info:
            continue
        
        content = file_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_mime_content(content)
        
        entries.append({
            "file_info": file_info,
            "headers": parsed["headers"],
            "parts": parsed["parts"],
        })
    
    # Sort by query, turn, sequence
    entries.sort(
        key=lambda e: (
            e["file_info"]["query"],
            e["file_info"]["turn"],
            e["file_info"]["sequence"],
        )
    )
    
    return entries


def _try_prettify_json(text: str) -> str | None:
    """Try to parse and prettify JSON from text."""
    # Try direct JSON parse
    try:
        obj = json.loads(text)
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except (ValueError, json.JSONDecodeError):
        pass
    
    # Try extracting JSON from tool args (dict-like strings)
    if text.strip().startswith("{"):
        try:
            # Replace single quotes with double quotes for JSON
            json_text = text.replace("'", '"')
            obj = json.loads(json_text)
            return json.dumps(obj, indent=2, ensure_ascii=False)
        except (ValueError, json.JSONDecodeError):
            pass
    
    return None


_PRETTIFY_JS = r"""
const _rawCache = {};

function togglePretty(id) {
  const el = document.getElementById(id);
  const btn = document.getElementById('btn-' + id);
  if (!_rawCache[id]) _rawCache[id] = el.textContent;

  if (btn.dataset.pretty === '1') {
    el.textContent = _rawCache[id];
    btn.textContent = 'pretty';
    btn.dataset.pretty = '0';
    return;
  }

  const jsonStr = el.dataset.json;
  if (jsonStr) {
    el.textContent = jsonStr;
  }
  btn.textContent = 'plain';
  btn.dataset.pretty = '1';
}

function toggleType(type) {
  const btn = document.getElementById('toggle-' + type);
  const isHidden = btn.getAttribute('data-hidden') === '1';
  const rows = document.querySelectorAll('.row-' + type.toLowerCase());
  
  if (isHidden) {
    rows.forEach(row => row.style.display = '');
    btn.setAttribute('data-hidden', '0');
    btn.classList.remove('opacity-30');
  } else {
    rows.forEach(row => row.style.display = 'none');
    btn.setAttribute('data-hidden', '1');
    btn.classList.add('opacity-30');
  }
  filterByText();
}

function filterByText() {
  const searchInput = document.getElementById('search-input');
  const searchText = searchInput.value.toLowerCase();
  const allRows = document.querySelectorAll('tbody tr');
  const statusEl = document.getElementById('match-status');
  
  // Clear all existing highlights
  document.querySelectorAll('mark.search-highlight').forEach(mark => {
    const parent = mark.parentNode;
    parent.replaceChild(document.createTextNode(mark.textContent), mark);
    parent.normalize();
  });
  
  let visibleRows = 0;
  let totalMatches = 0;
  
  allRows.forEach(row => {
    const typeClass = Array.from(row.classList).find(c => c.startsWith('row-'));
    const type = typeClass ? typeClass.replace('row-', '').toUpperCase() : '';
    const typeBtn = document.getElementById('toggle-' + type);
    const typeHidden = typeBtn && typeBtn.getAttribute('data-hidden') === '1';
    
    if (typeHidden) {
      return;
    }
    
    if (!searchText) {
      row.style.display = '';
      visibleRows++;
      return;
    }
    
    // Search in row text content
    const rowText = row.textContent.toLowerCase();
    if (rowText.includes(searchText)) {
      row.style.display = '';
      visibleRows++;
      // Count matches in this row
      const matches = rowText.split(searchText).length - 1;
      totalMatches += matches;
    } else {
      row.style.display = 'none';
    }
  });
  
  // Highlight matches (limit to first 100)
  if (searchText && totalMatches > 0) {
    let highlightCount = 0;
    const maxHighlights = 100;
    
    for (const row of allRows) {
      if (row.style.display === 'none') continue;
      if (highlightCount >= maxHighlights) break;
      
      highlightCount = highlightText(row, searchText, maxHighlights - highlightCount);
    }
    
    if (totalMatches > maxHighlights) {
      statusEl.innerHTML = `<span class="text-orange-600 font-semibold">⚠️ ${totalMatches} matches found</span><br><span class="text-xs text-gray-600">Highlighting first ${maxHighlights} only</span>`;
      statusEl.style.display = 'block';
    } else {
      statusEl.innerHTML = `<span class="text-green-600 font-semibold">✓ ${totalMatches} match${totalMatches !== 1 ? 'es' : ''} highlighted</span>`;
      statusEl.style.display = 'block';
    }
  } else if (searchText && totalMatches === 0) {
    statusEl.innerHTML = '<span class="text-gray-500 text-xs">No matches found</span>';
    statusEl.style.display = 'block';
  } else {
    statusEl.style.display = 'none';
  }
}

function highlightText(element, searchText, maxCount) {
  const walker = document.createTreeWalker(
    element,
    NodeFilter.SHOW_TEXT,
    null,
    false
  );
  
  const nodesToReplace = [];
  let node;
  
  while (node = walker.nextNode()) {
    if (node.nodeValue.toLowerCase().includes(searchText)) {
      nodesToReplace.push(node);
    }
  }
  
  let highlightCount = 0;
  
  for (const node of nodesToReplace) {
    if (highlightCount >= maxCount) break;
    
    const regex = new RegExp('(' + searchText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
    const parts = node.nodeValue.split(regex);
    const fragment = document.createDocumentFragment();
    
    parts.forEach((part, i) => {
      if (part.toLowerCase() === searchText) {
        if (highlightCount < maxCount) {
          const mark = document.createElement('mark');
          mark.className = 'search-highlight';
          mark.textContent = part;
          fragment.appendChild(mark);
          highlightCount++;
        } else {
          fragment.appendChild(document.createTextNode(part));
        }
      } else if (part) {
        fragment.appendChild(document.createTextNode(part));
      }
    });
    
    node.parentNode.replaceChild(fragment, node);
  }
  
  return highlightCount;
}
"""


def render_html(entries: list[dict[str, Any]], session_name: str) -> str:
    """Render parsed turn log entries into a table-style layout."""
    
    # Build table rows
    rows_html = ""
    line_num = 0
    
    for entry in entries:
        file_info = entry["file_info"]
        headers = entry["headers"]
        parts = entry["parts"]
        
        line_num += 1
        
        entry_type = file_info["type"].upper()
        query = file_info["query"]
        turn = file_info["turn"]
        seq = file_info["sequence"]
        
        # Color coding per type
        type_colors = {
            "THINKING": "bg-purple-100 text-purple-800 border-purple-300",
            "TEXT": "bg-blue-100 text-blue-800 border-blue-300",
            "PLAN": "bg-yellow-100 text-yellow-800 border-yellow-300",
            "EXEC": "bg-green-100 text-green-800 border-green-300",
        }
        type_color = type_colors.get(entry_type, "bg-gray-100 text-gray-800 border-gray-300")
        
        # Tool info if available
        tool_name = headers.get("Tool", "")
        
        # Combine all parts into content sections
        content_parts_html = ""
        
        for part_idx, (content_type, body) in enumerate(parts):
            if not body:
                continue
            
            # Try to prettify JSON
            json_data = _try_prettify_json(body)
            part_id = f"part-{line_num}-{part_idx}"
            
            body_escaped = html.escape(body)
            
            # Toggle button for JSON (but not for PLAN)
            toggle_btn = ""
            if json_data and entry_type != "PLAN":
                json_attr = f' data-json="{html.escape(json_data, quote=True)}"'
                toggle_btn = f"""<button onclick="togglePretty('{part_id}')" class="text-[9px] bg-white text-gray-700 border border-gray-300 px-1 py-0.5 rounded cursor-pointer hover:bg-gray-100 ml-1 font-mono" id="btn-{part_id}" data-pretty="0">pretty</button>"""
            else:
                json_attr = ""
            
            # Build header for this part
            # For TEXT entries, don't show content_type in content
            # For PLAN, show tool name if available
            # For EXEC, show tool name once (on first part) + content_type (input/output)
            header_html = ""
            if entry_type == "TEXT" and content_type == "text":
                # No header, badge is in separate column
                if toggle_btn:
                    header_html = f"""
  <div class="flex items-center gap-1 mb-1">
    {toggle_btn}
  </div>
"""
            elif entry_type == "PLAN":
                # Show tool name for PLAN entries (compact, no prettify button)
                if tool_name:
                    header_html = f"""
  <div class="flex items-center gap-1 mb-1">
    <span class="text-[9px] font-mono bg-gray-200 px-1.5 py-0.5 rounded font-semibold">{tool_name}</span>
  </div>
"""
            elif entry_type == "EXEC":
                # Show tool name on first part, then content_type (input/output)
                labels_html = ""
                if part_idx == 0 and tool_name:
                    labels_html = f'<span class="text-[9px] font-mono bg-gray-200 px-1.5 py-0.5 rounded font-semibold">{tool_name}</span>'
                labels_html += f'<span class="text-[9px] font-mono bg-gray-200 px-1.5 py-0.5 rounded font-semibold">{content_type}</span>'
                header_html = f"""
  <div class="flex items-center gap-1 mb-1">
    {labels_html}
    {toggle_btn}
  </div>
"""
            else:
                # Default: show content type + tool if available
                part_label = f"{content_type}"
                if part_idx == 0 and tool_name:
                    part_label += f" ({tool_name})"
                header_html = f"""
  <div class="flex items-center gap-1 mb-1">
    <span class="text-[9px] font-mono bg-gray-200 px-1.5 py-0.5 rounded font-semibold">{part_label}</span>
    {toggle_btn}
  </div>
"""
            
            content_parts_html += f"""
<div class="mb-1 last:mb-0">
{header_html}  <pre class="text-[11px] whitespace-pre-wrap font-mono bg-white border border-gray-200 rounded px-2 py-0.5 break-words" id="{part_id}"{json_attr}>{body_escaped}</pre>
</div>
"""
        
        rows_html += f"""
<tr class="border-b border-gray-200 hover:bg-gray-50 row-{entry_type.lower()}">
  <td class="pr-1 py-1 text-[9px] text-gray-400 text-right font-mono align-top col-num">{line_num}</td>
  <td class="pl-0 pr-1 py-1 text-[9px] text-gray-400 text-right font-mono align-top col-qts">Q{query}/T{turn}/S{seq}</td>
  <td class="pr-1 py-1 align-top col-type">
    <span class="inline-block px-2 py-0.5 rounded text-[10px] font-bold {type_color} border">{entry_type}</span>
  </td>
  <td class="px-2 py-1 align-top">
{content_parts_html}
  </td>
</tr>
"""
    
    # Build summary stats
    thinking_count = sum(1 for e in entries if e["file_info"]["type"] == "thinking")
    text_count = sum(1 for e in entries if e["file_info"]["type"] == "text")
    plan_count = sum(1 for e in entries if e["file_info"]["type"] == "plan")
    exec_count = sum(1 for e in entries if e["file_info"]["type"] == "exec")
    
    # Count unique queries and turns
    queries = set(e["file_info"]["query"] for e in entries)
    turns = set((e["file_info"]["query"], e["file_info"]["turn"]) for e in entries)
    total_queries = len(queries)
    total_turns = len(turns)
    
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Atom Agentic AI Turn Log: {html.escape(session_name)}</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
body {{ font-size: 12px; }}
pre {{ margin: 0; line-height: 1.4; }}
table {{ border-collapse: collapse; table-layout: fixed; width: 100%; }}
tr:hover {{ background-color: rgba(0, 0, 0, 0.02); }}
td {{ word-wrap: break-word; overflow-wrap: break-word; }}
thead {{ position: sticky; top: 0; z-index: 10; }}
thead tr {{ background-color: #f3f4f6; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
.col-num {{ width: 25px; white-space: nowrap; }}
.col-qts {{ width: 55px; white-space: nowrap; }}
.col-type {{ width: 60px; white-space: nowrap; }}
.filter-btn {{ cursor: pointer; transition: opacity 0.2s; }}
.filter-btn:hover {{ opacity: 0.8; }}
.filter-panel {{ position: fixed; top: 20px; right: 20px; z-index: 100; background: white; border: 2px solid #0053e2; border-radius: 8px; padding: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); min-width: 200px; }}
.filter-panel input {{ width: 100%; padding: 6px 8px; border: 1px solid #d1d5db; border-radius: 4px; font-size: 11px; margin-bottom: 8px; }}
.filter-panel input:focus {{ outline: none; border-color: #0053e2; }}
mark.search-highlight {{ background-color: #ffc220; color: #000; padding: 1px 2px; border-radius: 2px; font-weight: 600; }}
</style>
<script>
{_PRETTIFY_JS}
</script>
</head>
<body class="bg-gray-50 text-gray-800 font-sans p-4">
<div class="max-w-6xl mx-auto">

<!-- Floating Filter Panel -->
<div class="filter-panel">
<div class="text-[10px] font-bold text-gray-700 mb-2">🎯 FILTERS</div>
<input type="text" id="search-input" placeholder="Search text..." oninput="filterByText()" />
<div id="match-status" class="text-[9px] mb-2" style="display: none;"></div>
<div class="flex flex-col gap-1">
  <button id="toggle-THINKING" onclick="toggleType('THINKING')" data-hidden="0" class="filter-btn inline-block px-2 py-1 rounded text-[10px] font-bold bg-purple-100 text-purple-800 border border-purple-300 text-left">💭 THINKING</button>
  <button id="toggle-TEXT" onclick="toggleType('TEXT')" data-hidden="0" class="filter-btn inline-block px-2 py-1 rounded text-[10px] font-bold bg-blue-100 text-blue-800 border border-blue-300 text-left">🤖 TEXT</button>
  <button id="toggle-PLAN" onclick="toggleType('PLAN')" data-hidden="0" class="filter-btn inline-block px-2 py-1 rounded text-[10px] font-bold bg-yellow-100 text-yellow-800 border border-yellow-300 text-left">📋 PLAN</button>
  <button id="toggle-EXEC" onclick="toggleType('EXEC')" data-hidden="0" class="filter-btn inline-block px-2 py-1 rounded text-[10px] font-bold bg-green-100 text-green-800 border border-green-300 text-left">⚡ EXEC</button>
</div>
</div>

<!-- Header -->
<div class="flex items-center gap-3 border-b-2 border-[#ffc220] pb-3 mb-4">
<h1 class="text-lg font-bold text-[#0053e2]">⚛️  Atom Agentic AI Turn Log: {html.escape(session_name)}</h1>
<span class="text-xs text-gray-400 ml-auto">
{line_num} lines · {total_queries} queries · {total_turns} turns · 
💭{thinking_count} 🤖{text_count} 📋{plan_count} ⚡{exec_count}
</span>
</div>

<!-- Table -->
<table class="w-full border-collapse">
<thead>
<tr class="bg-gray-100 border-b-2 border-gray-300">
  <th class="pr-1 py-1 text-right text-[10px] font-bold text-gray-700 col-num">#</th>
  <th class="pl-0 pr-1 py-1 text-center text-[10px] font-bold text-gray-700 col-qts">Q/T/S</th>
  <th class="pr-1 py-1 text-left text-[10px] font-bold text-gray-700 col-type">TYPE</th>
  <th class="px-2 py-1 text-left text-[10px] font-bold text-gray-700">CONTENT</th>
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>

<!-- Footer -->
<div class="mt-6 pt-4 border-t border-gray-300 text-center text-xs text-gray-500">
Generated by <strong>turn_log_to_html.py</strong> • Atom Agentic AI Turn Logger
</div>

</div>
</body>
</html>
"""


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Convert turn logs to HTML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
    python turn_log_to_html.py ~/atom-agentic-ai/logs/session-abc-123/
    python turn_log_to_html.py ~/atom-agentic-ai/logs/session-abc-123/ -o report.html
    python turn_log_to_html.py ~/atom-agentic-ai/logs/session-abc-123/ --open
""",
    )
    parser.add_argument("session_dir", help="Path to session log directory")
    parser.add_argument("-o", "--output", help="Output HTML path (default: session_dir.html)")
    parser.add_argument("--open", action="store_true", help="Open in browser after generation")
    args = parser.parse_args()
    
    session_path = Path(args.session_dir)
    if not session_path.is_dir():
        print(f"Error: {session_path} is not a directory", file=sys.stderr)
        sys.exit(1)
    
    # Load and parse all log files
    entries = load_session_logs(session_path)
    if not entries:
        print(f"Error: No turn log files found in {session_path}", file=sys.stderr)
        sys.exit(1)
    
    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = session_path.parent / f"{session_path.name}.html"
    
    # Render HTML
    html_content = render_html(entries, session_path.name)
    output_path.write_text(html_content, encoding="utf-8")
    
    print(f"✅ Generated {output_path} ({len(entries)} entries)")
    
    # Open in browser if requested
    if args.open:
        import platform
        import subprocess
        
        system = platform.system()
        if system == "Darwin":  # macOS
            subprocess.run(["open", str(output_path)], check=False)
        elif system == "Windows":
            subprocess.run(["start", str(output_path)], shell=True, check=False)
        else:  # Linux
            subprocess.run(["xdg-open", str(output_path)], check=False)
        print(f"🌐 Opened in browser")


if __name__ == "__main__":
    main()
