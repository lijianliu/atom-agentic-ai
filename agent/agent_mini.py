import asyncio
import subprocess
from pydantic_ai import Agent
from model import build_model

agent = Agent(
    model=build_model(),
    system_prompt= "You are a helpful bot.",
    model_settings={"max_tokens": 127_000}
)

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

async def main():
    print("🤖 Mini Agent Started. Type 'exit' to quit.")
    message_history = []
    while True:
        prompt = input("\n👤 You: ")
        if prompt.strip().lower() in ('exit', 'quit'):
            break

        if not prompt.strip():
            continue

        print("⏳ Agent is thinking...")
        result = await agent.run(prompt, message_history=message_history)
        message_history.extend(result.new_messages())
        print(f"\n⚛️ Agent: {result.output}")

if __name__ == "__main__":
    asyncio.run(main())