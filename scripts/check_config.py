"""Report configured integrations and common misconfigurations without printing secret values."""
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import settings
from app.providers import models


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

print()
if warnings:
    print("Warnings:")
    for w in warnings:
        print(f"  - {w}")
else:
    print("No configuration warnings.")
