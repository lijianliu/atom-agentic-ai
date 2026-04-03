import argparse
import asyncio
import readline  # noqa: F401 — enables arrow keys & history in input()
import subprocess
from pathlib import Path
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded, ModelRetry, UnexpectedModelBehavior
from model import build_model

DEFAULT_SYSTEM_PROMPT = "You are a helpful bot."
DEFAULT_MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2


def load_system_prompt(file_path: str | None) -> str:
    """Load system prompt from file or return default."""
    if file_path is None:
        return DEFAULT_SYSTEM_PROMPT
    
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"System prompt file not found: {file_path}")
    
    return path.read_text().strip()


def create_agent(system_prompt: str) -> Agent:
    """Create and return an Agent with the given system prompt."""
    return Agent(
        model=build_model(),
        system_prompt=system_prompt,
        model_settings={"max_tokens": 127_000}
    )

def register_tools(agent: Agent) -> None:
    """Register tools on the agent."""
    @agent.tool_plain
    def execute_script(command: str) -> str:
        print(f"execute_script(\"{command}\")")
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300
        )
        output = f"Exit Code: {result.returncode}\n" + "STDOUT:\n{result.stdout}\n"
        if result.stderr:
            output += f"STDERR:\n{result.stderr}\n"
        return output

def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Mini Agent - A simple conversational AI agent"
    )
    parser.add_argument(
        "-s", "--system-prompt-file",
        type=str,
        default=None,
        help="Path to a file containing the system prompt. If not provided, uses default prompt."
    )
    parser.add_argument(
        "-r", "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Maximum number of retries on error (default: {DEFAULT_MAX_RETRIES})"
    )
    return parser.parse_args()


async def run_with_retry(
    agent: Agent,
    prompt: str,
    message_history: list,
    max_retries: int = DEFAULT_MAX_RETRIES,
):
    """Run agent with retry logic for transient errors."""
    last_error = None
    
    for attempt in range(1, max_retries + 1):
        try:
            result = await agent.run(prompt, message_history=message_history)
            return result
        except UsageLimitExceeded as e:
            last_error = e
            print(f"\n⚠️  Usage limit exceeded (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                print(f"   Retrying in {RETRY_DELAY_SECONDS}s...")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        except ModelRetry as e:
            last_error = e
            print(f"\n⚠️  Model requested retry (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                print(f"   Retrying in {RETRY_DELAY_SECONDS}s...")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        except UnexpectedModelBehavior as e:
            last_error = e
            print(f"\n⚠️  Unexpected model behavior (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                print(f"   Retrying in {RETRY_DELAY_SECONDS}s...")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        except Exception as e:
            # Catch-all for other errors (network issues, API errors, etc.)
            last_error = e
            error_type = type(e).__name__
            print(f"\n⚠️  {error_type} (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                print(f"   Retrying in {RETRY_DELAY_SECONDS}s...")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
    
    # All retries exhausted
    print(f"\n❌ All {max_retries} attempts failed. Last error: {last_error}")
    return None


async def main():
    args = parse_args()
    
    # Load system prompt from file or use default
    try:
        system_prompt = load_system_prompt(args.system_prompt_file)
    except FileNotFoundError as e:
        print(f"❌ Error: {e}")
        return
    
    # Create agent with the system prompt
    agent = create_agent(system_prompt)
    register_tools(agent)
    
    if args.system_prompt_file:
        print(f"📄 Loaded system prompt from: {args.system_prompt_file}")
    
    print("🤖 Mini Agent Started. Type 'exit' to quit.")
    print(f"🔄 Max retries on error: {args.max_retries}")
    message_history = []
    while True:
        prompt = input("\n👤 You: ")
        if prompt.strip().lower() in ('exit', 'quit'):
            break

        if not prompt.strip():
            continue

        print("⏳ Agent is thinking...")
        result = await run_with_retry(
            agent,
            prompt,
            message_history,
            max_retries=args.max_retries,
        )
        
        if result is not None:
            message_history.extend(result.new_messages())
            print(f"\n⚛️ Agent: {result.output}")
        else:
            print("\n💡 Tip: Try rephrasing your request or type 'exit' to quit.")


if __name__ == "__main__":
    asyncio.run(main())