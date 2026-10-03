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
- **Huge documents ask before embedding.** Dense search embeds 32 chunks per request, and a 20 MB text file makes about
  17,000 chunks. A document that needs more than `MAX_EMBED_CHUNKS_PER_DOC` chunks embedded (default 1000; chunks whose
  vectors are cached cost nothing, so a re-upload never asks) pauses as `awaiting_confirmation` with the estimate (passages,
  requests) while it stays searchable by keyword. **Embed all** (`confirm`) embeds it; **Keyword only** (`skip`) indexes it
  without vectors, and keyword search works as usual. The choice is stored with the job, so a restart or a quota wait does not
  ask again, and **Reindex** starts over. `GET /api/quota` reports what is left of the embedding quota (`embedding`).
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
  spreadsheet tables, vectors and the uploaded files (from disk or from the bucket); `DELETE /api/documents/{id}` does the
  same for one document, and deleting an account does it for every chat. A vector store or bucket that is down never blocks
  the delete (the rows go; the failure is logged).
- **Files can live in a bucket.** `STORAGE_BACKEND=s3` keeps uploads and spreadsheet table files in any S3-compatible object
  store (`S3_*` settings), so several hosts share them; the default is the data directory, unchanged. Extraction reads a
  temporary copy of the object. See `DEPLOYMENT.md`.
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
used to abstain. Spreadsheets and CSV files are now also loaded, at ingestion, as real tables in a small SQLite file per
document (`tables/<session>/<document>-<token>.sqlite`, kept through the storage backend and cached on each host that
answers a question; no new dependency), with each sheet's real row numbers.

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

## Accounts and safety

- **Accounts:** register with an email and password (scrypt hashes; sign-in is a revocable bearer token stored hashed).
  Every chat, document and job belongs to its owner; other users get 404. `DELETE /api/auth/me` removes an account and
  everything it owns. The first account claims chats created before accounts existed. `AUTH_ENABLED=false` returns to
  the single-user, open mode for a purely local install.
- **Rate limits:** per user on chat and upload, per address and email on sign-in (`RATE_LIMIT_*`). The limiter is
  in-process, so run one worker.
- **Uploads:** read in chunks with a size cap; the real content type must match the extension (a program renamed
  `.pdf` is refused); Office files are checked for decompression bombs, PDFs for page count, images for pixel count;
  names are cleaned; chats and users have document and storage caps. See `backend/app/uploads.py`.
- **Interface:** sign-in screen, delete or rename chats, remove documents, Process/Skip/Retry for paused documents,
  upload errors, spreadsheet answers shown as tables with saved citations, and a drawer sidebar on phones.

## Operating and scaling

- **Restart-safe uploads:** unfinished ingestion jobs resume at start-up (three attempts, then a clear failure), keep the
  person's earlier decision, and run at most `INGESTION_CONCURRENCY` at a time.
- **Administration:** `python -m app.admin users | reset-password | create-user | revoke-tokens | delete-user | usage`
  (no mail service exists, so this is how a forgotten password is handled); people can also change their own password.
- **Several processes:** jobs are claimed with leases so several web processes and dedicated workers (`python -m app.worker`)
  share one database without running anything twice; rate limits can be shared through Redis (`REDIS_URL`); start-up is
  serialised. `docker-compose.scale.yml` runs the whole arrangement on one host.
- **Several hosts:** with PostgreSQL, a Qdrant server, Redis and `STORAGE_BACKEND=s3`, nothing is left on a host's disk but
  caches, so web processes and workers can run on different machines (`docker-compose.s3.yml` tries the object store locally).
- **Optional extras:** ClamAV scanning of uploads (`CLAMAV_HOST`, `docker-compose.scan.yml`), password reset by e-mail
  (`SMTP_HOST`) and confirmation of new accounts' addresses (`REQUIRE_EMAIL_VERIFICATION`). Macros, embedded programs and PDF
  JavaScript are always refused unless `ALLOW_ACTIVE_CONTENT=true`.
- **Migrations:** the schema is managed by Alembic and upgraded at start-up; installs from before Alembic are adopted
  with their data. **PostgreSQL** is supported (`DATABASE_URL`, or the `docker-compose.postgres.yml` overlay) with
  `python -m app.admin copy-database` to move an existing SQLite install. See `DEPLOYMENT.md`.

## Running in Docker

`docker compose up --build` serves the app on http://127.0.0.1:8080 (nginx in front of the backend, data in a named
volume). See `DEPLOYMENT.md` for exposing it safely and for the known limits. Optional overlays add PostgreSQL, a worker
fleet, ClamAV and an S3-compatible object store.

## Evaluation

`python -m app.evaluation.run` (from `backend/`) runs 56 labelled questions over a synthetic corpus without any model
or key and compares with a saved baseline; `--mode live --yes` asks them through a real provider and reports accuracy,
citations, abstention, model calls per question and latency. See `eval/README.md`.

## Validation

Run `python scripts/validate_project.py` or `pytest` from `backend/`. Provider
connectivity is only attempted when its key is configured.
