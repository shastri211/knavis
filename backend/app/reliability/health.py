async def check_provider(fn):
    try:
        await fn()
        return {"status":"ok"}
    except Exception:
        return {"status":"degraded"}

def overall(components):
    statuses = [x.get("status") for x in components.values()]
    if all(s == "ok" for s in statuses):
        return "ok"
    if any(s == "ok" for s in statuses):
        return "degraded"
    return "down"
