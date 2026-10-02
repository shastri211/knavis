"""Decide what hosted work a document needs, and run it with per-unit caching.

``build_plan`` is cheap and local: it says how many OCR pages, figure descriptions and
transcriptions are still unpaid for (cached results are not counted), so the caller can ask for
confirmation before spending quota. ``execute_plan`` stores every result as soon as it arrives; if
the quota runs out part-way, ``QuotaExhausted`` propagates and the next run continues where this one stopped.
"""
import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..ingest.elements import FIGURE, TRANSCRIPT, Element, format_clock
from ..ingest.extractors.textual import markdown_elements
from ..ingest.figures import Figure, collect_figures, normalize_png
from ..reliability.governor import QuotaExhausted
from .asr import ASRProvider, asr_candidates
from .base import ASRResult, FigureDescription, OCRPage, SpecialistUnavailable
from .cache import asr_key, figure_key, get_unit, ocr_key, put_unit
from .ocr import OCRProvider, get_ocr_provider
from .vision import VisionProvider, get_vision_provider

# A standalone image whose OCR text is shorter than this is probably a chart or a photo: describe it too.
SPARSE_OCR_CHARS = 40


@dataclass
class Plan:
    kind: str
    ocr_pages: list[int] = field(default_factory=list)       # every page that needs OCR (PDF)
    ocr_image: bool = False                                  # a standalone image needs OCR
    figures: list[Figure] = field(default_factory=list)      # figure candidates (vision), capped
    asr: bool = False
    ocr: OCRProvider | None = None
    vision: VisionProvider | None = None
    asr_providers: list[ASRProvider] = field(default_factory=list)
    pending_ocr: list[int] = field(default_factory=list)     # pages with no cached result
    pending_figures: list[Figure] = field(default_factory=list)
    pending_asr: bool = False
    unavailable: list[str] = field(default_factory=list)     # hosted work needed but not possible

    @property
    def calls(self) -> int:
        """Hosted calls still to be paid for (cached units cost nothing)."""
        return (
            (len(self.pending_ocr) if self.ocr else 0)
            + (len(self.pending_figures) if self.vision else 0)
            + (1 if self.pending_asr and self.asr_providers else 0)
        )

    @property
    def needed(self) -> bool:
        return bool(self.ocr_pages or self.ocr_image or self.figures or self.asr)

    def summary(self) -> dict:
        return {
            "ocr_pages": len(self.ocr_pages), "figures": len(self.figures), "audio": self.asr,
            "calls": self.calls, "providers": {
                "ocr": self.ocr.name if self.ocr else None, "vision": self.vision.name if self.vision else None,
                "asr": [p.name for p in self.asr_providers] or None},
            "unavailable": self.unavailable,
        }


async def build_plan(db: Session, path: Path, kind: str, info: dict, elements: list[Element], content_hash: str) -> Plan:
    plan = Plan(kind=kind, ocr=get_ocr_provider(), vision=get_vision_provider())
    if kind == "pdf":
        plan.ocr_pages = list(info.get("ocr_pages", []))
        plan.pending_ocr = [p for p in plan.ocr_pages if get_unit(db, ocr_key(content_hash, p)) is None]
    elif kind == "image":
        plan.ocr_image = True
        plan.pending_ocr = [0] if get_unit(db, ocr_key(content_hash, None)) is None else []
    elif kind == "audio":
        plan.asr = True
        plan.asr_providers = asr_candidates(path)
        plan.pending_asr = get_unit(db, asr_key(content_hash)) is None

    if kind in ("pdf", "docx", "pptx") and plan.vision:
        try:
            plan.figures = await asyncio.to_thread(collect_figures, path, kind, info, elements, settings.vision_max_figures_per_doc)
        except Exception:   # figure description is optional: a file whose images cannot be read still ingests
            plan.figures = []
            info["figures_error"] = "Figures could not be read from this file; its text was indexed."
        plan.pending_figures = [f for f in plan.figures if get_unit(db, figure_key(f.sha)) is None]

    if plan.pending_ocr and not plan.ocr:
        plan.unavailable.append("ocr")
    if plan.pending_asr and not plan.asr_providers:
        plan.unavailable.append("asr")
    return plan


# ---- results -> elements -------------------------------------------------------------------

def ocr_elements(page: OCRPage) -> list[Element]:
    where = f"page {page.page}" if page.page else "image"
    prefix = f"p{page.page}:ocr" if page.page else "ocr"
    common = {"page": page.page, "locator": where, "source": "ocr", "meta": {"ocr_provider": page.provider}}
    if page.regions:   # providers that return geometry keep a region (paragraph) per element
        return [
            Element(id=f"{prefix}{i}", kind="paragraph", text=r["text"], bbox=r.get("bbox"), confidence=r.get("confidence"), **common)
            for i, r in enumerate(page.regions) if r.get("text", "").strip()
        ]
    return markdown_elements(page.text, id_prefix=prefix, confidence=page.confidence, **common)


def figure_element(figure: Figure, description: FigureDescription) -> Element:
    return Element(
        id=figure.key, kind=FIGURE, text="Figure (machine-described, values may be approximate)\n" + description.text,
        page=figure.page, slide=figure.slide, locator=figure.locator, source="vision",
        meta={"model": description.model, "provider": description.provider, "figure_sha": figure.sha},
    )


def transcript_elements(result: ASRResult) -> list[Element]:
    elements = [
        Element(
            id=f"t{i}", kind=TRANSCRIPT, text=s.text, source="asr", locator=f"{format_clock(s.start_s)}-{format_clock(s.end_s)}",
            meta={"start_s": s.start_s, "end_s": s.end_s, "language": result.language, "provider": result.provider,
                  **({"speaker": s.speaker} if s.speaker else {})},
        )
        for i, s in enumerate(result.segments)
    ]
    if not elements and result.text.strip():   # no timing available: keep the text
        elements.append(Element(id="transcript", kind=TRANSCRIPT, text=result.text, source="asr", meta={"language": result.language, "provider": result.provider}))
    return elements


# ---- execution -----------------------------------------------------------------------------

async def _run_ocr(db: Session, plan: Plan, path: Path, content_hash: str) -> list[Element]:
    pages_wanted = plan.ocr_pages if plan.kind == "pdf" else [None]
    if plan.pending_ocr and plan.ocr:
        todo = plan.pending_ocr if plan.kind == "pdf" else None
        async for page in plan.ocr.recognize(path, todo):
            put_unit(db, ocr_key(content_hash, page.page), page.provider, page.to_dict())   # stored before the next call
    elements: list[Element] = []
    for page_no in pages_wanted:
        cached = get_unit(db, ocr_key(content_hash, page_no))
        if cached:
            elements.extend(ocr_elements(OCRPage.from_dict(cached)))
    return elements


async def _describe(db: Session, vision: VisionProvider, figure: Figure) -> FigureDescription:
    cached = get_unit(db, figure_key(figure.sha))
    if cached:
        return FigureDescription.from_dict(cached)
    description = await vision.describe(figure.png)
    put_unit(db, figure_key(figure.sha), description.provider, description.to_dict())
    return description


async def _run_figures(db: Session, plan: Plan) -> list[Element]:
    elements = []
    for figure in plan.figures if plan.vision else []:
        description = await _describe(db, plan.vision, figure)
        if description.informative:
            elements.append(figure_element(figure, description))
    return elements


async def _run_asr(db: Session, plan: Plan, path: Path, content_hash: str) -> list[Element]:
    cached = get_unit(db, asr_key(content_hash))
    if cached is None:
        last: Exception | None = None
        for provider in plan.asr_providers:
            try:
                result = await provider.transcribe(path)
            except (QuotaExhausted, SpecialistUnavailable) as exc:   # try the next provider
                last = exc
                continue
            put_unit(db, asr_key(content_hash), provider.name, result.to_dict())
            cached = result.to_dict()
            break
        if cached is None:
            raise last or SpecialistUnavailable("No speech-to-text provider is available.")
    return transcript_elements(ASRResult.from_dict(cached))


async def execute_plan(db: Session, plan: Plan, path: Path, content_hash: str) -> tuple[list[Element], str]:
    """Run the plan. Returns the new elements and a status (``complete``, ``ocr_no_text``, ``audio_no_text``,
    ``ocr_unavailable`` or ``audio_unavailable``). ``QuotaExhausted`` propagates; finished units are already cached."""
    elements: list[Element] = []
    status = "complete"

    if plan.ocr_pages or plan.ocr_image:
        if "ocr" in plan.unavailable:
            status = "ocr_unavailable"
        else:
            found = await _run_ocr(db, plan, path, content_hash)
            elements += found
            if plan.ocr_image and not found:
                status = "ocr_no_text"
            if plan.ocr_image and plan.vision and sum(len(e.text) for e in found) < SPARSE_OCR_CHARS:
                normalized = normalize_png(path.read_bytes())    # little text: probably a chart or a photo
                if normalized:
                    png = normalized[0]
                    figure = Figure(key="fig0", locator="image", png=png, sha=hashlib.sha256(png).hexdigest())
                    description = await _describe(db, plan.vision, figure)
                    if description.informative:
                        elements.append(figure_element(figure, description))
                        status = "complete"

    if plan.figures:
        elements += await _run_figures(db, plan)

    if plan.asr:
        if "asr" in plan.unavailable:
            status = "audio_unavailable"
        else:
            found = await _run_asr(db, plan, path, content_hash)
            elements += found
            if not found:
                status = "audio_no_text"
    return elements, status
