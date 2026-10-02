from fastapi import APIRouter
from ..finalization.healthcheck import dependency_report, project_contract

router = APIRouter(prefix="/final", tags=["finalization"])

@router.get("/contract")
def contract():
    return {
        "dependencies": dependency_report(),
        "project_modules": project_contract(),
    }
