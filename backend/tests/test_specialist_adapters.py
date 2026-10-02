"""Hosted-provider adapters against mock transports.

The mocks follow the providers' published request/response shapes (checked against their docs). They prove the
adapters build the documented requests and read the documented responses; they cannot prove a live key works.
"""
import base64
import json

import fitz
import httpx
import pytest

from app.config import settings
from app.reliability.governor import Governor, QuotaExhausted
from app.reliability.hosted import get_governor, reset_breaker, set_governor
from app.specialists import asr as asr_module
from app.specialists.asr import AssemblyAI, GroqWhisper, asr_candidates
from app.specialists.base import SpecialistUnavailable
from app.specialists.ocr import MistralOCR, NvidiaOCR, get_ocr_provider
from app.specialists.vision import GeminiVision, GroqVision, NOT_INFORMATIVE, get_vision_provider


_runs = iter(range(10_000))


class FakeClock:
    def __init__(self):
        # Usage counters persist in the database, so every test gets its own days: no leakage between tests.
        self.start = self.t = 3_000_000.0 + next(_runs) * 200_000

    def clock(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds


@pytest.fixture(autouse=True)
def governor():
    clock = FakeClock()
    set_governor(Governor(clock=clock.clock, sleep=clock.sleep))
    reset_breaker()
    yield clock
    set_governor(None)
    reset_breaker()


def factory(handler):
    return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


def numbered_pdf(path, pages):
    pdf = fitz.open()
    for number in range(1, pages + 1):
        pdf.new_page().insert_text((72, 72), f"page-{number}-text")
    pdf.save(path)
    pdf.close()
    return path


async def collect(iterator):
    return [item async for item in iterator]


# ---- Mistral OCR ---------------------------------------------------------------------------

class MistralMock:
    def __init__(self, ocr_status=None, retry_after="600"):
        self.requests, self.uploads, self.ocr_calls, self.deletes = [], [], [], 0
        self.ocr_status, self.retry_after = ocr_status or {}, retry_after

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/v1/files":
            body = request.content
            self.uploads.append({"body": body, "purpose": b'name="purpose"' in body and b"ocr" in body})
            return httpx.Response(200, json={"id": f"file-{len(self.uploads)}"})
        if request.method == "GET" and path.startswith("/v1/files/") and path.endswith("/url"):
            return httpx.Response(200, json={"url": "https://signed.example/" + path.split("/")[3]})
        if request.method == "POST" and path == "/v1/ocr":
            self.ocr_calls.append(json.loads(request.content))
            status = self.ocr_status.get(len(self.ocr_calls))
            if status:
                return httpx.Response(status, headers={"retry-after": self.retry_after}, json={"message": "limit"})
            return httpx.Response(200, json={"model": "mistral-ocr-latest", "pages": [
                {"index": i, "markdown": f"upload-{len(self.uploads)}-page-{i}"} for i in range(self.pages_in_last_upload())
            ], "usage_info": {"pages_processed": self.pages_in_last_upload()}})
        if request.method == "DELETE":
            self.deletes += 1
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def pages_in_last_upload(self) -> int:
        body = self.uploads[-1]["body"]
        if b"%PDF" not in body:
            return 1
        data = body[body.index(b"%PDF"):body.rindex(b"%%EOF") + 5]
        with fitz.open(stream=data, filetype="pdf") as pdf:
            return len(pdf)


def uploaded_pdf(upload):
    body = upload["body"]
    return fitz.open(stream=body[body.index(b"%PDF"):body.rindex(b"%%EOF") + 5], filetype="pdf")


async def test_mistral_uploads_only_the_scanned_pages_and_maps_results_back(tmp_path):
    mock = MistralMock()
    provider = MistralOCR("key-123", client_factory=factory(mock))
    pages = await collect(provider.recognize(numbered_pdf(tmp_path / "doc.pdf", 5), [2, 5]))

    assert [p.page for p in pages] == [2, 5] and [p.text for p in pages] == ["upload-1-page-0", "upload-1-page-1"]
    with uploaded_pdf(mock.uploads[0]) as subset:                      # the other three pages never left the machine
        assert len(subset) == 2 and "page-2-text" in subset[0].get_text() and "page-5-text" in subset[1].get_text()
    assert mock.uploads[0]["purpose"] is True
    (body,) = mock.ocr_calls
    assert body["model"] == "mistral-ocr-latest" and body["table_format"] == "markdown" and "pages" not in body
    assert body["document"] == {"type": "document_url", "document_url": "https://signed.example/file-1"}
    assert all(r.headers["authorization"] == "Bearer key-123" for r in mock.requests)
    assert mock.deletes == 1                                           # the upload is removed afterwards
    assert any("expiry=1" in str(r.url) for r in mock.requests)


async def test_mistral_batches_to_the_page_quota_and_waits_between_batches(tmp_path, monkeypatch, governor):
    monkeypatch.setattr(settings, "quota_overrides", {"mistral_ocr": {"pages": {"minute": 30}}})
    mock = MistralMock()
    pages = await collect(MistralOCR("k", client_factory=factory(mock)).recognize(numbered_pdf(tmp_path / "big.pdf", 70), list(range(1, 71))))
    assert [p.page for p in pages] == list(range(1, 71))
    assert len(mock.ocr_calls) == 3 and mock.deletes == 3             # batches of 30, 30, 10
    assert governor.t > governor.start + 60                           # waited for the next minute between batches


async def test_mistral_reads_a_standalone_image_without_a_page_number(tmp_path):
    image = tmp_path / "scan.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    mock = MistralMock()
    (page,) = await collect(MistralOCR("k", client_factory=factory(mock)).recognize(image))
    assert page.page is None and mock.ocr_calls[0]["document"]["type"] == "image_url"


async def test_a_quota_stop_keeps_finished_batches_and_still_deletes_the_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "quota_overrides", {"mistral_ocr": {"pages": {"minute": 30}}})
    mock = MistralMock(ocr_status={2: 429})
    done = []
    with pytest.raises(QuotaExhausted):
        async for page in MistralOCR("k", client_factory=factory(mock)).recognize(numbered_pdf(tmp_path / "d.pdf", 40), list(range(1, 41))):
            done.append(page.page)
    assert done == list(range(1, 31))                                  # the first batch was delivered before the stop
    assert mock.deletes == 2


async def test_an_entire_pdf_is_never_sent_to_ocr_by_default(tmp_path):
    with pytest.raises(ValueError):
        await collect(MistralOCR("k", client_factory=factory(MistralMock())).recognize(numbered_pdf(tmp_path / "d.pdf", 2)))


# ---- NVIDIA OCR ----------------------------------------------------------------------------

async def test_nvidia_ocr_requests_paragraphs_and_keeps_geometry(tmp_path):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "nvidia/nemotron-ocr-v2", "data": [{"index": 0, "text_detections": [
            {"text_prediction": {"text": "First paragraph", "confidence": 0.99},
             "bounding_box": {"points": [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.5, "y": 0.2}, {"x": 0.1, "y": 0.2}]}},
            {"text_prediction": {"text": "Second paragraph", "confidence": 0.9}, "bounding_box": {"points": []}},
        ]}], "usage": {"images_size_mb": 0.2}})

    provider = NvidiaOCR("nv-key", "https://ocr.example/v1/ocr", client_factory=factory(handler))
    pages = await collect(provider.recognize(numbered_pdf(tmp_path / "d.pdf", 3), [3]))
    (request,) = seen
    assert request["merge_levels"] == ["paragraph"] and request["input"][0]["type"] == "image_url"
    assert request["input"][0]["url"].startswith("data:image/png;base64,")
    assert pages[0].page == 3 and pages[0].text == "First paragraph\n\nSecond paragraph"
    assert pages[0].regions[0]["confidence"] == 0.99 and pages[0].regions[0]["bbox"][0] == {"x": 0.1, "y": 0.1}
    assert pages[0].confidence == pytest.approx(0.945)


def test_ocr_provider_choice_follows_keys_and_the_pin(monkeypatch):
    monkeypatch.setattr(settings, "mistral_api_key", "m")
    monkeypatch.setattr(settings, "nvidia_api_key", "n")
    assert get_ocr_provider().name == "mistral"
    monkeypatch.setattr(settings, "ocr_provider", "nvidia")
    assert get_ocr_provider().name == "nvidia"
    monkeypatch.setattr(settings, "ocr_provider", "none")
    assert get_ocr_provider() is None
    monkeypatch.setattr(settings, "ocr_provider", "auto")
    monkeypatch.setattr(settings, "mistral_api_key", "")
    assert get_ocr_provider().name == "nvidia"
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    assert get_ocr_provider() is None


# ---- Speech to text ------------------------------------------------------------------------

async def test_groq_whisper_request_shape_and_audio_seconds_accounting(tmp_path):
    seen = {}

    def handler(request):
        seen["body"], seen["auth"], seen["path"] = request.content, request.headers["authorization"], request.url.path
        return httpx.Response(200, json={"text": "Hello world. Second part.", "language": "en", "duration": 12.0, "segments": [
            {"id": 0, "start": 0.0, "end": 4.0, "text": " Hello world."}, {"id": 1, "start": 4.0, "end": 12.0, "text": " Second part."},
            {"id": 2, "start": 12.0, "end": 12.5, "text": "  "}]})

    audio = tmp_path / "talk.mp3"
    audio.write_bytes(b"x" * 32_000)                                   # ~2 s by the 128 kbit/s estimate
    result = await GroqWhisper("g-key", "https://api.groq.test/openai/v1", "whisper-large-v3-turbo", client_factory=factory(handler)).transcribe(audio)

    assert seen["path"] == "/openai/v1/audio/transcriptions" and seen["auth"] == "Bearer g-key"
    for field in (b'name="model"', b"whisper-large-v3-turbo", b'name="response_format"', b"verbose_json", b"timestamp_granularities[]", b"segment"):
        assert field in seen["body"]
    assert [(s.start_s, s.end_s, s.text) for s in result.segments] == [(0.0, 4.0, "Hello world."), (4.0, 12.0, "Second part.")]
    assert result.language == "en" and result.duration_s == 12.0
    # the up-front estimate (2 s) was replaced by the 12 s Groq reported; remaining() is the tightest window (hourly, 7200)
    assert get_governor().remaining("groq_whisper", "audio_seconds") == 7200 - 12


async def test_groq_whisper_refuses_files_over_its_limit_without_calling_the_api(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_module, "GROQ_MAX_BYTES", 10)
    big = tmp_path / "long.wav"
    big.write_bytes(b"x" * 11)
    calls = []
    provider = GroqWhisper("k", "https://api.groq.test/v1", "m", client_factory=factory(lambda r: calls.append(r)))
    with pytest.raises(SpecialistUnavailable, match="25 MB"):
        await provider.transcribe(big)
    assert calls == []


async def test_assemblyai_uses_its_default_model_list_so_other_languages_work(tmp_path):
    requests = []
    polls = iter([{"status": "processing"}, {
        "status": "completed", "text": "namaste doston", "language_code": "hi", "language_confidence": 0.93, "audio_duration": 8.0,
        "utterances": [{"start": 1000, "end": 4000, "speaker": "A", "text": "namaste doston"}]}])

    def handler(request):
        requests.append(request)
        if request.url.path == "/v2/upload":
            return httpx.Response(200, json={"upload_url": "https://cdn.example/audio"})
        if request.method == "POST":
            return httpx.Response(200, json={"id": "tr-1"})
        return httpx.Response(200, json=next(polls))

    async def no_sleep(_):
        return None

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3")
    result = await AssemblyAI("aai-key", client_factory=factory(handler), sleep=no_sleep).transcribe(audio)

    submitted = json.loads(next(r for r in requests if r.url.path == "/v2/transcript" and r.method == "POST").content)
    assert submitted == {"audio_url": "https://cdn.example/audio", "language_detection": True, "speaker_labels": True}
    assert "speech_models" not in submitted and all(r.headers["authorization"] == "aai-key" for r in requests)
    assert result.language == "hi" and result.duration_s == 8.0
    assert [(s.start_s, s.end_s, s.speaker) for s in result.segments] == [(1.0, 4.0, "A")]


async def test_assemblyai_surfaces_a_failed_transcription(tmp_path):
    def handler(request):
        if request.url.path == "/v2/upload":
            return httpx.Response(200, json={"upload_url": "u"})
        if request.method == "POST":
            return httpx.Response(200, json={"id": "t"})
        return httpx.Response(200, json={"status": "error", "error": "audio too short"})

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="audio too short"):
        await AssemblyAI("k", client_factory=factory(handler)).transcribe(audio)


def test_asr_provider_order_depends_on_file_size_and_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "g")
    monkeypatch.setattr(settings, "assemblyai_api_key", "a")
    small = tmp_path / "s.mp3"
    small.write_bytes(b"x")
    assert [p.name for p in asr_candidates(small)] == ["groq", "assemblyai"]    # Groq first, AssemblyAI as fallback
    monkeypatch.setattr(asr_module, "GROQ_MAX_BYTES", 0)
    assert [p.name for p in asr_candidates(small)] == ["assemblyai"]            # too big for Groq
    monkeypatch.setattr(asr_module, "GROQ_MAX_BYTES", 10)
    monkeypatch.setattr(settings, "asr_provider", "assemblyai")
    assert [p.name for p in asr_candidates(small)] == ["assemblyai"]
    monkeypatch.setattr(settings, "asr_provider", "auto")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "assemblyai_api_key", "")
    assert asr_candidates(small) == []


# ---- Vision --------------------------------------------------------------------------------

async def test_groq_vision_sends_one_image_with_the_description_prompt():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Type: bar chart\nRegion | Sales\nNorth | 10"}}]})

    description = await GroqVision("g", "https://api.groq.test/v1", "vision-model-x", client_factory=factory(handler)).describe(b"\x89PNGdata")
    (payload,) = seen
    parts = payload["messages"][0]["content"]
    assert payload["model"] == "vision-model-x" and payload["temperature"] == 0
    assert NOT_INFORMATIVE in parts[0]["text"] and "Never invent" in parts[0]["text"]
    assert parts[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(b"\x89PNGdata").decode()
    assert description.informative and description.text.startswith("Type: bar chart") and description.provider == "groq"


async def test_a_decorative_image_is_recognised_and_not_indexed():
    handler = lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "NOT_INFORMATIVE"}}]})
    description = await GroqVision("g", "https://api.groq.test/v1", "m", client_factory=factory(handler)).describe(b"png")
    assert description.informative is False and description.text == ""


async def test_gemini_is_blocked_until_the_owner_accepts_free_tier_data_use(monkeypatch):
    calls = []
    handler = lambda request: calls.append(request) or httpx.Response(200, json={})
    provider = GeminiVision("gem-key", "gemini-2.5-flash-lite", client_factory=factory(handler))
    monkeypatch.setattr(settings, "allow_free_tier_data_use", False)
    with pytest.raises(SpecialistUnavailable, match="improve Google"):
        await provider.describe(b"png")
    assert calls == []                                                 # nothing was sent


async def test_gemini_request_shape_once_allowed(monkeypatch):
    monkeypatch.setattr(settings, "allow_free_tier_data_use", True)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Type: line chart"}, {"text": " rising trend"}]}}]})

    description = await GeminiVision("gem-key", "gemini-2.5-flash-lite", client_factory=factory(handler)).describe(b"png")
    (request,) = seen
    body = json.loads(request.content)
    assert request.url.path == "/v1beta/models/gemini-2.5-flash-lite:generateContent" and request.headers["x-goog-api-key"] == "gem-key"
    assert body["contents"][0]["parts"][1]["inline_data"] == {"mime_type": "image/png", "data": base64.b64encode(b"png").decode()}
    assert description.text == "Type: line chart rising trend"


def test_vision_provider_choice_respects_keys_and_the_privacy_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "g")
    monkeypatch.setattr(settings, "allow_free_tier_data_use", False)
    assert get_vision_provider() is None                               # a Gemini key alone does not send data to Gemini
    monkeypatch.setattr(settings, "allow_free_tier_data_use", True)
    assert get_vision_provider().name == "gemini"
    monkeypatch.setattr(settings, "groq_api_key", "k")
    assert get_vision_provider().name == "groq"                        # Groq is preferred when available
    monkeypatch.setattr(settings, "vision_provider", "none")
    assert get_vision_provider() is None
    monkeypatch.setattr(settings, "vision_provider", "auto")
