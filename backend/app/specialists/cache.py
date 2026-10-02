"""Per-unit cache of paid specialist results (an OCR page, a described figure, a transcript)."""
from sqlalchemy.orm import Session

from ..models import SpecialistCache

# Bump when a prompt or parsing change makes old results wrong.
VERSION = "1"


def ocr_key(content_hash: str, page: int | None) -> str:
    return f"ocr{VERSION}:{content_hash}:{page or 0}"


def figure_key(image_hash: str) -> str:
    return f"fig{VERSION}:{image_hash}"


def asr_key(content_hash: str) -> str:
    return f"asr{VERSION}:{content_hash}"


def get_unit(db: Session, key: str) -> dict | None:
    row = db.get(SpecialistCache, key)
    return dict(row.payload) if row is not None else None


def put_unit(db: Session, key: str, provider: str, payload: dict) -> None:
    db.merge(SpecialistCache(id=key, provider=provider, payload=payload))
    db.commit()   # commit per unit: progress survives a later failure or quota pause
