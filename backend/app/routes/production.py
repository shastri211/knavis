from fastapi import APIRouter, Request
from ..reliability.logging import request_id

router = APIRouter(tags=["production"])

@router.get("/health/live")
def live():
    return {"status":"ok"}

@router.get("/health/ready")
def ready():
    # Keep readiness conservative. External providers are checked lazily by
    # their first request so startup does not fail just because a provider
    # is rate-limited.
    return {"status":"ready"}

@router.get("/health/version")
def version():
    return {"version":"0.4.0"}

@router.get("/request-id")
def get_request_id(request: Request):
    return {"request_id": getattr(request.state, "request_id", request_id())}
