"""Report configured integrations and common misconfigurations without printing secret values."""
from collections import Counter
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import settings
from app.providers import models
from app.specialists.asr import asr_candidates
from app.specialists.ocr import get_ocr_provider
from app.specialists.vision import get_vision_provider


def state(value: str) -> str:
    return "configured" if value else "not configured"


def env_keys(path: Path) -> list[str]:
    keys = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].strip().upper())
    return keys


for name, value in (
    ("NVIDIA", settings.nvidia_api_key),
    ("Groq", settings.groq_api_key),
    ("OpenRouter", settings.openrouter_api_key),
    ("AssemblyAI", settings.assemblyai_api_key),
    ("Mistral", settings.mistral_api_key),
    ("Gemini", settings.gemini_api_key),
    ("Qdrant Cloud", settings.qdrant_url),
):
    print(f"{name}: {state(value)}")
print(f"Local data directory: {settings.data_dir}")

warnings = []
env_file = ROOT / ".env"
if not env_file.exists():
    warnings.append(".env not found; copy .env.example to .env")
else:
    duplicates = sorted(k for k, n in Counter(env_keys(env_file)).items() if n > 1)
    if duplicates:
        warnings.append(f"Duplicate settings in .env (the last value wins): {', '.join(duplicates)}")

catalog = {(m["provider"], m["id"]) for m in models()}
if (settings.default_provider, settings.default_model) not in catalog:
    warnings.append(
        f"DEFAULT_PROVIDER/DEFAULT_MODEL ({settings.default_provider}/{settings.default_model}) is not in the "
        "model catalog; requests without a model fall back to the first catalog entry"
    )
if settings.data_dir != ROOT and ROOT not in settings.data_dir.parents:
    warnings.append(f"DATA_DIR is outside the project folder: {settings.data_dir}")
if settings.nvidia_api_key and not settings.embedding_dimensions:
    warnings.append("NVIDIA_API_KEY is set but EMBEDDING_DIMENSIONS is not; dense indexing will fail")
if not any((settings.nvidia_api_key, settings.groq_api_key, settings.openrouter_api_key)):
    warnings.append("No chat provider key is configured; only fixed replies and utilities will work")

# ---- hosted specialists ----
ocr, vision = get_ocr_provider(), get_vision_provider()
print()
print("Specialists:")
print(f"  OCR (scanned pages, images):  {ocr.name if ocr else 'not available (set MISTRAL_API_KEY or NVIDIA_API_KEY)'}")
print(f"  Figure descriptions:          {vision.name + ' / ' + settings.vision_model if vision and vision.name == 'groq' else (vision.name if vision else 'not available (set GROQ_API_KEY)')}")
print(f"  Speech-to-text:               {', '.join(p.name for p in asr_candidates(Path(__file__))) or 'not available (set GROQ_API_KEY or ASSEMBLYAI_API_KEY)'}")
print(f"  Large jobs wait for a yes above {settings.confirm_above_calls} hosted calls; at most {settings.vision_max_figures_per_doc} figures per document.")
if settings.gemini_api_key and not settings.allow_free_tier_data_use:
    print("  Gemini key present but NOT used: its free tier uses content to improve Google's products "
          "(set ALLOW_FREE_TIER_DATA_USE=true to allow it).")
if not ocr:
    warnings.append("No OCR provider: scanned PDFs and images stay unsearchable (marked ocr_unavailable)")

# ---- model ids against the providers' own catalogs (free list calls, no tokens) ----
def listed_models(base_url: str, key: str) -> set[str] | None:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
        response.raise_for_status()
        return {m["id"] for m in response.json().get("data", [])}
    except Exception as exc:
        warnings.append(f"Could not list models from {base_url}: {type(exc).__name__}")
        return None

if "--online" in sys.argv:
    print()
    print("Checking configured model ids against provider catalogs...")
    if settings.groq_api_key:
        available = listed_models(settings.groq_base_url, settings.groq_api_key)
        if available is not None:
            wanted = {"VISION_MODEL": settings.vision_model, "ASR_MODEL": settings.asr_model}
            wanted.update({f"catalog {m['id']}": m["id"] for m in models() if m["provider"] == "groq"})
            for label, model_id in wanted.items():
                print(f"  groq  {model_id:40} {'ok' if model_id in available else 'NOT LISTED'}")
                if model_id not in available:
                    warnings.append(f"Groq does not list {model_id} ({label}); update it or requests using it will fail")
    if settings.nvidia_api_key:
        available = listed_models(settings.nvidia_base_url, settings.nvidia_api_key)
        if available is not None:
            for m in models():
                if m["provider"] == "nvidia":
                    print(f"  nvidia {m['id']:39} {'ok' if m['id'] in available else 'NOT LISTED'}")
                    if m["id"] not in available:
                        warnings.append(f"NVIDIA does not list {m['id']}")
else:
    print()
    print("(Run with --online to check model ids against the providers' catalogs.)")

print()
if warnings:
    print("Warnings:")
    for w in warnings:
        print(f"  - {w}")
else:
    print("No configuration warnings.")
