"""Figure description: turns a chart, graph, diagram or handwritten note into searchable text, once."""
import base64
from typing import Protocol

import httpx

from ..config import settings
from ..reliability.hosted import hosted_call
from .base import FigureDescription, SpecialistUnavailable

NOT_INFORMATIVE = "NOT_INFORMATIVE"

PROMPT = (
    "You are describing one figure from a document so that it can be found by search and used to answer "
    "questions. If the image is only decorative (logo, icon, background, a photo with no information), reply "
    f"with exactly {NOT_INFORMATIVE}. Otherwise write:\n"
    "Type: <bar chart | line chart | pie chart | table | diagram | screenshot | handwriting | other>\n"
    "Title and axis labels or legend, if visible.\n"
    "For a chart or graph, the data as a markdown table with the values you can read; write 'approx.' "
    "for values estimated from bar heights or positions.\n"
    "Transcribe any handwritten or printed text verbatim.\n"
    "Then one to three sentences on what the figure shows. Never invent values or text that is not visible."
)


class VisionProvider(Protocol):
    name: str

    async def describe(self, png: bytes) -> FigureDescription: ...


def _parse(text: str, provider: str, model: str) -> FigureDescription:
    text = (text or "").strip()
    informative = bool(text) and not text.upper().startswith(NOT_INFORMATIVE)
    return FigureDescription(text=text if informative else "", informative=informative, provider=provider, model=model)


class GroqVision:
    """A vision-capable Groq model through the OpenAI-compatible chat endpoint (one image per request)."""
    name = "groq"
    quota_provider = "groq_vision"

    def __init__(self, api_key: str, base_url: str, model: str, client_factory=None):
        self.api_key, self.base_url, self.model = api_key, base_url.rstrip("/"), model
        self.client_factory = client_factory or httpx.AsyncClient

    async def describe(self, png: bytes) -> FigureDescription:
        payload = {
            "model": self.model, "temperature": 0, "max_tokens": 700,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
            ]}],
        }

        async def call() -> dict:
            async with self.client_factory(timeout=120) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                )
            response.raise_for_status()
            return response.json()

        data = await hosted_call(self.quota_provider, call)
        return _parse(data["choices"][0]["message"].get("content", ""), self.name, self.model)


class GeminiVision:
    """Gemini through generateContent. Gemini's free tier uses submitted content to improve Google's
    products, so this provider refuses to run unless ALLOW_FREE_TIER_DATA_USE is set."""
    name = "gemini"
    quota_provider = "gemini_vision"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key: str, model: str, client_factory=None):
        self.api_key, self.model = api_key, model
        self.client_factory = client_factory or httpx.AsyncClient

    async def describe(self, png: bytes) -> FigureDescription:
        if not settings.allow_free_tier_data_use:
            raise SpecialistUnavailable(
                "Gemini's free tier uses submitted content to improve Google's products. "
                "Set ALLOW_FREE_TIER_DATA_USE=true to allow it for these documents."
            )
        payload = {
            "contents": [{"parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(png).decode("ascii")}},
            ]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 700},
        }

        async def call() -> dict:
            async with self.client_factory(timeout=120) as client:
                response = await client.post(
                    f"{self.BASE}/models/{self.model}:generateContent", json=payload,
                    headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                )
            response.raise_for_status()
            return response.json()

        data = await hosted_call(self.quota_provider, call)
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
        return _parse("".join(p.get("text", "") for p in parts), self.name, self.model)


def get_vision_provider() -> VisionProvider | None:
    """Groq when its key is set; Gemini only with a key AND the data-use opt-in. ``VISION_PROVIDER=none`` disables."""
    choice = settings.vision_provider.lower()
    if choice in ("auto", "groq") and settings.groq_api_key:
        return GroqVision(settings.groq_api_key, settings.groq_base_url, settings.vision_model)
    if choice in ("auto", "gemini") and settings.gemini_api_key and settings.allow_free_tier_data_use:
        return GeminiVision(settings.gemini_api_key, settings.gemini_vision_model)
    return None
