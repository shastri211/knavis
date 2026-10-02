import logging

from qdrant_client import QdrantClient, models

logger = logging.getLogger("mragrag")

_SCROLL_BATCH = 256


def session_filter(session_id: str) -> models.Filter:
    return models.Filter(must=[models.FieldCondition(key="session_id", match=models.MatchValue(value=session_id))])


class QdrantStore:
    def __init__(self, path="data/qdrant", url=None, api_key=None):
        # A server/cloud deployment supports concurrent processes. Local Qdrant is
        # intentionally owned by one process and is shared through the pipeline singleton.
        self.client = QdrantClient(url=url, api_key=api_key) if url else QdrantClient(path=path)

    def ensure_collection(self, name: str, vector_size: int):
        if not self.client.collection_exists(name):
            self.client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=vector_size,
                    distance=models.Distance.COSINE,
                ),
            )
            self._index_payload(name)
            return
        actual = self.client.get_collection(name).config.params.vectors
        if hasattr(actual, "size") and actual.size != vector_size:
            raise ValueError(
                f"Qdrant collection '{name}' has dimension {actual.size}, expected {vector_size}. "
                "Use a new collection or matching embedding configuration."
            )

    def _index_payload(self, name: str) -> None:
        """Index the fields every query filters on. (Local on-disk Qdrant has no payload indexes and ignores this.)"""
        for field in ("session_id", "document_id"):
            try:
                self.client.create_payload_index(name, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD)
            except Exception as exc:   # an unindexed filter still works, just slower on a very large collection
                logger.info("Payload index on %s.%s not created: %s", name, field, str(exc)[:100])

    def upsert(self, collection: str, ids, vectors, payloads):
        points = [
            models.PointStruct(
                id=i,
                vector=v,
                payload=p,
            )
            for i, v, p in zip(ids, vectors, payloads)
        ]
        self.client.upsert(collection_name=collection, points=points)

    def search(self, collection: str, vector, k=20, query_filter=None):
        hits = self.client.query_points(
            collection_name=collection,
            query=vector,
            limit=k,
            query_filter=query_filter,
            with_payload=True,
        ).points
        return [
            {"id": str(x.id), "score": float(x.score), "payload": x.payload}
            for x in hits
        ]

    def delete(self, collection: str, ids) -> None:
        """Remove points by id (no-op when the collection does not exist yet)."""
        if ids and self.client.collection_exists(collection):
            self.client.delete(collection_name=collection, points_selector=models.PointIdsList(points=list(ids)))

    def delete_session(self, collection: str, session_id: str) -> None:
        """Remove every point of a session (no-op when the collection does not exist)."""
        if self.client.collection_exists(collection):
            self.client.delete(collection_name=collection, points_selector=models.FilterSelector(filter=session_filter(session_id)))

    def delete_document(self, collection: str, document_id: str) -> None:
        if self.client.collection_exists(collection):
            selector = models.FilterSelector(filter=models.Filter(must=[
                models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]))
            self.client.delete(collection_name=collection, points_selector=selector)

    def count_session(self, collection: str, session_id: str) -> int:
        if not self.client.collection_exists(collection):
            return 0
        return self.client.count(collection, count_filter=session_filter(session_id), exact=True).count

    def move_legacy_collection(self, legacy: str, target: str, session_id: str, vector_size: int) -> int:
        """Copy a per-session collection (the old layout) into the shared one, then drop it.

        Point ids are unchanged, so the copy is idempotent: an interrupted move is simply repeated.
        Returns the number of points moved, or ``-1`` when the legacy collection has another dimension and was left alone.
        """
        if not self.client.collection_exists(legacy):
            return 0
        actual = self.client.get_collection(legacy).config.params.vectors
        if hasattr(actual, "size") and actual.size != vector_size:
            logger.warning("Legacy collection %s has dimension %s, not %s; leaving it alone", legacy, actual.size, vector_size)
            return -1
        self.ensure_collection(target, vector_size)
        moved, offset = 0, None
        while True:
            points, offset = self.client.scroll(legacy, limit=_SCROLL_BATCH, offset=offset, with_payload=True, with_vectors=True)
            if points:
                self.client.upsert(target, points=[
                    models.PointStruct(id=p.id, vector=p.vector, payload={**(p.payload or {}), "session_id": session_id})
                    for p in points])
                moved += len(points)
            if offset is None:
                break
        self.client.delete_collection(legacy)
        return moved
