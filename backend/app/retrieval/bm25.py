from rank_bm25 import BM25Okapi

from .text import tokenize


class BM25Index:
    def __init__(self):
        self.ids = []
        self.texts = []
        self.meta = []
        self.index = None

    def build(self, chunks):
        self.ids = [c.id for c in chunks]
        self.texts = [c.text for c in chunks]
        self.meta = [c.metadata for c in chunks]
        tokens = [tokenize(t) for t in self.texts]
        self.index = BM25Okapi(tokens) if tokens else None

    def search(self, query: str, k=20):
        if not self.index:
            return []
        scores = self.index.get_scores(tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        # BM25 scores are corpus-relative (and can be <= 0 on very small corpora), so
        # they are only used for ranking. Sufficiency is judged by the evidence gate.
        return [
            {"id": self.ids[i], "text": self.texts[i], "metadata": self.meta[i],
             "score": float(scores[i]), "bm25_score": float(scores[i]), "rank": rank}
            for rank, i in enumerate(order)
        ]
