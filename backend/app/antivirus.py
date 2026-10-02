"""Optional virus scanning of uploads with ClamAV (the ``clamd`` daemon), used when CLAMAV_HOST is set.

The upload is streamed to clamd with the INSTREAM command over TCP, so nothing is written to disk first and no extra
dependency is needed. Behaviour when the scanner cannot be reached is a choice:

* ``CLAMAV_REQUIRED=true`` (recommended once you rely on it): uploads are refused until the scanner is back;
* otherwise the problem is logged and the upload continues (the other upload checks still apply).

clamd refuses streams larger than its ``StreamMaxLength`` (25 MB by default). Set it at least as high as MAX_UPLOAD_MB;
the bundled overlay (``docker-compose.scan.yml``) does.
"""
import logging
import socket
import struct
from dataclasses import dataclass

from .config import settings
from .guardrails import GuardrailError

logger = logging.getLogger("mragrag")

_CHUNK = 1 << 16


class ScanUnavailableError(GuardrailError):
    """Uploads are refused because the (required) scanner cannot be reached. Maps to HTTP 503."""


class ScannerUnavailable(RuntimeError):
    """The scanner could not be reached or gave an answer that means nothing was checked."""


@dataclass
class ScanResult:
    clean: bool
    signature: str | None = None


def enabled() -> bool:
    return bool(settings.clamav_host)


def scan(raw: bytes) -> ScanResult:
    """Scan ``raw`` with clamd. Raises ``ScannerUnavailable`` when it cannot be checked."""
    try:
        with socket.create_connection((settings.clamav_host, settings.clamav_port), timeout=settings.clamav_timeout) as sock:
            sock.settimeout(settings.clamav_timeout)
            sock.sendall(b"zINSTREAM\0")
            for start in range(0, len(raw), _CHUNK):
                part = raw[start:start + _CHUNK]
                sock.sendall(struct.pack(">I", len(part)) + part)
            sock.sendall(struct.pack(">I", 0))
            reply = b""
            while not reply.endswith(b"\0") and (more := sock.recv(4096)):
                reply += more
    except OSError as exc:
        raise ScannerUnavailable(f"{type(exc).__name__}: {exc}") from exc
    answer = reply.rstrip(b"\0\n").decode("utf-8", "replace")
    if answer.endswith("OK"):
        return ScanResult(True)
    if answer.endswith("FOUND"):
        return ScanResult(False, answer.split(":", 1)[-1].replace("FOUND", "").strip() or "unknown")
    raise ScannerUnavailable(f"unexpected scanner answer: {answer[:120]!r}")


def check_upload(raw: bytes) -> None:
    """Raise ``GuardrailError`` for an infected file or (when required) an unreachable scanner. A no-op when disabled."""
    if not enabled():
        return
    try:
        result = scan(raw)
    except ScannerUnavailable as exc:
        if settings.clamav_required:
            logger.error("Virus scanner unavailable, upload refused: %s", exc)
            raise ScanUnavailableError("Uploads are paused because the virus scanner is not available. Try again shortly.") from exc
        logger.warning("Virus scanner unavailable, upload accepted without a scan: %s", exc)
        return
    if not result.clean:
        logger.warning("Upload refused by the virus scanner: %s", result.signature)
        raise GuardrailError(f"This file was blocked by the virus scanner ({result.signature}).")

