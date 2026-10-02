import logging
import httpx
from dataclasses import dataclass

from .config import settings
from .reliability.hosted import hosted_call

logger = logging.getLogger("mragrag")


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


def resolve_selection(provider=None, model=None):
    """Return a catalog-valid ``(provider, model)``.

    An explicit but invalid choice raises ``ProviderError`` so callers can reject it.
    Missing parts fall back to the configured defaults; if those defaults are not in the
    catalog (e.g. a stale DEFAULT_MODEL in .env) the first catalog entry is used instead
    of failing every request that omits a model.
    """
    available = models()
    if provider and model:
        validate(provider, model)
        return provider, model
    if provider:
        for item in available:
            if item["provider"] == provider:
                return provider, item["id"]
        raise ProviderError(f"Unknown provider: {provider}")
    if model:
        for item in available:
            if item["id"] == model:
                return item["provider"], model
        raise ProviderError("Model is not in the approved catalog")
    for item in available:
        if item["provider"] == settings.default_provider and item["id"] == settings.default_model:
            return item["provider"], item["id"]
    logger.warning("Configured default %s/%s is not in the model catalog; using %s/%s",
                   settings.default_provider, settings.default_model, available[0]["provider"], available[0]["id"])
    return available[0]["provider"], available[0]["id"]


async def chat(provider, model, messages, **kwargs):
    validate(provider, model)
    key = {"nvidia": settings.nvidia_api_key, "groq": settings.groq_api_key,
           "openrouter": settings.openrouter_api_key}.get(provider, "")
    if not key:
        raise ProviderError(f"{provider} API key is not configured")
    base = {"nvidia": settings.nvidia_base_url, "groq": settings.groq_base_url,
            "openrouter": settings.openrouter_base_url}[provider]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def call():
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{base}/chat/completions", headers=headers,
                                         json={"model": model, "messages": messages, **kwargs})
        response.raise_for_status()
        return response.json()

    # Quota governor + circuit breaker + bounded retries (QuotaExhausted tells the caller when to retry).
    data = await hosted_call(f"{provider}_chat", call)
    return Response(data["choices"][0]["message"].get("content", ""), provider, model, data.get("usage"))
