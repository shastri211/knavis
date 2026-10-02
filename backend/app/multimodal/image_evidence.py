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
    out = []
    for i, d in enumerate(detections):
        if not d.text.strip():
            continue
        out.append(ImageEvidence(
            id=f"{source_id}:ocr{i}",
            source_id=source_id,
            text=d.text.strip(),
            bbox=d.bbox,
            confidence=d.confidence,
            page=d.page,
        ))
    return out
