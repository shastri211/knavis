from rank_bm25 import BM25Okapi

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
        tokens = [t.lower().split() for t in self.texts]
        self.index = BM25Okapi(tokens) if tokens else None

    def search(self, query: str, k=20):
        if not self.index:
            return []
        scores = self.index.get_scores(query.lower().split())
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        return [
            {"id": self.ids[i], "text": self.texts[i], "metadata": self.meta[i],
             "score": float(scores[i]), "rank": rank}
            for rank, i in enumerate(order)
        ]
