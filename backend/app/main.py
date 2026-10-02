from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .db import init_db
from .ingest.store import migrate_legacy_documents
from .reliability.logging import configure, request_id
from .core_routes import router as core_router
from .auth import router as auth_router
from .routes.agent import router as agent_router
from .routes.jobs import router as jobs_router
from .routes.production import router as production_router
from .routes.finalization import router as finalization_router
from .routes.specialists import router as specialists_router

app = FastAPI(
    title="KNAVIS",
    version="0.3.0",
    description="KNAVIS: Knowledge Navigation Intelligence. CPU-first multilingual multimodal evidence-grounded RAG."
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.frontend_origin.split(",") if o.strip()],   # comma-separated for several origins
    allow_credentials=False,   # sign-in is a bearer token in a header, not a cookie
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(core_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(production_router, prefix="/api")
app.include_router(finalization_router, prefix="/api")
app.include_router(specialists_router, prefix="/api")


@app.middleware("http")
async def reject_oversized_uploads(request, call_next):
    """Turn away an honest oversized upload from its Content-Length header before reading the body.
    (The upload route also enforces the limit while streaming, which covers requests that do not declare a length.)"""
    if request.url.path.endswith("/uploads") and request.method == "POST":
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > settings.max_upload_mb * 1024 * 1024 + (1 << 20):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": f"File exceeds the upload size limit of {settings.max_upload_mb} MB."}, status_code=413)
    return await call_next(request)


@app.middleware("http")
async def add_request_id(request, call_next):
    rid = request_id()
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response

@app.on_event("startup")
def startup():
    configure()
    init_db()
    migrate_legacy_documents()   # older databases: build chunks from stored evidence once

@app.get("/")
def root():
    return {"name":"KNAVIS","version":"0.3.0","status":"ok"}
