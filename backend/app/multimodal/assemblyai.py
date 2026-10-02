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

class AssemblyAIClient:
    """
    Production audio boundary.

    Async pre-recorded STT:
      POST https://api.assemblyai.com/v2/transcript
      GET  https://api.assemblyai.com/v2/transcript/{id}

    AssemblyAI currently documents Universal as supporting 99 languages for
    prerecorded audio and automatic language detection. Code-switching support
    exists, but quality varies by language pair, so evaluation remains required.
    """

    TRANSCRIPT_URL = "https://api.assemblyai.com/v2/transcript"
    UPLOAD_URL = "https://api.assemblyai.com/v2/upload"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def headers(self):
        return {
            "authorization": self.api_key,
            "content-type": "application/json",
        }

    async def upload(self, audio: bytes) -> str:
        """Upload local audio and return AssemblyAI's temporary URL."""
        if not self.api_key:
            raise RuntimeError("AssemblyAI API key is not configured")
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                self.UPLOAD_URL,
                headers={"authorization": self.api_key, "content-type": "application/octet-stream"},
                content=audio,
            )
        response.raise_for_status()
        return response.json()["upload_url"]

    async def submit(
        self,
        audio_url: str,
        language_detection=True,
        speaker_labels=True,
        speech_model="universal-3-pro",
    ):
        if not self.api_key:
            raise RuntimeError("AssemblyAI API key is not configured")

        payload = {
            "audio_url": audio_url,
            "language_detection": language_detection,
            "speaker_labels": speaker_labels,
            "speech_models": [speech_model],
        }

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                self.TRANSCRIPT_URL,
                headers=self.headers(),
                json=payload,
            )
        r.raise_for_status()
        return r.json()["id"]

    async def get(self, transcript_id: str) -> TranscriptResult:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(
                f"{self.TRANSCRIPT_URL}/{transcript_id}",
                headers=self.headers(),
            )
        r.raise_for_status()
        data = r.json()

        if data.get("status") == "error":
            raise RuntimeError(data.get("error") or "AssemblyAI transcription failed")

        segments = []
        for u in data.get("utterances") or []:
            segments.append(TranscriptSegment(
                text=u.get("text", ""),
                start_ms=int(u.get("start", 0)),
                end_ms=int(u.get("end", 0)),
                speaker=u.get("speaker"),
            ))

        return TranscriptResult(
            transcript_id=transcript_id,
            text=data.get("text") or "",
            language_code=data.get("language_code"),
            language_confidence=data.get("language_confidence"),
            segments=segments,
        )

    async def transcribe_file(self, path, poll_seconds=2, max_wait_seconds=900) -> TranscriptResult:
        upload_url = await self.upload(path.read_bytes())
        transcript_id = await self.submit(upload_url)
        attempts = max(1, max_wait_seconds // poll_seconds)
        for _ in range(attempts):
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(f"{self.TRANSCRIPT_URL}/{transcript_id}", headers=self.headers())
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "completed":
                return await self.get(transcript_id)
            if data.get("status") == "error":
                raise RuntimeError(data.get("error") or "AssemblyAI transcription failed")
            await asyncio.sleep(poll_seconds)
        raise RuntimeError("AssemblyAI transcription timed out")
