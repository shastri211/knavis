from dataclasses import dataclass
import asyncio
import httpx

@dataclass
class TranscriptSegment:
    text: str
    start_ms: int
    end_ms: int
    speaker: str | None = None

@dataclass
class TranscriptResult:
    transcript_id: str
    text: str
    language_code: str | None
    language_confidence: float | None
    segments: list[TranscriptSegment]
    duration_s: float | None = None

class AssemblyAIClient:
    """
    AssemblyAI pre-recorded speech-to-text over REST.

      POST /v2/upload                 raw bytes -> {"upload_url"}
      POST /v2/transcript             {"audio_url", "language_detection", "speaker_labels", ...} -> {"id"}
      GET  /v2/transcript/{id}        poll until status is "completed" or "error"

    ``speech_models`` is an ordered preference list (AssemblyAI's default is
    ["universal-3-5-pro", "universal-2"]). It is only sent when given: pinning a single model
    breaks languages that model does not cover.
    """

    TRANSCRIPT_URL = "https://api.assemblyai.com/v2/transcript"
    UPLOAD_URL = "https://api.assemblyai.com/v2/upload"

    def __init__(self, api_key: str, client_factory=None, sleep=asyncio.sleep):
        self.api_key = api_key
        self.client_factory = client_factory or httpx.AsyncClient
        self.sleep = sleep

    def headers(self):
        return {"authorization": self.api_key, "content-type": "application/json"}

    async def upload(self, audio: bytes) -> str:
        """Upload local audio and return AssemblyAI's temporary URL."""
        if not self.api_key:
            raise RuntimeError("AssemblyAI API key is not configured")
        async with self.client_factory(timeout=180) as client:
            response = await client.post(
                self.UPLOAD_URL,
                headers={"authorization": self.api_key, "content-type": "application/octet-stream"},
                content=audio,
            )
        response.raise_for_status()
        return response.json()["upload_url"]

    async def submit(self, audio_url: str, language_detection=True, speaker_labels=True, speech_models=None) -> str:
        if not self.api_key:
            raise RuntimeError("AssemblyAI API key is not configured")
        payload = {"audio_url": audio_url, "language_detection": language_detection, "speaker_labels": speaker_labels}
        if speech_models:
            payload["speech_models"] = list(speech_models)
        async with self.client_factory(timeout=60) as client:
            r = await client.post(self.TRANSCRIPT_URL, headers=self.headers(), json=payload)
        r.raise_for_status()
        return r.json()["id"]

    async def fetch(self, transcript_id: str) -> dict:
        async with self.client_factory(timeout=60) as client:
            r = await client.get(f"{self.TRANSCRIPT_URL}/{transcript_id}", headers=self.headers())
        r.raise_for_status()
        return r.json()

    @staticmethod
    def parse(transcript_id: str, data: dict) -> TranscriptResult:
        segments = [
            TranscriptSegment(
                text=u.get("text", ""), start_ms=int(u.get("start", 0)), end_ms=int(u.get("end", 0)),
                speaker=u.get("speaker"),
            )
            for u in data.get("utterances") or []
        ]
        duration = data.get("audio_duration")
        return TranscriptResult(
            transcript_id=transcript_id, text=data.get("text") or "", language_code=data.get("language_code"),
            language_confidence=data.get("language_confidence"), segments=segments,
            duration_s=float(duration) if duration is not None else None,
        )

    async def wait(self, transcript_id: str, poll_seconds=2, max_wait_seconds=900) -> TranscriptResult:
        for _ in range(max(1, int(max_wait_seconds // poll_seconds))):
            data = await self.fetch(transcript_id)
            if data.get("status") == "completed":
                return self.parse(transcript_id, data)
            if data.get("status") == "error":
                raise RuntimeError(data.get("error") or "AssemblyAI transcription failed")
            await self.sleep(poll_seconds)
        raise RuntimeError("AssemblyAI transcription timed out")

    async def transcribe_file(self, path, poll_seconds=2, max_wait_seconds=900, speech_models=None) -> TranscriptResult:
        upload_url = await self.upload(path.read_bytes())
        transcript_id = await self.submit(upload_url, speech_models=speech_models)
        return await self.wait(transcript_id, poll_seconds, max_wait_seconds)
