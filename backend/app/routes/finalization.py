from fastapi import APIRouter, Depends
from ..auth import current_principal
from ..finalization.healthcheck import dependency_report, project_contract

router = APIRouter(prefix="/final", tags=["finalization"], dependencies=[Depends(current_principal)])

@router.get("/contract")
def contract():
    return {
        "dependencies": dependency_report(),
        "project_modules": project_contract(),
    }
