from dataclasses import dataclass

@dataclass
class ImageEvidence:
    id: str
    source_id: str
    text: str
    bbox: list[dict]
    confidence: float | None
    page: int | None

def ocr_to_evidence(source_id: str, detections) -> list[ImageEvidence]:
    """Convert OCR detections to evidence.

    IDs are unique per page: detections are indexed from 0 on every page, so without the
    page in the ID, page 2's first detection would overwrite page 1's when stored.
    """
    out = []
    for i, d in enumerate(detections):
        if not d.text.strip():
            continue
        prefix = f"{source_id}:p{d.page}" if d.page is not None else source_id
        out.append(ImageEvidence(
            id=f"{prefix}:ocr{i}",
            source_id=source_id,
            text=d.text.strip(),
            bbox=d.bbox,
            confidence=d.confidence,
            page=d.page,
        ))
    return out
