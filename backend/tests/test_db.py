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
