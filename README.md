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
   keeps answering from persistent keyword search (SQLite FTS5).
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
| Hosted OCR (`MISTRAL_API_KEY`, else `NVIDIA_API_KEY`) | PNG, JPG, WEBP, TIFF, BMP, GIF, and scanned PDF pages | Mistral: billed per scanned page; NVIDIA: one call per page |
| Hosted transcription (`GROQ_API_KEY` for files up to 25 MB, else `ASSEMBLYAI_API_KEY`) | MP3, WAV, M4A, AAC, FLAC, OGG, MP4, WEBM | one request per file |
| Hosted figure description (`GROQ_API_KEY`) | charts, graphs, diagrams and photos inside PDF, DOCX, PPTX, and low-text images | one request per distinct figure |

Charts inside PowerPoint are native objects: their data is read locally with no model call.

### Hosted specialists and the free tiers

These are the parts that spend free-tier quota, so they are built to spend as little as possible:

- **Only what is needed is sent.** A PDF with 3 scanned pages among 40 uploads a 3-page PDF
  (less data leaves the machine, and Mistral bills 3 pages). Blank pages, tiny images, repeated
  logos and scanned pages (read by OCR, not described as figures) are never sent.
- **Every result is cached per unit** (OCR page, figure, recording), so a retry, a quota pause or
  a re-upload never pays twice for the same unit.
- **A quota governor** counts usage against each provider's limits (persisted, so a restart does
  not forget today's spending), waits out per-minute windows, puts a provider on cooldown after a
  429, and *pauses* the document (`waiting_for_quota`) instead of failing. See `GET /api/quota`.
  The built-in limits are conservative placeholders (only Groq Whisper's are published); set your
  account's real limits with `QUOTA_OVERRIDES`.
- **Big jobs ask first.** A file needing more than `CONFIRM_ABOVE_CALLS` hosted calls (default
  25) shows `awaiting_confirmation` with the estimate; **Process** runs it, **Skip** keeps just the
  text that is already searchable. Native text is indexed before any hosted call is made.
- **Privacy:** Gemini's free tier uses submitted content to improve Google's products, so Gemini is
  never used unless you set `ALLOW_FREE_TIER_DATA_USE=true`. Check the data terms of every provider
  before sending sensitive documents.
- **Provider model ids change.** Run `python scripts/check_config.py --online` to check them against
  the providers' catalogs, and `python scripts/live_smoke.py --yes` to exercise each configured
  specialist once with tiny synthetic inputs (the unit tests use mocks and cannot prove a live key works).

Without a provider, such files are marked `ocr_unavailable` / `audio_unavailable`, are never
presented as searchable, and can be retried (`POST /api/documents/{id}/process`) after configuring one.
Not yet supported: handwriting quality tuning, e-mail attachments, video frames.

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

Sessions and messages persist in local SQLite (answers keep their citations when a chat is reopened).
Qdrant local storage is used only when NVIDIA embeddings are configured; set `QDRANT_URL` and
`QDRANT_API_KEY` for Qdrant Cloud. Documents remain session-scoped.

## Retrieval and storage

- **Keyword search is persistent.** Chunks are indexed in a SQLite FTS5 table (`chunks_fts`) that is written in
  the same transaction as the chunks, so it follows ingestion, re-chunking and deletion. Nothing is rebuilt per
  question. Text is reduced to the same stemmed terms the evidence gate uses, so Hindi words stay whole and
  "retained" finds "retain". Databases from before the index existed are re-indexed once at startup.
- **One vector collection.** Every session shares the Qdrant collection `knavis_chunks`
  (`QDRANT_COLLECTION` to rename it); each point carries `session_id` and `document_id` and every search is
  filtered by session, with a payload index on both on a Qdrant server. A session that still has its own
  `session_<id>` collection is moved into the shared one the first time it is used (same point ids, so an
  interrupted move is simply repeated), then the old collection is dropped.
- **Deleting is complete.** `DELETE /api/sessions/{id}` removes messages, documents, chunks, keyword-index rows,
  spreadsheet tables, vectors and the uploaded files; `DELETE /api/documents/{id}` does the same for one
  document. A vector store that is down never blocks the delete (the rows go; the failure is logged).
- **Schema changes are additive.** Startup adds any new nullable model column to an existing database
  (`app/migrations.py`), which is how `messages.citations` appeared without a migration tool.

## One model call per question

Rules, not a model, route a turn: greetings, thanks, "what can you do", the date, time and day, and prompt
injection are handled for free. Once a session has documents, every other message is a grounded document
question (the evidence gate abstains when the documents do not cover it; it is never answered from general
knowledge), so a normal question costs exactly one model call: the answer. Only a session with no documents
still asks the model to tell chat from a question about files that were never uploaded.

## Spreadsheet analytics

Retrieval cannot compute, so "which channel had the highest number of conversions" or "total sales by region"
used to abstain. Spreadsheets and CSV files are now also loaded, at ingestion, as real tables in a per-session
SQLite file (`<data dir>/tables/<session>.sqlite`; no new dependency), with each sheet's real row numbers.

An analytical question is recognised by rules (a total, average, count, "how many", highest/lowest, "by" or
"per" a column, a numeric filter) *and* a match with a column, a value of a column or the table; with only
spreadsheets in the session, a computing word alone is enough. Then:

1. **One model call** writes a single SQLite `SELECT` from the table schemas (column names, types, the values of
   low-cardinality columns, ranges, 0/1 flags, an example row).
2. **The query is validated strictly:** one statement, `SELECT`/`WITH` only, no comments, and SQLite's own
   authorizer allows reads of the session's own tables and columns and a short list of functions only (no
   `PRAGMA`, `ATTACH`, writes, recursive queries or extensions, however they are spelt). It runs on a
   read-only connection with a time limit and a row cap, and an unknown `"name"` is an error, not a string.
3. **The answer is rendered from the rows**, not written by the model: a lead sentence, the result table, and a
   source line naming the file, the sheet, the rows and the columns the query actually read. The one label the
   model contributes is dropped if it states a number the result does not contain.

If the query cannot be validated or run, the answer is a safe "I couldn't compute that reliably" abstention.
If the model decides the question is not about the tables and the session also has other documents, retrieval
answers it instead. Spreadsheet text stays searchable for lookups.

Settings: `ANALYTICS_ENABLED`, `ANALYTICS_TIMEOUT_SECONDS`, `ANALYTICS_MAX_ROWS` (see `.env.example`).

## Validation

Run `python scripts/validate_project.py` or `pytest` from `backend/`. Provider
connectivity is only attempted when its key is configured.
