"""全局应用设置 REST API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from . import db
from .models import PromptResponse, UpdatePromptRequest
from .realtime import run_db
from .security import require_auth

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(require_auth)],
)


@router.get("/prompt", response_model=PromptResponse)
async def get_prompt():
    return {"prompt": await run_db(db.get_global_system_prompt)}


@router.put("/prompt", response_model=PromptResponse)
async def update_prompt(req: UpdatePromptRequest):
    try:
        prompt = await run_db(db.set_global_system_prompt, req.prompt)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"prompt": prompt}
