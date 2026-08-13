"""FastAPI 应用入口。"""
from fastapi import FastAPI, WebSocket

from . import db
from .routes_configs import router as configs_router
from .routes_review import router as review_router
from .routes_sessions import router as sessions_router
from .ws import websocket_endpoint

app = FastAPI(title="AI 面试助手", version="0.1.0")

# 启动时初始化数据库
@app.on_event("startup")
async def startup():
    conn = db.get_db()
    try:
        db.init_db(conn)
    finally:
        conn.close()

# 模块加载时初始化数据库，保证 TestClient 等不触发 startup 事件的场景下表已存在
conn = db.get_db()
try:
    db.init_db(conn)
finally:
    conn.close()

app.include_router(sessions_router)
app.include_router(configs_router)
app.include_router(review_router)


@app.websocket("/ws/{session_id}")
async def ws_endpoint(ws: WebSocket, session_id: str):
    await websocket_endpoint(ws, session_id)


@app.get("/health")
async def health():
    return {"status": "ok"}
