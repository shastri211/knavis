"""Checks applied to an upload before anything is stored or parsed.

An upload is untrusted. This module decides, from the bytes alone, whether it may be accepted:

* it is read in chunks and abandoned the moment it passes the size limit (never held whole in memory);
* its content must look like what its extension claims (a program renamed ``.pdf``, or a ``.txt`` full of
  binary, is refused);
* archives (Office files are zip files) are checked for decompression bombs before they are opened;
* PDFs and images are checked for page count and pixel count, because a tiny file can describe an enormous one.

Nothing here parses a document for its text; that is the extractors' job, after these checks pass.
"""
import io
import re
import unicodedata
import zipfile
from pathlib import Path

from fastapi import UploadFile

from .config import settings
from .guardrails import GuardrailError
from .ingest.registry import format_of

_CHUNK = 1 << 20
_NAME_LIMIT = 120

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"          # legacy .doc / .xls / .ppt
_ZIP = (b"PK\x03\x04", b"PK\x05\x06")
_IMAGE_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",), ".gif": (b"GIF87a", b"GIF89a"),
    ".tif": (b"II*\x00", b"MM\x00*"), ".tiff": (b"II*\x00", b"MM\x00*"), ".bmp": (b"BM",),
}
_FOREIGN = (b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"%PDF", b"PK\x03\x04", _OLE, b"\x89PNG", b"\xff\xd8\xff")


_PROGRAM_SUFFIXES = (".exe", ".dll", ".scr", ".com", ".bat", ".cmd", ".msi", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".ps1",
                     ".jar", ".lnk", ".hta", ".cpl", ".sys")
_PDF_ACTIVE_RE = re.compile(r"/(JavaScript|JS|Launch)\b")
_OLE_MACRO_MARKER = "_VBA_PROJECT".encode("utf-16-le")      # a macro project's directory entry in a legacy Office file


class UploadTooLarge(GuardrailError):
    """Maps to HTTP 413."""


def clean_filename(name: str | None) -> str:
    """A display-safe file name: no path, no control or direction-override characters, bounded length."""
    name = Path((name or "").replace("\\", "/")).name
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn")).strip(" .")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if len(stem) + len(ext) + 1 > _NAME_LIMIT:
        stem = stem[: _NAME_LIMIT - len(ext) - 1]
    cleaned = f"{stem}.{ext}" if ext else stem
    return cleaned if cleaned.strip(".") else "upload.bin"


async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload without ever holding more than ``max_bytes`` + one chunk."""
    parts, total = [], 0
    while chunk := await file.read(_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLarge(f"File exceeds the upload size limit of {settings.max_upload_mb} MB.")
        parts.append(chunk)
    return b"".join(parts)


def _looks_like_audio_or_video(head: bytes) -> bool:
    return (
        head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)   # mp3 / aac frame sync
        or (head[:4] == b"RIFF" and head[8:12] == b"WAVE") or head[4:8] == b"ftyp"            # wav, m4a / mp4
        or head[:4] in (b"fLaC", b"OggS", b"\x1a\x45\xdf\xa3")                                # flac, ogg, webm
    )


def _signature_ok(kind: str, ext: str, head: bytes) -> bool:
    if kind == "pdf":
        return b"%PDF-" in head[:1024]
    if kind in ("docx", "pptx", "xlsx"):
        if ext in (".doc", ".ppt", ".xls"):
            return head.startswith(_OLE) or head.startswith(_ZIP)
        if ext == ".rtf":
            return head.lstrip().startswith(b"{\\rtf")
        return head.startswith(_ZIP)
    if kind == "image":
        if ext == ".webp":
            return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        return head.startswith(_IMAGE_SIGNATURES.get(ext, ()))
    if kind == "audio":
        return _looks_like_audio_or_video(head)
    return True


def _check_text(head: bytes, ext: str) -> None:
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):   # UTF-16 text legitimately contains NUL bytes
        return
    if head.startswith(_FOREIGN) or b"\x00" in head:
        raise GuardrailError(f"This does not look like a {ext} text file; its content is binary.")


def check_archive(raw: bytes) -> None:
    """Refuse corrupt zips and decompression bombs before an Office file is opened."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise GuardrailError("This file is corrupted or is not a valid Office document.") from exc
    if len(entries) > settings.max_archive_entries:
        raise GuardrailError("This file contains too many parts to process safely.")
    unpacked = sum(e.file_size for e in entries)
    packed = max(1, sum(e.compress_size for e in entries))
    if unpacked > settings.max_unpacked_mb * 1024 * 1024 or (unpacked > 50 * 1024 * 1024 and unpacked / packed > 150):
        raise GuardrailError("This file expands to an unsafe size and was refused.")
    if not settings.allow_active_content:
        names = [e.filename.lower() for e in entries]
        if any(n.endswith("vbaproject.bin") or n.startswith(("basic/", "scripts/")) for n in names):
            raise GuardrailError("This document contains macros, which are not accepted. Save a copy without macros "
                                 "(for example as a plain .docx or .xlsx) and upload that.")
        if any(n.endswith(_PROGRAM_SUFFIXES) for n in names):
            raise GuardrailError("This document has a program embedded in it, which is not accepted.")


def check_pdf(raw: bytes) -> None:
    import fitz
    try:
        with fitz.open(stream=raw, filetype="pdf") as pdf:
            pages = pdf.page_count
    except Exception as exc:
        raise GuardrailError("This PDF could not be read; it may be corrupted.") from exc
    if pages > settings.max_pdf_pages:
        raise GuardrailError(f"This PDF has {pages} pages; the limit is {settings.max_pdf_pages}.")
    if not settings.allow_active_content and _pdf_has_active_content(raw):
        raise GuardrailError("This PDF contains JavaScript or launch actions, which are not accepted. "
                             "Print it to a new PDF without them and upload that.")


def _pdf_has_active_content(raw: bytes) -> bool:
    """JavaScript and launch actions, looked for in every PDF object (including those inside compressed object streams)."""
    import fitz
    with fitz.open(stream=raw, filetype="pdf") as pdf:
        for xref in range(1, min(pdf.xref_length(), 200_000)):
            try:
                if _PDF_ACTIVE_RE.search(pdf.xref_object(xref, compressed=True)):
                    return True
            except Exception:   # an unreadable object is not evidence of anything
                continue
    return False


def check_image(raw: bytes) -> None:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
    except Exception as exc:
        raise GuardrailError("This image could not be read; it may be corrupted.") from exc
    if width * height > settings.max_image_megapixels * 1_000_000:
        raise GuardrailError("This image is too large to process safely.")


def inspect_upload(filename: str, raw: bytes) -> None:
    """Raise ``GuardrailError`` unless the bytes are what the extension says and are safe to hand to an extractor."""
    path = Path(filename)
    ext = path.suffix.lower()
    fmt = format_of(path)
    if fmt is None:
        raise GuardrailError(f"Unsupported file type: {ext or 'unknown'}")
    if not raw:
        raise GuardrailError("The file is empty.")
    head = raw[:4096]
    if not _signature_ok(fmt.kind, ext, head):
        raise GuardrailError(f"The content of this file does not match its {ext} extension.")
    if head.startswith(_OLE) and not settings.allow_active_content and _OLE_MACRO_MARKER in raw:
        raise GuardrailError("This document contains macros, which are not accepted. Save a copy without macros and upload that.")
    if fmt.kind not in ("pdf", "docx", "pptx", "xlsx", "image", "audio"):
        _check_text(head, ext)
    if fmt.kind == "pdf":
        check_pdf(raw)
    elif fmt.kind == "image":
        check_image(raw)
    elif head.startswith(_ZIP) and fmt.kind in ("docx", "pptx", "xlsx"):
        check_archive(raw)
