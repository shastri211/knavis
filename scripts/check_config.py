"""Report configured integrations without printing secret values."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import settings


def state(value: str) -> str:
    return "configured" if value else "not configured"


for name, value in (
    ("NVIDIA", settings.nvidia_api_key),
    ("Groq", settings.groq_api_key),
    ("OpenRouter", settings.openrouter_api_key),
    ("AssemblyAI", settings.assemblyai_api_key),
    ("Qdrant Cloud", settings.qdrant_url),
):
    print(f"{name}: {state(value)}")
print(f"Local data directory: {settings.data_dir.resolve()}")
