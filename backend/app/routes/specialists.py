from fastapi import APIRouter, Depends

from ..auth import current_principal

from ..config import settings
from ..ingest.store import EMBED_BATCH
from ..reliability.hosted import get_governor
from ..specialists.ocr import get_ocr_provider
from ..specialists.vision import get_vision_provider

EMBED_PROVIDER = "nvidia_embed"

router = APIRouter(tags=["specialists"], dependencies=[Depends(current_principal)])


@router.get("/quota")
def quota():
    """Usage against each provider's limit in the current window (limits are conservative defaults unless overridden),
    and what is left of the embedding quota, which large documents draw on (``embedding``)."""
    governor = get_governor()
    remaining = governor.remaining(EMBED_PROVIDER, "requests")
    return {
        "providers": governor.snapshot(),
        "embedding": {
            "provider": EMBED_PROVIDER,
            "configured": bool(settings.nvidia_api_key),
            "chunks_per_request": EMBED_BATCH,
            "remaining_requests": remaining,                       # in the tightest window; None when no limit is set
            "remaining_chunks": None if remaining is None else remaining * EMBED_BATCH,
            "max_chunks_per_document": settings.max_embed_chunks_per_doc,   # a document needing more waits for a yes
        },
    }


@router.get("/specialists")
def specialists():
    """Which hosted specialist would run for each job type, and what the owner should know about each."""
    ocr, vision = get_ocr_provider(), get_vision_provider()
    return {
        "ocr": {"provider": ocr.name if ocr else None},
        "vision": {
            "provider": vision.name if vision else None,
            "gemini_blocked_until_opt_in": bool(settings.gemini_api_key) and not settings.allow_free_tier_data_use,
        },
        "asr": {"configured": [name for name, key in (("groq", settings.groq_api_key), ("assemblyai", settings.assemblyai_api_key)) if key]},
        "confirm_above_calls": settings.confirm_above_calls,
        "notes": {"gemini": "Gemini's free tier uses submitted content to improve Google's products; it is only used with ALLOW_FREE_TIER_DATA_USE=true."},
    }
