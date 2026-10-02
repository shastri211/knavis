"""Dispatch a file to its extractor. Local, deterministic, and free: no model is called here."""
from pathlib import Path

from .elements import ExtractionError, ExtractionResult
from .extractors.legacy import convert_legacy
from .extractors.office import extract_docx, extract_pptx
from .extractors.pdf import extract_pdf
from .extractors.tabular import extract_csv, extract_xlsx
from .extractors.textual import (
    extract_email, extract_html, extract_json, extract_markdown, extract_subtitles, extract_text, extract_xml,
)
from .registry import format_of

# Bumped whenever extraction output changes, so cached extractions are never reused across versions.
EXTRACTOR_VERSION = "2"

_EXTRACTORS = {
    "pdf": extract_pdf, "docx": extract_docx, "pptx": extract_pptx, "xlsx": extract_xlsx, "csv": extract_csv,
    "text": extract_text, "markdown": extract_markdown, "json": extract_json, "xml": extract_xml,
    "html": extract_html, "subtitles": extract_subtitles, "email": extract_email,
}


def extract_document(path: Path, work_dir: Path | None = None) -> ExtractionResult:
    """Extract structured elements from ``path``.

    Images and audio need a hosted specialist (OCR / speech-to-text); they return no elements
    and an ``info`` entry the caller uses to decide whether to spend a call.
    """
    path = Path(path)
    fmt = format_of(path)
    if fmt is None:
        raise ExtractionError(f"Unsupported file type: {path.suffix or 'none'}")
    if fmt.kind == "image":
        return ExtractionResult("image", [], {"needs_ocr": True, "estimated_ocr_calls": 1})
    if fmt.kind == "audio":
        return ExtractionResult("audio", [], {"needs_asr": True, "estimated_asr_calls": 1})

    source = path
    if fmt.tier == "legacy":
        source = convert_legacy(path, fmt.convert_to, work_dir or path.parent / "converted")
    result = _EXTRACTORS[fmt.kind](source)
    if source is not path:
        result.info["converted_from"] = path.suffix.lower()
    return result
