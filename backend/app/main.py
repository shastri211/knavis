from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .db import init_db
from .reliability.logging import configure, request_id
from .core_routes import router as core_router
from .routes.agent import router as agent_router
from .routes.jobs import router as jobs_router
from .routes.production import router as production_router
from .routes.finalization import router as finalization_router

app = FastAPI(
    title="KNAVIS",
    version="0.3.0",
    description="KNAVIS: Knowledge Navigation Intelligence. CPU-first multilingual multimodal evidence-grounded RAG."
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(core_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(production_router, prefix="/api")
app.include_router(finalization_router, prefix="/api")


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

@app.get("/")
def root():
    return {"name":"KNAVIS","version":"0.3.0","status":"ok"}
