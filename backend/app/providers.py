import httpx
from dataclasses import dataclass

from .config import settings


@dataclass
class Response:
    text: str
    provider: str
    model: str
    usage: dict | None = None


# Entries are limited to model IDs verified against provider documentation.
# Pricing and account availability are provider-specific and are never inferred.
CATALOG = [
    {"id": "meta/llama-3.1-8b-instruct", "provider": "nvidia", "name": "Llama 3.1 8B Instruct", "category": "general", "modalities": ["text"], "languages": "provider-dependent", "status": "verify availability", "selectable": True},
    {"id": "openai/gpt-oss-20b", "provider": "groq", "name": "GPT OSS 20B", "category": "reasoning", "modalities": ["text"], "languages": "provider-dependent", "status": "provider catalog", "selectable": True},
    {"id": "openai/gpt-oss-120b", "provider": "groq", "name": "GPT OSS 120B", "category": "reasoning", "modalities": ["text"], "languages": "provider-dependent", "status": "provider catalog", "selectable": True},
    {"id": "llama-3.3-70b-versatile", "provider": "groq", "name": "Llama 3.3 70B Versatile", "category": "general", "modalities": ["text"], "languages": "provider-dependent", "status": "provider catalog", "selectable": True},
    {"id": "llama-3.1-8b-instant", "provider": "groq", "name": "Llama 3.1 8B Instant", "category": "fast/general", "modalities": ["text"], "languages": "provider-dependent", "status": "provider catalog", "selectable": True},
]


class ProviderError(RuntimeError):
    pass


def models():
    available = [item for item in CATALOG if item["selectable"]]
    if settings.openrouter_model:
        available.append({"id": settings.openrouter_model, "provider": "openrouter",
                          "name": settings.openrouter_model, "category": "configured model",
                          "modalities": ["text"], "languages": "provider-dependent",
                          "status": "configured; availability checked at request time", "selectable": True})
    return available


def validate(provider, model):
    if not any(item["provider"] == provider and item["id"] == model and item["selectable"] for item in models()):
        raise ProviderError("Model is not in the approved catalog")


async def chat(provider, model, messages, **kwargs):
    validate(provider, model)
    key = {"nvidia": settings.nvidia_api_key, "groq": settings.groq_api_key,
           "openrouter": settings.openrouter_api_key}.get(provider, "")
    if not key:
        raise ProviderError(f"{provider} API key is not configured")
    base = {"nvidia": settings.nvidia_base_url, "groq": settings.groq_base_url,
            "openrouter": settings.openrouter_base_url}[provider]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(f"{base}/chat/completions", headers=headers,
                                     json={"model": model, "messages": messages, **kwargs})
    response.raise_for_status()
    data = response.json()
    return Response(data["choices"][0]["message"].get("content", ""), provider, model, data.get("usage"))
