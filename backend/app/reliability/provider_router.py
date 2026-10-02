from dataclasses import dataclass

@dataclass
class ProviderPolicy:
    primary: str
    fallback: list[str]
    allow_fallback: bool = True

class ProviderUnavailable(RuntimeError):
    pass

async def call_with_fallback(call, policy: ProviderPolicy):
    providers = [policy.primary] + (policy.fallback if policy.allow_fallback else [])
    last = None
    for provider in providers:
        try:
            return await call(provider)
        except Exception as exc:
            last = exc
    raise ProviderUnavailable("All configured providers failed") from last
