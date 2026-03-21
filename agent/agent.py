import argparse
import asyncio
import os
import subprocess
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)

from agent.model import build_model, build_openai_model


# Security: Only allow file operations within /tmp
SAFE_ROOT = Path("/tmp/atom").resolve()

def get_system_prompt() -> str:
        """Get Pulse agent's system prompt with TEX team knowledge."""
        return '''you are a good agent'''

def _validate_path(path: str) -> Path:
    """Resolve path and ensure it's within /tmp. Raises ValueError if not."""
    resolved = Path(path).resolve()
    if not (resolved == SAFE_ROOT or str(resolved).startswith(str(SAFE_ROOT) + os.sep)):
        raise ValueError(f"Access denied: path must be within /tmp. Got: {resolved}")
    return resolved

# ---------------------------------------------------------------------------
# Tools — plain functions, registered via Agent(tools=[...]) at build time
# ---------------------------------------------------------------------------

def execute_script(command: str) -> str:
    """Execute a shell command and return stdout/stderr."""
    print(f">>> execute_script(\"{' '.join(command.split())[:120]}\")")
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = f"Exit Code: {result.returncode}\n"
    output += f"STDOUT:\n{result.stdout}\n"
    if result.stderr:
        output += f"STDERR:\n{result.stderr}\n"
    return output


def read_file(path: str) -> str:
    """Read contents of a file. Path must be within /tmp."""
    print(f'>>> read_file("{path}")')
    try:
        resolved = _validate_path(path)
        if not resolved.exists():
            return f"Error: File not found: {resolved}"
        if not resolved.is_file():
            return f"Error: Not a file: {resolved}"
        return resolved.read_text()
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(path: str, content: str) -> str:
    """Write content to a file. Path must be within /tmp."""
    print(f'>>> write_file("{path}", <{len(content)} chars>)')
    try:
        resolved = _validate_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
        return f"Successfully wrote {len(content)} characters to {resolved}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error writing file: {e}"


def list_dir(path: str = "/tmp") -> str:
    """List files and directories at a path. Path must be within /tmp."""
    print(f'>>> list_dir("{path}")')
    try:
        resolved = _validate_path(path)
        if not resolved.exists():
            return f"Error: Path not found: {resolved}"
        if not resolved.is_dir():
            return f"Error: Not a directory: {resolved}"

        entries = []
        for entry in sorted(resolved.iterdir()):
            entry_type = "[DIR]" if entry.is_dir() else "[FILE]"
            size = entry.stat().st_size if entry.is_file() else "-"
            entries.append(f"{entry_type} {entry.name} ({size} bytes)")

        if not entries:
            return f"Directory {resolved} is empty."
        return f"Contents of {resolved}:\n" + "\n".join(entries)
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error listing directory: {e}"


# Why not @agent.tool_plain decorators?
# ---------------------------------------------------------------------------
# @agent.tool_plain binds tools to a specific agent instance at *import time*
# (i.e. when the decorator line executes, before __main__ runs).
#
# Our agent instance is created inside build_agent(), which is called in
# __main__ *after* CLI args are parsed — so the instance doesn't exist yet
# when the module is being loaded. Using decorators here would require a
# module-level `agent = build_agent()` call, but at that point we haven't
# parsed --openai yet, creating a chicken-and-egg problem.
#
# Passing _TOOLS explicitly to Agent(tools=_TOOLS) in build_agent() keeps
# tools as plain functions and wires them to whichever instance is actually
# constructed at runtime — clean, explicit, no import-order surprises.
# ---------------------------------------------------------------------------
_TOOLS = [execute_script, read_file, write_file, list_dir]


def build_agent(use_openai: bool = False) -> Agent:
    """Build the agent with the appropriate model and all tools."""
    if use_openai:
        return Agent(
            model=build_openai_model(),
            model_settings={"max_tokens": 127000},
            system_prompt=get_system_prompt(),
            tools=_TOOLS,
        )
    return Agent(
        model=build_model(),
        model_settings={
            "max_tokens": 127000,
            "anthropic_cache_instructions": True,
            "anthropic_cache_tool_definitions": True,
            "anthropic_cache_messages": True,
        },
        system_prompt=get_system_prompt(),
        tools=_TOOLS,
    )


from pydantic_ai._agent_graph import CallToolsNode, End, ModelRequestNode, UserPromptNode


def print_request_parts(request: ModelRequest, verbose: bool) -> None:
    """Print relevant parts from a model request."""
    for part in request.parts:
        if isinstance(part, UserPromptPart):
            if verbose:
                print(f"  📤 [UserPrompt] {part.content}")
            else:
                print(f"\n💬 Request: {part.content}")


def print_response_parts(response: ModelResponse, verbose: bool) -> None:
    """Print relevant parts from a model response (thinking, text, tool calls)."""
    for part in response.parts:
        if isinstance(part, ThinkingPart):
            if verbose:
                print(f"  🧠 [Thinking] {part.content}")
            else:
                print(f"\n🧠 Thinking:\n{part.content}")
        elif isinstance(part, TextPart):
            if verbose:
                print(f"  💬 [Text] {part.content}")
            else:
                print(f"\n💬 Response:\n{part.content}")
        elif isinstance(part, ToolCallPart):
            args_str = str(part.args)[:100] + "..." if len(str(part.args)) > 100 else str(part.args)
            if verbose:
                print(f"  🔧 [ToolCall] {part.tool_name}({args_str})")
            else:
                print(f"\n🔧 Tool Call: {part.tool_name}({args_str})")


async def main(agent: Agent, verbose: bool = False) -> None:
    print("🤖 Mini Agent Started. Type 'exit' to quit. Press Ctrl+C to cancel input.")
    if verbose:
        print("   (verbose mode enabled)")
    
    message_history = []
    while True:
        try:
            prompt = input("\n👤 You: ")
        except KeyboardInterrupt:
            # Ctrl+C pressed - discard current input and start fresh
            print("  (cancelled)")
            continue
        
        if prompt.strip().lower() in ("exit", "quit"):
            break
        if not prompt.strip():
            continue

        print("⏳ Agent is thinking...")
        async with agent.iter(prompt, message_history=message_history) as agent_run:
            async for node in agent_run:
                if verbose:
                    # Verbose mode: show all node types with full details
                    match node:
                        case UserPromptNode():
                            print("VERBOSE> 📤 [UserPromptNode]")
                        case ModelRequestNode(request=req):
                            print(f"VERBOSE> 📤 [ModelRequestNode]")
                            print_request_parts(req, verbose=True)
                        case CallToolsNode(model_response=resp):
                            print(f"VERBOSE> 📥 [CallToolsNode]")
                            print_response_parts(resp, verbose=True)
                        case End(data=data):
                            print(f"VERBOSE> ✅ [End] {str(data)[:200]}")
                        case _:
                            print(f"VERBOSE> 🔄 [{type(node).__name__}]")
                else:
                    # Non-verbose: just show thinking, response text, and tool calls
                    match node:
                        case CallToolsNode(model_response=resp):
                            print_response_parts(resp, verbose=False)
                        case End():
                            pass  # Final output printed below
                        case _:
                            pass  # Skip other nodes in non-verbose mode

        result = agent_run.result
        message_history.extend(result.new_messages())
        print(f"\n Agent: {result.output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mini Agent with file tools (sandboxed to /tmp)")
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose mode to show all node details",
    )
    parser.add_argument(
        "--openai",
        action="store_true",
        help="Use OpenAI directly via PERSONAL_OPENAI_API_KEY instead of the LLM Gateway",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    agent = build_agent(use_openai=args.openai)
    asyncio.run(main(agent, verbose=args.verbose))
