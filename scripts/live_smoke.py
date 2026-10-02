"""Check each configured hosted specialist against the REAL service with a tiny synthetic input.

The unit tests prove the adapters build the documented requests; only this script proves your keys, the
endpoint URLs and the model ids actually work. It spends a handful of free-tier calls (about one OCR page,
one figure description, one 2-second transcription, one embedding) and never prints a key.

    python scripts/live_smoke.py            # list what would be called, call nothing
    python scripts/live_smoke.py --yes      # run the checks
"""
import asyncio
import io
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.db import init_db
from app.specialists.asr import asr_candidates
from app.specialists.ocr import get_ocr_provider
from app.specialists.vision import get_vision_provider


def text_image(path: Path) -> Path:
    image = Image.new("RGB", (900, 260), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    draw.text((30, 40), "KNAVIS OCR check 4271", fill="black", font=font)
    draw.text((30, 130), "Retention: 90 days", fill="black", font=font)
    image.save(path)
    return path


def chart_image() -> bytes:
    image = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=22)
    draw.text((180, 15), "Sales by region", fill="black", font=font)
    draw.line((60, 350, 560, 350), fill="black", width=3)
    draw.line((60, 350, 60, 60), fill="black", width=3)
    for i, (label, value, color) in enumerate([("North", 120, "#2b6cb0"), ("South", 240, "#c05621"), ("West", 180, "#2f855a")]):
        x = 110 + i * 150
        draw.rectangle((x, 350 - value, x + 90, 350), fill=color)
        draw.text((x + 10, 360), label, fill="black", font=font)
        draw.text((x + 25, 350 - value - 28), str(value), fill="black", font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def silence_wav(path: Path, seconds: int = 2) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000 * seconds)
    return path


def plan(work: Path) -> list[tuple[str, str, callable]]:
    checks = []
    ocr = get_ocr_provider()
    if ocr:
        async def run_ocr():
            pages = [p async for p in ocr.recognize(text_image(work / "ocr.png"))]
            text = pages[0].text if pages else ""
            return f"read {len(text)} chars: {text[:80]!r}", "4271" in text
        checks.append((f"OCR via {ocr.name}", "1 image / page", run_ocr))
    vision = get_vision_provider()
    if vision:
        async def run_vision():
            description = await vision.describe(chart_image())
            return f"informative={description.informative}: {description.text[:120]!r}", description.informative
        checks.append((f"Figure description via {vision.name} ({getattr(vision, 'model', '')})", "1 request", run_vision))
    wav = silence_wav(work / "silence.wav")
    for provider in asr_candidates(wav):
        async def run_asr(provider=provider):
            result = await provider.transcribe(wav)
            return f"language={result.language} duration={result.duration_s} segments={len(result.segments)} (silence: no text is expected)", True
        checks.append((f"Speech-to-text via {provider.name}", "1 request", run_asr))
    if settings.nvidia_api_key:
        from app.retrieval.embeddings import NVIDIAEmbeddingClient

        async def run_embed():
            client = NVIDIAEmbeddingClient(settings.nvidia_api_key, settings.nvidia_base_url, settings.embedding_model)
            vectors = (await client.embed(["data retention policy"], "query")).vectors
            dimension = len(vectors[0])
            return f"dimension {dimension} (EMBEDDING_DIMENSIONS={settings.embedding_dimensions})", dimension == settings.embedding_dimensions
        checks.append((f"Embeddings via NVIDIA ({settings.embedding_model})", "1 request", run_embed))
    return checks


async def main(run: bool) -> int:
    init_db()
    with tempfile.TemporaryDirectory() as tmp:
        checks = plan(Path(tmp))
        if not checks:
            print("No hosted specialist is configured; set keys in .env first (see scripts/check_config.py).")
            return 1
        print("Checks:" if run else "Would run (nothing was called; add --yes to run):")
        for name, cost, _ in checks:
            print(f"  - {name}  [{cost}]")
        if not run:
            return 0
        failures = 0
        for name, _, check in checks:
            try:
                detail, ok = await check()
                print(f"\n{'PASS' if ok else 'CHECK'}  {name}\n      {detail}")
                failures += 0 if ok else 1
            except Exception as exc:   # never print request details: they could contain a key
                print(f"\nFAIL  {name}\n      {type(exc).__name__}: {str(exc)[:200]}")
                failures += 1
        print(f"\n{len(checks) - failures}/{len(checks)} checks passed.")
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--yes" in sys.argv)))
