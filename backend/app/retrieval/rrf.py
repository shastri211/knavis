from collections import defaultdict

def reciprocal_rank_fusion(result_lists, k=60, limit=20):
    """
    Fuse ranked result lists by reciprocal rank.
    Result item must contain an 'id'. Higher fused score is better.

    When the same id appears in several lists the fields are merged, so signals that
    carry distinct keys (``dense_score``, ``bm25_score``) are all preserved.
    """
    scores = defaultdict(float)
    items = {}
    for results in result_lists:
        for rank, item in enumerate(results):
            item_id = item["id"]
            scores[item_id] += 1.0 / (k + rank + 1)
            items[item_id] = {**items.get(item_id, {}), **item}
    ordered = sorted(scores, key=scores.get, reverse=True)[:limit]
    return [{**items[i], "rrf_score": scores[i]} for i in ordered]
