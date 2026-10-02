"""Persistence for the ingestion pipeline: elements, chunks, and the two content-hash caches."""
import hashlib
from array import array
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import DocChunk, Document, EmbeddingCache, Evidence, ExtractionCache
from ..retrieval import fts
from .chunker import ChunkData, build_chunks
from .elements import OCR_TEXT, PARAGRAPH, RECORD, TABLE, TRANSCRIPT, Element
from .extract import EXTRACTOR_VERSION

_EMBED_BATCH = 32          # texts per embedding request
_IN_LIMIT = 500            # ids per SQL IN clause


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- elements ----------------------------------------------------------------------------

def materialize(elements: list[Element], document_id: str) -> list[Element]:
    """Copies of ``elements`` with ids made unique to ``document_id`` (cached ones are relative)."""
    out = []
    for el in elements:
        data = el.to_dict()
        data["id"] = f"{document_id}:{el.id}"
        if el.group:
            data["group"] = f"{document_id}:{el.group}"
        out.append(Element.from_dict(data))
    return out


def save_elements(db: Session, document: Document, elements: list[Element]) -> int:
    """Store elements as evidence rows (the provenance record behind every chunk)."""
    stored = 0
    for el in elements:
        if not (el.text or "").strip():
            continue
        extra = {"locator": el.locator, "slide": el.slide, "sheet": el.sheet, "level": el.level, "bbox": el.bbox,
                 "source": el.source, "confidence": el.confidence, "logical_document_id": el.group, **el.meta}
        db.merge(Evidence(
            id=el.id, document_id=document.id, kind=el.kind, text=el.text, page=el.page,
            metadata_json={k: v for k, v in extra.items() if v is not None},
        ))
        stored += 1
    db.flush()
    return stored


_LEGACY_KINDS = {"ocr": OCR_TEXT, "audio_transcript": TRANSCRIPT, "structured": RECORD, "table": TABLE}


def elements_from_evidence(rows: list[Evidence]) -> list[Element]:
    """Rebuild elements from evidence rows (documents ingested before chunks were stored)."""
    out = []
    for row in rows:
        meta = dict(row.metadata_json or {})
        known = {k: meta.pop(k, None) for k in ("locator", "slide", "sheet", "level", "bbox", "source", "confidence")}
        group = meta.pop("logical_document_id", None)
        out.append(Element(
            id=row.id, kind=_LEGACY_KINDS.get(row.kind, PARAGRAPH), text=row.text, page=row.page,
            slide=known["slide"], sheet=known["sheet"], locator=known["locator"], level=known["level"],
            bbox=known["bbox"], source=known["source"] or "native", confidence=known["confidence"],
            group=group, meta=meta,
        ))
    out.sort(key=lambda e: (e.page or 0, e.slide or 0, e.id))
    return out


# ---- chunks ------------------------------------------------------------------------------

def save_chunks(db: Session, document: Document, chunks: list[ChunkData]) -> list[DocChunk]:
    """Replace the document's chunks (and their keyword-index entries)."""
    db.query(DocChunk).filter(DocChunk.document_id == document.id).delete()
    rows = []
    for c in chunks:
        meta = {"element_ids": c.element_ids, "slide": c.slide, "sheet": c.sheet, "bbox": c.bbox,
                "logical_document_id": c.group, **c.meta}
        row = DocChunk(
            id=f"{document.id}:c{c.ordinal}", document_id=document.id, session_id=document.session_id,
            ordinal=c.ordinal, kind=c.kind, text=c.text, page=c.page, locator=c.locator, section=c.section,
            metadata_json={k: v for k, v in meta.items() if v is not None}, content_hash=sha256_text(c.text),
        )
        db.add(row)
        rows.append(row)
    db.flush()
    fts.replace_document(db, document.id, rows)   # the keyword index changes in the same transaction as the chunks
    return rows


def ensure_chunks(db: Session, document: Document) -> None:
    """Chunk documents that were ingested before chunks were stored (older databases)."""
    if document.status == "failed" or db.query(DocChunk.id).filter(DocChunk.document_id == document.id).first():
        return
    rows = db.query(Evidence).filter(Evidence.document_id == document.id).all()
    if rows:
        save_chunks(db, document, build_chunks(elements_from_evidence(rows)))
        db.commit()


def migrate_legacy_documents() -> int:
    """Chunk every document that has evidence but no chunks. Run once at startup, never mid-request."""
    from ..db import SessionLocal
    migrated = 0
    with SessionLocal() as db:
        for document in db.query(Document).filter(Document.status != "failed").all():
            has = lambda: db.query(DocChunk.id).filter(DocChunk.document_id == document.id).first() is not None
            if not has():
                ensure_chunks(db, document)
                migrated += has()   # documents with no evidence (e.g. OCR unavailable) stay unchunked
    return migrated


# ---- extraction cache --------------------------------------------------------------------

def _cache_id(content_hash: str) -> str:
    return f"{content_hash}:{EXTRACTOR_VERSION}"


def get_cached_extraction(db: Session, content_hash: str):
    row = db.get(ExtractionCache, _cache_id(content_hash))
    if row is None:
        return None
    return row.kind, [Element.from_dict(e) for e in row.elements_json], dict(row.info_json)


def put_cached_extraction(db: Session, content_hash: str, kind: str, elements: list[Element], info: dict) -> None:
    db.merge(ExtractionCache(
        id=_cache_id(content_hash), kind=kind, elements_json=[e.to_dict() for e in elements], info_json=info,
    ))


# ---- embedding cache ---------------------------------------------------------------------

def _embedding_key(model: str, input_type: str, text: str) -> str:
    return sha256_text(f"{model}\0{input_type}\0{text}")


async def embed_with_cache(db: Session, client, texts: list[str], input_type: str = "passage") -> tuple[list[list[float]], dict]:
    """Embed ``texts`` calling the provider only for texts never embedded before.

    Returns ``(vectors in input order, {"cached": n, "embedded": n, "requests": n})``.
    """
    model = client.model
    keys = [_embedding_key(model, input_type, t) for t in texts]
    unique = list(dict.fromkeys(keys))

    found: dict[str, list[float]] = {}
    for start in range(0, len(unique), _IN_LIMIT):
        for row in db.query(EmbeddingCache).filter(EmbeddingCache.id.in_(unique[start:start + _IN_LIMIT])):
            vector = array("f")
            vector.frombytes(row.vector)
            found[row.id] = list(vector)

    text_of = dict(zip(keys, texts))
    missing = [k for k in unique if k not in found]
    requests = 0
    for start in range(0, len(missing), _EMBED_BATCH):
        batch = missing[start:start + _EMBED_BATCH]
        result = await client.embed([text_of[k] for k in batch], input_type=input_type)
        requests += 1
        for key, vector in zip(batch, result.vectors):
            found[key] = vector
            db.merge(EmbeddingCache(id=key, model=model, dimension=len(vector), vector=array("f", vector).tobytes()))
        db.commit()   # keep progress if a later batch fails or is rate limited
    return [found[k] for k in keys], {"cached": len(unique) - len(missing), "embedded": len(missing), "requests": requests}
