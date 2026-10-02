from functools import lru_cache
from uuid import NAMESPACE_URL, uuid5

from ..config import settings
from ..retrieval.chunker import chunk_text
from ..retrieval.bm25 import BM25Index
from ..retrieval.embeddings import NVIDIAEmbeddingClient
from ..retrieval.qdrant_store import QdrantStore
from ..retrieval.rrf import reciprocal_rank_fusion
from ..grounding.answer_service import GroundedAnswerService
from ..provider_service import ProviderService
from ..db import SessionLocal
from ..models import Evidence, Document


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
        safe = "".join(
            c if c.isalnum() else "_"
            for c in session_id
        )
        return f"session_{safe}"

    async def index_evidence_nodes(self, session_id, nodes):
        """
        Convert extracted evidence nodes into chunks, embeddings,
        lexical evidence, and Qdrant vectors.

        Qdrant collection dimensions are validated against the
        configured embedding dimension before upsert.
        """
        chunks = []

        for node in nodes:
            if not node.text.strip():
                continue

            chunks.extend(
                chunk_text(
                    node.id,
                    node.text,
                    metadata={
                        "source": node.source_name,
                        "page": node.page,
                        "logical_document_id": node.logical_document_id,
                        "modality": node.modality,
                        "document_id": node.document_id,
                        "slide": node.slide,
                        "sheet": node.sheet,
                    },
                )
            )

        # If there is nothing to index, or dense retrieval is disabled,
        # the SQLite/BM25 path can still provide lexical retrieval.
        if not chunks or not self.qdrant:
            return 0

        vectors = await self.embedding.embed(
            [c.text for c in chunks],
            input_type="passage",
        )

        # ----------------------------------------------------------
        # CRITICAL DIMENSION VALIDATION
        # ----------------------------------------------------------
        #
        # len(vectors.vectors) = number of vectors/chunks.
        #
        # It is NOT the embedding dimension.
        #
        # The actual dimension is the length of one vector.
        # ----------------------------------------------------------

        if not vectors.vectors:
            return 0

        actual_dimension = len(vectors.vectors[0])
        configured_dimension = settings.embedding_dimensions

        if actual_dimension != configured_dimension:
            raise ValueError(
                "Embedding dimension mismatch: "
                f"model returned {actual_dimension} dimensions, "
                f"but EMBEDDING_DIMENSIONS is configured as "
                f"{configured_dimension}."
            )

        collection = self.collection(session_id)

        # Create the session-specific collection if it does not exist.
        # If it already exists, QdrantStore validates its dimension.
        self.qdrant.ensure_collection(
            collection,
            configured_dimension,
        )

        ids = [
            str(
                uuid5(
                    NAMESPACE_URL,
                    f"{session_id}:{chunk.id}",
                )
            )
            for chunk in chunks
        ]

        payloads = [
            {
                "text": chunk.text,
                "source": chunk.metadata.get("source"),
                **chunk.metadata,
                "chunk_id": chunk.id,
            }
            for chunk in chunks
        ]

        self.qdrant.upsert(
            collection,
            ids,
            vectors.vectors,
            payloads,
        )

        return len(chunks)

    async def dense_search(
        self,
        session_id,
        query,
        k=20,
    ):
        """
        Perform dense semantic retrieval from the session-specific
        Qdrant collection.
        """
        if not self.qdrant:
            return []

        collection = self.collection(session_id)

        # Avoid querying a collection that has not been created yet.
        if not self.qdrant.client.collection_exists(collection):
            return []

        emb = await self.embedding.embed(
            [query],
            input_type="query",
        )

        if not emb.vectors:
            return []

        # Validate query embedding dimension before searching.
        actual_dimension = len(emb.vectors[0])
        configured_dimension = settings.embedding_dimensions

        if actual_dimension != configured_dimension:
            raise ValueError(
                "Query embedding dimension mismatch: "
                f"model returned {actual_dimension} dimensions, "
                f"but EMBEDDING_DIMENSIONS is configured as "
                f"{configured_dimension}."
            )

        hits = self.qdrant.search(
            collection,
            emb.vectors[0],
            k=k,
        )

        return [
            {
                "id": h["payload"].get("chunk_id", h["id"]),
                "text": h["payload"].get("text", ""),
                "metadata": h["payload"],
                "score": h["score"],
            }
            for h in hits
        ]

    async def retrieve(
        self,
        session_id,
        query,
        k=20,
    ):
        """
        Hybrid retrieval.

        SQLite provides the durable lexical source of truth.
        Qdrant provides dense retrieval when configured and indexed.

        Results are combined using reciprocal rank fusion.
        """

        db = SessionLocal()

        try:
            rows = (
                db.query(Evidence, Document)
                .join(
                    Document,
                    Evidence.document_id == Document.id,
                )
                .filter(
                    Document.session_id == session_id,
                    Document.status != "failed",
                )
                .all()
            )

            docs = []

            for evidence, document in rows:
                if not evidence.text.strip():
                    continue

                metadata = {
                    "document_id": document.id,
                    "source": document.filename,
                    "page": evidence.page,
                    **(evidence.metadata_json or {}),
                }

                for chunk in chunk_text(
                    evidence.id,
                    evidence.text,
                    metadata=metadata,
                ):
                    docs.append(
                        type(
                            "C",
                            (),
                            {
                                "id": chunk.id,
                                "text": chunk.text,
                                "metadata": {
                                    **chunk.metadata,
                                    "chunk_id": chunk.id,
                                },
                            },
                        )
                    )

        finally:
            db.close()

        # Build lexical retrieval from durable SQLite evidence.
        self.bm25.build(docs)

        sparse = self.bm25.search(
            query,
            k,
        )

        dense = []

        # Dense retrieval is optional. The application remains usable
        # through the lexical path when NVIDIA/Qdrant is unavailable.
        if (
            self.qdrant
            and self.qdrant.client.collection_exists(
                self.collection(session_id)
            )
        ):
            dense = await self.dense_search(
                session_id,
                query,
                k,
            )

        return reciprocal_rank_fusion(
            [dense, sparse],
            limit=k,
        )

    async def answer(
        self,
        session_id,
        query,
        provider,
        model,
        language=None,
    ):
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

        return await self.answerer.answer(
            query,
            candidates,
            provider,
            model,
            language,
        )


@lru_cache(maxsize=1)
def get_pipeline() -> IntegratedRAGPipeline:
    """
    Return the shared RAG pipeline instance.
    """
    return IntegratedRAGPipeline()