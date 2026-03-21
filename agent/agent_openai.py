import os
import asyncio
import subprocess
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

provider = OpenAIProvider(api_key=os.environ.get("PERSONAL_OPENAI_API_KEY"))
#model = OpenAIResponsesModel("gpt-5.4-pro", provider=provider)
model = OpenAIResponsesModel("gpt-4.1", provider=provider)

agent = Agent(
    model=model,
    model_settings={"max_tokens": 127000},
    system_prompt="You are an agent who can read, write and execute files.",
)


@agent.tool_plain
def execute_script(command: str) -> str:
    print(f">>> execute_script(\"{command}\")")
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=60
    )
    output = f"Exit Code: {result.returncode}\n"
    output += f"STDOUT:\n{result.stdout}\n"
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

        print("\n Agent: ", end="", flush=True)

        async with agent.run_stream(prompt, message_history=message_history) as result:
            async for chunk in result.stream_text(delta=True):
                print(chunk, end="", flush=True)

            print()  # newline after streaming completes
            message_history.extend(result.new_messages())


if __name__ == "__main__":
    asyncio.run(main())
