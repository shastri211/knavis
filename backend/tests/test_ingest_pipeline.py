"""Ingestion pipeline behaviour: stored chunks, caches (no repeated paid work), duplicates, OCR."""
import io
import json

import fitz
import pytest

from conftest import FACT, MODEL, make_txt, make_pdf, make_pptx, make_xlsx

QUESTION = "How long must company data be retained after the contract ends?"


def chunks_of(document_id):
    from app.db import SessionLocal
    from app.models import DocChunk
    with SessionLocal() as db:
        return [(c.ordinal, c.kind, c.page, c.locator, c.section, c.text, c.metadata_json)
                for c in db.query(DocChunk).filter(DocChunk.document_id == document_id).order_by(DocChunk.ordinal)]


def new_session(client):
    return client.post("/api/sessions", json={"title": "t"}).json()["id"]


# ---- chunks are stored once, with provenance ---------------------------------------------

def test_chunks_are_stored_with_page_slide_sheet_and_locator(client, upload):
    pdf_doc, _ = upload(new_session(client), "policy.pdf", make_pdf(), "application/pdf")
    first = chunks_of(pdf_doc["id"])[0]
    assert first[2] == 1 and first[3] == "page 1" and "90 days" in first[5]

    pptx_doc, _ = upload(new_session(client), "policy.pptx", make_pptx(), "application/octet-stream")
    (slide_chunk,) = chunks_of(pptx_doc["id"])
    assert slide_chunk[1] == "slide" and slide_chunk[3] == "slide 1" and slide_chunk[6]["slide"] == 1

    xlsx_doc, _ = upload(new_session(client), "policy.xlsx", make_xlsx(), "application/octet-stream")
    kinds = [c[1] for c in chunks_of(xlsx_doc["id"])]
    assert kinds == ["summary", "table"]
    assert chunks_of(xlsx_doc["id"])[1][3].startswith("sheet Policy, rows")


def test_document_details_report_what_extraction_found(client, upload):
    document, _ = upload(new_session(client), "policy.pdf", make_pdf(), "application/pdf")
    details = document["details"]
    assert details["detected_type"] == "pdf" and details["chunks"] >= 1 and details["from_cache"] is False
    assert details["info"]["pages"] == 1 and details["info"]["estimated_ocr_calls"] == 0


def test_citations_carry_the_locator(client, llm, upload, ask):
    session = new_session(client)
    upload(session, "policy.pdf", make_pdf(), "application/pdf")
    result = ask(session, QUESTION)
    assert result["citations"][0]["locator"] == "page 1"


# ---- no repeated work --------------------------------------------------------------------

def test_the_same_file_in_the_same_session_is_not_processed_twice(client, llm, upload):
    session = new_session(client)
    first, _ = upload(session, "policy.txt", make_txt(), "text/plain")
    response = client.post("/api/uploads", data={"session_id": session}, files={"file": ("copy-of-policy.txt", make_txt(), "text/plain")})
    body = response.json()
    assert body["duplicate"] is True and body["document"]["id"] == first["id"]
    assert len(client.get(f"/api/sessions/{session}/documents").json()) == 1


def test_a_file_seen_before_is_not_extracted_again_in_another_session(client, llm, upload, ask, monkeypatch):
    import app.jobs as jobs
    calls = []
    real = jobs.extract_document
    monkeypatch.setattr(jobs, "extract_document", lambda *a, **k: calls.append(1) or real(*a, **k))

    data = make_pdf()
    first_session, second_session = new_session(client), new_session(client)
    first, _ = upload(first_session, "policy.pdf", data, "application/pdf")
    second, job = upload(second_session, "policy.pdf", data, "application/pdf")

    assert len(calls) == 1                                             # extracted once, reused once
    assert first["details"]["from_cache"] is False and second["details"]["from_cache"] is True
    assert job["stage"] == "indexed"
    # different documents, identical content, independent evidence ids
    assert [c[5] for c in chunks_of(first["id"])] == [c[5] for c in chunks_of(second["id"])]
    assert ask(second_session, QUESTION)["citations"]


# ---- OCR: only scanned pages, unique ids, cached ----------------------------------------

class FakeOCRProvider:
    """Stands in for a hosted OCR provider and records every request it receives."""
    name = "fake"
    requests: list[list[int]] = []
    texts = {2: "# Scanned policy\n\nCompany data must be retained for 90 days.", 3: "Second scanned page."}

    async def recognize(self, path, pages=None):
        from app.specialists.base import OCRPage
        FakeOCRProvider.requests.append(list(pages or []))
        for page in pages:
            yield OCRPage(page=page, text=self.texts[page], provider=self.name)


def make_scanned_pdf():
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "A normal text page with enough characters to need no OCR at all. " * 2)
    for _ in range(2):
        page = pdf.new_page()
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 500), False)
        pixmap.clear_with(200)
        page.insert_image(page.rect, pixmap=pixmap)
    data = pdf.tobytes()
    pdf.close()
    return data


def test_only_scanned_pages_are_ocrd_each_page_is_kept_and_the_result_is_cached(client, llm, upload, ask, monkeypatch):
    import app.specialists.run as run
    FakeOCRProvider.requests = []
    monkeypatch.setattr(run, "get_ocr_provider", lambda: FakeOCRProvider())

    data = make_scanned_pdf()
    first_session, second_session = new_session(client), new_session(client)
    document, job = upload(first_session, "scan.pdf", data, "application/pdf")

    assert FakeOCRProvider.requests == [[2, 3]]                        # page 1 has native text: never sent
    assert job["status"] == "completed" and document["status"] == "indexed"
    assert document["details"]["info"]["plan"]["ocr_pages"] == 2 and document["details"]["info"]["plan"]["calls"] == 2
    chunks = chunks_of(document["id"])
    assert {c[2] for c in chunks} == {1, 2, 3}                          # page 3 did not overwrite page 2
    by_page = {c[2]: c[5] for c in chunks}
    assert "Company data must be retained for 90 days." in by_page[2] and "Section: Scanned policy" in by_page[2]
    assert "Second scanned page." in by_page[3]

    again, _ = upload(second_session, "scan-copy.pdf", data, "application/pdf")
    assert FakeOCRProvider.requests == [[2, 3]] and again["details"]["from_cache"] is True   # re-upload: no OCR call

    llm.answer = "Company data must be retained for 90 days [EVIDENCE 1]."
    assert ask(second_session, "How many days must company data be retained for?")["citations"]


def test_without_an_ocr_provider_nothing_is_cached_so_a_later_upload_can_use_ocr(client, upload, monkeypatch):
    import app.specialists.run as run
    FakeOCRProvider.requests = []
    data = make_scanned_pdf()
    first, _ = upload(new_session(client), "scan.pdf", data, "application/pdf")
    assert first["status"] == "ocr_unavailable" and FakeOCRProvider.requests == []
    assert any(c[2] == 1 for c in chunks_of(first["id"]))              # the native page is searchable meanwhile

    monkeypatch.setattr(run, "get_ocr_provider", lambda: FakeOCRProvider())
    second, _ = upload(new_session(client), "scan.pdf", data, "application/pdf")
    assert second["status"] == "indexed" and FakeOCRProvider.requests == [[2, 3]]


# ---- spreadsheets -------------------------------------------------------------------------

def test_a_large_spreadsheet_is_findable_row_by_row_not_one_giant_chunk(client, llm, upload, ask):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Items"
    ws.append(["code", "region", "amount"])
    for i in range(3000):
        ws.append([f"item{i}", f"region-{i % 7}", i * 3])
    buf = io.BytesIO(); wb.save(buf)

    session = new_session(client)
    document, _ = upload(session, "items.xlsx", buf.getvalue())
    chunks = chunks_of(document["id"])
    assert len(chunks) > 20 and max(len(c[5]) for c in chunks) <= 1800
    assert all(c[5].startswith("code | region | amount") for c in chunks if c[1] == "table")

    llm.answer = "item2999 is in region-3 [EVIDENCE 1]."
    result = ask(session, "Which region is item2999 in?")
    assert "item2999 | region-3 | 8997" in llm.answer_prompt
    assert result["citations"][0]["locator"].startswith("sheet Items, rows ")


# ---- embeddings: cached, batched ----------------------------------------------------------

class FakeEmbeddings:
    model = "fake-embed"

    def __init__(self):
        self.requests = []

    async def embed(self, texts, input_type="passage"):
        from app.retrieval.embeddings import EmbeddingResult
        self.requests.append((input_type, list(texts)))
        return EmbeddingResult(vectors=[[float(len(t) % 7), 1.0, float(sum(map(ord, t)) % 11), 0.5] for t in texts], model=self.model)


def rows(n, prefix="chunk"):
    from types import SimpleNamespace
    return [SimpleNamespace(id=f"d:c{i}", text=f"{prefix} number {i} about retention", metadata_json={}, page=1,
                            locator="page 1", section=None, kind="paragraph", document_id="d") for i in range(n)]


@pytest.fixture
def dense_pipeline(tmp_path, monkeypatch):
    from app.config import settings
    from app.integration.pipeline import IntegratedRAGPipeline
    from app.retrieval.qdrant_store import QdrantStore
    monkeypatch.setattr(settings, "embedding_dimensions", 4)
    pipeline = IntegratedRAGPipeline()
    pipeline.qdrant = QdrantStore(path=str(tmp_path / "qdrant"))
    pipeline.embedding = FakeEmbeddings()
    return pipeline


async def test_embeddings_are_batched_and_never_computed_twice(dense_pipeline):
    items = rows(70, prefix="unique-first")
    stats = await dense_pipeline.index_chunks("s1", items, "a.txt")
    assert stats == {"cached": 0, "embedded": 70, "requests": 3}        # 70 texts -> 3 requests of <=32
    assert [len(t) for _, t in dense_pipeline.embedding.requests] == [32, 32, 6]

    again = await dense_pipeline.index_chunks("s2", items, "a.txt")      # same text, another session
    assert again == {"cached": 70, "embedded": 0, "requests": 0}
    assert len(dense_pipeline.embedding.requests) == 3                  # no further provider calls


async def test_repeated_questions_reuse_the_cached_query_embedding(dense_pipeline):
    await dense_pipeline.index_chunks("s1", rows(3, prefix="q"), "a.txt")
    before = len(dense_pipeline.embedding.requests)
    first = await dense_pipeline.dense_search("s1", "how long is retention", 3)
    await dense_pipeline.dense_search("s1", "how long is retention", 3)
    assert len(dense_pipeline.embedding.requests) == before + 1         # embedded once, answered twice
    assert first and "dense_score" in first[0] and first[0]["metadata"]["source"] == "a.txt"


# ---- older databases ----------------------------------------------------------------------

def test_documents_stored_before_chunks_existed_are_chunked_at_startup(client, llm, ask):
    """Older databases hold evidence rows only; the startup migration makes them searchable."""
    from app.ingest.store import migrate_legacy_documents
    from app.db import SessionLocal
    from app.models import ChatSession, Document, DocChunk, Evidence
    session = new_session(client)
    with SessionLocal() as db:
        doc = Document(session_id=session, filename="old.txt", content_type="text/plain", path="x", status="indexed")
        db.add(doc); db.flush()
        db.add(Evidence(id=f"{doc.id}:text", document_id=doc.id, kind="text", text=FACT, page=None, metadata_json={}))
        db.commit()
        doc_id = doc.id
    assert migrate_legacy_documents() >= 1
    assert migrate_legacy_documents() == 0   # idempotent
    result = ask(session, QUESTION)
    assert result["message"]["content"] == llm.answer and result["citations"][0]["source"] == "old.txt"
    with SessionLocal() as db:
        assert db.query(DocChunk).filter(DocChunk.document_id == doc_id).count() == 1
