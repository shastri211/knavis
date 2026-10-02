from dataclasses import dataclass
from .providers import chat as raw_chat, models as catalog_models

@dataclass
class ProviderResponse:
    text: str
    provider: str
    model: str
    usage: dict | None = None

class ProviderService:
    async def chat(self, provider, model, messages, **kwargs):
        r = await raw_chat(provider, model, messages, **kwargs)
        return ProviderResponse(r.text, r.provider, r.model, r.usage)

    def models(self):
        return catalog_models()
