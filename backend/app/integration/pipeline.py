from functools import lru_cache
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from ..config import settings
from ..retrieval.bm25 import BM25Index
from ..retrieval.embeddings import NVIDIAEmbeddingClient
from ..retrieval.qdrant_store import QdrantStore
from ..retrieval.rrf import reciprocal_rank_fusion
from ..retrieval.text import is_overview_query
from ..grounding.answer_service import GroundedAnswerService
from ..ingest.store import embed_with_cache
from ..provider_service import ProviderService
from ..db import SessionLocal
from ..models import DocChunk, Document


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

        self.bm25 = BM25Index()
        self.providers = ProviderService()
        self.answerer = GroundedAnswerService(self.providers)

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

        collection = self.collection(session_id)
        # Creates the session collection, or validates the dimension of an existing one.
        self.qdrant.ensure_collection(collection, settings.embedding_dimensions)

        ids = [str(uuid5(NAMESPACE_URL, f"{session_id}:{row.id}")) for row in rows]
        payloads = [
            {
                **(row.metadata_json or {}),
                "text": row.text, "source": source_name, "page": row.page, "locator": row.locator,
                "section": row.section, "kind": row.kind, "document_id": row.document_id, "chunk_id": row.id,
            }
            for row in rows
        ]
        self.qdrant.upsert(collection, ids, vectors, payloads)
        return stats

    def delete_chunk_points(self, session_id, chunk_ids):
        """Drop the vectors of chunks that are about to be replaced (the document is re-chunked)."""
        if self.qdrant and chunk_ids:
            self.qdrant.delete(
                self.collection(session_id),
                [str(uuid5(NAMESPACE_URL, f"{session_id}:{cid}")) for cid in chunk_ids],
            )

    async def dense_search(self, session_id, query, k=20):
        """Dense semantic retrieval from the session-specific Qdrant collection."""
        if not self.qdrant:
            return []

        collection = self.collection(session_id)

        # Avoid querying a collection that has not been created yet.
        if not self.qdrant.client.collection_exists(collection):
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

        hits = self.qdrant.search(collection, vectors[0], k=k)
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
    def _overview_sample(docs, k):
        """Evenly spaced chunks from each document, in reading order, up to ``k`` total."""
        by_document = {}
        for chunk in docs:
            by_document.setdefault(chunk.metadata.get("document_id"), []).append(chunk)
        if not by_document:
            return []

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
            return self._overview_sample(docs, k)

        self.bm25.build(docs)
        sparse = self.bm25.search(query, k)

        dense = []
        # Dense retrieval is optional; the application stays usable lexically when
        # NVIDIA/Qdrant is unavailable.
        if self.qdrant and self.qdrant.client.collection_exists(self.collection(session_id)):
            dense = await self.dense_search(session_id, query, k)

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
