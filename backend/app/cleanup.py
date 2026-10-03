"""Deleting a document or a whole session, including everything derived from it.

Rows go first (one transaction); vectors and files follow. A failure to remove those is logged and never
undoes the delete: they are unreachable once their rows are gone, because every query is scoped by session.
"""
import logging
import shutil

from sqlalchemy.orm import Session

from . import storage
from .analytics import tablestore
from .config import settings
from .models import ChatSession, DocChunk, Document, Evidence, Job, UsageEvent
from .retrieval import fts

logger = logging.getLogger("mragrag")


def _remove_files(documents: list[tuple[str, str]]) -> None:
    for document_id, ref in documents:
        storage.delete(ref)   # the upload, wherever it lives; failures are logged, never raised
        shutil.rmtree(settings.data_dir / "converted" / document_id, ignore_errors=True)


def delete_document(db: Session, document: Document, pipeline) -> None:
    """Remove a document: its chunks, evidence, keyword-index entries, spreadsheet tables, vectors and uploaded file (from disk or object storage)."""
    document_id, path = document.id, document.path
    fts.delete_document(db, document_id)
    tablestore.drop_document_tables(db, document_id)
    db.query(Job).filter(Job.document_id == document_id).delete()
    db.delete(document)   # chunks and evidence cascade
    db.commit()
    pipeline.purge_document_vectors(document_id)
    _remove_files([(document_id, path)])


def delete_session(db: Session, session: ChatSession, pipeline) -> None:
    """Remove a session and everything it owns: messages, documents, chunks, keyword index, tables, vectors, files."""
    session_id = session.id
    documents = [(d.id, d.path) for d in db.query(Document.id, Document.path).filter(Document.session_id == session_id)]
    document_ids = [d for d, _ in documents]
    fts.delete_session(db, session_id)
    tablestore.drop_session_tables(db, session_id)
    db.query(Job).filter(Job.session_id == session_id).delete()
    db.query(UsageEvent).filter(UsageEvent.session_id == session_id).delete()
    db.query(DocChunk).filter(DocChunk.session_id == session_id).delete()
    if document_ids:
        db.query(Evidence).filter(Evidence.document_id.in_(document_ids)).delete(synchronize_session=False)
    db.delete(session)   # messages and documents cascade
    db.commit()
    pipeline.purge_session_vectors(session_id)
    _remove_files(documents)
