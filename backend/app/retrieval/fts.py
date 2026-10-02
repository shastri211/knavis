"""Persistent keyword index, kept next to the chunks it indexes.

Replaces the in-memory BM25 index that used to be rebuilt from every chunk on every question. Rows are written in the
same transaction as the chunks (see ``ingest.store.save_chunks``), so the index cannot drift from them, and
``rebuild_if_stale`` repairs databases created before it existed.

Text is reduced to the same stemmed content terms the evidence gate uses (``retrieval.text``), so a question
"retained" finds "retain", and Hindi words stay whole. Two implementations, chosen by the database in use:

* SQLite: an FTS5 table. Its unicode61 tokenizer keeps Devanagari vowel signs inside a word, and it only ever sees
  text that was already split on spaces. Ranking is BM25.
* PostgreSQL: a table with a ``tsvector`` built directly from the term list (``array_to_tsvector``), so no text
  parser can split a Hindi word, and a GIN index. Ranking is ``ts_rank``.

These tables are created here for whichever database is in use and are deliberately outside Alembic.
"""
import logging
import re

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from .text import content_terms, tokenize

logger = logging.getLogger("mragrag")

TABLE = "chunks_fts"
_BATCH = 500


def _postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def ensure_table(db: Session) -> None:
    if _postgres(db):
        for statement in (
            f"CREATE TABLE IF NOT EXISTS {TABLE} (chunk_id text PRIMARY KEY, session_id text NOT NULL, "
            "document_id text NOT NULL, terms tsvector NOT NULL)",
            f"CREATE INDEX IF NOT EXISTS {TABLE}_terms ON {TABLE} USING GIN (terms)",
            f"CREATE INDEX IF NOT EXISTS {TABLE}_session ON {TABLE} (session_id)",
            f"CREATE INDEX IF NOT EXISTS {TABLE}_document ON {TABLE} (document_id)",
        ):
            db.execute(sql(statement))
    else:
        # scope: indexed so a query only intersects posting lists of its own session; the id columns are payload.
        db.execute(sql(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5("
            "scope, terms, chunk_id UNINDEXED, document_id UNINDEXED, tokenize='unicode61 remove_diacritics 0')"
        ))
    db.commit()


def scope_token(session_id: str) -> str:
    """The session as one FTS5 token (letters and digits only)."""
    return "s" + re.sub(r"[^0-9a-zA-Z]", "", session_id).lower()


def index_text(text: str) -> str:
    return " ".join(content_terms(text))


def _insert(db: Session, rows) -> None:
    if _postgres(db):
        db.execute(
            sql(f"INSERT INTO {TABLE}(chunk_id, session_id, document_id, terms) "
                "VALUES (:chunk_id, :session_id, :document_id, array_to_tsvector(CAST(:terms AS text[])))"),
            [{"chunk_id": r.id, "session_id": r.session_id, "document_id": r.document_id,
              "terms": sorted(set(content_terms(r.text)))} for r in rows],
        )
        return
    db.execute(
        sql(f"INSERT INTO {TABLE}(scope, terms, chunk_id, document_id) VALUES (:scope, :terms, :chunk_id, :document_id)"),
        [{"scope": scope_token(r.session_id), "terms": index_text(r.text), "chunk_id": r.id, "document_id": r.document_id}
         for r in rows],
    )


def replace_document(db: Session, document_id: str, rows) -> None:
    """Index ``rows`` (chunks with ``id``, ``text``, ``document_id``, ``session_id``) in place of the document's old entries."""
    delete_document(db, document_id)
    rows = list(rows)
    for start in range(0, len(rows), _BATCH):
        _insert(db, rows[start:start + _BATCH])


def delete_document(db: Session, document_id: str) -> None:
    db.execute(sql(f"DELETE FROM {TABLE} WHERE document_id = :d"), {"d": document_id})


def delete_session(db: Session, session_id: str) -> None:
    if _postgres(db):
        db.execute(sql(f"DELETE FROM {TABLE} WHERE session_id = :s"), {"s": session_id})
    else:
        db.execute(sql(f"DELETE FROM {TABLE} WHERE scope = :s"), {"s": scope_token(session_id)})


def query_terms(query: str) -> list[str]:
    terms = content_terms(query) or tokenize(query)
    return list(dict.fromkeys(terms))


def match_expression(session_id: str, terms: list[str]) -> str:
    quoted = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    return f'scope : "{scope_token(session_id)}" AND terms : ({quoted})'


def _tsquery(terms: list[str]) -> str:
    """Terms as exact lexemes ('a' | 'b'): no text-search parser or stemmer touches them."""
    return " | ".join("'" + t.replace("\\", "\\\\").replace("'", "''") + "'" for t in terms)


def search(db: Session, session_id: str, query: str, k: int = 20) -> list[tuple[str, float]]:
    """Best-matching chunk ids of the session with a score where higher is better."""
    terms = query_terms(query)
    if not terms:
        return []
    if _postgres(db):
        rows = db.execute(
            sql(f"SELECT chunk_id, ts_rank(terms, q) AS score FROM {TABLE}, CAST(:q AS tsquery) AS q "
                "WHERE session_id = :s AND terms @@ q ORDER BY score DESC, chunk_id LIMIT :k"),
            {"q": _tsquery(terms), "s": session_id, "k": int(k)},
        ).all()
        return [(chunk_id, float(score)) for chunk_id, score in rows]
    rows = db.execute(
        sql(f"SELECT chunk_id, bm25({TABLE}, 0.0, 1.0, 0.0, 0.0) AS score FROM {TABLE} "
            f"WHERE {TABLE} MATCH :q ORDER BY score LIMIT :k"),
        {"q": match_expression(session_id, terms), "k": int(k)},
    ).all()
    return [(chunk_id, -float(score)) for chunk_id, score in rows]


def rebuild(db: Session) -> int:
    """Re-index every chunk from scratch. Returns the number of chunks indexed."""
    from ..models import DocChunk
    db.execute(sql(f"DELETE FROM {TABLE}"))
    total, last = 0, ""
    while True:   # keyset pages, so memory stays bounded however many chunks there are
        batch = (db.query(DocChunk.id, DocChunk.document_id, DocChunk.session_id, DocChunk.text)
                 .filter(DocChunk.id > last).order_by(DocChunk.id).limit(_BATCH).all())
        if not batch:
            break
        _insert(db, batch)
        total, last = total + len(batch), batch[-1].id
    db.commit()
    return total


def rebuild_if_stale(db: Session) -> int | None:
    """Rebuild when the index and the chunks disagree (a database from before the index existed, or a crash).

    Returns the number of chunks re-indexed, or ``None`` when nothing needed repairing.
    """
    from ..models import DocChunk
    chunks = db.query(DocChunk.id).count()
    indexed = db.execute(sql(f"SELECT count(*) FROM {TABLE}")).scalar() or 0
    if chunks == indexed:
        return None
    logger.info("Rebuilding the keyword index (%s chunks, %s indexed)", chunks, indexed)
    return rebuild(db)
