"""SQLite 数据库层。"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

# SQLite 单进程写锁：保护 seq 计算 + INSERT 的原子性（见 add_transcript）。
_seq_lock = threading.Lock()


def get_db(db_path: str = None) -> sqlite3.Connection:
    """获取数据库连接。

    数据库路径来源优先级：显式参数 > 环境变量 AI_DB_PATH > 缺省 "interview.db"。
    """
    if db_path is None:
        db_path = os.environ.get("AI_DB_PATH", "interview.db")
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
    """添加转写记录。

    用模块级锁保护 seq 计算 + INSERT 的原子性，避免并发分片算出相同 seq
    （见 final review：C1 改为并发 LLM 任务后，多个分片可能同时进入本函数）。
    """
    timestamp = _now()
    with _seq_lock:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM transcripts WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO transcripts (session_id, source, text, timestamp, seq) VALUES (?, ?, ?, ?, ?)",
            (session_id, source, text, timestamp, seq),
        )
        conn.commit()
    return {
        "id": cur.lastrowid,
        "session_id": session_id,
        "source": source,
        "text": text,
        "timestamp": timestamp,
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
