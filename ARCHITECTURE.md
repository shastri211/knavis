# Architecture

Frontend -> FastAPI -> safety -> language/semantic conversation -> bounded agent -> modality processing -> dense + SQLite FTS5 keyword search -> RRF (spreadsheet computations: text-to-SQL over per-session tables) -> rerank -> evidence gate -> selected user LLM -> citation validation -> output safety.

NVIDIA, Groq and OpenRouter are interchangeable generation providers. AssemblyAI is production ASR. OCR/vision/embeddings/reranking are specialist tasks, not user-selected arbitrary LLM calls.

Knowledge questions never receive an unrestricted general-knowledge fallback. If evidence is inadequate, abstain. Document content is untrusted data and cannot override policy.

Document scope: arbitrary/unknown uploads, PDFs/images, scans, forms, IDs, passports, visas, bank statements, tables, charts, DOCX/PPTX/XLSX/CSV/JSON/TXT, large PDFs and logical-document segmentation. No video processing in production scope.
