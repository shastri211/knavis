# KNAVIS: Knowledge Navigation Intelligence

A multimodal, multilingual agentic RAG system: a CPU-first FastAPI and React application
for evidence-grounded document Q&A.
The live chat path uses a LangGraph hierarchy: Supervisor -> Guardrails ->
Greeting/Utility/RAG. The RAG specialist has bounded retrieval attempts and
abstains when its session documents do not provide sufficient evidence.

## Setup

1. Install Python 3.11 and Node.js 20+.
2. Create a virtual environment and install backend packages:
   `python -m pip install -r backend/requirements.txt`
3. Copy `.env.example` to `.env`, then configure at least one chat provider.
   Add `NVIDIA_API_KEY` as well to enable dense embeddings; without it the app
   retains lexical BM25 retrieval over persisted evidence.
4. Check configuration without exposing secrets:
   `python scripts/check_config.py`
5. Start the backend: `scripts/start_backend.ps1`
6. In a second terminal start the frontend: `scripts/start_frontend.ps1`

Open `http://127.0.0.1:5173`.

## Supported Now

### File types

| Tier | Formats | Cost |
|---|---|---|
| Local, free | PDF, DOCX, PPTX, XLSX/XLSM, CSV/TSV, Markdown, TXT/log/config/source files, JSON, XML, HTML, SRT/VTT subtitles, EML | none |
| Legacy (needs LibreOffice installed) | DOC, ODT, RTF, PPT, ODP, XLS, ODS | none |
| Hosted OCR (`NVIDIA_API_KEY`) | PNG, JPG, WEBP, TIFF, BMP, GIF, and scanned PDF pages | one call per page/image |
| Hosted transcription (`ASSEMBLYAI_API_KEY`) | MP3, WAV, M4A, AAC, FLAC, OGG, MP4, WEBM | one job per file |

OCR and transcription are wired in but have not yet been verified against the live
services. Without a key, such files are marked `ocr_unavailable` / `audio_unavailable`
and are never presented as searchable. Not yet supported: chart/figure understanding,
handwriting quality tuning, e-mail attachments.

### How ingestion works

1. **Extract (local):** each format becomes structured elements (headings, paragraphs,
   tables, slides, sheet summaries, timed cues) with page / slide / sheet / row provenance.
   PDF pages with no text but a page-sized image are queued for OCR; blank pages are not.
2. **Chunk once:** structure-aware chunks (about 1,200 characters, never more than 1,800)
   with a `Section: A > B` breadcrumb. Tables split by rows with the header repeated;
   spreadsheets cite real sheet row numbers; slides stay whole.
3. **Cache by content hash:** the same file uploaded again (any session) reuses its
   extraction, including OCR/transcription already paid for. Identical text is never
   embedded twice, and repeated questions reuse their query embedding.
4. **Duplicates:** uploading the same file twice into one session returns the existing
   document instead of processing it again.

`GET /api/sessions/{id}/documents` returns each document's `details` (pages, estimated OCR
calls, chunk count, whether it came from the cache).

Sessions and messages persist in local SQLite. Qdrant local storage is used
only when NVIDIA embeddings are configured; set `QDRANT_URL` and
`QDRANT_API_KEY` for Qdrant Cloud. Documents remain session-scoped.

## Validation

Run `python scripts/validate_project.py` or `pytest` from `backend/`. Provider
connectivity is only attempted when its key is configured.
