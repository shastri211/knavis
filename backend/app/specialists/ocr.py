"""OCR providers. Both yield one ``OCRPage`` per page as soon as it is read, so the caller can store
each result before the next call (a quota pause or failure then loses nothing already paid for)."""
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

import httpx

from ..config import settings
from ..multimodal.ocr import NVIDIAOCRClient
from ..multimodal.page_render import render_page_png, subset_pdf
from ..reliability.hosted import get_governor, hosted_call
from .base import OCRPage, SpecialistUnavailable

IMAGE_MIME_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".bmp": "image/bmp", ".gif": "image/gif",
}


BATCH_WAIT_SECONDS = 70.0


class OCRProvider(Protocol):
    name: str

    def recognize(self, path: Path, pages: list[int] | None) -> AsyncIterator[OCRPage]: ...


def _batches(items: list[int], size: int | None) -> list[list[int]]:
    size = max(1, size or len(items) or 1)
    return [items[i:i + size] for i in range(0, len(items), size)]


class MistralOCR:
    """Mistral OCR. One request covers many pages and only the scanned ones are sent,
    so a 40-page PDF with 3 scanned pages is billed for 3 pages.

    Flow per batch: upload a PDF made of just those pages (purpose "ocr"), get a short-lived signed
    URL, call POST /v1/ocr with it, then delete the upload (Mistral otherwise keeps uploads for 30
    days). The request/response shape follows Mistral's OCR docs; it has not been exercised against
    a live key.
    """
    name = "mistral"
    quota_provider = "mistral_ocr"

    def __init__(self, api_key: str, base_url: str = "https://api.mistral.ai/v1", model: str = "mistral-ocr-latest", client_factory=None):
        self.api_key, self.base_url, self.model = api_key, base_url.rstrip("/"), model
        self.client_factory = client_factory or httpx.AsyncClient

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _upload(self, client, filename: str, content: bytes) -> str:
        response = await client.post(
            f"{self.base_url}/files", headers=self._auth,
            files={"file": (filename, content)}, data={"purpose": "ocr"},
        )
        response.raise_for_status()
        return response.json()["id"]

    async def _signed_url(self, client, file_id: str) -> str:
        response = await client.get(f"{self.base_url}/files/{file_id}/url", headers=self._auth, params={"expiry": 1})
        response.raise_for_status()
        return response.json()["url"]

    async def _ocr(self, client, url: str, is_pdf: bool) -> dict:
        body = {
            "model": self.model,
            "document": {"type": "document_url", "document_url": url} if is_pdf else {"type": "image_url", "image_url": url},
            "table_format": "markdown",
            "include_image_base64": False,
        }
        response = await client.post(f"{self.base_url}/ocr", headers=self._auth, json=body)
        response.raise_for_status()
        return response.json()

    async def recognize(self, path: Path, pages: list[int] | None = None) -> AsyncIterator[OCRPage]:
        is_pdf = path.suffix.lower() == ".pdf"
        if is_pdf and not pages:
            raise ValueError("Pass the pages to read; an entire PDF is never sent to OCR by default.")
        batches = _batches(pages, get_governor().max_units(self.quota_provider, "pages")) if is_pdf else [[]]
        async with self.client_factory(timeout=180) as client:
            for batch in batches:
                # Only the pages that need OCR are uploaded: a smaller request, less data leaving the
                # machine, and the provider bills just these pages.
                if is_pdf:
                    filename, content = "pages.pdf", await asyncio.to_thread(subset_pdf, path, batch)
                else:
                    filename, content = path.name, path.read_bytes()
                file_id = await hosted_call(self.quota_provider, lambda: self._upload(client, filename, content))
                try:
                    url = await hosted_call(self.quota_provider, lambda: self._signed_url(client, file_id))
                    data = await hosted_call(
                        self.quota_provider, lambda: self._ocr(client, url, is_pdf), units={"pages": max(1, len(batch))},
                        max_wait=BATCH_WAIT_SECONDS,   # a background job may wait out a minute window between batches
                    )
                    for item in data.get("pages", []):
                        index = item["index"]          # position inside the uploaded subset, from 0
                        yield OCRPage(
                            page=batch[index] if is_pdf and index < len(batch) else None,
                            text=(item.get("markdown") or "").strip(), provider=self.name,
                        )
                finally:
                    try:   # best effort: do not leave the user's document on the provider
                        await client.delete(f"{self.base_url}/files/{file_id}", headers=self._auth)
                    except httpx.HTTPError:
                        pass


class NvidiaOCR:
    """NVIDIA hosted OCR: one image per request, so one quota unit per page."""
    name = "nvidia"
    quota_provider = "nvidia_ocr"

    def __init__(self, api_key: str, base_url: str, client_factory=None):
        self.client = NVIDIAOCRClient(api_key, base_url, client_factory=client_factory)

    async def recognize(self, path: Path, pages: list[int] | None = None) -> AsyncIterator[OCRPage]:
        is_pdf = path.suffix.lower() == ".pdf"
        if is_pdf and not pages:
            raise ValueError("Pass the pages to read; an entire PDF is never sent to OCR by default.")
        for page in (pages if is_pdf else [None]):
            if is_pdf:
                image, mime = await asyncio.to_thread(render_page_png, path, page), "image/png"
            else:
                image, mime = path.read_bytes(), IMAGE_MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
            detections = await hosted_call(self.quota_provider, lambda i=image, m=mime, p=page: self.client.image_bytes(i, m, p))
            kept = [d for d in detections if d.text.strip()]
            confidences = [d.confidence for d in kept if d.confidence is not None]
            yield OCRPage(
                page=page, text="\n\n".join(d.text.strip() for d in kept), provider=self.name,
                regions=[{"text": d.text.strip(), "bbox": d.bbox, "confidence": d.confidence} for d in kept],
                confidence=sum(confidences) / len(confidences) if confidences else None,
            )


def get_ocr_provider() -> OCRProvider | None:
    """Mistral when its key is set, otherwise NVIDIA; ``OCR_PROVIDER`` pins one, ``none`` disables OCR."""
    choice = settings.ocr_provider.lower()
    if choice in ("auto", "mistral") and settings.mistral_api_key:
        return MistralOCR(settings.mistral_api_key)
    if choice in ("auto", "nvidia") and settings.nvidia_api_key:
        return NvidiaOCR(settings.nvidia_api_key, settings.nvidia_ocr_base_url)
    return None
