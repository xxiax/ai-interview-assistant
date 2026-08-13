"""配置管理 API 路由。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db

router = APIRouter(prefix="/api/configs", tags=["configs"])


class SaveConfigRequest(BaseModel):
    name: str
    data: dict
    is_active: bool = False


@router.get("/{config_type}")
async def get_configs(config_type: str):
    conn = db.get_db()
    try:
        return db.get_configs(conn, config_type)
    finally:
        conn.close()


@router.post("/{config_type}")
async def save_config(config_type: str, req: SaveConfigRequest):
    conn = db.get_db()
    try:
        return db.save_config(conn, config_type, req.name, req.data, req.is_active)
    finally:
        conn.close()


@router.post("/{config_type}/activate/{config_id}")
async def activate_config(config_type: str, config_id: int):
    conn = db.get_db()
    try:
        configs = db.get_configs(conn, config_type)
        target = next((c for c in configs if c["id"] == config_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="配置不存在")
        # 清除所有 active，再启用目标
        for c in configs:
            if c["is_active"]:
                conn.execute("UPDATE configs SET is_active = 0 WHERE id = ?", (c["id"],))
        conn.execute("UPDATE configs SET is_active = 1 WHERE id = ?", (config_id,))
        conn.commit()
        return db.get_active_config(conn, config_type)
    finally:
        conn.close()
