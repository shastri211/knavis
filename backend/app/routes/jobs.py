from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import Principal, current_principal, owned_job
from ..db import get_db

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(current_principal)])


@router.get("/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db), me: Principal = Depends(current_principal)):
    job = owned_job(db, me, job_id)
    return {
        "id": job.id, "document_id": job.document_id, "type": job.type,
        "status": job.status, "progress": job.progress,
        "stage": job.stage, "error": job.error,
        "created_at": job.created_at, "updated_at": job.updated_at,
    }
