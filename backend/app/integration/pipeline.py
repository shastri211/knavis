import asyncio
import logging
import time
from functools import lru_cache
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from ..config import settings
from ..retrieval.bm25 import BM25Index
from ..retrieval.embeddings import NVIDIAEmbeddingClient
from ..retrieval.qdrant_store import QdrantStore
from ..retrieval.rrf import reciprocal_rank_fusion
from pathlib import Path

from ..retrieval.text import content_terms, is_overview_query
from ..grounding.answer_service import GroundedAnswerService
from ..ingest.store import embed_with_cache
from ..provider_service import ProviderService
from ..db import SessionLocal
from ..models import DocChunk, Document
from ..reliability.governor import QuotaExhausted

logger = logging.getLogger("mragrag")

# After a vector-store failure, skip it for this long instead of paying a connection timeout on every call.
DENSE_RETRY_SECONDS = 60


class DenseUnavailable(RuntimeError):
    """The vector store (or embedding step) cannot be used right now. Keyword search keeps working."""


class IntegratedRAGPipeline:
    def __init__(self):
        self.embedding = NVIDIAEmbeddingClient(
            settings.nvidia_api_key,
            settings.nvidia_base_url,
            settings.embedding_model,
        )

        self.qdrant = (
            QdrantStore(
                str(settings.data_dir / "qdrant"),
                settings.qdrant_url or None,
                settings.qdrant_api_key or None,
            )
            if settings.nvidia_api_key
            else None
        )

        self._dense_down_until = 0.0
        self._dense_last_error = ""
        self.bm25 = BM25Index()
        self.providers = ProviderService()
        self.answerer = GroundedAnswerService(self.providers)

    def dense_ready(self) -> bool:
        return self.qdrant is not None and time.monotonic() >= self._dense_down_until

    def dense_failed(self, exc: Exception) -> None:
        self._dense_down_until = time.monotonic() + DENSE_RETRY_SECONDS
        self._dense_last_error = f"{type(exc).__name__}: {str(exc)[:100]}"
        logger.warning("Vector search unavailable (%s: %s); using keyword search for %ss",
                       type(exc).__name__, str(exc)[:120], DENSE_RETRY_SECONDS)

    def collection(self, session_id: str) -> str:
        """
        Return a safe, session-scoped Qdrant collection name.

        Each chat session gets its own collection so documents from
        unrelated sessions cannot leak into retrieval.
        """
        safe = "".join(c if c.isalnum() else "_" for c in session_id)
        return f"session_{safe}"

    async def index_chunks(self, session_id, rows, source_name):
        """Embed stored chunks (reusing cached vectors) and upsert them into Qdrant.

        Lexical retrieval needs no indexing step: it reads the same chunks from SQLite.
        Returns embedding stats, or ``None`` when dense retrieval is not configured.
        """
        if not rows or not self.qdrant:
            return None
        if not self.dense_ready():
            raise DenseUnavailable(self._dense_last_error or "the vector store was unreachable a moment ago")

        collection = self.collection(session_id)
        try:
            # Reach the vector store first: if it is down, no embedding quota is spent on vectors that cannot be
            # stored. This also creates the session collection or validates the dimension of an existing one.
            await asyncio.to_thread(self.qdrant.ensure_collection, collection, settings.embedding_dimensions)

            with SessionLocal() as db:
                vectors, stats = await embed_with_cache(db, self.embedding, [r.text for r in rows], "passage")

            # The dimension is the length of one vector (not the number of vectors).
            actual_dimension = len(vectors[0])
            if actual_dimension != settings.embedding_dimensions:
                raise ValueError(
                    "Embedding dimension mismatch: "
                    f"model returned {actual_dimension} dimensions, "
                    f"but EMBEDDING_DIMENSIONS is configured as {settings.embedding_dimensions}."
                )

            ids = [str(uuid5(NAMESPACE_URL, f"{session_id}:{row.id}")) for row in rows]
            payloads = [
                {
                    **(row.metadata_json or {}),
                    "text": row.text, "source": source_name, "page": row.page, "locator": row.locator,
                    "section": row.section, "kind": row.kind, "document_id": row.document_id, "chunk_id": row.id,
                }
                for row in rows
            ]
            await asyncio.to_thread(self.qdrant.upsert, collection, ids, vectors, payloads)
            return stats
        except QuotaExhausted:
            raise
        except Exception as exc:
            if not isinstance(exc, ValueError):   # a misconfiguration is not an outage
                self.dense_failed(exc)
            message = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {str(exc)[:100]}"
            raise DenseUnavailable(message) from exc

    async def delete_chunk_points(self, session_id, chunk_ids):
        """Drop the vectors of chunks that are about to be replaced (the document is re-chunked)."""
        if self.dense_ready() and chunk_ids:
            try:
                await asyncio.to_thread(
                    self.qdrant.delete, self.collection(session_id),
                    [str(uuid5(NAMESPACE_URL, f"{session_id}:{cid}")) for cid in chunk_ids])
            except Exception as exc:   # stale vectors are harmless; never fail an ingestion over them
                self.dense_failed(exc)

    async def dense_search(self, session_id, query, k=20):
        """Dense semantic retrieval from the session-specific Qdrant collection."""
        if not self.qdrant:
            return []

        collection = self.collection(session_id)

        # Avoid querying a collection that has not been created yet.
        if not await asyncio.to_thread(self.qdrant.client.collection_exists, collection):
            return []

        # Repeated questions reuse the cached query vector instead of calling the provider.
        with SessionLocal() as db:
            vectors, _ = await embed_with_cache(db, self.embedding, [query], "query")

        actual_dimension = len(vectors[0])
        if actual_dimension != settings.embedding_dimensions:
            raise ValueError(
                "Query embedding dimension mismatch: "
                f"model returned {actual_dimension} dimensions, "
                f"but EMBEDDING_DIMENSIONS is configured as {settings.embedding_dimensions}."
            )

        hits = await asyncio.to_thread(self.qdrant.search, collection, vectors[0], k=k)
        return [
            {
                "id": h["payload"].get("chunk_id", h["id"]),
                "text": h["payload"].get("text", ""),
                "metadata": h["payload"],
                "score": h["score"],
                "dense_score": h["score"],
            }
            for h in hits
        ]

    def _load_chunks(self, session_id):
        """The session's chunks in reading order (document upload time, then position)."""
        with SessionLocal() as db:
            rows = (
                db.query(DocChunk, Document)
                .join(Document, DocChunk.document_id == Document.id)
                .filter(Document.session_id == session_id, Document.status != "failed")
                .order_by(Document.created_at, Document.id, DocChunk.ordinal)
                .all()
            )
            return [
                SimpleNamespace(
                    id=chunk.id, text=chunk.text,
                    metadata={
                        **(chunk.metadata_json or {}),
                        "document_id": document.id, "source": document.filename, "page": chunk.page,
                        "locator": chunk.locator, "section": chunk.section, "kind": chunk.kind, "chunk_id": chunk.id,
                    },
                )
                for chunk, document in rows
                if chunk.text.strip()
            ]

    @staticmethod
    def _overview_sample(docs, k, query=""):
        """Evenly spaced chunks from each document, in reading order, up to ``k`` total.

        When the request names a document ("summarize the Git and GitHub fundamentals document") only the
        documents whose file name matches it are sampled.
        """
        by_document = {}
        for chunk in docs:
            by_document.setdefault(chunk.metadata.get("document_id"), []).append(chunk)
        if not by_document:
            return []
        wanted = set(content_terms(query))
        overlap = {}
        for doc_id, chunks in by_document.items():
            name_terms = set(content_terms(Path(chunks[0].metadata.get("source", "")).stem.replace("_", " ")))
            overlap[doc_id] = (len(wanted & name_terms), len(name_terms))
        best = max(score for score, _ in overlap.values())
        if best >= 2 or (best == 1 and any(score == 1 and size <= 2 for score, size in overlap.values())):
            by_document = {d: c for d, c in by_document.items() if overlap[d][0] == best}

        budget = max(1, k // len(by_document))
        picked = []
        for chunks in by_document.values():
            if len(chunks) <= budget:
                indexes = range(len(chunks))
            elif budget == 1:
                indexes = [0]
            else:
                indexes = sorted({round(i * (len(chunks) - 1) / (budget - 1)) for i in range(budget)})
            picked.extend(chunks[i] for i in indexes)

        return [
            {"id": c.id, "text": c.text, "metadata": c.metadata,
             "overview": True, "rrf_score": 1.0 / (rank + 1)}
            for rank, c in enumerate(picked[:k])
        ]

    async def retrieve(self, session_id, query, k=20):
        """
        Hybrid retrieval.

        SQLite holds the chunks (lexical source of truth); Qdrant provides dense retrieval
        when configured and indexed. Results are combined with reciprocal rank fusion.
        """
        docs = self._load_chunks(session_id)

        # "Summarize this document" has no content terms to match, so similarity search
        # would return noise. Sample the documents themselves, in reading order.
        if is_overview_query(query):
            return self._overview_sample(docs, k, query)

        self.bm25.build(docs)
        sparse = self.bm25.search(query, k)

        dense = []
        # Dense retrieval is optional: if the vector store or the embedding quota is unavailable the question
        # is answered from keyword search instead of failing.
        if self.dense_ready():
            try:
                dense = await self.dense_search(session_id, query, k)
            except QuotaExhausted:
                logger.info("Embedding quota used up; answering from keyword search")
            except Exception as exc:
                self.dense_failed(exc)

        return reciprocal_rank_fusion([dense, sparse], limit=k)

    async def answer(self, session_id, query, provider, model, language=None):
        """
        Retrieve evidence and generate a grounded answer.

        If there is no reliable retrieved evidence, abstain instead
        of allowing the LLM to answer from general knowledge.
        """

        candidates = await self.retrieve(
            session_id,
            query,
            settings.top_k_rerank
            if hasattr(settings, "top_k_rerank")
            else 12,
        )

        if not candidates:
            return {
                "answer": (
                    "I don't have enough reliable evidence in the "
                    "provided material to answer that accurately, "
                    "so I won't guess."
                ),
                "citations": [],
                "grounded": False,
                "reason": "no_evidence",
            }

        return await self.answerer.answer(query, candidates, provider, model, language)


@lru_cache(maxsize=1)
def get_pipeline() -> IntegratedRAGPipeline:
    """
    Return the shared RAG pipeline instance.
    """
    return IntegratedRAGPipeline()
