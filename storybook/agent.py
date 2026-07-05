### Children's Storybook Generation

import re
import json
import base64
import asyncio
import logging
import urllib.request
from typing import List, AsyncGenerator, Optional

from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent, SequentialAgent, ParallelAgent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.callback_context import CallbackContext
from google.adk.events import Event, EventActions
from google.adk.models.lite_llm import LiteLlm
from google.genai import types
from openai import OpenAI

logger = logging.getLogger("storybook")

# Text (story) LLM: OpenAI GPT-4o via LiteLLM. Requires OPENAI_API_KEY in .env
TEXT_MODEL = LiteLlm(model="openai/gpt-4o")

# Image (illustration) model your API key can access.
#   - "gpt-image-1"  : always returns base64; quality is "low"/"medium"/"high"/"auto"
#   - "dall-e-3"     : returns a URL by default; quality is "standard"/"hd"
IMAGE_MODEL = "gpt-image-1"
IMAGE_QUALITY = "low"       # fast + cheap; raise to "medium"/"high" for nicer art
IMAGE_SIZE = "1024x1024"
NUM_PAGES = 5


# ===========================================================================
# Structured schema (used to validate the JSON the writer returns)
# ===========================================================================
class StoryPage(BaseModel):
    page_number: int = Field(description="Page number, from 1 to 5")
    text: str = Field(description="Body text for this page, written in KOREAN, 1-3 short kid-friendly sentences")
    visual: str = Field(description="Scene description for the illustrator, in ENGLISH (character, background, colors, mood)")


class StoryBook(BaseModel):
    title: str = Field(description="Story title, written in KOREAN")
    pages: List[StoryPage] = Field(description="Exactly 5 pages")


def _parse_story(raw) -> dict:
    """Parse the story JSON stored in state (handles code fences / extra text)."""
    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw).strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)
        data = json.loads(text)
    return StoryBook.model_validate(data).model_dump()


def _image_bytes_from_response(response) -> Optional[bytes]:
    """Extract PNG bytes from an OpenAI image response (base64 OR url)."""
    data0 = response.data[0]
    b64 = getattr(data0, "b64_json", None)
    if b64:
        return base64.b64decode(b64)
    url = getattr(data0, "url", None)
    if url:
        with urllib.request.urlopen(url) as resp:
            return resp.read()
    return None


# ===========================================================================
# Callbacks  (progress display requirement)
# ===========================================================================
def before_story_cb(callback_context: CallbackContext) -> Optional[types.Content]:
    print("📝 스토리 작성 중...")
    logger.info("story_writer started")
    return None   # returning None lets the agent run normally


def after_story_cb(callback_context: CallbackContext) -> Optional[types.Content]:
    print("✅ 스토리 작성 완료")
    logger.info("story_writer finished")
    return None


def _make_progress_cb(page_no: int):
    """Build a before_agent_callback that announces a page is being illustrated."""
    def _before(callback_context: CallbackContext) -> Optional[types.Content]:
        print(f"🎨 이미지 {page_no}/{NUM_PAGES} 생성 중...")
        logger.info("illustrating page %d/%d", page_no, NUM_PAGES)
        return None
    return _before


# ===========================================================================
# 1. Story Writer Agent (LlmAgent)
#    Returns JSON as text; output_key saves it to state["story_data"].
# ===========================================================================
story_writer = LlmAgent(
    name="story_writer",
    model=TEXT_MODEL,
    description="Writer agent that turns a theme into a 5-page children's story as structured JSON data.",
    instruction="""You are a warm children's book author.
Using the 'theme' provided by the user, create a story for children aged 4-7.

[Rules]
- The story MUST have exactly 5 pages.
- Write each page's `text` in KOREAN, using 1-3 easy, gentle sentences.
- Across the 5 pages, the story should flow naturally: beginning -> development -> ending.
- Write each page's `visual` in ENGLISH, describing the scene concretely so an
  illustrator can draw it (include the main character's appearance, background,
  colors, and mood; keep the character description consistent across all pages).
- Keep the story safe and positive, with nothing violent or scary.

[Output format]
Respond with ONLY the following JSON, with no extra explanation and no markdown code fences.
{
  "title": "story title in Korean",
  "pages": [
    {"page_number": 1, "text": "...", "visual": "..."},
    {"page_number": 2, "text": "...", "visual": "..."},
    {"page_number": 3, "text": "...", "visual": "..."},
    {"page_number": 4, "text": "...", "visual": "..."},
    {"page_number": 5, "text": "...", "visual": "..."}
  ]
}""",
    output_key="story_data",
    before_agent_callback=before_story_cb,   # progress: "스토리 작성 중..."
    after_agent_callback=after_story_cb,     # progress: "스토리 작성 완료"
)


# ===========================================================================
# 2. Page Illustrator Agent (custom BaseAgent)
#    One instance per page. Reads its page from state["story_data"], generates
#    the image, and saves it as an Artifact. Five of these run inside a
#    ParallelAgent, so all images are generated concurrently.
# ===========================================================================
class PageIllustratorAgent(BaseAgent):
    page_index: int   # 0-based (page_number = page_index + 1)

    def __init__(self, page_index: int):
        super().__init__(
            name=f"page_illustrator_{page_index + 1}",
            description=f"Generates the illustration for page {page_index + 1}.",
            page_index=page_index,
            before_agent_callback=_make_progress_cb(page_index + 1),   # "이미지 N/5 생성 중..."
        )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        page_no = self.page_index + 1
        status_key = f"page_{page_no}_status"

        # Read the shared story written by story_writer.
        try:
            story = _parse_story(ctx.session.state.get("story_data"))
            page = story["pages"][self.page_index]
        except Exception as exc:  # noqa: BLE001
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=f"❌ 이미지 {page_no}/{NUM_PAGES} 실패: {exc}")]),
                actions=EventActions(state_delta={status_key: "error"}),
            )
            return

        prompt = (
            "Children's picture book illustration, soft watercolor style, "
            "warm and friendly, bright cheerful colors, no text in the image. "
            f"Scene: {page['visual']}"
        )

        # Generate the image. Run the blocking OpenAI call in a worker thread so
        # the 5 parallel agents truly overlap instead of blocking the event loop.
        try:
            client = OpenAI(timeout=180)
            response = await asyncio.to_thread(
                client.images.generate,
                model=IMAGE_MODEL,
                prompt=prompt,
                size=IMAGE_SIZE,
                quality=IMAGE_QUALITY,
                n=1,
            )
            image_bytes = _image_bytes_from_response(response)
        except Exception as exc:  # noqa: BLE001
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=f"❌ 이미지 {page_no}/{NUM_PAGES} 실패: {exc}")]),
                actions=EventActions(state_delta={status_key: "error"}),
            )
            return

        if not image_bytes:
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=f"❌ 이미지 {page_no}/{NUM_PAGES}: 빈 응답")]),
                actions=EventActions(state_delta={status_key: "no_image"}),
            )
            return

        # Save as an ADK Artifact so it appears in the adk web "Artifacts" tab.
        filename = f"page_{page_no}.png"
        version = await ctx.artifact_service.save_artifact(
            app_name=ctx.session.app_name,
            user_id=ctx.session.user_id,
            session_id=ctx.session.id,
            filename=filename,
            artifact=types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
        )

        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=f"🖼️ 이미지 {page_no}/{NUM_PAGES} 완료 → {filename}")]),
            actions=EventActions(
                artifact_delta={filename: version},
                state_delta={status_key: "ok"},
            ),
        )


# ===========================================================================
# 3. Book Presenter Agent (custom BaseAgent)
#    Gathers the story text + per-page image results into the final storybook.
# ===========================================================================
class BookPresenterAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        try:
            story = _parse_story(ctx.session.state.get("story_data"))
        except Exception as exc:  # noqa: BLE001
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=f"동화 데이터를 읽지 못했어요: {exc}")]),
            )
            return

        lines = [f"📖 {story['title']}", ""]
        for page in story["pages"]:
            n = page["page_number"]
            status = ctx.session.state.get(f"page_{n}_status", "unknown")
            image = f"page_{n}.png" if status == "ok" else f"(이미지 생성 실패: {status})"
            lines += [
                f"Page {n}",
                f"Text: {page['text']}",
                f"Visual: {page['visual']}",
                f"Image: {image}",
                "",
            ]
        lines.append("모든 삽화는 Artifacts 탭에서 page_1.png ~ page_5.png 로 확인할 수 있어요.")

        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text="\n".join(lines))]),
        )


# ===========================================================================
# 4. Assemble the Workflow Agents
# ===========================================================================
# ParallelAgent: the 5 page illustrators run concurrently ("fan out").
parallel_illustrator = ParallelAgent(
    name="parallel_illustrator",
    description="Generates all 5 page illustrations at the same time.",
    sub_agents=[PageIllustratorAgent(i) for i in range(NUM_PAGES)],
)

book_presenter = BookPresenterAgent(
    name="book_presenter",
    description="Gathers the story text and generated images into the final storybook.",
)

# SequentialAgent: writer -> parallel illustrators -> presenter. This is the entry point.
root_agent = SequentialAgent(
    name="storybook_pipeline",
    description="Theme -> write story -> illustrate 5 pages in parallel -> present the finished book.",
    sub_agents=[story_writer, parallel_illustrator, book_presenter],
)