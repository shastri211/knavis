"""The single list of supported file types (used for upload validation and dispatch)."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Format:
    kind: str                 # extractor family
    tier: str = "native"      # native: local, free | ocr / asr: needs a hosted specialist | legacy: needs conversion
    convert_to: str | None = None   # for legacy formats: the modern extension to convert to


def _group(kind: str, extensions: str, **kw) -> dict[str, Format]:
    return {ext: Format(kind, **kw) for ext in extensions.split()}


FORMATS: dict[str, Format] = {
    **_group("pdf", ".pdf"),
    **_group("docx", ".docx"),
    **_group("pptx", ".pptx"),
    **_group("xlsx", ".xlsx .xlsm"),
    **_group("csv", ".csv .tsv"),
    **_group("markdown", ".md .markdown"),
    **_group("text",
             ".txt .log .rst .tex .ini .cfg .conf .toml .yaml .yml "
             ".py .js .ts .java .c .cpp .h .cs .go .rs .rb .php .sql .sh .bat .ps1"),
    **_group("json", ".json"),
    **_group("html", ".html .htm"),
    **_group("xml", ".xml"),
    **_group("subtitles", ".srt .vtt"),
    **_group("email", ".eml"),
    **_group("image", ".png .jpg .jpeg .webp .tif .tiff .bmp .gif", tier="ocr"),
    **_group("audio", ".mp3 .wav .m4a .aac .flac .ogg .mp4 .webm", tier="asr"),
    # Legacy binary formats: converted with LibreOffice when it is installed.
    **_group("docx", ".doc .odt .rtf", tier="legacy", convert_to=".docx"),
    **_group("pptx", ".ppt .odp", tier="legacy", convert_to=".pptx"),
    **_group("xlsx", ".xls .ods", tier="legacy", convert_to=".xlsx"),
}

ALLOWED_EXTENSIONS = frozenset(FORMATS)


def format_of(path: Path | str) -> Format | None:
    return FORMATS.get(Path(path).suffix.lower())


def detect_type(path: Path | str) -> str:
    fmt = format_of(path)
    return fmt.kind if fmt else "unknown"
