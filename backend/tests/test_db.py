from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db


@pytest.fixture()
def conn():
    connection = db.get_db()
    db.init_db(connection)
    yield connection
    connection.close()


def _recording_session(conn):
    session = db.create_session(conn, "测试")
    return db.start_session(conn, session["id"], "both")[0]


def test_state_machine_and_ended_write_barrier(conn):
    session = db.create_session(conn, "状态机")
    with pytest.raises(db.SessionStateError):
        db.end_session(conn, session["id"])
    started, _ = db.start_session(conn, session["id"], "mobile")
    assert started["status"] == "recording"
    ended, _ = db.end_session(conn, session["id"])
    assert ended["status"] == "ended"
    with pytest.raises(db.SessionStateError):
        db.add_transcript(conn, session["id"], "mobile", "不允许")
    with pytest.raises(db.SessionStateError):
        db.add_answer(conn, session["id"], "问题", "答案")


def test_concurrent_transcripts_receive_unique_monotonic_seq(conn):
    session = _recording_session(conn)

    def write(index: int):
        worker_conn = db.get_db()
        try:
            return db.add_transcript(worker_conn, session["id"], "pc", f"文本 {index}")[
                "seq"
            ]
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=6) as executor:
        seqs = list(executor.map(write, range(12)))
    assert sorted(seqs) == list(range(1, 13))
    assert [item["seq"] for item in db.get_transcripts(conn, session["id"])] == list(
        range(1, 13)
    )


def test_answer_event_persists_request_id_for_websocket_replay(conn):
    session = _recording_session(conn)
    answer = db.add_answer(
        conn,
        session["id"],
        "并发问题",
        "并发答案",
        request_id="request-123",
    )
    events = db.get_events(conn, session["id"])
    event = next(item for item in events if item["event_id"] == answer["event_id"])

    assert answer["request_id"] == "request-123"
    assert event["payload"]["request_id"] == "request-123"


def test_repeated_init_preserves_existing_transcript_sequence(conn):
    session = _recording_session(conn)
    first = db.add_transcript(conn, session["id"], "pc", "先插入")
    second = db.add_transcript(conn, session["id"], "pc", "后插入")
    conn.execute(
        "UPDATE transcripts SET timestamp = '2026-01-02' WHERE id = ?", (first["id"],)
    )
    conn.execute(
        "UPDATE transcripts SET timestamp = '2026-01-01' WHERE id = ?", (second["id"],)
    )
    conn.commit()

    db.init_db(conn)
    rows = db.get_transcripts(conn, session["id"])
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert [row["seq"] for row in rows] == [1, 2]


def test_legacy_database_without_seq_index_is_renumbered_once(conn):
    session = _recording_session(conn)
    first = db.add_transcript(conn, session["id"], "pc", "先插入")
    second = db.add_transcript(conn, session["id"], "pc", "后插入")
    conn.execute("DROP INDEX ux_transcripts_session_seq")
    conn.execute(
        "UPDATE transcripts SET timestamp = '2026-01-02' WHERE id = ?", (first["id"],)
    )
    conn.execute(
        "UPDATE transcripts SET timestamp = '2026-01-01' WHERE id = ?", (second["id"],)
    )
    conn.commit()

    db.init_db(conn)
    rows = db.get_transcripts(conn, session["id"])
    assert [row["id"] for row in rows] == [second["id"], first["id"]]
    assert [row["seq"] for row in rows] == [1, 2]


def test_audio_chunk_idempotency_and_conflict_detection(conn):
    session = _recording_session(conn)
    metadata = {
        "chunk_id": "76aa92c8-28cc-4fd7-a818-54ec457305f5",
        "session_id": session["id"],
        "source": "pc",
        "codec": "webm_opus",
        "chunk_seq": 0,
        "captured_at": "2026-08-17T00:00:00+00:00",
        "duration_ms": 1000,
        "content_sha256": hashlib.sha256(b"audio").hexdigest(),
    }
    accepted, record = db.reserve_audio_chunk(conn, **metadata)
    assert accepted is True
    duplicate, same = db.reserve_audio_chunk(conn, **metadata)
    assert duplicate is False
    assert same["chunk_id"] == record["chunk_id"]

    with pytest.raises(ValueError, match="chunk_id"):
        db.reserve_audio_chunk(conn, **{**metadata, "chunk_seq": 1})
    with pytest.raises(ValueError, match="chunk_seq"):
        db.reserve_audio_chunk(
            conn,
            **{**metadata, "chunk_id": "d6f734ad-a638-418c-adb5-55a1bc0be7fa"},
        )

    db.mark_audio_chunk_status(
        conn,
        metadata["chunk_id"],
        "failed",
        error_code="missing_predecessor",
    )
    retried, retried_record = db.reserve_audio_chunk(conn, **metadata)
    assert retried is True
    assert retried_record["status"] == "queued"


@pytest.mark.parametrize("error_code", ["service_restart", "service_shutdown"])
def test_interrupted_audio_chunk_is_retryable_without_advancing_sequence(
    conn, error_code
):
    session = _recording_session(conn)
    metadata = {
        "chunk_id": "76aa92c8-28cc-4fd7-a818-54ec457305f5",
        "session_id": session["id"],
        "source": "pc",
        "codec": "webm_opus",
        "chunk_seq": 0,
        "captured_at": "2026-08-17T00:00:00+00:00",
        "duration_ms": 1000,
        "content_sha256": hashlib.sha256(b"audio").hexdigest(),
    }
    db.reserve_audio_chunk(conn, **metadata)
    if error_code == "service_restart":
        db.init_db(conn)
    else:
        db.mark_audio_chunk_status(
            conn,
            metadata["chunk_id"],
            "failed",
            error_code=error_code,
        )

    assert db.get_next_audio_chunk_seq(conn, session["id"], "pc") == 0
    accepted, record = db.reserve_audio_chunk(conn, **metadata)
    assert accepted is True
    assert record["status"] == "queued"


def test_nonretryable_failed_chunk_is_consumed_for_sequence_recovery(conn):
    session = _recording_session(conn)
    metadata = {
        "chunk_id": "86aa92c8-28cc-4fd7-a818-54ec457305f5",
        "session_id": session["id"],
        "source": "pc",
        "codec": "webm_opus",
        "chunk_seq": 0,
        "captured_at": "2026-08-17T00:00:00+00:00",
        "duration_ms": 1000,
        "content_sha256": hashlib.sha256(b"audio").hexdigest(),
    }
    db.reserve_audio_chunk(conn, **metadata)
    db.mark_audio_chunk_status(
        conn, metadata["chunk_id"], "failed", error_code="config_missing"
    )

    assert db.get_next_audio_chunk_seq(conn, session["id"], "pc") == 1


def test_cancel_audio_source_chunks_is_scoped_idempotent_and_advances_sequence(conn):
    session = _recording_session(conn)

    def reserve(source: str, chunk_seq: int) -> str:
        chunk_id = f"{source}-{chunk_seq}-76aa92c8-28cc-4fd7-a818-54ec457305f5"
        db.reserve_audio_chunk(
            conn,
            chunk_id=chunk_id,
            session_id=session["id"],
            source=source,
            codec="webm_opus",
            chunk_seq=chunk_seq,
            captured_at=f"2026-08-17T00:00:0{chunk_seq}+00:00",
            duration_ms=1000,
            content_sha256=hashlib.sha256(
                f"{source}:{chunk_seq}".encode()
            ).hexdigest(),
        )
        return chunk_id

    pc_zero = reserve("pc", 0)
    pc_one = reserve("pc", 1)
    pc_two = reserve("pc", 2)
    mobile_zero = reserve("mobile", 0)
    db.mark_audio_chunk_status(
        conn, pc_one, "failed", error_code="service_shutdown"
    )
    db.mark_audio_chunk_status(conn, pc_two, "done")

    events = db.cancel_audio_source_chunks(
        conn,
        session["id"],
        "pc",
        1,
        "capture_stopped",
    )

    assert [event["payload"]["chunk_seq"] for event in events] == [0, 1]
    assert all(event["payload"]["status"] == "cancelled" for event in events)
    assert all(
        event["payload"]["error_code"] == "capture_stopped" for event in events
    )
    chunks = {
        (chunk["source"], chunk["chunk_seq"]): chunk
        for chunk in db.get_audio_chunks(conn, session["id"])
    }
    assert (chunks[("pc", 0)]["status"], chunks[("pc", 0)]["error_code"]) == (
        "cancelled",
        "capture_stopped",
    )
    assert (chunks[("pc", 1)]["status"], chunks[("pc", 1)]["error_code"]) == (
        "cancelled",
        "capture_stopped",
    )
    assert chunks[("pc", 2)]["status"] == "done"
    assert chunks[("mobile", 0)]["status"] == "queued"
    assert db.get_next_audio_chunk_seq(conn, session["id"], "pc") == 3

    assert (
        db.cancel_audio_source_chunks(
            conn,
            session["id"],
            "pc",
            1,
            "capture_stopped",
        )
        == []
    )
    persisted_events = db.get_events(conn, session["id"], 0, 200)
    cancelled_acks = [
        event
        for event in persisted_events
        if event["type"] == "chunk_ack"
        and event["payload"]["status"] == "cancelled"
    ]
    assert len(cancelled_acks) == 2
    assert {event["payload"]["chunk_id"] for event in cancelled_acks} == {
        pc_zero,
        pc_one,
    }
    assert mobile_zero not in {
        event["payload"]["chunk_id"] for event in cancelled_acks
    }


def test_audio_reservation_rechecks_radio_mode_inside_transaction(conn):
    session = db.create_session(conn, "模式事务屏障")
    db.start_session(conn, session["id"], "pc")
    metadata = {
        "chunk_id": "76aa92c8-28cc-4fd7-a818-54ec457305f5",
        "session_id": session["id"],
        "source": "pc",
        "codec": "webm_opus",
        "chunk_seq": 0,
        "captured_at": "2026-08-17T00:00:00+00:00",
        "duration_ms": 1000,
        "content_sha256": hashlib.sha256(b"audio").hexdigest(),
    }

    db.set_radio_mode(conn, session["id"], "mobile")
    with pytest.raises(db.AudioSourceNotAllowedError):
        db.reserve_audio_chunk(conn, **metadata)
    assert db.get_audio_chunks(conn, session["id"]) == []

    db.set_radio_mode(conn, session["id"], "pc")
    accepted, record = db.reserve_audio_chunk(conn, **metadata)
    assert accepted
    assert record["status"] == "queued"


def test_audio_chunk_final_state_is_persisted_for_reconciliation(conn):
    session = _recording_session(conn)
    metadata = {
        "chunk_id": "76aa92c8-28cc-4fd7-a818-54ec457305f5",
        "session_id": session["id"],
        "source": "pc",
        "codec": "webm_opus",
        "chunk_seq": 0,
        "captured_at": "2026-08-17T00:00:00+00:00",
        "duration_ms": 1000,
        "content_sha256": hashlib.sha256(b"audio").hexdigest(),
    }
    db.reserve_audio_chunk(conn, **metadata)
    db.mark_audio_chunk_status(conn, metadata["chunk_id"], "done")

    chunks = db.get_audio_chunks(conn, session["id"])
    assert chunks[0]["status"] == "done"
    events = db.get_events(conn, session["id"], 0, 200)
    chunk_events = [event for event in events if event["type"] == "chunk_ack"]
    assert [event["payload"]["status"] for event in chunk_events] == [
        "queued",
        "done",
    ]


def test_audio_chunk_terminal_state_cannot_be_rolled_back(conn):
    session = _recording_session(conn)
    metadata = {
        "chunk_id": "76aa92c8-28cc-4fd7-a818-54ec457305f5",
        "session_id": session["id"],
        "source": "pc",
        "codec": "webm_opus",
        "chunk_seq": 0,
        "captured_at": "2026-08-17T00:00:00+00:00",
        "duration_ms": 1000,
        "content_sha256": hashlib.sha256(b"audio").hexdigest(),
    }
    db.reserve_audio_chunk(conn, **metadata)
    db.mark_audio_chunk_status(conn, metadata["chunk_id"], "done")

    assert (
        db.mark_audio_chunk_status(
            conn,
            metadata["chunk_id"],
            "cancelled",
            error_code="session_ended",
        )
        is None
    )
    chunk = db.get_audio_chunks(conn, session["id"])[0]
    assert (chunk["status"], chunk["transcript_id"], chunk["error_code"]) == (
        "done",
        None,
        None,
    )


def test_cancelled_audio_chunk_cannot_create_transcript(conn):
    session = _recording_session(conn)
    metadata = {
        "chunk_id": "76aa92c8-28cc-4fd7-a818-54ec457305f5",
        "session_id": session["id"],
        "source": "pc",
        "codec": "webm_opus",
        "chunk_seq": 0,
        "captured_at": "2026-08-17T00:00:00+00:00",
        "duration_ms": 1000,
        "content_sha256": hashlib.sha256(b"audio").hexdigest(),
    }
    db.reserve_audio_chunk(conn, **metadata)
    db.mark_audio_chunk_status(
        conn,
        metadata["chunk_id"],
        "cancelled",
        error_code="session_ended",
    )

    with pytest.raises(ValueError, match="音频分片状态"):
        db.add_transcript(
            conn,
            session["id"],
            "pc",
            "不应写入",
            chunk_id=metadata["chunk_id"],
            chunk_seq=metadata["chunk_seq"],
            captured_at=metadata["captured_at"],
        )
    assert db.get_transcripts(conn, session["id"]) == []


def test_persistent_usage_budget_is_atomic(conn):
    limits = [(60, 10), (3600, 20)]
    db.reserve_usage(conn, ["token:a", "credential:b"], "llm_tokens", 6, limits)
    with pytest.raises(db.UsageLimitExceeded) as exc_info:
        db.reserve_usage(conn, ["token:a", "credential:b"], "llm_tokens", 5, limits)
    assert exc_info.value.service == "llm_tokens"
    assert exc_info.value.retry_after_seconds > 0


def test_plaintext_config_is_migrated_to_encrypted_secret(conn):
    conn.execute(
        "INSERT INTO configs (type, name, data, is_active) VALUES ('llm', 'legacy', ?, 1)",
        (
            '{"base_url":"https://llm.example/v1","model":"m","api_key":"legacy-secret"}',
        ),
    )
    conn.commit()
    db.init_db(conn)

    public = db.get_configs(conn, "llm")[0]
    active = db.get_active_config(conn, "llm")
    ciphertext = conn.execute("SELECT ciphertext FROM config_secrets").fetchone()[0]
    assert "api_key" not in public["data"]
    assert "legacy-secret" not in ciphertext
    assert active["data"]["api_key"] == "legacy-secret"


def test_legacy_llm_fields_are_migrated_to_global_prompt_and_removed(conn):
    conn.execute("DELETE FROM app_settings WHERE key = 'system_prompt'")
    conn.execute(
        "INSERT INTO configs (type, name, data, is_active) VALUES ('llm', 'legacy', ?, 1)",
        (
            (
                '{"base_url":"https://llm.example/v1","model":"m",'
                '"api_key":"legacy-secret","system_prompt":"你是后端面试专家",'
                '"audio_model":"old-audio"}'
            ),
        ),
    )
    conn.commit()

    db.init_db(conn)

    active = db.get_active_config(conn, "llm")
    assert active["data"] == {
        "base_url": "https://llm.example/v1",
        "model": "m",
        "api_key": "legacy-secret",
    }
    assert db.get_global_system_prompt(conn) == "你是后端面试专家"


def test_reviews_are_only_persisted_for_ended_sessions(conn):
    session = _recording_session(conn)
    with pytest.raises(db.SessionStateError):
        db.add_review(conn, session["id"], "过早复盘", "llm")
    db.end_session(conn, session["id"])
    review = db.add_review(conn, session["id"], "最终复盘", "llm")
    assert db.get_reviews(conn, session["id"])[0]["id"] == review["id"]
