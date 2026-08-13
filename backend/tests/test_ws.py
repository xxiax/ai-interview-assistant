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
        # 连接后应先收到初始 session_state 广播
        msg = ws.receive_json()
        assert msg["type"] == "session_state"
        assert msg["radio_mode"] == "pc"
        # 发送一个音频分片（空音频，ASR 会返回空文本，但连接应正常）
        ws.send_json({"type": "audio_chunk", "source": "pc", "data": ""})
        # 发送收音模式切换
        ws.send_json({"type": "set_radio_mode", "mode": "both"})
        # 应收到切换后的 session_state 广播
        msg = ws.receive_json()
        assert msg["type"] == "session_state"
        assert msg["radio_mode"] == "both"


def test_ws_unknown_session():
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/nonexistent") as ws:
            ws.receive_json()
