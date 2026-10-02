"""Ingestion with hosted specialists, end to end through the real API. Only the providers are faked."""
import hashlib
import io
import random

import fitz
import pytest

from app.config import settings
from app.reliability.governor import QuotaExhausted
from app.specialists.base import ASRResult, ASRSegment, FigureDescription, OCRPage, SpecialistUnavailable
from conftest import FACT, MODEL
from test_ingest_pipeline import chunks_of, make_scanned_pdf, new_session

QUESTION = "How long must company data be retained after the contract ends?"


# ---- fakes ---------------------------------------------------------------------------------

class FakeOCR:
    name = "fake-ocr"

    def __init__(self, texts=None, stop_at=None):
        self.requests, self.texts, self.stop_at, self.stopped = [], texts or {2: "# Scanned policy\n\n" + FACT, 3: "Second scanned page."}, stop_at, False

    async def recognize(self, path, pages=None):
        self.requests.append(list(pages) if pages else None)
        for page in pages or [None]:
            if page is not None and page == self.stop_at and not self.stopped:
                self.stopped = True
                raise QuotaExhausted("fake_ocr", "pages", "minute", 42)
            yield OCRPage(page=page, text=self.texts.get(page, ""), provider=self.name)


class FakeVision:
    name = "fake-vision"

    def __init__(self, informative=True, text=None):
        self.calls, self.informative = 0, informative
        self.text = text or "Type: bar chart\nRegion | Sales\nNorth | 10\nSouth | 20\nSales are higher in the south."

    async def describe(self, png):
        self.calls += 1
        return FigureDescription(text=self.text if self.informative else "", informative=self.informative, provider=self.name, model="fake-model")


class FakeASR:
    def __init__(self, name="fake-asr", error=None):
        self.name, self.calls, self.error = name, 0, error

    async def transcribe(self, path):
        self.calls += 1
        if self.error:
            raise self.error
        return ASRResult(
            segments=[ASRSegment(1.0, 5.0, FACT, "A"), ASRSegment(6.0, 9.0, "Backups are kept for 30 days.", "B")],
            text=FACT, language="en", duration_s=9.0, provider=self.name, model="fake")


class Providers:
    ocr = vision = None
    asr: list = []


@pytest.fixture
def providers(monkeypatch):
    import app.jobs as jobs
    import app.specialists.run as run
    chosen = Providers()
    chosen.asr = []
    monkeypatch.setattr(run, "get_ocr_provider", lambda: chosen.ocr)
    monkeypatch.setattr(run, "get_vision_provider", lambda: chosen.vision)
    monkeypatch.setattr(jobs, "get_vision_provider", lambda: chosen.vision)
    monkeypatch.setattr(run, "asr_candidates", lambda path: list(chosen.asr))
    return chosen


def noise_png(seed, size=300):
    from PIL import Image
    image = Image.frombytes("RGB", (size, size), random.Random(seed).randbytes(size * size * 3))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def pdf_with_figures(seeds, text="Quarterly report with enough native text that this page never needs OCR. " * 3):
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_textbox(fitz.Rect(72, 72, 520, 160), text, fontsize=11)
    for index, seed in enumerate(seeds):
        page.insert_image(fitz.Rect(72, 200 + index * 250, 472, 430 + index * 250), stream=noise_png(seed))
    data = pdf.tobytes()
    pdf.close()
    return data


def status_of(client, session, name=None):
    docs = client.get(f"/api/sessions/{session}/documents").json()
    return docs[0]


def process(client, document_id, action):
    return client.post(f"/api/documents/{document_id}/process", json={"action": action})


# ---- confirmation and skipping -------------------------------------------------------------

def test_a_big_plan_waits_for_confirmation_while_the_native_text_stays_searchable(client, llm, upload, providers, monkeypatch):
    monkeypatch.setattr(settings, "confirm_above_calls", 1)
    providers.ocr = FakeOCR()
    session = new_session(client)
    document, job = upload(session, "scan.pdf", make_scanned_pdf(), "application/pdf")

    assert document["status"] == "awaiting_confirmation" and job["status"] == "paused" and job["stage"] == "awaiting_confirmation"
    assert document["details"]["pause"]["calls"] == 2 and "Confirm to process" in job["error"]
    assert providers.ocr.requests == []                                   # nothing was spent
    assert [c[2] for c in chunks_of(document["id"])] == [1]               # page 1's native text is already searchable

    assert process(client, document["id"], "confirm").status_code == 200
    document = status_of(client, session)
    assert document["status"] == "indexed" and providers.ocr.requests == [[2, 3]]
    assert {c[2] for c in chunks_of(document["id"])} == {1, 2, 3}


def test_skipping_keeps_the_native_text_and_is_not_remembered_as_a_complete_result(client, llm, upload, providers, monkeypatch):
    monkeypatch.setattr(settings, "confirm_above_calls", 1)
    providers.ocr = FakeOCR()
    data = make_scanned_pdf()
    document, _ = upload(new_session(client), "scan.pdf", data, "application/pdf")
    assert process(client, document["id"], "skip").status_code == 200
    skipped = chunks_of(document["id"])
    assert providers.ocr.requests == [] and {c[2] for c in skipped} == {1}

    monkeypatch.setattr(settings, "confirm_above_calls", 25)             # a later upload may decide differently
    second, _ = upload(new_session(client), "scan.pdf", data, "application/pdf")
    assert second["status"] == "indexed" and providers.ocr.requests == [[2, 3]]


def test_process_rejects_actions_that_make_no_sense(client, upload, providers):
    document, _ = upload(new_session(client), "plain.txt", b"just text here for the test", "text/plain")
    assert process(client, document["id"], "confirm").status_code == 400
    assert process(client, "no-such-document", "confirm").status_code == 404
    assert client.post(f"/api/documents/{document['id']}/process", json={"action": "explode"}).status_code == 422


# ---- quota pause and resume ----------------------------------------------------------------

def test_hitting_a_quota_pauses_the_document_and_resuming_never_pays_for_a_page_twice(client, llm, upload, ask, providers):
    from app.db import SessionLocal
    from app.specialists.cache import get_unit, ocr_key
    providers.ocr = FakeOCR(stop_at=3)
    data = make_scanned_pdf()
    session = new_session(client)
    document, job = upload(session, "scan.pdf", data, "application/pdf")

    assert document["status"] == "waiting_for_quota" and job["status"] == "paused"
    assert "resets in about 42s" in job["error"] and document["details"]["pause"]["resets_in"] == 42
    with SessionLocal() as db:
        digest = hashlib.sha256(data).hexdigest()
        assert get_unit(db, ocr_key(digest, 2)) is not None and get_unit(db, ocr_key(digest, 3)) is None   # page 2 was kept
    assert process(client, document["id"], "confirm").status_code == 400  # a quota pause is resumed with "retry"

    assert process(client, document["id"], "retry").status_code == 200
    document = status_of(client, session)
    assert document["status"] == "indexed"
    assert providers.ocr.requests == [[2, 3], [3]]                        # page 2 was NOT requested again
    assert {c[2] for c in chunks_of(document["id"])} == {1, 2, 3}
    llm.answer = f"{FACT} [EVIDENCE 1]"
    assert ask(session, QUESTION)["citations"]


def test_running_out_of_embedding_quota_pauses_after_the_text_is_saved(client, llm, upload, ask, monkeypatch):
    from app.integration.pipeline import IntegratedRAGPipeline

    async def exhausted(self, session_id, rows, source_name):
        raise QuotaExhausted("nvidia_embed", "requests", "minute", 30)

    monkeypatch.setattr(IntegratedRAGPipeline, "index_chunks", exhausted)
    session = new_session(client)
    document, job = upload(session, "policy.txt", FACT.encode(), "text/plain")
    assert document["status"] == "waiting_for_quota" and "nvidia embed" not in job["error"] and "resets in" in job["error"]
    llm.answer = f"{FACT} [EVIDENCE 1]"
    assert ask(session, QUESTION)["citations"]                            # lexical search works while vectors wait

    async def fine(self, session_id, rows, source_name):
        return None

    monkeypatch.setattr(IntegratedRAGPipeline, "index_chunks", fine)
    assert process(client, document["id"], "retry").status_code == 200
    assert status_of(client, session)["status"] == "indexed"


# ---- figure descriptions -------------------------------------------------------------------

def test_a_chart_image_is_described_once_indexed_with_its_page_and_cached(client, llm, upload, ask, providers):
    providers.vision = FakeVision()
    data = pdf_with_figures([1])
    first_session, second_session = new_session(client), new_session(client)
    document, _ = upload(first_session, "report.pdf", data, "application/pdf")

    assert providers.vision.calls == 1 and document["status"] == "indexed"
    figure = next(c for c in chunks_of(document["id"]) if c[1] == "figure")
    assert figure[2] == 1 and figure[3] == "page 1" and "machine-described" in figure[5] and "Type: bar chart" in figure[5]
    llm.answer = "Sales are higher in the south [EVIDENCE 1]."
    result = ask(first_session, "What does the figure show about sales by region?")
    assert result["citations"] and "Sales are higher in the south" in llm.answer_prompt

    again, _ = upload(second_session, "report.pdf", data, "application/pdf")
    assert providers.vision.calls == 1 and again["details"]["from_cache"] is True


def test_the_number_of_described_figures_per_document_is_capped(client, upload, providers, monkeypatch):
    monkeypatch.setattr(settings, "vision_max_figures_per_doc", 1)
    providers.vision = FakeVision()
    document, _ = upload(new_session(client), "two.pdf", pdf_with_figures([11, 12]), "application/pdf")
    assert providers.vision.calls == 1 and document["details"]["info"]["plan"]["figures"] == 1


def test_decorative_images_are_not_indexed_and_a_repeated_image_is_not_described_again(client, upload, providers):
    providers.vision = FakeVision(informative=False)
    first, _ = upload(new_session(client), "a.pdf", pdf_with_figures([21], text="First report text that is long enough to need no OCR. " * 3), "application/pdf")
    assert providers.vision.calls == 1 and not [c for c in chunks_of(first["id"]) if c[1] == "figure"]
    second, _ = upload(new_session(client), "b.pdf", pdf_with_figures([21], text="A different report with other native words entirely. " * 3), "application/pdf")
    assert providers.vision.calls == 1                                    # same image: the earlier verdict was reused
    assert not [c for c in chunks_of(second["id"]) if c[1] == "figure"]


def test_scanned_pages_are_read_by_ocr_not_described_as_figures(client, upload, providers):
    providers.ocr, providers.vision = FakeOCR(), FakeVision()
    upload(new_session(client), "scan.pdf", make_scanned_pdf(), "application/pdf")
    assert providers.ocr.requests == [[2, 3]] and providers.vision.calls == 0


def test_figures_are_described_later_when_a_vision_provider_is_added(client, upload, providers):
    data = pdf_with_figures([31])
    first, _ = upload(new_session(client), "r.pdf", data, "application/pdf")
    assert first["status"] == "indexed" and not [c for c in chunks_of(first["id"]) if c[1] == "figure"]
    providers.vision = FakeVision()
    second, _ = upload(new_session(client), "r.pdf", data, "application/pdf")   # the earlier result was not final for figures
    assert providers.vision.calls == 1 and [c for c in chunks_of(second["id"]) if c[1] == "figure"]


# ---- standalone images ---------------------------------------------------------------------

def test_an_image_with_little_text_is_also_described_but_a_text_scan_is_not(client, upload, providers):
    providers.ocr, providers.vision = FakeOCR(texts={None: ""}), FakeVision()
    chart, _ = upload(new_session(client), "chart.png", noise_png(41), "image/png")
    assert chart["status"] == "indexed" and providers.vision.calls == 1
    assert [c[1] for c in chunks_of(chart["id"])] == ["figure"]

    providers.ocr = FakeOCR(texts={None: "A scanned letter. " + FACT})
    letter, _ = upload(new_session(client), "letter.png", noise_png(42), "image/png")
    assert providers.vision.calls == 1                                    # enough text was found: no second call
    assert "retained for 90 days" in chunks_of(letter["id"])[0][5]


# ---- audio ---------------------------------------------------------------------------------

def test_audio_becomes_timestamped_chunks_that_can_be_cited_and_is_cached(client, llm, upload, ask, providers):
    providers.asr = [FakeASR()]
    session, other = new_session(client), new_session(client)
    document, _ = upload(session, "talk.mp3", b"ID3 fake audio bytes", "audio/mpeg")
    assert document["status"] == "indexed"
    (chunk,) = chunks_of(document["id"])
    assert chunk[1] == "transcript" and chunk[3] == "00:01-00:09" and chunk[6]["start_s"] == 1.0 and chunk[6]["end_s"] == 9.0
    assert "A: " + FACT in chunk[5]                                       # speaker labels are kept
    result = ask(session, QUESTION)
    assert result["citations"][0]["locator"] == "00:01-00:09"

    upload(other, "talk-again.mp3", b"ID3 fake audio bytes", "audio/mpeg")
    assert providers.asr[0].calls == 1                                    # the same recording is not transcribed twice


def test_a_provider_over_quota_falls_back_to_the_next_one(client, upload, providers):
    exhausted, working = FakeASR("groq-like", QuotaExhausted("groq_whisper", "requests", "day", 3600)), FakeASR("assembly-like")
    providers.asr = [exhausted, working]
    document, _ = upload(new_session(client), "a.mp3", b"ID3-a", "audio/mpeg")
    assert document["status"] == "indexed" and (exhausted.calls, working.calls) == (1, 1)


def test_when_every_provider_is_over_quota_the_audio_waits_instead_of_failing(client, upload, providers):
    providers.asr = [FakeASR("one", QuotaExhausted("groq_whisper", "requests", "day", 3600)), FakeASR("two", QuotaExhausted("assemblyai", "requests", "day", 7200))]
    document, job = upload(new_session(client), "b.mp3", b"ID3-b", "audio/mpeg")
    assert document["status"] == "waiting_for_quota" and job["status"] == "paused"


def test_audio_nobody_can_transcribe_is_reported_clearly(client, upload, providers):
    document, _ = upload(new_session(client), "c.mp3", b"ID3-c", "audio/mpeg")                  # no provider configured
    assert document["status"] == "audio_unavailable" and document["details"]["info"]["plan"]["unavailable"] == ["asr"]
    providers.asr = [FakeASR("tiny-limit", SpecialistUnavailable("This file is 40 MB; Groq's free tier accepts up to 25 MB."))]
    document, _ = upload(new_session(client), "d.mp3", b"ID3-d", "audio/mpeg")
    assert document["status"] == "audio_unavailable" and "25 MB" in document["details"]["info"]["unavailable_reason"]


# ---- API and chat --------------------------------------------------------------------------

def test_quota_and_specialist_status_are_visible(client, monkeypatch):
    quota = {p["provider"]: p for p in client.get("/api/quota").json()["providers"]}
    assert quota["groq_whisper"]["limits"]["requests"]["day"]["limit"] == 2000
    monkeypatch.setattr(settings, "gemini_api_key", "g")
    monkeypatch.setattr(settings, "allow_free_tier_data_use", False)
    info = client.get("/api/specialists").json()
    assert info["vision"]["gemini_blocked_until_opt_in"] is True and info["confirm_above_calls"] == settings.confirm_above_calls


def test_a_spent_free_tier_limit_gets_a_clear_chat_reply(client, upload, monkeypatch):
    async def exhausted(provider, model, messages, **kwargs):
        raise QuotaExhausted("groq_chat", "requests", "day", 5400)

    monkeypatch.setattr("app.provider_service.raw_chat", exhausted)
    session = new_session(client)
    upload(session, "policy.txt", FACT.encode(), "text/plain")
    response = client.post("/api/chat", json={"session_id": session, "content": QUESTION, "provider": "nvidia", "model": MODEL}).json()
    assert response["route"] == "blocked" and "free-tier limit" in response["message"]["content"] and "1.5 h" in response["message"]["content"]
    assert response["message"]["intent"] == "QUOTA_EXHAUSTED"
