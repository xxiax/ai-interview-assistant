"""FastAPI 应用入口。"""
from fastapi import FastAPI

app = FastAPI(title="AI 面试助手", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok"}
