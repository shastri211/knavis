"""Legacy binary formats (.doc, .ppt, .xls, .odt, .rtf, ...) via LibreOffice, when installed."""
import os
import shutil
import subprocess
from pathlib import Path

from ..elements import ExtractionError

_WINDOWS_PATHS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


def find_soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return next((p for p in _WINDOWS_PATHS if os.path.exists(p)), None)


def convert_legacy(path: Path, target_ext: str, out_dir: Path) -> Path:
    exe = find_soffice()
    if not exe:
        raise ExtractionError(
            f"{path.suffix} files need LibreOffice to be converted. Install LibreOffice, "
            f"or save the file as {target_ext} and upload that instead."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [exe, "--headless", "--convert-to", target_ext.lstrip("."), "--outdir", str(out_dir), str(path)],
            capture_output=True, timeout=180, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"Converting this {path.suffix} file timed out.") from exc
    converted = out_dir / f"{path.stem}{target_ext}"
    if not converted.exists():
        raise ExtractionError(f"This {path.suffix} file could not be converted. Try saving it as {target_ext}.")
    return converted
