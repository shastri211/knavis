"""Speech-to-text providers: Groq Whisper (fast, free-tier limits verified) and AssemblyAI (long files)."""
import wave
from pathlib import Path
from typing import Protocol

import httpx

from ..config import settings
from ..multimodal.assemblyai import AssemblyAIClient
from ..reliability.hosted import get_governor, hosted_call
from .base import ASRResult, ASRSegment, SpecialistUnavailable

GROQ_MAX_BYTES = 24 * 1024 * 1024     # Groq's free tier accepts files up to 25 MB
_AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".mp4": "video/mp4", ".webm": "video/webm",
}


class ASRProvider(Protocol):
    name: str

    async def transcribe(self, path: Path) -> ASRResult: ...


def estimate_duration_s(path: Path) -> float:
    """Seconds of audio, known exactly for WAV and roughly (128 kbit/s) for compressed formats.
    Used only to check the audio-seconds quota before a call; the real duration is recorded after."""
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wav:
                return wav.getnframes() / float(wav.getframerate())
        except (wave.Error, EOFError):
            pass
    return path.stat().st_size / 16000.0


class GroqWhisper:
    """Groq's OpenAI-compatible transcription endpoint with ``verbose_json`` for segment timestamps."""
    name = "groq"
    quota_provider = "groq_whisper"

    def __init__(self, api_key: str, base_url: str, model: str, client_factory=None):
        self.api_key, self.base_url, self.model = api_key, base_url.rstrip("/"), model
        self.client_factory = client_factory or httpx.AsyncClient

    async def transcribe(self, path: Path) -> ASRResult:
        size = path.stat().st_size
        if size > GROQ_MAX_BYTES:
            raise SpecialistUnavailable(f"This file is {size / 1048576:.0f} MB; Groq's free tier accepts up to 25 MB.")
        estimate = max(1, int(estimate_duration_s(path)))
        content = path.read_bytes()

        async def call() -> dict:
            async with self.client_factory(timeout=300) as client:
                response = await client.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": (path.name, content, _AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream"))},
                    data={"model": self.model, "response_format": "verbose_json", "timestamp_granularities[]": "segment"},
                )
            response.raise_for_status()
            return response.json()

        data = await hosted_call(self.quota_provider, call, units={"requests": 1, "audio_seconds": estimate})
        segments = [
            ASRSegment(start_s=float(s.get("start", 0)), end_s=float(s.get("end", 0)), text=(s.get("text") or "").strip())
            for s in data.get("segments") or [] if (s.get("text") or "").strip()
        ]
        duration = data.get("duration") or (segments[-1].end_s if segments else None)
        if duration:   # replace the up-front estimate with what Groq actually counted
            get_governor().adjust(self.quota_provider, audio_seconds=int(duration) - estimate)
        return ASRResult(segments=segments, text=(data.get("text") or "").strip(), language=data.get("language"),
                         duration_s=float(duration) if duration else None, provider=self.name, model=self.model)


class AssemblyAI:
    """AssemblyAI: accepts large files and returns speaker-labelled utterances."""
    name = "assemblyai"
    quota_provider = "assemblyai"

    def __init__(self, api_key: str, client_factory=None, sleep=None):
        kwargs = {"sleep": sleep} if sleep else {}
        self.client = AssemblyAIClient(api_key, client_factory=client_factory, **kwargs)

    async def transcribe(self, path: Path) -> ASRResult:
        upload_url = await hosted_call(self.quota_provider, lambda: self.client.upload(path.read_bytes()))
        transcript_id = await hosted_call(self.quota_provider, lambda: self.client.submit(upload_url), units={"requests": 1})
        transcript = await self.client.wait(transcript_id)
        segments = [
            ASRSegment(start_s=s.start_ms / 1000, end_s=s.end_ms / 1000, text=s.text.strip(), speaker=s.speaker)
            for s in transcript.segments if s.text.strip()
        ]
        return ASRResult(segments=segments, text=transcript.text.strip(), language=transcript.language_code,
                         duration_s=transcript.duration_s, provider=self.name, model="assemblyai")


def asr_candidates(path: Path) -> list[ASRProvider]:
    """Providers to try, in order. Groq first when the file fits its limit; AssemblyAI otherwise or as a fallback."""
    choice = settings.asr_provider.lower()
    candidates: list[ASRProvider] = []
    if choice in ("auto", "groq") and settings.groq_api_key and path.stat().st_size <= GROQ_MAX_BYTES:
        candidates.append(GroqWhisper(settings.groq_api_key, settings.groq_base_url, settings.asr_model))
    if choice in ("auto", "assemblyai") and settings.assemblyai_api_key:
        candidates.append(AssemblyAI(settings.assemblyai_api_key))
    return candidates
