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

Native extraction: PDF text, DOCX, PPTX, XLSX, CSV, JSON, TXT and Markdown.
Images, scanned-PDF pages, and audio are accepted but are marked as requiring a
specialist; OCR and AssemblyAI transcription are not yet wired into ingestion,
so those files are not falsely advertised as searchable.

Sessions and messages persist in local SQLite. Qdrant local storage is used
only when NVIDIA embeddings are configured; set `QDRANT_URL` and
`QDRANT_API_KEY` for Qdrant Cloud. Documents remain session-scoped.

## Validation

Run `python scripts/validate_project.py` or `pytest` from `backend/`. Provider
connectivity is only attempted when its key is configured.
