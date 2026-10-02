from .bm25 import BM25Index
from .rrf import reciprocal_rank_fusion

class HybridRetrievalService:
    """
    Orchestrates sparse + dense retrieval and RRF.

    Dense retrieval is injected so we can benchmark NVIDIA embeddings against
    alternatives without changing the service.
    """
    def __init__(self, dense_search):
        self.bm25 = BM25Index()
        self.dense_search = dense_search

    def build_sparse(self, chunks):
        self.bm25.build(chunks)

    async def search(self, query, k_dense=20, k_sparse=20, final_k=20):
        sparse = self.bm25.search(query, k_sparse)
        dense = await self.dense_search(query, k_dense)
        fused = reciprocal_rank_fusion([dense, sparse], limit=final_k)
        return fused
