### Children's Storybook Generation

import re
import json
import base64
import asyncio
import urllib.request
from typing import List

from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import ToolContext
from google.genai import types
from openai import OpenAI

# Text (story) LLM: OpenAI GPT-4o via LiteLLM. Requires OPENAI_API_KEY in .env
TEXT_MODEL = LiteLlm(model="openai/gpt-4o")

# Image (illustration) model your API key can access.
#   - "gpt-image-1"  : always returns base64; quality is "low"/"medium"/"high"/"auto"
#   - "dall-e-3"     : returns a URL by default; quality is "standard"/"hd"
# The tool handles BOTH response shapes. If you switch to dall-e-3, also change
# IMAGE_QUALITY below (e.g. "standard"), since the quality values differ.
IMAGE_MODEL = "gpt-image-1"
IMAGE_QUALITY = "low"     # fast + cheap; raise to "medium"/"high" for nicer art
IMAGE_SIZE = "1024x1024"


# ---------------------------------------------------------------------------
# 1. Story structure definition (used to validate the JSON the writer returns)
# ---------------------------------------------------------------------------
class StoryPage(BaseModel):
    page_number: int = Field(description="Page number, from 1 to 5")
    text: str = Field(description="Body text for this page, written in KOREAN, 1-3 short kid-friendly sentences")
    visual: str = Field(description="Scene description for the illustrator, in ENGLISH (character, background, colors, mood)")


class StoryBook(BaseModel):
    title: str = Field(description="Story title, written in KOREAN")
    pages: List[StoryPage] = Field(description="Exactly 5 pages")


# ---------------------------------------------------------------------------
# 2. Story Writer Agent
#    Returns JSON as text; output_key saves that text to state["story_data"].
# ---------------------------------------------------------------------------
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
    output_key="story_data",   # <-- Saves the returned JSON text to state["story_data"]
)


# ---------------------------------------------------------------------------
# Helper: parse the story JSON stored in state (handles code fences / whitespace)
# ---------------------------------------------------------------------------
def _parse_story(raw) -> dict:
    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw).strip()
        # Extract the first {...} block in case the model added extra text or ``` fences.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)
        data = json.loads(text)
    # Validate against the schema (raises if the shape is wrong).
    return StoryBook.model_validate(data).model_dump()


# ---------------------------------------------------------------------------
# Helper: extract PNG bytes from an OpenAI image response (base64 OR url).
# ---------------------------------------------------------------------------
def _image_bytes_from_response(response) -> bytes | None:
    data0 = response.data[0]
    b64 = getattr(data0, "b64_json", None)
    if b64:
        return base64.b64decode(b64)
    url = getattr(data0, "url", None)
    if url:
        with urllib.request.urlopen(url) as resp:
            return resp.read()
    return None


# ---------------------------------------------------------------------------
# Helper: generate a single page's image (runs the blocking OpenAI call in a
# worker thread so pages can be generated concurrently).
# ---------------------------------------------------------------------------
async def _generate_page_image(client: OpenAI, page: dict) -> tuple:
    page_no = page["page_number"]
    prompt = (
        "Children's picture book illustration, soft watercolor style, "
        "warm and friendly, bright cheerful colors, no text in the image. "
        f"Scene: {page['visual']}"
    )
    response = await asyncio.to_thread(
        client.images.generate,
        model=IMAGE_MODEL,
        prompt=prompt,
        size=IMAGE_SIZE,
        quality=IMAGE_QUALITY,
        n=1,
    )
    return page, _image_bytes_from_response(response)


# ---------------------------------------------------------------------------
# 3. Illustration tool
#    Reads story_data directly from Session State, generates all page images
#    concurrently, and saves them as ADK Artifacts.
#    -> This is the clearest demonstration of the two agents sharing state.
# ---------------------------------------------------------------------------
async def illustrate_story(tool_context: ToolContext) -> dict:
    """Read the story stored in state['story_data'] and generate an illustration
    for each page. Each image is saved as an Artifact named page_<number>.png.
    """
    # (Key step) Read the data the previous story_writer agent stored in state.
    raw = tool_context.state.get("story_data")
    if not raw:
        return {"status": "error", "message": "No 'story_data' found in state. Write the story first."}

    try:
        story = _parse_story(raw)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"Failed to parse story_data: {exc}"}

    client = OpenAI(timeout=180)   # Uses OPENAI_API_KEY from .env

    # Generate all page images concurrently to keep the tool fast.
    outcomes = await asyncio.gather(
        *[_generate_page_image(client, page) for page in story["pages"]],
        return_exceptions=True,
    )

    results = []
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            results.append({"status": "error", "message": str(outcome)})
            continue
        page, image_bytes = outcome
        page_no = page["page_number"]
        page_info = {"page": page_no, "text": page["text"], "visual": page["visual"]}
        if not image_bytes:
            results.append({**page_info, "status": "no_image", "artifact": None})
            continue
        # Save as an ADK Artifact -> previewable/downloadable in the adk web UI.
        filename = f"page_{page_no}.png"
        await tool_context.save_artifact(
            filename,
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
        )
        results.append({**page_info, "status": "ok", "artifact": filename})

    ok_count = sum(1 for r in results if r.get("status") == "ok")
    return {
        "status": "success" if ok_count else "error",
        "title": story.get("title"),
        "page_count": len(story["pages"]),
        "generated_count": ok_count,
        "generated": results,
    }


# ---------------------------------------------------------------------------
# 4. Illustrator Agent
#    Reads state via the {story_data?} instruction template to show the model,
#    and calls the illustrate_story tool to actually generate the images.
# ---------------------------------------------------------------------------
illustrator = LlmAgent(
    name="illustrator",
    model=TEXT_MODEL,
    description="Illustrator agent that reads the story data from Session State and generates an image for each page.",
    instruction="""You are a children's book illustrator.
The story created by the previous writer agent is stored in Session State under the key 'story_data'.

[Current story data for reference]
{story_data?}

[Your task]
1. You MUST call the illustrate_story tool exactly once to generate illustrations for all 5 pages.
2. After the tool returns, present the finished storybook to the user IN KOREAN, using the story
   data and the tool result. Print the title once, then one block per page in EXACTLY this format:

   📖 <title>

   Page 1
   Text: <page 1 text>
   Visual: <page 1 visual>
   Image: page_1.png

   Page 2
   Text: <page 2 text>
   Visual: <page 2 visual>
   Image: page_2.png

   ... (through Page 5)

3. Finally, add one short Korean line telling the user the images are saved in the
   Artifacts tab as page_1.png ~ page_5.png.
Do not omit any page. Keep the `Text` and `Visual` values exactly as they appear in the story data.
""",
    tools=[illustrate_story],
    output_key="illustration_result",
)


# ---------------------------------------------------------------------------
# 5. Root Agent (Pipeline)
#    Runs story_writer -> illustrator in order. Both share the same Session State.
#    adk web recognizes this 'root_agent' as the entry point.
# ---------------------------------------------------------------------------
root_agent = SequentialAgent(
    name="storybook_pipeline",
    description="Pipeline that builds a children's storybook: theme -> story_writer -> illustrator.",
    sub_agents=[story_writer, illustrator],
)