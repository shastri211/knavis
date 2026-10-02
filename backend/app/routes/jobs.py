from fastapi import APIRouter, HTTPException
from ..db import SessionLocal
from ..models import Job

router = APIRouter(prefix="/jobs", tags=["jobs"])

@router.get("/{job_id}")
def get_job(job_id: str):
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return {
            "id": job.id, "document_id": job.document_id, "type": job.type,
            "status": job.status, "progress": job.progress,
            "stage": job.stage, "error": job.error,
            "created_at": job.created_at, "updated_at": job.updated_at,
        }
    finally:
        db.close()
