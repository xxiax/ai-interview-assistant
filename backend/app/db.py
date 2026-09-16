"""SQLite 数据层：状态机、事件日志、幂等音频分片和加密配置。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from .security import decrypt_secret, encrypt_secret

VALID_SESSION_STATUSES = {"idle", "recording", "ended"}
VALID_RADIO_MODES = {"pc", "mobile", "both"}
VALID_AUDIO_SOURCES = {"pc", "mobile"}
VALID_AUDIO_CODECS = {"webm_opus", "ogg_opus", "m4a_aac", "wav_pcm_s16le"}
VALID_AUDIO_CHUNK_STATUSES = {"queued", "done", "failed", "cancelled"}
VALID_AUDIO_CANCEL_REASONS = {"capture_stopped", "source_disabled"}
VALID_CONFIG_TYPES = {"llm", "search", "asr", "network"}
GLOBAL_SYSTEM_PROMPT_KEY = "system_prompt"
VALID_ANSWER_SOURCES = {"llm", "search+llm"}
RETRYABLE_AUDIO_ERROR_CODES = {
    "missing_predecessor",
    "processing_failed",
    "service_restart",
    "service_shutdown",
    "usage_limited",
}
CHUNK_SEQ_MAX = 2_147_483_647
SQLITE_INT_MAX = 9_223_372_036_854_775_807
# 岗位 JD 与简历上限：足够放完整 JD 和一页简历，同时挡住把整本文档塞进 prompt。
MAX_SESSION_CONTEXT_CHARS = 8_000
_write_lock = threading.RLock()


class SessionNotFoundError(LookupError):
    pass


class SessionStateError(RuntimeError):
    def __init__(self, current_status: str, expected: str):
        super().__init__(f"会话状态为 {current_status}，当前操作要求 {expected}")
        self.current_status = current_status
        self.expected = expected


class AudioSourceNotAllowedError(ValueError):
    def __init__(self, source: str, radio_mode: str):
        super().__init__("当前收音模式不允许该音频来源")
        self.source = source
        self.radio_mode = radio_mode


class UsageLimitExceeded(RuntimeError):
    def __init__(self, service: str, retry_after_seconds: int):
        super().__init__(f"{service} 用量预算已耗尽")
        self.service = service
        self.retry_after_seconds = max(1, retry_after_seconds)


def get_db(db_path: str | None = None) -> sqlite3.Connection:
    """获取启用外键、WAL 和 busy timeout 的数据库连接。"""
    if db_path is None:
        db_path = os.environ.get("AI_DB_PATH", "interview.db")
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """使用单进程锁和 BEGIN IMMEDIATE 保护 SQLite 写事务。"""
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _renumber_transcripts(conn: sqlite3.Connection) -> None:
    """迁移旧数据库时保留记录并重建稳定的会话内序号。"""
    session_ids = conn.execute("SELECT DISTINCT session_id FROM transcripts").fetchall()
    for session_row in session_ids:
        rows = conn.execute(
            "SELECT id FROM transcripts WHERE session_id = ? ORDER BY timestamp, id",
            (session_row["session_id"],),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE transcripts SET seq = ? WHERE id = ?", (-row["id"], row["id"])
            )
        for seq, row in enumerate(rows, start=1):
            conn.execute(
                "UPDATE transcripts SET seq = ? WHERE id = ?", (seq, row["id"])
            )


def _migrate_plaintext_config_secrets(conn: sqlite3.Connection) -> None:
    """把旧 configs.data 中的 api_key 迁移到加密表。"""
    rows = conn.execute("SELECT id, data FROM configs").fetchall()
    for row in rows:
        try:
            data = json.loads(row["data"])
        except json.JSONDecodeError:
            continue
        api_key = data.pop("api_key", None)
        if not api_key:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO config_secrets (config_id, name, ciphertext) VALUES (?, 'api_key', ?)",
            (row["id"], encrypt_secret(str(api_key))),
        )
        conn.execute(
            "UPDATE configs SET data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), row["id"]),
        )


def _migrate_global_llm_prompt(conn: sqlite3.Connection) -> None:
    """把旧 LLM 配置内的提示词迁移为全局提示词，并移除旧音频字段。"""
    existing = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (GLOBAL_SYSTEM_PROMPT_KEY,)
    ).fetchone()
    global_prompt = str(existing["value"]) if existing else None
    rows = conn.execute(
        "SELECT id, data, is_active FROM configs WHERE type = 'llm' ORDER BY is_active DESC, id DESC"
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, json.JSONDecodeError):
            continue
        changed = False
        legacy_prompt = data.pop("system_prompt", None)
        if global_prompt is None and isinstance(legacy_prompt, str):
            global_prompt = legacy_prompt.strip()
        if "audio_model" in data:
            data.pop("audio_model", None)
            changed = True
        if "system_prompt" in data:
            changed = True
        if legacy_prompt is not None:
            changed = True
        if changed:
            conn.execute(
                "UPDATE configs SET data = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), row["id"]),
            )
    if global_prompt is None:
        global_prompt = ""
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (GLOBAL_SYSTEM_PROMPT_KEY, global_prompt[:8000]),
    )


def init_db(conn: sqlite3.Connection) -> None:
    """初始化并非破坏性迁移数据库结构。"""
    transcript_seq_index_exists = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'ux_transcripts_session_seq'"
        ).fetchone()
        is not None
    )
    conn.executescript(
        """
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
            chunk_id TEXT,
            chunk_seq INTEGER,
            captured_at TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'llm',
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            data TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS config_secrets (
            config_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            ciphertext TEXT NOT NULL,
            PRIMARY KEY (config_id, name),
            FOREIGN KEY (config_id) REFERENCES configs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS audio_chunks (
            chunk_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            source TEXT NOT NULL,
            codec TEXT NOT NULL,
            chunk_seq INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            content_sha256 TEXT,
            status TEXT NOT NULL,
            transcript_id INTEGER,
            error_code TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, source, chunk_seq),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (transcript_id) REFERENCES transcripts(id)
        );

        CREATE TABLE IF NOT EXISTS session_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            request_key TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS usage_buckets (
            principal_id TEXT NOT NULL,
            service TEXT NOT NULL,
            window_seconds INTEGER NOT NULL,
            window_start INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (principal_id, service, window_seconds, window_start)
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    _ensure_column(conn, "transcripts", "chunk_id", "TEXT")
    _ensure_column(conn, "transcripts", "chunk_seq", "INTEGER")
    _ensure_column(conn, "transcripts", "captured_at", "TEXT")
    _ensure_column(conn, "audio_chunks", "content_sha256", "TEXT")
    _ensure_column(conn, "reviews", "request_key", "TEXT")
    # 岗位 JD 与简历：会话级答题背景，供 LLM 生成针对岗位的答案而不是通用答案。
    _ensure_column(conn, "sessions", "job_description", "TEXT")
    _ensure_column(conn, "sessions", "resume", "TEXT")

    conn.execute(
        "UPDATE sessions SET status = 'idle' WHERE status NOT IN ('idle', 'recording', 'ended')"
    )
    conn.execute(
        "UPDATE sessions SET radio_mode = 'pc' WHERE radio_mode NOT IN ('pc', 'mobile', 'both')"
    )
    # 僵尸 recording 恢复：recording 只存活于进程内存（问题线程、ASR 任务都在
    # 进程内），后端被杀/崩溃后不可能还在录。不重置的话，客户端下次连接时
    # sync_complete 会照着库里的 'recording' 报告状态，悬浮窗/主窗口凭空显示
    # "录制中"。这里回到 idle 而不是 ended：会话没有正常走完，用户仍可重新
    # 开始这场面试（start_session 要求 idle）。
    conn.execute("UPDATE sessions SET status = 'idle' WHERE status = 'recording'")
    interrupted_chunks = conn.execute(
        "SELECT * FROM audio_chunks WHERE status = 'queued'"
    ).fetchall()
    for row in interrupted_chunks:
        conn.execute(
            "UPDATE audio_chunks SET status = 'failed', error_code = 'service_restart' WHERE chunk_id = ?",
            (row["chunk_id"],),
        )
        payload = _audio_chunk_event_payload(
            {**dict(row), "status": "failed", "error_code": "service_restart"}
        )
        _insert_event(conn, row["session_id"], "chunk_ack", payload)
    if not transcript_seq_index_exists:
        _renumber_transcripts(conn)
    conn.execute(
        """
        UPDATE configs
        SET is_active = CASE
            WHEN id = (SELECT MAX(c2.id) FROM configs c2 WHERE c2.type = configs.type AND c2.is_active = 1)
            THEN 1 ELSE 0 END
        WHERE is_active = 1
        """
    )
    _migrate_plaintext_config_secrets(conn)
    _migrate_global_llm_prompt(conn)

    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_transcripts_session_seq
            ON transcripts(session_id, seq);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_transcripts_chunk_id
            ON transcripts(chunk_id) WHERE chunk_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_configs_one_active
            ON configs(type) WHERE is_active = 1;
        CREATE INDEX IF NOT EXISTS ix_transcripts_session
            ON transcripts(session_id, seq);
        CREATE INDEX IF NOT EXISTS ix_answers_session
            ON answers(session_id, id);
        CREATE INDEX IF NOT EXISTS ix_events_session
            ON session_events(session_id, id);
        CREATE INDEX IF NOT EXISTS ix_reviews_session
            ON reviews(session_id, id DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_reviews_request_key
            ON reviews(session_id, request_key) WHERE request_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_usage_buckets_updated
            ON usage_buckets(updated_at);

        CREATE TRIGGER IF NOT EXISTS trg_sessions_validate_insert
        BEFORE INSERT ON sessions
        WHEN NEW.status NOT IN ('idle', 'recording', 'ended')
          OR NEW.radio_mode NOT IN ('pc', 'mobile', 'both')
        BEGIN
            SELECT RAISE(ABORT, 'invalid session state');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_sessions_validate_update
        BEFORE UPDATE OF status, radio_mode ON sessions
        WHEN NEW.status NOT IN ('idle', 'recording', 'ended')
          OR NEW.radio_mode NOT IN ('pc', 'mobile', 'both')
        BEGIN
            SELECT RAISE(ABORT, 'invalid session state');
        END;
        """
    )
    conn.commit()


def _session_payload(session: dict) -> dict:
    return {
        "status": session["status"],
        "radio_mode": session["radio_mode"],
        "ended_at": session.get("ended_at"),
    }


def _insert_event(
    conn: sqlite3.Connection, session_id: str, event_type: str, payload: dict
) -> dict:
    created_at = _now()
    cur = conn.execute(
        "INSERT INTO session_events (session_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (session_id, event_type, json.dumps(payload, ensure_ascii=False), created_at),
    )
    return {
        "event_id": cur.lastrowid,
        "session_id": session_id,
        "type": event_type,
        "created_at": created_at,
        "payload": payload,
    }


def _audio_chunk_payload(row: sqlite3.Row | dict) -> dict:
    chunk = dict(row)
    return {
        "chunk_id": chunk["chunk_id"],
        "session_id": chunk["session_id"],
        "source": chunk["source"],
        "codec": chunk["codec"],
        "chunk_seq": chunk["chunk_seq"],
        "captured_at": chunk["captured_at"],
        "duration_ms": chunk["duration_ms"],
        "status": chunk["status"],
        "transcript_id": chunk.get("transcript_id"),
        "error_code": chunk.get("error_code"),
        "created_at": chunk["created_at"],
    }


def _audio_chunk_event_payload(row: sqlite3.Row | dict) -> dict:
    payload = _audio_chunk_payload(row)
    payload["chunk_created_at"] = payload.pop("created_at")
    payload.pop("session_id")
    return payload


def create_session(conn: sqlite3.Connection, title: str) -> dict:
    title = title.strip()
    if not title or len(title) > 200:
        raise ValueError("会话标题长度必须为 1 到 200 个字符")
    session_id = str(uuid.uuid4())
    with _transaction(conn):
        conn.execute(
            "INSERT INTO sessions (id, title, status, radio_mode, created_at) VALUES (?, ?, 'idle', 'pc', ?)",
            (session_id, title, _now()),
        )
        session = get_session(conn, session_id)
        _insert_event(conn, session_id, "session_state", _session_payload(session))
    return session


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def require_session(conn: sqlite3.Connection, session_id: str) -> dict:
    session = get_session(conn, session_id)
    if not session:
        raise SessionNotFoundError("会话不存在")
    return session


def get_session_snapshot(conn: sqlite3.Connection, session_id: str) -> tuple[dict, int]:
    """在同一读事务中取得会话状态和事件游标，供重连同步使用。"""
    conn.execute("BEGIN")
    try:
        session = require_session(conn, session_id)
        latest_event_id = int(
            conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM session_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return session, latest_event_id


def list_sessions(
    conn: sqlite3.Connection, limit: int = 50, offset: int = 0
) -> list[dict]:
    if not 1 <= limit <= 200 or not 0 <= offset <= SQLITE_INT_MAX:
        raise ValueError("非法会话分页范围")
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def start_session(
    conn: sqlite3.Connection, session_id: str, radio_mode: str
) -> tuple[dict, dict]:
    if radio_mode not in VALID_RADIO_MODES:
        raise ValueError("非法收音模式")
    with _transaction(conn):
        session = require_session(conn, session_id)
        if session["status"] != "idle":
            raise SessionStateError(session["status"], "idle")
        conn.execute(
            "UPDATE sessions SET status = 'recording', radio_mode = ? WHERE id = ? AND status = 'idle'",
            (radio_mode, session_id),
        )
        session = require_session(conn, session_id)
        event = _insert_event(
            conn, session_id, "session_state", _session_payload(session)
        )
    return session, event


def set_radio_mode(
    conn: sqlite3.Connection, session_id: str, radio_mode: str
) -> tuple[dict, dict]:
    if radio_mode not in VALID_RADIO_MODES:
        raise ValueError("非法收音模式")
    with _transaction(conn):
        session = require_session(conn, session_id)
        if session["status"] != "recording":
            raise SessionStateError(session["status"], "recording")
        conn.execute(
            "UPDATE sessions SET radio_mode = ? WHERE id = ? AND status = 'recording'",
            (radio_mode, session_id),
        )
        session = require_session(conn, session_id)
        event = _insert_event(
            conn, session_id, "session_state", _session_payload(session)
        )
    return session, event


def end_session(conn: sqlite3.Connection, session_id: str) -> tuple[dict, dict | None]:
    with _transaction(conn):
        session = require_session(conn, session_id)
        if session["status"] == "ended":
            return session, None
        if session["status"] != "recording":
            raise SessionStateError(session["status"], "recording")
        conn.execute(
            "UPDATE sessions SET status = 'ended', ended_at = ? WHERE id = ? AND status = 'recording'",
            (_now(), session_id),
        )
        session = require_session(conn, session_id)
        event = _insert_event(
            conn, session_id, "session_state", _session_payload(session)
        )
    return session, event


def set_session_context(
    conn: sqlite3.Connection,
    session_id: str,
    job_description: str | None,
    resume: str | None,
) -> dict:
    """更新会话级答题背景（岗位 JD 与简历）。

    只写 job_description / resume 两列，不碰 status 与 radio_mode，
    因此不会触发 trg_sessions_validate_update。已结束的会话仍可补录背景，
    因为复盘生成也会用到这份上下文。
    """
    jd = (job_description or "").strip()
    cv = (resume or "").strip()
    if len(jd) > MAX_SESSION_CONTEXT_CHARS or len(cv) > MAX_SESSION_CONTEXT_CHARS:
        raise ValueError(f"岗位 JD 与简历各自不能超过 {MAX_SESSION_CONTEXT_CHARS} 个字符")
    with _transaction(conn):
        require_session(conn, session_id)
        conn.execute(
            "UPDATE sessions SET job_description = ?, resume = ? WHERE id = ?",
            (jd or None, cv or None, session_id),
        )
        session = require_session(conn, session_id)
    return session


def get_session_context(conn: sqlite3.Connection, session_id: str) -> tuple[str, str]:
    """取会话级 JD 与简历；会话不存在时返回空串而不是抛错。

    调用点在实时答案链路上，缺背景只应降级成通用答案，不应中断答案生成。
    """
    row = conn.execute(
        "SELECT job_description, resume FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not row:
        return "", ""
    return str(row["job_description"] or ""), str(row["resume"] or "")


def ensure_recording(conn: sqlite3.Connection, session_id: str) -> dict:
    session = require_session(conn, session_id)
    if session["status"] != "recording":
        raise SessionStateError(session["status"], "recording")
    return session


def ensure_audio_source_allowed(session: dict, source: str) -> None:
    if session["radio_mode"] != "both" and session["radio_mode"] != source:
        raise AudioSourceNotAllowedError(source, session["radio_mode"])


def delete_session(conn: sqlite3.Connection, session_id: str) -> dict:
    """删除会话并级联清空全部子数据;进行中的会话必须先结束。"""
    with _transaction(conn):
        session = require_session(conn, session_id)
        if session["status"] == "recording":
            raise SessionStateError(session["status"], "ended 或 idle")
        # 子表均声明 ON DELETE CASCADE;显式按序删除以兼容外键关闭的场景
        conn.execute("DELETE FROM audio_chunks WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM reviews WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM answers WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM transcripts WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_events WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return session


def reserve_audio_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    session_id: str,
    source: str,
    codec: str,
    chunk_seq: int,
    captured_at: str,
    duration_ms: int,
    content_sha256: str,
) -> tuple[bool, dict]:
    if source not in VALID_AUDIO_SOURCES:
        raise ValueError("非法音频来源")
    if codec not in VALID_AUDIO_CODECS:
        raise ValueError("非法音频编码")
    if not 0 <= chunk_seq <= CHUNK_SEQ_MAX or duration_ms < 100 or duration_ms > 10_000:
        raise ValueError("非法音频分片元数据")
    if len(content_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in content_sha256
    ):
        raise ValueError("非法音频内容摘要")
    with _transaction(conn):
        session = ensure_recording(conn, session_id)
        ensure_audio_source_allowed(session, source)
        row = conn.execute(
            "SELECT * FROM audio_chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        if row:
            existing = dict(row)
            comparable = (
                "session_id",
                "source",
                "codec",
                "chunk_seq",
                "captured_at",
                "duration_ms",
                "content_sha256",
            )
            supplied = {
                "session_id": session_id,
                "source": source,
                "codec": codec,
                "chunk_seq": chunk_seq,
                "captured_at": captured_at,
                "duration_ms": duration_ms,
                "content_sha256": content_sha256,
            }
            if any(existing[key] != supplied[key] for key in comparable):
                raise ValueError("chunk_id 已被其他音频分片使用")
            if existing["error_code"] in RETRYABLE_AUDIO_ERROR_CODES and existing[
                "status"
            ] in {"failed", "cancelled"}:
                conn.execute(
                    "UPDATE audio_chunks SET status = 'queued', error_code = NULL WHERE chunk_id = ?",
                    (chunk_id,),
                )
                existing["status"] = "queued"
                existing["error_code"] = None
                event = _insert_event(
                    conn,
                    session_id,
                    "chunk_ack",
                    _audio_chunk_event_payload(existing),
                )
                existing["event_id"] = event["event_id"]
                return True, existing
            return False, existing

        row = conn.execute(
            "SELECT * FROM audio_chunks WHERE session_id = ? AND source = ? AND chunk_seq = ?",
            (session_id, source, chunk_seq),
        ).fetchone()
        if row:
            raise ValueError("chunk_seq 已被其他音频分片使用")

        conn.execute(
            """
            INSERT INTO audio_chunks
                (chunk_id, session_id, source, codec, chunk_seq, captured_at, duration_ms,
                 content_sha256, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
            """,
            (
                chunk_id,
                session_id,
                source,
                codec,
                chunk_seq,
                captured_at,
                duration_ms,
                content_sha256,
                _now(),
            ),
        )
        accepted = True
        row = conn.execute(
            "SELECT * FROM audio_chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        record = dict(row)
        event = _insert_event(
            conn, session_id, "chunk_ack", _audio_chunk_event_payload(record)
        )
        record["event_id"] = event["event_id"]
    return accepted, record


def mark_audio_chunk_status(
    conn: sqlite3.Connection,
    chunk_id: str,
    status: str,
    *,
    transcript_id: int | None = None,
    error_code: str | None = None,
) -> dict | None:
    if status not in VALID_AUDIO_CHUNK_STATUSES:
        raise ValueError("非法音频分片状态")
    with _transaction(conn):
        if status == "done":
            current = conn.execute(
                "SELECT * FROM audio_chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
            if current is not None and current["status"] == "queued":
                session = ensure_recording(conn, current["session_id"])
                ensure_audio_source_allowed(session, current["source"])
        cur = conn.execute(
            """
            UPDATE audio_chunks
            SET status = ?, transcript_id = ?, error_code = ?
            WHERE chunk_id = ? AND status = 'queued'
            """,
            (status, transcript_id, error_code, chunk_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM audio_chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        event = _insert_event(
            conn, row["session_id"], "chunk_ack", _audio_chunk_event_payload(row)
        )
    return event


def cancel_pending_audio_chunks(
    conn: sqlite3.Connection, session_id: str, error_code: str
) -> list[dict]:
    """结束会话或服务关闭时取消所有尚未落库的分片。"""
    with _transaction(conn):
        rows = conn.execute(
            "SELECT * FROM audio_chunks WHERE session_id = ? AND status = 'queued'",
            (session_id,),
        ).fetchall()
        status = "failed" if error_code == "service_shutdown" else "cancelled"
        events = []
        for row in rows:
            conn.execute(
                "UPDATE audio_chunks SET status = ?, error_code = ? WHERE chunk_id = ?",
                (status, error_code, row["chunk_id"]),
            )
            updated = {**dict(row), "status": status, "error_code": error_code}
            events.append(
                _insert_event(
                    conn,
                    session_id,
                    "chunk_ack",
                    _audio_chunk_event_payload(updated),
                )
            )
    return events


def cancel_audio_source_chunks(
    conn: sqlite3.Connection,
    session_id: str,
    source: str,
    through_chunk_seq: int,
    reason: str,
) -> list[dict]:
    """取消单一来源指定水位内尚可重试的分片，并持久化逐片确认事件。"""
    if source not in VALID_AUDIO_SOURCES:
        raise ValueError("非法音频来源")
    if type(through_chunk_seq) is not int or not 0 <= through_chunk_seq <= CHUNK_SEQ_MAX:
        raise ValueError("非法音频取消水位")
    if reason not in VALID_AUDIO_CANCEL_REASONS:
        raise ValueError("非法音频取消原因")

    retryable_placeholders = ", ".join(
        "?" for _ in sorted(RETRYABLE_AUDIO_ERROR_CODES)
    )
    retryable_codes = tuple(sorted(RETRYABLE_AUDIO_ERROR_CODES))
    with _transaction(conn):
        require_session(conn, session_id)
        rows = conn.execute(
            f"""
            SELECT * FROM audio_chunks
            WHERE session_id = ?
              AND source = ?
              AND chunk_seq <= ?
              AND (
                    status = 'queued'
                    OR (
                        status = 'failed'
                        AND error_code IN ({retryable_placeholders})
                    )
              )
            ORDER BY chunk_seq
            """,
            (session_id, source, through_chunk_seq, *retryable_codes),
        ).fetchall()
        events = []
        for row in rows:
            conn.execute(
                """
                UPDATE audio_chunks
                SET status = 'cancelled', transcript_id = NULL, error_code = ?
                WHERE chunk_id = ?
                """,
                (reason, row["chunk_id"]),
            )
            updated = {
                **dict(row),
                "status": "cancelled",
                "transcript_id": None,
                "error_code": reason,
            }
            events.append(
                _insert_event(
                    conn,
                    session_id,
                    "chunk_ack",
                    _audio_chunk_event_payload(updated),
                )
            )
    return events


def get_max_audio_chunk_seq(
    conn: sqlite3.Connection, session_id: str, source: str
) -> int:
    """该会话该源已 reserve 的最大 chunk_seq;无记录返回 -1。"""
    require_session(conn, session_id)
    row = conn.execute(
        "SELECT MAX(chunk_seq) AS m FROM audio_chunks WHERE session_id = ? AND source = ?",
        (session_id, source),
    ).fetchone()
    if row is None or row["m"] is None:
        return -1
    return int(row["m"])


def get_next_audio_chunk_seq(
    conn: sqlite3.Connection,
    session_id: str,
    source: str,
    start_at: int = 0,
) -> int:
    """从 ``start_at`` 起跳过已消费终态，返回下一个仍需处理的序号。"""
    require_session(conn, session_id)
    if source not in VALID_AUDIO_SOURCES:
        raise ValueError("非法音频来源")
    if type(start_at) is not int or not 0 <= start_at <= CHUNK_SEQ_MAX:
        raise ValueError("非法音频起始序号")
    rows = conn.execute(
        """
        SELECT chunk_seq, status, error_code
        FROM audio_chunks
        WHERE session_id = ? AND source = ? AND chunk_seq >= ?
        ORDER BY chunk_seq
        """,
        (session_id, source, start_at),
    ).fetchall()
    expected = start_at
    for row in rows:
        if row["chunk_seq"] != expected:
            break
        consumed = row["status"] in {"done", "cancelled"} or (
            row["status"] == "failed"
            and row["error_code"] not in RETRYABLE_AUDIO_ERROR_CODES
        )
        if not consumed:
            break
        expected += 1
    return expected


def get_audio_chunks(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    source: str | None = None,
    after_source: str | None = None,
    after_chunk_seq: int = -1,
    limit: int = 200,
) -> list[dict]:
    require_session(conn, session_id)
    if source is not None and source not in VALID_AUDIO_SOURCES:
        raise ValueError("非法音频来源")
    if after_source is not None and after_source not in VALID_AUDIO_SOURCES:
        raise ValueError("非法分片游标来源")
    if not 1 <= limit <= 200 or after_chunk_seq < -1 or after_chunk_seq > CHUNK_SEQ_MAX:
        raise ValueError("非法分片查询范围")
    if source is not None:
        if after_source is not None:
            raise ValueError("指定 source 时不能再提供 after_source")
        rows = conn.execute(
            """
            SELECT * FROM audio_chunks
            WHERE session_id = ? AND source = ? AND chunk_seq > ?
            ORDER BY source, chunk_seq
            LIMIT ?
            """,
            (session_id, source, after_chunk_seq, limit),
        ).fetchall()
    elif after_source is None:
        if after_chunk_seq != -1:
            raise ValueError("跨来源分页必须同时提供 after_source")
        rows = conn.execute(
            """
            SELECT * FROM audio_chunks
            WHERE session_id = ?
            ORDER BY source, chunk_seq
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM audio_chunks
            WHERE session_id = ?
              AND (source > ? OR (source = ? AND chunk_seq > ?))
            ORDER BY source, chunk_seq
            LIMIT ?
            """,
            (session_id, after_source, after_source, after_chunk_seq, limit),
        ).fetchall()
    return [_audio_chunk_payload(row) for row in rows]


def add_transcript(
    conn: sqlite3.Connection,
    session_id: str,
    source: str,
    text: str,
    *,
    chunk_id: str | None = None,
    chunk_seq: int | None = None,
    captured_at: str | None = None,
) -> dict:
    if source not in VALID_AUDIO_SOURCES:
        raise ValueError("非法音频来源")
    text = text.strip()
    if not text:
        raise ValueError("转写文本不能为空")
    timestamp = _now()
    with _transaction(conn):
        session = ensure_recording(conn, session_id)
        ensure_audio_source_allowed(session, source)
        chunk = None
        if chunk_id:
            chunk = conn.execute(
                "SELECT * FROM audio_chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
            if chunk is None:
                raise ValueError("音频分片不存在")
            if chunk["status"] != "queued":
                raise ValueError("音频分片状态不允许写入转写")
            if (
                chunk["session_id"] != session_id
                or chunk["source"] != source
                or chunk["chunk_seq"] != chunk_seq
            ):
                raise ValueError("音频分片与转写元数据不一致")
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM transcripts WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO transcripts
                (session_id, source, text, timestamp, seq, chunk_id, chunk_seq, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                source,
                text,
                timestamp,
                seq,
                chunk_id,
                chunk_seq,
                captured_at,
            ),
        )
        transcript = {
            "id": cur.lastrowid,
            "session_id": session_id,
            "source": source,
            "text": text,
            "timestamp": timestamp,
            "seq": seq,
            "chunk_id": chunk_id,
            "chunk_seq": chunk_seq,
            "captured_at": captured_at,
        }
        event = _insert_event(conn, session_id, "transcript", transcript)
        if chunk_id:
            updated = conn.execute(
                """
                UPDATE audio_chunks
                SET status = 'done', transcript_id = ?, error_code = NULL
                WHERE chunk_id = ? AND status = 'queued'
                """,
                (cur.lastrowid, chunk_id),
            )
            if updated.rowcount != 1:
                raise ValueError("音频分片状态不允许写入转写")
            row = conn.execute(
                "SELECT * FROM audio_chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
            chunk_event = _insert_event(
                conn, session_id, "chunk_ack", _audio_chunk_event_payload(row)
            )
    transcript["event_id"] = event["event_id"]
    if chunk_id:
        transcript["chunk_event"] = chunk_event
    return transcript


def get_transcripts(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    require_session(conn, session_id)
    rows = conn.execute(
        "SELECT * FROM transcripts WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_recent_transcript_context(
    conn: sqlite3.Connection, session_id: str, limit: int = 20
) -> str:
    rows = conn.execute(
        "SELECT source, text FROM transcripts WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return "\n".join(f"[{row['source']}] {row['text']}" for row in reversed(rows))


def add_answer(
    conn: sqlite3.Connection,
    session_id: str,
    question: str,
    answer: str,
    source: str = "llm",
    request_id: str | None = None,
    thread_id: str | None = None,
    revision: int | None = None,
) -> dict:
    question = question.strip()
    answer = answer.strip()
    if not question or not answer:
        raise ValueError("问题和答案不能为空")
    if source not in VALID_ANSWER_SOURCES:
        raise ValueError("非法答案来源")
    created_at = _now()
    with _transaction(conn):
        ensure_recording(conn, session_id)
        cur = conn.execute(
            "INSERT INTO answers (session_id, question, answer, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, question, answer, source, created_at),
        )
        result = {
            "id": cur.lastrowid,
            "session_id": session_id,
            "question": question,
            "answer": answer,
            "source": source,
            "created_at": created_at,
        }
        event_payload = dict(result)
        if request_id:
            event_payload["request_id"] = request_id
        if thread_id:
            event_payload["thread_id"] = thread_id
        if revision is not None:
            event_payload["revision"] = revision
        event = _insert_event(conn, session_id, "answer", event_payload)
    result["event_id"] = event["event_id"]
    if request_id:
        result["request_id"] = request_id
    if thread_id:
        result["thread_id"] = thread_id
    if revision is not None:
        result["revision"] = revision
    return result


def get_answers(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    require_session(conn, session_id)
    rows = conn.execute(
        "SELECT * FROM answers WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def add_review(
    conn: sqlite3.Connection,
    session_id: str,
    content: str,
    source: str,
    request_key: str | None = None,
) -> dict:
    content = content.strip()
    if not content:
        raise ValueError("复盘内容不能为空")
    if source not in VALID_ANSWER_SOURCES:
        raise ValueError("非法复盘来源")
    created_at = _now()
    with _transaction(conn):
        session = require_session(conn, session_id)
        if session["status"] != "ended":
            raise SessionStateError(session["status"], "ended")
        if request_key:
            existing = conn.execute(
                "SELECT * FROM reviews WHERE session_id = ? AND request_key = ?",
                (session_id, request_key),
            ).fetchone()
            if existing:
                return _public_review(existing)
        cur = conn.execute(
            """
            INSERT INTO reviews (session_id, content, source, request_key, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, content, source, request_key, created_at),
        )
        row = conn.execute(
            "SELECT * FROM reviews WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _public_review(row)


def _public_review(row: sqlite3.Row | dict) -> dict:
    review = dict(row)
    return {
        "id": review["id"],
        "session_id": review["session_id"],
        "content": review["content"],
        "source": review["source"],
        "created_at": review["created_at"],
    }


def get_review_by_request_key(
    conn: sqlite3.Connection, session_id: str, request_key: str
) -> dict | None:
    require_session(conn, session_id)
    row = conn.execute(
        "SELECT * FROM reviews WHERE session_id = ? AND request_key = ?",
        (session_id, request_key),
    ).fetchone()
    return _public_review(row) if row else None


def get_reviews(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    require_session(conn, session_id)
    rows = conn.execute(
        "SELECT * FROM reviews WHERE session_id = ? ORDER BY id DESC",
        (session_id,),
    ).fetchall()
    return [_public_review(row) for row in rows]


def _public_config(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> dict:
    config = dict(row)
    secret_count = conn.execute(
        "SELECT COUNT(*) FROM config_secrets WHERE config_id = ?",
        (config["id"],),
    ).fetchone()[0]
    return {
        "id": config["id"],
        "type": config["type"],
        "name": config["name"],
        "data": json.loads(config["data"]),
        "is_active": bool(config["is_active"]),
        "secret_configured": secret_count > 0,
    }


def save_config(
    conn: sqlite3.Connection, config_type: str, name: str, data: dict, is_active: bool
) -> dict:
    if config_type not in VALID_CONFIG_TYPES:
        raise ValueError("未知配置类型")
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("配置名称长度必须为 1 到 100 个字符")
    public_data = dict(data)
    api_key = str(public_data.pop("api_key", "")).strip()
    if not api_key:
        raise ValueError("api_key 不能为空")
    with _transaction(conn):
        if is_active:
            conn.execute(
                "UPDATE configs SET is_active = 0 WHERE type = ?", (config_type,)
            )
        cur = conn.execute(
            "INSERT INTO configs (type, name, data, is_active) VALUES (?, ?, ?, ?)",
            (
                config_type,
                name,
                json.dumps(public_data, ensure_ascii=False),
                int(is_active),
            ),
        )
        conn.execute(
            "INSERT INTO config_secrets (config_id, name, ciphertext) VALUES (?, 'api_key', ?)",
            (cur.lastrowid, encrypt_secret(api_key)),
        )
        row = conn.execute(
            "SELECT * FROM configs WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _public_config(conn, row)


def update_config(
    conn: sqlite3.Connection,
    config_type: str,
    config_id: int,
    name: str,
    data: dict,
    is_active: bool,
) -> dict:
    """按 id 更新已有配置;api_key 为空串表示沿用已存密钥(编辑模式不改 key)。"""
    if config_type not in VALID_CONFIG_TYPES:
        raise ValueError("未知配置类型")
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("配置名称长度必须为 1 到 100 个字符")
    if not 1 <= config_id <= SQLITE_INT_MAX:
        raise ValueError("非法配置 ID")
    public_data = dict(data)
    api_key = str(public_data.pop("api_key", "")).strip()
    with _transaction(conn):
        row = conn.execute(
            "SELECT * FROM configs WHERE id = ? AND type = ?",
            (config_id, config_type),
        ).fetchone()
        if not row:
            raise LookupError("配置不存在")
        if is_active:
            conn.execute(
                "UPDATE configs SET is_active = 0 WHERE type = ?", (config_type,)
            )
        conn.execute(
            "UPDATE configs SET name = ?, data = ?, is_active = ? WHERE id = ?",
            (
                name,
                json.dumps(public_data, ensure_ascii=False),
                int(is_active),
                config_id,
            ),
        )
        if api_key:
            conn.execute(
                "UPDATE config_secrets SET ciphertext = ? "
                "WHERE config_id = ? AND name = 'api_key'",
                (encrypt_secret(api_key), config_id),
            )
        row = conn.execute(
            "SELECT * FROM configs WHERE id = ?", (config_id,)
        ).fetchone()
    return _public_config(conn, row)


def get_configs(conn: sqlite3.Connection, config_type: str) -> list[dict]:
    if config_type not in VALID_CONFIG_TYPES:
        raise ValueError("未知配置类型")
    rows = conn.execute(
        "SELECT * FROM configs WHERE type = ? ORDER BY id",
        (config_type,),
    ).fetchall()
    return [_public_config(conn, row) for row in rows]


def get_active_config(conn: sqlite3.Connection, config_type: str) -> dict | None:
    if config_type not in VALID_CONFIG_TYPES:
        raise ValueError("未知配置类型")
    row = conn.execute(
        "SELECT * FROM configs WHERE type = ? AND is_active = 1",
        (config_type,),
    ).fetchone()
    if not row:
        return None
    data = json.loads(row["data"])
    secrets = conn.execute(
        "SELECT name, ciphertext FROM config_secrets WHERE config_id = ?",
        (row["id"],),
    ).fetchall()
    for secret_row in secrets:
        data[secret_row["name"]] = decrypt_secret(secret_row["ciphertext"])
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "data": data,
        "is_active": True,
    }


def get_global_system_prompt(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (GLOBAL_SYSTEM_PROMPT_KEY,),
    ).fetchone()
    return str(row["value"]) if row else ""


def set_global_system_prompt(conn: sqlite3.Connection, prompt: str) -> str:
    prompt = prompt.strip()
    if len(prompt) > 8_000:
        raise ValueError("提示词长度不能超过 8000 个字符")
    with _transaction(conn):
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (GLOBAL_SYSTEM_PROMPT_KEY, prompt),
        )
    return prompt


def activate_config(conn: sqlite3.Connection, config_type: str, config_id: int) -> dict:
    if config_type not in VALID_CONFIG_TYPES:
        raise ValueError("未知配置类型")
    if not 1 <= config_id <= SQLITE_INT_MAX:
        raise ValueError("非法配置 ID")
    with _transaction(conn):
        row = conn.execute(
            "SELECT * FROM configs WHERE id = ? AND type = ?",
            (config_id, config_type),
        ).fetchone()
        if not row:
            raise LookupError("配置不存在")
        conn.execute("UPDATE configs SET is_active = 0 WHERE type = ?", (config_type,))
        conn.execute("UPDATE configs SET is_active = 1 WHERE id = ?", (config_id,))
        row = conn.execute(
            "SELECT * FROM configs WHERE id = ?", (config_id,)
        ).fetchone()
    return _public_config(conn, row)


def delete_config(conn: sqlite3.Connection, config_type: str, config_id: int) -> None:
    """删除配置及其密文;类型不匹配或不存在视为不存在。"""
    if config_type not in VALID_CONFIG_TYPES:
        raise ValueError("未知配置类型")
    if not 1 <= config_id <= SQLITE_INT_MAX:
        raise ValueError("非法配置 ID")
    with _transaction(conn):
        row = conn.execute(
            "SELECT id FROM configs WHERE id = ? AND type = ?",
            (config_id, config_type),
        ).fetchone()
        if not row:
            raise LookupError("配置不存在")
        conn.execute(
            "DELETE FROM config_secrets WHERE config_id = ?", (config_id,)
        )
        conn.execute("DELETE FROM configs WHERE id = ?", (config_id,))


def get_events(
    conn: sqlite3.Connection, session_id: str, after_event_id: int = 0, limit: int = 200
) -> list[dict]:
    require_session(conn, session_id)
    if not 0 <= after_event_id <= SQLITE_INT_MAX or not 1 <= limit <= 200:
        raise ValueError("非法事件查询范围")
    rows = conn.execute(
        """
        SELECT id, session_id, type, payload, created_at
        FROM session_events
        WHERE session_id = ? AND id > ?
        ORDER BY id
        LIMIT ?
        """,
        (session_id, after_event_id, limit),
    ).fetchall()
    return [
        {
            "event_id": row["id"],
            "session_id": row["session_id"],
            "type": row["type"],
            "created_at": row["created_at"],
            "payload": json.loads(row["payload"]),
        }
        for row in rows
    ]


def get_latest_event_id(conn: sqlite3.Connection, session_id: str) -> int:
    require_session(conn, session_id)
    return int(
        conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM session_events WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
    )


def reserve_usage(
    conn: sqlite3.Connection,
    principal_ids: list[str],
    service: str,
    amount: int,
    limits: list[tuple[int, int]],
) -> None:
    """原子预留付费调用预算；进程重启后仍沿用当前窗口用量。"""
    if not principal_ids or any(not item for item in principal_ids):
        raise ValueError("用量主体不能为空")
    if not service or amount <= 0:
        raise ValueError("非法用量预留")
    normalized_limits = sorted(set(limits))
    if not normalized_limits or any(
        window_seconds <= 0 or limit <= 0 for window_seconds, limit in normalized_limits
    ):
        raise ValueError("非法用量限制")

    now = int(time.time())
    reservations: list[tuple[str, int, int, int]] = []
    with _transaction(conn):
        for principal_id in sorted(set(principal_ids)):
            for window_seconds, limit in normalized_limits:
                window_start = now - (now % window_seconds)
                row = conn.execute(
                    """
                    SELECT amount FROM usage_buckets
                    WHERE principal_id = ? AND service = ?
                      AND window_seconds = ? AND window_start = ?
                    """,
                    (principal_id, service, window_seconds, window_start),
                ).fetchone()
                current = int(row["amount"]) if row else 0
                if current + amount > limit:
                    retry_after = window_start + window_seconds - now
                    raise UsageLimitExceeded(service, retry_after)
                reservations.append(
                    (principal_id, window_seconds, window_start, current + amount)
                )

        for principal_id, window_seconds, window_start, new_amount in reservations:
            conn.execute(
                """
                INSERT INTO usage_buckets
                    (principal_id, service, window_seconds, window_start, amount, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(principal_id, service, window_seconds, window_start)
                DO UPDATE SET amount = excluded.amount, updated_at = excluded.updated_at
                """,
                (
                    principal_id,
                    service,
                    window_seconds,
                    window_start,
                    new_amount,
                    _now(),
                ),
            )
        oldest_window = now - max(window for window, _ in normalized_limits) * 2
        conn.execute(
            "DELETE FROM usage_buckets WHERE window_start < ?", (oldest_window,)
        )
