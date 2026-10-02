from qdrant_client import QdrantClient, models

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
            return
        actual = self.client.get_collection(name).config.params.vectors
        if hasattr(actual, "size") and actual.size != vector_size:
            raise ValueError(
                f"Qdrant collection '{name}' has dimension {actual.size}, expected {vector_size}. "
                "Use a new collection or matching embedding configuration."
            )

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
