from fastapi import APIRouter

from ..config import settings
from ..reliability.hosted import get_governor
from ..specialists.ocr import get_ocr_provider
from ..specialists.vision import get_vision_provider

router = APIRouter(tags=["specialists"])


@router.get("/quota")
def quota():
    """Usage against each provider's limit in the current window (limits are conservative defaults unless overridden)."""
    return {"providers": get_governor().snapshot()}


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
