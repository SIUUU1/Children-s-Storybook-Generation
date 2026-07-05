"""
Demo: run the storybook pipeline for TWO different themes and save the results.
"""

import os
import sys
import asyncio

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from storybook.agent import root_agent

# Load storybook/.env using a path relative to THIS file, so it works no matter
# which directory `uv run demo.py` / `python demo.py` is invoked from.
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storybook", ".env")
loaded = load_dotenv(_ENV_PATH)

if not os.getenv("OPENAI_API_KEY"):
    sys.exit(
        f"OPENAI_API_KEY 를 찾을 수 없습니다.\n"
        f"  - .env 경로: {_ENV_PATH} (로드 성공 여부: {loaded})\n"
        f"  - storybook/.env 파일에 'OPENAI_API_KEY=sk-...' 형식으로 키가 들어있는지 확인하세요."
    )

APP_NAME = "storybook"
USER_ID = "demo_user"

# Requirement: demonstrate at least two different story themes.
THEMES = [
    "보라색 하늘을 좋아하는 아기 토끼",
    "별을 모으러 떠난 작은 고양이",
]


async def run_one_theme(runner: InMemoryRunner, theme: str, out_dir: str) -> None:
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)

    # Drive the pipeline with the theme as the user message.
    story_text = ""
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=theme)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(f"  [{event.author}] {part.text}")
                    # The final formatted storybook comes from the book_presenter agent.
                    if event.author == "book_presenter":
                        story_text = part.text

    os.makedirs(out_dir, exist_ok=True)

    # Save the finished storybook text.
    if story_text:
        with open(os.path.join(out_dir, "story.txt"), "w", encoding="utf-8") as f:
            f.write(story_text)

    # Save all generated artifacts (page_1.png ... page_5.png) to disk.
    keys = await runner.artifact_service.list_artifact_keys(
        app_name=APP_NAME, user_id=USER_ID, session_id=session.id
    )
    for filename in keys:
        part = await runner.artifact_service.load_artifact(
            app_name=APP_NAME, user_id=USER_ID, session_id=session.id, filename=filename
        )
        if part and part.inline_data and part.inline_data.data:
            with open(os.path.join(out_dir, filename), "wb") as f:
                f.write(part.inline_data.data)
    print(f"  [저장됨] {out_dir}/  (story.txt + {len(keys)}개 이미지)")


async def main() -> None:
    runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)
    for i, theme in enumerate(THEMES, start=1):
        print("=" * 70)
        print(f"DEMO {i}/{len(THEMES)} — 테마: {theme}")
        print("=" * 70)
        await run_one_theme(runner, theme, os.path.join("demo_output", f"theme_{i}"))
        print()


if __name__ == "__main__":
    asyncio.run(main())