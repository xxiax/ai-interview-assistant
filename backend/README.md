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
