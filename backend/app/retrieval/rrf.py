from collections import defaultdict

def reciprocal_rank_fusion(result_lists, k=60, limit=20):
    """
    Fuse ranked result lists by reciprocal rank.
    Result item must contain an 'id'. Higher fused score is better.
    """
    scores = defaultdict(float)
    items = {}
    for results in result_lists:
        for rank, item in enumerate(results):
            item_id = item["id"]
            scores[item_id] += 1.0 / (k + rank + 1)
            items[item_id] = item
    ordered = sorted(scores, key=scores.get, reverse=True)[:limit]
    return [{**items[i], "rrf_score": scores[i]} for i in ordered]
