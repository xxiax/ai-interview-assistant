# 后端核心（MVP）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 AI 辅助面试工具的后端核心：FastAPI 服务 + WebSocket 实时转写/答案生成 + 会话管理 + LLM/搜索配置管理。

**Architecture:** FastAPI 提供 REST API 和 WebSocket 端点。音频分片通过 WebSocket 上传，后端调用 Groq Whisper 转写，转写文本触发 LLM 生成答案，结果通过 WebSocket 广播给所有连接的客户端。SQLite 存储会话、转写、答案和配置。

**Tech Stack:** Python 3.10, FastAPI, uvicorn, websockets, httpx, SQLite (sqlite3 标准库), Groq API, OpenAI 兼容 LLM API

## Global Constraints

- Python 3.10+（本机 3.10.6）
- 所有 API key 通过环境变量或配置页管理，**绝不硬编码**
- LLM 使用 OpenAI 兼容格式（base_url + api_key + model）
- ASR 使用 Groq Whisper API（whisper-large-v3）
- 数据库使用 SQLite（标准库 sqlite3，无需 ORM）
- 所有异步操作使用 `async/await`
- 代码注释使用中文
- 每个任务结束必须提交 git

---

### Task 1: 项目脚手架与依赖

**Files:**
- Create: `backend/requirements.txt`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/.env.example`

**Interfaces:**
- Consumes: 无
- Produces: `app.main:app`（FastAPI 实例，后续任务挂载路由）

- [ ] **Step 1: 创建 requirements.txt**

```txt
fastapi==0.115.6
uvicorn[standard]==0.34.0
httpx==0.28.1
python-dotenv==1.0.1
websockets==14.1
```

- [ ] **Step 2: 创建 app/__init__.py**

```python
"""AI 面试助手后端服务。"""
```

- [ ] **Step 3: 创建 app/main.py**

```python
"""FastAPI 应用入口。"""
from fastapi import FastAPI

app = FastAPI(title="AI 面试助手", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok"}
```

- [ ] **Step 4: 创建 .env.example**

```env
# Groq Whisper API
GROQ_API_KEY=

# LLM 中转站配置
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
```

- [ ] **Step 5: 创建虚拟环境并安装依赖**

```bash
cd backend
python -m venv .venv
source .venv/Scripts/activate  # Windows Git Bash
pip install -r requirements.txt
```

- [ ] **Step 6: 运行测试验证服务启动**

```bash
cd backend
uvicorn app.main:app --port 8000
# 另开终端
curl http://localhost:8000/health
# 期望: {"status":"ok"}
```

- [ ] **Step 7: 提交**

```bash
git add backend/
git commit -m "feat: backend scaffold with FastAPI and health check"
```

---

### Task 2: 数据库层（SQLite）

**Files:**
- Create: `backend/app/db.py`
- Test: `backend/tests/test_db.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `get_db()` → `sqlite3.Connection`（每请求一个连接）
  - `init_db()` → None（建表）
  - `create_session(title: str) -> dict`
  - `get_session(session_id: str) -> dict | None`
  - `end_session(session_id: str) -> None`
  - `add_transcript(session_id, source, text) -> dict`
  - `get_transcripts(session_id) -> list[dict]`
  - `add_answer(session_id, question, answer, source) -> dict`
  - `get_answers(session_id) -> list[dict]`
  - `save_config(type, name, data, is_active) -> dict`
  - `get_configs(type) -> list[dict]`
  - `get_active_config(type) -> dict | None`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_db.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app import db


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    conn = db.get_db(str(db_path))
    db.init_db(conn)
    yield conn
    conn.close()


def test_create_and_get_session(conn):
    session = db.create_session(conn, "测试面试")
    assert session["title"] == "测试面试"
    assert session["status"] == "idle"
    got = db.get_session(conn, session["id"])
    assert got["id"] == session["id"]


def test_add_and_get_transcripts(conn):
    session = db.create_session(conn, "测试")
    db.add_transcript(conn, session["id"], "pc", "你好")
    db.add_transcript(conn, session["id"], "mobile", "面试官好")
    transcripts = db.get_transcripts(conn, session["id"])
    assert len(transcripts) == 2
    assert transcripts[0]["source"] == "pc"
    assert transcripts[0]["text"] == "你好"


def test_add_and_get_answers(conn):
    session = db.create_session(conn, "测试")
    db.add_answer(conn, session["id"], "什么是 FastAPI?", "FastAPI 是一个 Web 框架", "llm")
    answers = db.get_answers(conn, session["id"])
    assert len(answers) == 1
    assert answers[0]["question"] == "什么是 FastAPI?"


def test_save_and_get_configs(conn):
    db.save_config(conn, "llm", "我的中转", {"base_url": "http://x", "api_key": "k"}, True)
    configs = db.get_configs(conn, "llm")
    assert len(configs) == 1
    assert configs[0]["name"] == "我的中转"
    active = db.get_active_config(conn, "llm")
    assert active["name"] == "我的中转"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_db.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.db'`）

- [ ] **Step 3: 实现 db.py**

```python
"""SQLite 数据库层。"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone


def get_db(db_path: str = "interview.db") -> sqlite3.Connection:
    """获取数据库连接。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """初始化数据库表。"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'idle',
        radio_mode TEXT NOT NULL DEFAULT 'pc',
        created_at TEXT NOT NULL,
        ended_at TEXT
    );

    CREATE TABLE IF NOT EXISTS transcripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        source TEXT NOT NULL,
        text TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        seq INTEGER NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );

    CREATE TABLE IF NOT EXISTS answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'llm',
        created_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );

    CREATE TABLE IF NOT EXISTS configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        name TEXT NOT NULL,
        data TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 0
    );
    """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(conn: sqlite3.Connection, title: str) -> dict:
    """创建会话。"""
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, title, status, radio_mode, created_at) VALUES (?, ?, 'idle', 'pc', ?)",
        (session_id, title, _now()),
    )
    conn.commit()
    return get_session(conn, session_id)


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """获取会话。"""
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def end_session(conn: sqlite3.Connection, session_id: str) -> None:
    """结束会话。"""
    conn.execute(
        "UPDATE sessions SET status = 'ended', ended_at = ? WHERE id = ?",
        (_now(), session_id),
    )
    conn.commit()


def add_transcript(conn: sqlite3.Connection, session_id: str, source: str, text: str) -> dict:
    """添加转写记录。"""
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM transcripts WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO transcripts (session_id, source, text, timestamp, seq) VALUES (?, ?, ?, ?, ?)",
        (session_id, source, text, _now(), seq),
    )
    conn.commit()
    return {
        "id": cur.lastrowid,
        "session_id": session_id,
        "source": source,
        "text": text,
        "seq": seq,
    }


def get_transcripts(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """获取会话的所有转写。"""
    rows = conn.execute(
        "SELECT * FROM transcripts WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_answer(conn: sqlite3.Connection, session_id: str, question: str, answer: str, source: str = "llm") -> dict:
    """添加答案记录。"""
    cur = conn.execute(
        "INSERT INTO answers (session_id, question, answer, source, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, question, answer, source, _now()),
    )
    conn.commit()
    return {
        "id": cur.lastrowid,
        "session_id": session_id,
        "question": question,
        "answer": answer,
        "source": source,
    }


def get_answers(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """获取会话的所有答案。"""
    rows = conn.execute(
        "SELECT * FROM answers WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_config(conn: sqlite3.Connection, type: str, name: str, data: dict, is_active: bool) -> dict:
    """保存配置。若 is_active 为 True，先清除同类型其他配置的 active 标记。"""
    if is_active:
        conn.execute("UPDATE configs SET is_active = 0 WHERE type = ?", (type,))
    cur = conn.execute(
        "INSERT INTO configs (type, name, data, is_active) VALUES (?, ?, ?, ?)",
        (type, name, json.dumps(data, ensure_ascii=False), 1 if is_active else 0),
    )
    conn.commit()
    return {"id": cur.lastrowid, "type": type, "name": name, "data": data, "is_active": is_active}


def get_configs(conn: sqlite3.Connection, type: str) -> list[dict]:
    """获取某类型的所有配置。"""
    rows = conn.execute(
        "SELECT * FROM configs WHERE type = ? ORDER BY id",
        (type,),
    ).fetchall()
    return [{"id": r["id"], "type": r["type"], "name": r["name"], "data": json.loads(r["data"]), "is_active": bool(r["is_active"])} for r in rows]


def get_active_config(conn: sqlite3.Connection, type: str) -> dict | None:
    """获取某类型的当前启用配置。"""
    row = conn.execute(
        "SELECT * FROM configs WHERE type = ? AND is_active = 1",
        (type,),
    ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "type": row["type"], "name": row["name"], "data": json.loads(row["data"]), "is_active": True}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_db.py -v`
Expected: PASS（4 个测试全过）

- [ ] **Step 5: 提交**

```bash
git add backend/
git commit -m "feat: SQLite database layer with sessions, transcripts, answers, configs"
```

---

### Task 3: LLM 客户端（OpenAI 兼容）

**Files:**
- Create: `backend/app/llm.py`
- Test: `backend/tests/test_llm.py`

**Interfaces:**
- Consumes: `db.get_active_config(conn, "llm")` → `{"data": {"base_url", "api_key", "model", "auth_field"}}`
- Produces:
  - `generate_answer(question: str, context: str = "") -> str`（异步，调用 LLM 生成回答要点）
  - `generate_review(transcripts: list[dict], answers: list[dict]) -> str`（异步，生成复盘报告）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_llm.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app import llm


@pytest.mark.asyncio
async def test_generate_answer_without_config():
    """无配置时应抛出明确错误。"""
    with pytest.raises(RuntimeError, match="未配置 LLM"):
        await llm.generate_answer("什么是 FastAPI?")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_llm.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.llm'`）

- [ ] **Step 3: 实现 llm.py**

```python
"""LLM 客户端（OpenAI 兼容格式）。"""
import httpx

from . import db


def _get_llm_config() -> dict:
    """获取当前启用的 LLM 配置，未配置则抛错。"""
    conn = db.get_db()
    try:
        config = db.get_active_config(conn, "llm")
    finally:
        conn.close()
    if not config:
        raise RuntimeError("未配置 LLM，请在设置页配置")
    return config["data"]


async def _chat(messages: list[dict], temperature: float = 0.7) -> str:
    """调用 OpenAI 兼容的 chat completions 接口。"""
    config = _get_llm_config()
    base_url = config.get("base_url", "").rstrip("/")
    api_key = config.get("api_key", "")
    model = config.get("model", "")
    auth_field = config.get("auth_field", "Authorization")

    if not base_url or not api_key or not model:
        raise RuntimeError("LLM 配置不完整：需要 base_url、api_key、model")

    headers = {auth_field: f"Bearer {api_key}"}
    if auth_field.lower() == "authorization":
        headers[auth_field] = f"Bearer {api_key}"

    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def generate_answer(question: str, context: str = "") -> str:
    """根据面试问题生成回答要点。"""
    system = (
        "你是一名资深面试辅导专家。请根据面试官的问题，给出简洁、有条理的回答要点。"
        "回答要点应包含：核心答案、关键点、可能的追问方向。"
        "使用中文回答，控制在 200 字以内。"
    )
    user = f"面试官的问题：{question}"
    if context:
        user += f"\n\n面试上下文（供参考）：{context}"
    return await _chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.5,
    )


async def generate_review(transcripts: list[dict], answers: list[dict]) -> str:
    """根据完整转写和答案生成复盘报告。"""
    system = (
        "你是一名资深面试辅导专家。请根据面试的完整转写和 AI 生成的答案，"
        "生成一份复盘报告，包含：1. 面试问题清单 2. 每个问题的回答评估 3. 改进建议。"
        "使用中文回答。"
    )
    transcript_text = "\n".join(f"[{t['source']}] {t['text']}" for t in transcripts)
    answer_text = "\n".join(f"Q: {a['question']}\nA: {a['answer']}" for a in answers)
    user = f"面试转写：\n{transcript_text}\n\nAI 答案：\n{answer_text}"
    return await _chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.4,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_llm.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/
git commit -m "feat: LLM client with OpenAI-compatible chat completions"
```

---

### Task 4: ASR 客户端（Groq Whisper）

**Files:**
- Create: `backend/app/asr.py`
- Test: `backend/tests/test_asr.py`

**Interfaces:**
- Consumes: 环境变量 `GROQ_API_KEY`
- Produces:
  - `transcribe_audio(audio_bytes: bytes, source: str) -> str`（异步，调用 Groq Whisper 转写音频分片）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_asr.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app import asr


def test_missing_api_key():
    """未设置 GROQ_API_KEY 时应抛出明确错误。"""
    os.environ.pop("GROQ_API_KEY", None)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        asr._get_api_key()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_asr.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.asr'`）

- [ ] **Step 3: 实现 asr.py**

```python
"""ASR 客户端（Groq Whisper API）。"""
import os

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"


def _get_api_key() -> str:
    """获取 Groq API key。"""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 GROQ_API_KEY 环境变量")
    return key


async def transcribe_audio(audio_bytes: bytes, source: str = "pc") -> str:
    """调用 Groq Whisper 转写音频分片。"""
    api_key = _get_api_key()
    files = {"file": ("chunk.webm", audio_bytes, "audio/webm")}
    data = {"model": GROQ_MODEL, "language": "zh"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("text", "")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_asr.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/
git commit -m "feat: ASR client with Groq Whisper API"
```

---

### Task 5: 会话管理 API

**Files:**
- Create: `backend/app/routes_sessions.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_sessions_api.py`

**Interfaces:**
- Consumes: `db.create_session`, `db.get_session`, `db.end_session`, `db.get_transcripts`, `db.get_answers`
- Produces:
  - `POST /api/sessions` → 创建会话
  - `GET /api/sessions` → 获取会话列表
  - `GET /api/sessions/{id}` → 获取会话详情
  - `POST /api/sessions/{id}/end` → 结束会话
  - `GET /api/sessions/{id}/transcripts` → 获取转写列表
  - `GET /api/sessions/{id}/answers` → 获取答案列表

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_sessions_api.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_create_session():
    resp = client.post("/api/sessions", json={"title": "测试面试"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "测试面试"
    assert data["status"] == "idle"
    return data["id"]


def test_get_session():
    session_id = test_create_session()
    resp = client.get(f"/api/sessions/{session_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == session_id


def test_end_session():
    session_id = test_create_session()
    resp = client.post(f"/api/sessions/{session_id}/end")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ended"


def test_get_transcripts_empty():
    session_id = test_create_session()
    resp = client.get(f"/api/sessions/{session_id}/transcripts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_sessions():
    test_create_session()
    test_create_session()
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert len(resp.json()) >= 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_sessions_api.py -v`
Expected: FAIL（404，路由未注册）

- [ ] **Step 3: 实现 routes_sessions.py**

```python
"""会话管理 API 路由。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    title: str


@router.post("")
async def create_session(req: CreateSessionRequest):
    conn = db.get_db()
    try:
        session = db.create_session(conn, req.title)
    finally:
        conn.close()
    return session


@router.get("")
async def list_sessions():
    conn = db.get_db()
    try:
        rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/{session_id}")
async def get_session(session_id: str):
    conn = db.get_db()
    try:
        session = db.get_session(conn, session_id)
    finally:
        conn.close()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.post("/{session_id}/end")
async def end_session(session_id: str):
    conn = db.get_db()
    try:
        session = db.get_session(conn, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        db.end_session(conn, session_id)
        return db.get_session(conn, session_id)
    finally:
        conn.close()


@router.get("/{session_id}/transcripts")
async def get_transcripts(session_id: str):
    conn = db.get_db()
    try:
        return db.get_transcripts(conn, session_id)
    finally:
        conn.close()


@router.get("/{session_id}/answers")
async def get_answers(session_id: str):
    conn = db.get_db()
    try:
        return db.get_answers(conn, session_id)
    finally:
        conn.close()
```

- [ ] **Step 4: 在 main.py 挂载路由**

```python
"""FastAPI 应用入口。"""
from fastapi import FastAPI

from . import db
from .routes_sessions import router as sessions_router

app = FastAPI(title="AI 面试助手", version="0.1.0")

# 启动时初始化数据库
@app.on_event("startup")
async def startup():
    conn = db.get_db()
    try:
        db.init_db(conn)
    finally:
        conn.close()

app.include_router(sessions_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_sessions_api.py -v`
Expected: PASS（4 个测试全过）

- [ ] **Step 6: 提交**

```bash
git add backend/
git commit -m "feat: session management REST API"
```

---

### Task 6: 配置管理 API

**Files:**
- Create: `backend/app/routes_configs.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_configs_api.py`

**Interfaces:**
- Consumes: `db.save_config`, `db.get_configs`, `db.get_active_config`
- Produces:
  - `GET /api/configs/{type}` → 获取某类型配置列表
  - `POST /api/configs/{type}` → 保存配置
  - `POST /api/configs/{type}/activate/{config_id}` → 启用某配置

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_configs_api.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_save_and_get_llm_config():
    resp = client.post("/api/configs/llm", json={
        "name": "我的中转",
        "data": {"base_url": "http://x", "api_key": "k", "model": "gpt-4o"},
        "is_active": True,
    })
    assert resp.status_code == 200
    config_id = resp.json()["id"]

    resp = client.get("/api/configs/llm")
    assert resp.status_code == 200
    configs = resp.json()
    assert len(configs) == 1
    assert configs[0]["name"] == "我的中转"
    assert configs[0]["is_active"] is True


def test_activate_switches_active():
    client.post("/api/configs/llm", json={
        "name": "配置A", "data": {"base_url": "a"}, "is_active": True,
    })
    resp = client.post("/api/configs/llm", json={
        "name": "配置B", "data": {"base_url": "b"}, "is_active": True,
    })
    config_b_id = resp.json()["id"]

    configs = client.get("/api/configs/llm").json()
    active = [c for c in configs if c["is_active"]]
    assert len(active) == 1
    assert active[0]["id"] == config_b_id
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_configs_api.py -v`
Expected: FAIL（404，路由未注册）

- [ ] **Step 3: 实现 routes_configs.py**

```python
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
```

- [ ] **Step 4: 在 main.py 挂载路由**

```python
from .routes_configs import router as configs_router

app.include_router(configs_router)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_configs_api.py -v`
Expected: PASS（2 个测试全过）

- [ ] **Step 6: 提交**

```bash
git add backend/
git commit -m "feat: config management REST API for LLM and search"
```

---

### Task 7: WebSocket 实时转写与答案生成

**Files:**
- Create: `backend/app/ws.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_ws.py`

**Interfaces:**
- Consumes: `db.add_transcript`, `db.add_answer`, `asr.transcribe_audio`, `llm.generate_answer`
- Produces:
  - `WS /ws/{session_id}` → 实时音频转写 + 答案生成
  - 客户端消息类型：`audio_chunk`（base64 音频）、`set_radio_mode`、`regenerate_answer`、`end_session`
  - 服务端消息类型：`transcript`、`answer`、`session_state`、`error`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_ws.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_ws_connect_and_receive():
    session_id = client.post("/api/sessions", json={"title": "测试"}).json()["id"]
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        # 发送一个音频分片（空音频，ASR 会返回空文本，但连接应正常）
        ws.send_json({"type": "audio_chunk", "source": "pc", "data": ""})
        # 发送收音模式切换
        ws.send_json({"type": "set_radio_mode", "mode": "both"})
        # 应收到 session_state 广播
        msg = ws.receive_json()
        assert msg["type"] == "session_state"
        assert msg["radio_mode"] == "both"


def test_ws_unknown_session():
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/nonexistent") as ws:
            ws.receive_json()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_ws.py -v`
Expected: FAIL（404，WebSocket 路由未注册）

- [ ] **Step 3: 实现 ws.py**

```python
"""WebSocket 实时转写与答案生成。"""
import base64
import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from . import db, asr, llm


class ConnectionManager:
    """管理会话的 WebSocket 连接。"""

    def __init__(self):
        self.connections: dict[str, list[WebSocket]] = {}

    async def connect(self, session_id: str, ws: WebSocket):
        await ws.accept()
        if session_id not in self.connections:
            self.connections[session_id] = []
        self.connections[session_id].append(ws)

    def disconnect(self, session_id: str, ws: WebSocket):
        if session_id in self.connections:
            self.connections[session_id].remove(ws)
            if not self.connections[session_id]:
                del self.connections[session_id]

    async def broadcast(self, session_id: str, message: dict):
        """向会话的所有连接广播消息。"""
        for ws in self.connections.get(session_id, []):
            try:
                await ws.send_json(message)
            except Exception:
                pass


manager = ConnectionManager()


async def _handle_audio_chunk(session_id: str, source: str, audio_b64: str):
    """处理音频分片：转写 + 生成答案。"""
    if not audio_b64:
        return
    audio_bytes = base64.b64decode(audio_b64)
    try:
        text = await asr.transcribe_audio(audio_bytes, source)
    except Exception as e:
        await manager.broadcast(session_id, {"type": "error", "message": f"转写失败: {e}"})
        return
    if not text.strip():
        return

    conn = db.get_db()
    try:
        transcript = db.add_transcript(conn, session_id, source, text.strip())
    finally:
        conn.close()
    await manager.broadcast(session_id, {
        "type": "transcript",
        "session_id": session_id,
        "source": source,
        "text": text.strip(),
        "seq": transcript["seq"],
    })

    # 若文本像问题，生成答案
    if _looks_like_question(text):
        try:
            answer = await llm.generate_answer(text.strip())
        except Exception as e:
            await manager.broadcast(session_id, {"type": "error", "message": f"答案生成失败: {e}"})
            return
        conn = db.get_db()
        try:
            db.add_answer(conn, session_id, text.strip(), answer, "llm")
        finally:
            conn.close()
        await manager.broadcast(session_id, {
            "type": "answer",
            "session_id": session_id,
            "question": text.strip(),
            "answer": answer,
        })


def _looks_like_question(text: str) -> bool:
    """判断文本是否像面试问题。"""
    question_markers = ["?", "？", "吗", "呢", "如何", "怎么", "为什么", "什么", "哪些", "请", "介绍", "说说", "谈谈"]
    return any(m in text for m in question_markers)


async def websocket_endpoint(ws: WebSocket, session_id: str):
    """WebSocket 端点：处理音频分片和会话控制。"""
    conn = db.get_db()
    try:
        session = db.get_session(conn, session_id)
    finally:
        conn.close()
    if not session:
        await ws.close(code=4004, reason="会话不存在")
        return

    await manager.connect(session_id, ws)
    # 广播当前会话状态
    await manager.broadcast(session_id, {
        "type": "session_state",
        "session_id": session_id,
        "status": session["status"],
        "radio_mode": session["radio_mode"],
    })

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            if msg_type == "audio_chunk":
                source = data.get("source", "pc")
                audio_b64 = data.get("data", "")
                asyncio.create_task(_handle_audio_chunk(session_id, source, audio_b64))

            elif msg_type == "set_radio_mode":
                mode = data.get("mode", "pc")
                conn = db.get_db()
                try:
                    conn.execute("UPDATE sessions SET radio_mode = ? WHERE id = ?", (mode, session_id))
                    conn.commit()
                finally:
                    conn.close()
                await manager.broadcast(session_id, {
                    "type": "session_state",
                    "session_id": session_id,
                    "status": "recording",
                    "radio_mode": mode,
                })

            elif msg_type == "regenerate_answer":
                question = data.get("question", "")
                if question:
                    try:
                        answer = await llm.generate_answer(question)
                    except Exception as e:
                        await manager.broadcast(session_id, {"type": "error", "message": f"答案生成失败: {e}"})
                        continue
                    conn = db.get_db()
                    try:
                        db.add_answer(conn, session_id, question, answer, "llm")
                    finally:
                        conn.close()
                    await manager.broadcast(session_id, {
                        "type": "answer",
                        "session_id": session_id,
                        "question": question,
                        "answer": answer,
                    })

            elif msg_type == "end_session":
                conn = db.get_db()
                try:
                    db.end_session(conn, session_id)
                finally:
                    conn.close()
                await manager.broadcast(session_id, {
                    "type": "session_state",
                    "session_id": session_id,
                    "status": "ended",
                    "radio_mode": session["radio_mode"],
                })
                break

    except WebSocketDisconnect:
        manager.disconnect(session_id, ws)
```

- [ ] **Step 4: 在 main.py 挂载 WebSocket 路由**

```python
from fastapi import WebSocket
from .ws import websocket_endpoint

@app.websocket("/ws/{session_id}")
async def ws_endpoint(ws: WebSocket, session_id: str):
    await websocket_endpoint(ws, session_id)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_ws.py -v`
Expected: PASS（2 个测试全过）

- [ ] **Step 6: 提交**

```bash
git add backend/
git commit -m "feat: WebSocket realtime transcription and answer generation"
```

---

### Task 8: 复盘 API

**Files:**
- Create: `backend/app/routes_review.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_review_api.py`

**Interfaces:**
- Consumes: `db.get_transcripts`, `db.get_answers`, `llm.generate_review`
- Produces:
  - `POST /api/sessions/{id}/review` → 生成复盘报告

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_review_api.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_review_without_llm_config():
    """未配置 LLM 时应返回明确错误。"""
    session_id = client.post("/api/sessions", json={"title": "测试"}).json()["id"]
    resp = client.post(f"/api/sessions/{session_id}/review")
    assert resp.status_code == 500
    assert "LLM" in resp.json()["detail"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_review_api.py -v`
Expected: FAIL（404，路由未注册）

- [ ] **Step 3: 实现 routes_review.py**

```python
"""复盘报告 API 路由。"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from . import db, llm

router = APIRouter(prefix="/api/sessions", tags=["review"])


@router.post("/{session_id}/review")
async def generate_review(session_id: str):
    conn = db.get_db()
    try:
        session = db.get_session(conn, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        transcripts = db.get_transcripts(conn, session_id)
        answers = db.get_answers(conn, session_id)
    finally:
        conn.close()

    if not transcripts:
        return JSONResponse(status_code=400, content={"detail": "会话没有转写记录"})

    try:
        review = await llm.generate_review(transcripts, answers)
    except RuntimeError as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"复盘生成失败: {e}"})

    return {"session_id": session_id, "review": review}
```

- [ ] **Step 4: 在 main.py 挂载路由**

```python
from .routes_review import router as review_router

app.include_router(review_router)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_review_api.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/
git commit -m "feat: review report generation API"
```

---

### Task 9: 搜索集成（可选增强）

**Files:**
- Create: `backend/app/search.py`
- Modify: `backend/app/llm.py`
- Test: `backend/tests/test_search.py`

**Interfaces:**
- Consumes: `db.get_active_config(conn, "search")` → `{"data": {"engine", "api_key", ...}}`
- Produces:
  - `search_web(query: str) -> list[dict]`（异步，返回搜索结果列表）
  - `generate_answer_with_search(question: str) -> str`（异步，搜索 + LLM 整理）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_search.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app import search


def test_no_search_config():
    """未配置搜索时应返回空结果。"""
    assert search.search_web("测试") == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_search.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.search'`）

- [ ] **Step 3: 实现 search.py**

```python
"""搜索客户端（可选增强）。"""
import httpx

from . import db


async def search_web(query: str) -> list[dict]:
    """搜索网络，返回结果列表。未配置搜索 API 时返回空列表。"""
    conn = db.get_db()
    try:
        config = db.get_active_config(conn, "search")
    finally:
        conn.close()
    if not config:
        return []

    data = config["data"]
    engine = data.get("engine", "")
    api_key = data.get("api_key", "")

    if not engine or not api_key:
        return []

    if engine == "google":
        return await _search_google(query, data)
    elif engine == "bing":
        return await _search_bing(query, data)
    return []


async def _search_google(query: str, data: dict) -> list[dict]:
    """Google Custom Search JSON API。"""
    cx = data.get("cx", "")
    api_key = data.get("api_key", "")
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": api_key, "cx": cx, "q": query, "num": 5}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return []
        items = resp.json().get("items", [])
        return [{"title": i.get("title", ""), "link": i.get("link", ""), "snippet": i.get("snippet", "")} for i in items]


async def _search_bing(query: str, data: dict) -> list[dict]:
    """Bing Web Search API。"""
    api_key = data.get("api_key", "")
    url = "https://api.bing.microsoft.com/v7.0/search"
    params = {"q": query, "count": 5}
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params, headers=headers)
        if resp.status_code != 200:
            return []
        items = resp.json().get("webPages", {}).get("value", [])
        return [{"title": i.get("name", ""), "link": i.get("url", ""), "snippet": i.get("snippet", "")} for i in items]
```

- [ ] **Step 4: 在 llm.py 添加 generate_answer_with_search**

```python
async def generate_answer_with_search(question: str, context: str = "") -> str:
    """搜索 + LLM 整理生成更准确的答案。"""
    from . import search

    results = await search.search_web(question)
    if not results:
        return await generate_answer(question, context)

    search_text = "\n".join(f"{r['title']}: {r['snippet']}" for r in results)
    system = (
        "你是一名资深面试辅导专家。请根据面试官的问题和搜索到的资料，"
        "给出简洁、有条理、有依据的回答要点。使用中文回答，控制在 300 字以内。"
    )
    user = f"面试官的问题：{question}\n\n搜索到的资料：\n{search_text}"
    if context:
        user += f"\n\n面试上下文（供参考）：{context}"
    return await _chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.5,
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_search.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/
git commit -m "feat: optional web search integration with LLM answer enhancement"
```

---

### Task 10: 端到端验证

**Files:**
- Modify: `backend/app/main.py`
- Create: `backend/README.md`

**Interfaces:**
- Consumes: 所有已实现模块
- Produces: 可运行的完整后端服务

- [ ] **Step 1: 运行全部测试**

```bash
cd backend
python -m pytest tests/ -v
```

Expected: 全部 PASS

- [ ] **Step 2: 启动服务并手动验证**

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- 访问 `http://localhost:8000/health` → `{"status":"ok"}`
- 访问 `http://localhost:8000/docs` → Swagger UI 正常显示
- 创建会话、配置 LLM、连接 WebSocket 均正常

- [ ] **Step 3: 编写 backend/README.md**

```markdown
# 后端服务

AI 面试助手后端，基于 FastAPI。

## 启动

```bash
cd backend
python -m venv .venv
source .venv/Scripts/activate  # Windows
pip install -r requirements.txt
cp .env.example .env  # 填入 GROQ_API_KEY
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

- `GET /health` — 健康检查
- `POST /api/sessions` — 创建会话
- `GET /api/sessions/{id}` — 会话详情
- `POST /api/sessions/{id}/end` — 结束会话
- `GET /api/sessions/{id}/transcripts` — 转写列表
- `GET /api/sessions/{id}/answers` — 答案列表
- `POST /api/sessions/{id}/review` — 生成复盘
- `GET /api/configs/{type}` — 配置列表
- `POST /api/configs/{type}` — 保存配置
- `WS /ws/{session_id}` — 实时转写与答案

## 环境变量

- `GROQ_API_KEY` — Groq Whisper API key
```

- [ ] **Step 4: 提交**

```bash
git add backend/
git commit -m "docs: backend README and end-to-end verification"
```
