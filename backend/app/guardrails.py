from pathlib import Path
from .config import settings

from .ingest.registry import ALLOWED_EXTENSIONS   # the one list of supported file types

class GuardrailError(ValueError):
    pass

def validate_message(text: str):
    if not text or not text.strip():
        raise GuardrailError("Message is empty.")
    if len(text) > settings.max_message_chars:
        raise GuardrailError("Message exceeds the configured length limit.")

def validate_upload(filename: str, size: int):
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise GuardrailError(f"Unsupported file type: {ext or 'unknown'}")
    if size > settings.max_upload_mb * 1024 * 1024:
        raise GuardrailError("File exceeds the configured upload size limit.")

def sanitize_error(exc: Exception) -> str:
    # Never expose provider keys, tracebacks, or local filesystem details.
    return "The operation could not be completed safely. Check the job status or server logs."
