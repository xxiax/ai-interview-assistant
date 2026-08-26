# AI 面试助手后端

本目录实现一个 FastAPI + SQLite 的单用户后端，负责会话状态、实时音频分片、FunASR/Groq 转写、FunASR WebSocket 连接复用与片级 partial/final、LLM 答案、可选搜索增强、断线恢复、配置管理和会后复盘。桌面与其他客户端的完成度以各自目录为准；本文只定义后端的当前行为。

本文是客户端和后续开发 AI 应使用的当前权威契约。代码仍是最终事实来源；历史计划中的无鉴权请求、明文 API Key 和旧 WebSocket 消息均已废弃。

## 1. 运行模型

- 应用版本：`1.0.0`。
- 目标生产运行时：Python 3.12；容器基线为 `python:3.12.11-slim-bookworm`。
- 当前是单用户、全局 Bearer Token 模式，不包含注册、登录、用户表或租户隔离。
- WebSocket 连接、广播、队列和部分限流在进程内，必须以 `--workers 1` 运行。
- SQLite 使用 WAL、外键、5 秒 busy timeout 和进程内写锁，适合当前单实例规模。
- `ffprobe` 是启动依赖；ASR 请求前还会验证媒体容器、编码、唯一音轨、实际时长和声明时长误差。
- `/health/ready` 先检查 SQLite；当 `AI_ASR_ENGINE=funasr` 时还会在 2 秒上限内探测 FunASR TCP/TLS 端点，但不会发送 Token 或业务音频。
- 默认 `AI_ASR_ENGINE=funasr`：WAV 走 FunASR 转写；设置为 `llm` 才使用激活 LLM 配置的主 `model` 做多模态转写，设置为 `groq` 或使用非 WAV 编码时走 Groq。旧 ASR 配置名称包含 `groq` 且模型误存 Paraformer 时，仍会兼容修正为 `whisper-large-v3`。
- ASR 出网代理按 `AI_OUTBOUND_PROXY`、active Network 配置、Windows 当前用户系统代理的顺序选择；Windows 代理兼容单地址和 `http=...;https=...` 格式。
- 容器部署文件已经存在，但当前阶段暂缓容器工作，尚未完成真实镜像和公网端到端验证。

## 2. 模块地图

| 文件 | 职责 |
|---|---|
| `app/main.py` | 加载环境、应用生命周期、中间件、安全响应头、路由和健康检查 |
| `app/models.py` | 严格 REST 请求/响应模型 |
| `app/protocol.py` | WebSocket v1 消息模型、`cancel_audio_source` 和版本校验 |
| `app/routes_sessions.py` | 会话、删除、转写、答案、音频状态和事件 REST API |
| `app/routes_configs.py` | LLM/Search/ASR/Network 配置保存、脱敏读取、激活和删除 |
| `app/routes_review.py` | ended 会话复盘、幂等缓存和 single-flight |
| `app/ws.py` | WebSocket 认证、事件同步、业务消息和连接管理 |
| `app/realtime.py` | 音频排序、背压、按来源取消水位、当前任务中止、FunASR 连接复用、片级最终转写、流式答案和队列 |
| `app/db.py` | SQLite 表、迁移、状态机、事件日志、幂等分片、持久取消结果和预算 |
| `app/security.py` | Token、Fernet、SSRF、Origin、限流和启动校验 |
| `app/cost_control.py` | LLM/Search/ASR 并发门和持久化用量预算 |
| `app/asr.py` | `ffprobe` 媒体校验、LLM/FunASR/Groq ASR 路由和出网代理 |
| `app/llm.py` | OpenAI-compatible 答案/复盘、提示注入隔离和输出上限 |
| `app/search.py` | Google 搜索增强和安全降级；不是向量数据库 RAG。Bing 已退役并移除 |

## 3. 本地安装与启动

### 3.1 安装

~~~powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
~~~

本地机器还必须安装 `ffprobe`，或通过 `AI_FFPROBE_PATH` 指向其可执行文件。

生成开发用服务 Token 和 Fernet 密钥：

~~~powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
~~~

把结果分别写入 `.env` 的 `AI_AUTH_TOKEN` 和 `AI_CONFIG_ENCRYPTION_KEY`。默认 `AI_ASR_ENGINE=funasr`，需要时再切换为 `llm` 或 `groq`。使用 FunASR 时必须设置 `AI_FUNASR_TOKEN`；源码不再提供凭据回退值。代码默认要求 HTTPS、拒绝凭据/query/fragment，并阻止解析到本机、私网和保留地址；当前不再要求额外的 LLM 主机白名单。仅本地调试时才显式开启 `AI_ALLOW_INSECURE_HTTP` 或 `AI_ALLOW_PRIVATE_LLM_HOSTS`。

### 3.2 启动

~~~powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
~~~

可选地通过 `AI_ENV_FILE` 指向其他环境文件。应用启动时会校验安全配置、`ffprobe` 可用性并初始化/迁移数据库；任一必要条件不满足都会启动失败。

## 4. 认证和健康检查

以下端点匿名可用：

| 方法 | 路径 | 语义 |
|---|---|---|
| `GET` | `/health` | 通用健康检查 |
| `GET` | `/health/live` | 进程存活检查 |
| `GET` | `/health/ready` | SQLite 可连接检查；FunASR 模式下额外探测 ASR TCP/TLS 端点 |

其他 REST API 必须发送：

~~~http
Authorization: Bearer <AI_AUTH_TOKEN>
~~~

REST 限流键由客户端 IP 和 Token 指纹共同组成。WebSocket 使用单独的认证、连接消息和重新生成限流。

## 5. 会话状态机

唯一正常路径是：

~~~text
idle -> recording -> ended
~~~

- 创建会话得到 `idle`。
- 只有 `idle` 可以开始并选择 `pc`、`mobile` 或 `both`。
- 只有 `recording` 可以切换收音模式、接收音频、写转写和答案。
- `end` 只能结束 `recording`；对已经 `ended` 的会话重复结束是幂等的。
- ended 后实时管线停止，仍排队的分片标为 `cancelled/session_ended`，并关闭该会话 WebSocket。
- 复盘只能对 `ended` 会话生成。

## 6. REST API

请求模型和响应模型都禁止未知字段。

### 6.1 会话与历史

| 方法 | 路径 | 请求/查询 | 说明 |
|---|---|---|---|
| `POST` | `/api/sessions` | `{"title":"..."}` | 创建会话，返回 `201` |
| `GET` | `/api/sessions` | `limit=50`，范围 1–200；`offset=0` | 按创建时间倒序分页 |
| `GET` | `/api/sessions/{id}` | — | 会话详情 |
| `POST` | `/api/sessions/{id}/start` | `{"radio_mode":"pc"}` | 开始；模式为 `pc/mobile/both` |
| `POST` | `/api/sessions/{id}/end` | — | 结束会话并停止实时任务 |
| `DELETE` | `/api/sessions/{id}` | — | 删除 idle/ended 会话并级联清理子数据；recording 返回 `409` |
| `GET` | `/api/sessions/{id}/transcripts` | — | 按会话内 `seq` 升序读取转写 |
| `GET` | `/api/sessions/{id}/answers` | — | 按 ID 升序读取答案 |
| `GET` | `/api/sessions/{id}/audio-chunks` | `source` 可选；跨来源游标为 `after_source + after_chunk_seq`；`limit=200`，范围 1–200 | 对账分片持久化状态 |
| `GET` | `/api/sessions/{id}/events` | `after_event_id=0`；`limit=200`，范围 1–200 | 按事件游标补拉 |

`audio-chunks` 按 `(source, chunk_seq)` 排序。指定 `source` 时，只需把上一页最后一项的 `chunk_seq` 作为 `after_chunk_seq`；不指定 `source` 时，必须同时把上一页最后一项的 `source` 和 `chunk_seq` 作为 `after_source`、`after_chunk_seq`。跨来源请求只传旧式 `after_chunk_seq` 会返回 `422`，避免 PC 与移动端各自从 0 编号时静默漏数据。

### 6.2 复盘

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| `POST` | `/api/sessions/{id}/review` | 可省略，或 `{"use_search":false}` | 生成并持久化复盘 |
| `GET` | `/api/sessions/{id}/reviews` | — | 按最新优先读取历史复盘 |

复盘规则：

- 会话必须 ended 且至少有一条转写。
- `use_search` 默认 `false`；只有显式为 `true` 才会把派生查询发送给已激活的搜索服务。
- 普通搜索故障降级为纯 LLM；搜索预算/并发耗尽返回带 `Retry-After` 的 `429`，不会继续产生额外 LLM 费用。
- 转写、答案和搜索结果都被序列化为不可信输入，不能覆盖系统指令。
- 输入总长超过 200,000 字符返回 `413`。
- 请求键由内容版本和 `use_search` 计算；同内容重复请求返回已有结果，并通过进程内锁避免并发重复调用。

### 6.3 配置

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/configs/{llm|search|asr|network}` | 读取脱敏配置列表 |
| `POST` | `/api/configs/{llm|search|asr|network}` | 保存配置，返回 `201` |
| `POST` | `/api/configs/{type}/activate/{id}` | 原子切换同类型唯一活动配置 |
| `DELETE` | `/api/configs/{type}/{id}` | 删除配置和对应密文，返回 `204` |
| `POST` | `/api/configs/llm/models` | 代理拉取第三方模型列表（"获取模型"按钮） |

LLM 配置请求：

~~~json
{
  "name": "main",
  "data": {
    "base_url": "https://api.example.com/v1",
    "api_key": "secret",
    "model": "model-name",
    "auth_field": "Authorization",
    "reasoning_effort": "low"
  },
  "is_active": true
}
~~~

`auth_field` 仅允许 `Authorization`、`X-API-Key`、`API-Key`（大小写不敏感）。`reasoning_effort` 可选 `low`、`medium`、`high`，默认 `low`，优先实时速度。只填写域名根地址时会自动补标准 `/v1` 前缀；已包含 `/v1` 或自定义路径时保持原值。LLM 请求调用 `{base_url}/chat/completions`，并校验流式响应 Content-Type、忽略标准 SSE 控制行。只有明确识别为 GPT-5、o1、o3、o4 系列的模型才发送 `reasoning_effort`，其他 OpenAI-compatible 模型保持 `temperature` 请求。

LLM 配置只保存服务地址、API Key、主 `model`、认证 Header 和 `reasoning_effort`。`AI_ASR_ENGINE=llm` 的 WAV 多模态转写直接复用该激活配置的主 `model`，不再单独配置音频模型。旧配置中的 `audio_model` 和 `system_prompt` 会在数据库初始化时移除；旧的 `system_prompt` 在尚未设置全局提示词时迁移为全局值。

全局提示词是独立于 LLM 配置的一份设置，所有 LLM 配置和实时回答共享：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/settings/prompt` | 读取全局提示词 |
| `PUT` | `/api/settings/prompt` | 写入或清除全局提示词，最大 8,000 字符 |

请求体为 `{"prompt": "你是一名资深后端面试官"}`。保存空字符串会清除自定义提示词并恢复内置提示词。全局提示词作用于实时回答和搜索增强回答；复盘和音频转写仍使用各自固定的系统提示词，服务端注入防护句始终保留。

`POST /api/configs/llm/models` 请求体为 `{"base_url": "https://...", "api_key": "", "auth_field": "Authorization"}`：`base_url` 与答案链路共用相同的 `/v1` 自动规范、HTTPS、DNS 和 SSRF 校验；`api_key` 为空时只有在规范化后的目标 URL 与当前激活配置一致时才复用已保存的 Key。服务端按 `auth_field` 调用 `{base_url}/models`，响应模型列表按 id 排序、去重、最多 200 项。

搜索配置请求：

~~~json
{
  "name": "google",
  "data": {
    "engine": "google",
    "api_key": "secret",
    "cx": "custom-search-engine-id"
  },
  "is_active": true
}
~~~

`engine` 仅支持 `google`；Google 必须提供 `cx`。Bing Web Search API 已于 2025-08 退役：新配置保存时直接返回 422 与明确错误，数据库中遗留的旧 `bing` 配置在运行时按不可用降级为纯 LLM，且不再消耗搜索预算。API Key 被拆出并用 Fernet 加密，读取接口不会返回密钥，只返回 `secret_configured`。

ASR 配置包含 `api_key` 和 `model`。激活配置的 `model` 以 `whisper` 开头时使用 Groq；为兼容旧客户端，配置名称包含 `groq` 也会选择 Groq，且名称为 Groq、模型却误存为 Paraformer 等非 Whisper 值时会修正为 `whisper-large-v3`。保存的 API Key 优先于 `GROQ_API_KEY` 环境变量。

Network 配置包含 `proxy_url`，支持 `http`、`https`、`socks5`、`socks5h`。ASR 代理优先级为 `AI_OUTBOUND_PROXY`、active Network 配置、Windows 当前用户 Internet Settings 系统代理；系统代理支持 `host:port` 和 `http=...;https=...` 两种常见写法。当前请求模型仍要求一个 `api_key` 字段并对其加密，但运行时代码只使用 `proxy_url`；不要把代理凭据嵌入 URL，因为 URL 属于公开配置数据。该配置接口与代理实现仍需在生产化前收敛。

### 6.4 ASR 路由与搜索增强边界

`AI_ASR_ENGINE=funasr` 时，WAV 走自部署 FunASR，且必须通过 `AI_FUNASR_TOKEN` 注入凭据。`AI_ASR_ENGINE=llm` 且分片为 `wav_pcm_s16le` 时，复用激活 LLM 配置的 `base_url`/`api_key`/`auth_field`/`model` 做多模态转写。`AI_ASR_ENGINE=groq` 或非 WAV 编码走 Groq。
- 所有路径都会先运行 `ffprobe` 媒体安全检查；FunASR 路径再从 WAV 中提取裸 PCM。
- Google 搜索只是按需向 LLM 提供最多 5 条清洗后的标题和摘要，没有文档摄取、Embedding、向量数据库、召回器或引用生成，因此不应称为向量 RAG。Bing Web Search API 已于 2025-08 退役并被移除。

## 7. WebSocket v1

连接地址：

~~~text
WS /ws/{session_id}
~~~

### 7.1 首包认证与同步

服务器接受连接后，第一条消息必须在 5 秒内发送：

~~~json
{
  "v": 1,
  "type": "authenticate",
  "token": "<AI_AUTH_TOKEN>",
  "last_event_id": 0
}
~~~

认证后服务器取得会话状态和事件快照，重放 `last_event_id` 之后的持久化事件，最后发送：

~~~json
{
  "v": 1,
  "type": "sync_complete",
  "session_id": "...",
  "latest_event_id": 12,
  "status": "recording",
  "radio_mode": "pc"
}
~~~

关闭码：

- `4401`：认证缺失、格式错误或 Token 错误。
- `4403`：浏览器 Origin 不在白名单。
- `4404`：会话不存在。
- `4429`：认证请求过于频繁。
- `1000`：会话正常结束。
- `1001`：服务关闭。

### 7.2 客户端消息

所有消息都必须携带 `"v": 1`，未知字段会被拒绝。

~~~json
{"v":1,"type":"start_session","radio_mode":"pc"}
{"v":1,"type":"set_radio_mode","mode":"both"}
{"v":1,"type":"cancel_audio_source","source":"pc","through_chunk_seq":17,"reason":"capture_stopped"}
{"v":1,"type":"regenerate_answer","question":"什么是 FastAPI？","use_search":false}
{"v":1,"type":"resume","after_event_id":12}
{"v":1,"type":"ping"}
{"v":1,"type":"end_session"}
~~~

音频消息：

~~~json
{
  "v": 1,
  "type": "audio_chunk",
  "chunk_id": "76aa92c8-28cc-4fd7-a818-54ec457305f5",
  "source": "pc",
  "codec": "webm_opus",
  "chunk_seq": 0,
  "captured_at": "2026-08-18T09:00:00+08:00",
  "duration_ms": 1000,
  "data": "<base64>"
}
~~~

字段约束：

- `chunk_id`：UUID；重试必须复用原 UUID。
- `source`：`pc` 或 `mobile`，且必须被当前 `radio_mode` 允许。
- `codec`：`webm_opus`、`ogg_opus`、`m4a_aac`、`wav_pcm_s16le`。
- `chunk_seq`：每个 `session_id + source` 独立，从 0 开始连续递增。
- `captured_at`：必须包含时区。
- `duration_ms`：100–10,000。
- `data`：严格 Base64；解码后默认最大 2 MiB。

`cancel_audio_source` 用于客户端明确结束某一来源的一轮采集：

- `source`：`pc` 或 `mobile`。
- `through_chunk_seq`：包含式取消水位，必须是 0 到后端 `CHUNK_SEQ_MAX` 范围内的严格整数。
- `reason`：只能是 `capture_stopped` 或 `source_disabled`。
- 请求会取消该会话/来源水位以内的 `queued`、带可重试错误码的 `failed` 以及当前正在执行的 ASR 任务，并为发生变化的分片写入持久化 `chunk_ack: cancelled`。
- 取消水位在进程内单调推进，重复请求幂等；晚到但序号不高于水位的旧分片会直接持久化为 cancelled。更高序号仍可作为后续新一轮采集继续进入队列。
- 取消水位本身目前不单独落库。已经写成 cancelled 的分片会跨重启保留，但尚未 reserve 的空洞取消范围不会跨后端重启；客户端持久取消意图并在重连补发可以覆盖常规路径，极端的“取消后立即重启、旧包随后迟到”仍可能返回来源不允许或序号冲突。
- `set_radio_mode: mobile` 也会由服务端自动取消当时已知的 PC 序号水位，防止旧 PC 分片在切换后触发 `source_not_allowed`。

### 7.3 分片可靠性

服务端在调用 ASR 前先保存 `audio_chunks` 记录和 `chunk_ack: queued` 事件：

- 相同 `chunk_id` 和完全相同元数据/内容重复发送不会重复转写，而是返回当前状态。
- 相同 `chunk_id` 对应不同内容，或相同来源/序号对应不同 UUID，会被拒绝。
- 每个会话/来源有独立有界队列；队列满返回 `audio_backpressure`，该新分片尚未预留，可以原样重发。
- 后到的序号先进入 pending；第一次等待缺失前序超时后标为 `failed/missing_predecessor` 并返回 `audio_sequence_gap`。同一缺口连续第二次超时会放弃该洞，从当前积压的最小序号继续；客户端仍必须依赖 outbox 和 REST 对账补传。
- `service_restart`、`service_shutdown`、`processing_failed`、`missing_predecessor`、`usage_limited` 是协议层可重试错误；发送同一分片会重新变成 `queued`。当前 Tauri 客户端对同一分片累计 3 次 `processing_failed` 后停止自动重试，并且中间 `queued` ACK 不清空失败次数。
- `session_ended` 和 `session_not_recording` 不属于可重试的实时状态，客户端应停止上传。
- `capture_stopped` 和 `source_disabled` 是显式取消终态。客户端停止、切来源或退出页面时应先关闭本地 capture gate，再发送取消水位；不能只停止录音 UI。
- 客户端可以通过 `GET /audio-chunks` 对账，不能只依赖当前连接上的即时回复。

当前 Tauri 的 PC 采集只使用 WASAPI loopback 获取电脑播放声，不请求本机麦克风；后端仍将其标记为 `source=pc`，不做说话人分离。

后端/Tauri 命令错误到达 LivePage 时可能是 JavaScript `Error`，也可能是 Tauri 直接返回的字符串。当前 LivePage 会把两种形状统一归一化为可显示文本，避免字符串错误因错误读取 `.message` 而产生空 toast。

`ffprobe` 会把声称的 codec 与实际容器/音频编码对照，并要求只有一个音轨。真实时长允许 100 毫秒到 `AI_MAX_AUDIO_DURATION_MS + 250` 毫秒（250 毫秒用于探测/取整余量）；声明时长误差不能超过 `max(AI_AUDIO_DURATION_TOLERANCE_MS, 真实时长的 20%)`。

### 7.4 事件、即时消息和恢复

写入 `session_events` 的持久化事件类型：

| 类型 | 内容 |
|---|---|
| `session_state` | 状态、收音模式、结束时间 |
| `chunk_ack` | 分片 queued/done/failed/cancelled 状态、错误码和转写关联 |
| `transcript` | 转写正文、来源、会话内序号和音频元数据 |
| `answer` | 问题、答案、来源和时间 |

`error`、`pong`、`sync_complete` 是纯传输消息。重复音频分片的当前状态也可能以没有新 `event_id` 的 `chunk_ack` 即时返回，因为没有发生新的持久化变更。

客户端必须：

1. 持久化自己处理完成的最大 `event_id`。
2. 按 `event_id` 去重。
3. 重连认证时传 `last_event_id`，或连接内发送 `resume`。
4. 在 `sync_complete` 后再认为快照同步完成。
5. 对音频另外维护 `chunk_id/chunk_seq` 状态，并使用 REST 对账。

`end_session` 的服务端时序说明：会话结束后端先写入 `session_state: ended` 事件并广播，随后取消在途任务、把仍在队列中的分片批量标为 `cancelled/session_ended`。这个批量取消**不会再逐个广播** `chunk_ack: cancelled`（避免连接关闭前的事件风暴）。因此客户端不应假设在连接关闭（关闭码 `1000`）之前能收齐所有被取消分片的 ACK；正确的终态来源是 `GET /api/sessions/{id}/audio-chunks` 对账结果。

### 7.5 实时转写和答案生成

当前默认 FunASR 路径按 `session + source` 复用同一条 WebSocket，但每个音频切片都独立执行 `start -> PCM -> stop -> final`；这样只避免重复握手，并没有把多片合成一个 FunASR utterance。每片 final 都会立即入库并驱动自动答案。partial 只广播到实时转写区，不做问题关键词、标点、长度或静音结束检测；当前协议也没有 `speech_end` 或“整个问题 final”。

LLM 答案请求使用 OpenAI-compatible SSE：每个 ASR final 都创建独立请求，同一会话由 `AI_LLM_SESSION_MAX_CONCURRENCY` 控制并发数（默认 3），全局由 `AI_LLM_MAX_CONCURRENCY` 控制（默认 4）。`answer_stream` 和最终 `answer` 都携带 `request_id`，前端可同时显示多个问题及其独立输出。只有完整生成的答案才写入 `answers` 表。答案上下文最多取最近 20 条转写，LLM 问题截断到 2,000 字符、上下文截断到 20,000 字符。

## 8. 数据库

| 表 | 用途 |
|---|---|
| `sessions` | 会话标题、状态、收音模式和时间 |
| `transcripts` | 会话内稳定序号、文本、来源和分片关联 |
| `answers` | 问题、答案、实际来源 |
| `audio_chunks` | 幂等键、内容摘要、序号和处理状态 |
| `session_events` | 可重放事件日志 |
| `reviews` | 复盘内容和请求版本键 |
| `configs` | 非秘密配置和激活状态 |
| `config_secrets` | Fernet 密文 |
| `usage_buckets` | 按服务、主体和时间窗口记录用量 |

启动迁移是非破坏性的，并会：

- 为旧转写重建稳定序号。
- 把旧 `configs.data.api_key` 迁移到加密表。
- 保证每种配置最多一个活动项。
- 把上次进程遗留的 `queued` 分片标为 `failed/service_restart`，写入 `chunk_ack` 事件，并允许相同分片重试。

实时排序的“下一个序号”会把 `done`、`cancelled` 和不可重试 failed 视为已消费终态，因此显式取消不会在下一轮采集制造永久缺序。Tauri 页面重进、WebSocket 重连或应用重启不会自动恢复旧采集；只有用户重新开始后产生的更高序号分片会继续上传。

## 9. 安全与成本边界

- `AI_AUTH_TOKEN` 至少 32 字符，比较使用常量时间算法。
- 配置密钥加密后入库，校验响应会遮蔽 `api_key/token/password/secret` 等字段。
- LLM Base URL 默认必须为 HTTPS，不得含内嵌凭据、query 或 fragment，并拒绝本机、私网、保留和异常 DNS 结果。当前没有 `AI_LLM_ALLOWED_HOSTS` 额外主机白名单。
- 浏览器 WebSocket Origin 必须精确命中白名单；`*` 在启动时被拒绝。原生客户端可以不发送 Origin。
- 服务设置安全响应头；生产可启用 Host 白名单、HTTPS 跳转和 HSTS。
- LLM、Search、ASR 各有全局并发门。
- 用量按服务 Token 指纹与供应商凭据指纹双层记入 SQLite 的分钟/小时/天预算。
- LLM 显式设置 `max_completion_tokens`；默认答案 512、复盘 2048。
- FunASR 地址由 `AI_FUNASR_URL` 配置；默认指向本机 `ws://127.0.0.1:10096/ws`。`AI_FUNASR_TOKEN` 必须通过 Secret Manager 注入。
- Network 配置目前只对代理 URL 做 scheme/hostname 校验，且代理运行时未消费其加密 `api_key` 字段；生产化前需要补齐秘密模型、失败策略和集成测试。

## 10. 环境变量

### 10.1 核心与媒体

| 变量 | 默认/要求 | 说明 |
|---|---|---|
| `AI_AUTH_TOKEN` | 必填，至少 32 字符 | REST 和 WebSocket 的服务访问令牌 |
| `AI_CONFIG_ENCRYPTION_KEY` | 必填 | Fernet 密钥；更换前必须迁移已有密文 |
| `AI_FUNASR_URL` | `ws://127.0.0.1:10096/ws` | FunASR WebSocket 地址；本地 SSH 隧道或同机宿主进程使用本机服务。Docker 容器访问宿主机时使用 `ws://host.docker.internal:10096/ws` |
| `AI_ASR_ENGINE` | `funasr` | `funasr` 使用 FunASR；`llm` 使用多模态 LLM；`groq` 启用 Groq |
| `AI_FUNASR_TOKEN` | FunASR 路径必填 | FunASR Token；只允许通过环境变量或 Secret Manager 注入 |
| `AI_FUNASR_STREAM` | `true` | 复用每个 `session + source` 的 WebSocket；当前每片仍独立发送 `start/PCM/stop` 并等待自己的 final |
| `AI_DB_PATH` | `interview.db` | SQLite 路径 |
| `AI_ENV_FILE` | `backend/.env` | 显式指定环境文件 |
| `GROQ_API_KEY` | Groq 路径调用时必填 | Groq Whisper Key；active ASR 配置中的密钥优先 |
| `ASR_LANGUAGE` | `zh` | 设为 `auto` 时不向 Groq 固定语言 |
| `AI_FFPROBE_PATH` | `ffprobe` | ffprobe 可执行文件 |
| `AI_SKIP_MEDIA_PROBE_CHECK` | 关闭 | 仅测试使用；生产禁止开启 |

### 10.2 网络与外部地址

| 变量 | 默认/要求 | 说明 |
|---|---|---|
| `AI_ALLOWED_ORIGINS` | 空 | 浏览器 Origin 白名单；逗号分隔，不允许 `*` |
| `AI_ALLOWED_HOSTS` | 空 | 可选 HTTP Host 白名单 |
| `AI_REQUIRE_HTTPS` | `false` | HTTPS 重定向与 HSTS |
| `AI_DOCS_ENABLED` | `false` | 是否开放 `/docs`、`/redoc` 与 `/openapi.json`；默认关闭，仅本地调试打开 |
| `AI_ALLOW_INSECURE_HTTP` | `false` | 仅本地调试允许 LLM HTTP |
| `AI_ALLOW_PRIVATE_LLM_HOSTS` | `false` | 仅本地调试允许本机、私网或保留地址 |
| `AI_OUTBOUND_PROXY` | 空 | ASR HTTP/WebSocket 出网代理最高优先级；未设置时依次使用 active Network 配置和 Windows 当前用户系统代理 |
| `AI_DNS_OVER_HTTPS_URL` | `https://dns.google/resolve` | 仅当系统 DNS 返回 `198.18.0.0/15` fake-IP 时用于解析 FunASR 公网地址 |

### 10.3 队列与请求限制

| 变量 | 默认 | 说明 |
|---|---|---|
| `AI_REST_RATE_LIMIT_PER_MINUTE` | 300 | 每 IP + Token 的 REST 上限 |
| `AI_MAX_AUDIO_CHUNK_BYTES` | 2,097,152 | Base64 解码后字节上限；启动校验最大 8 MiB |
| `AI_MAX_AUDIO_DURATION_MS` | 10,000 | 媒体探测上限基值；实际检查另有 250 毫秒余量，WebSocket 声明值仍固定最多 10,000 |
| `AI_AUDIO_DURATION_TOLERANCE_MS` | 750 | 声明/实际时长误差下限；实际容差取它与真实时长 20% 的较大值 |
| `AI_AUDIO_QUEUE_SIZE` | 8 | 每会话/来源的音频在途上限 |
| `AI_ANSWER_QUEUE_SIZE` | 8 | 每会话答案队列上限 |
| `AI_LLM_SESSION_MAX_CONCURRENCY` | 3 | 单个会话同时生成的独立答案数 |
| `AI_AUDIO_REORDER_WAIT_SECONDS` | 5 | 等待缺失序号的时长，允许 0.1–60 |
| `AI_FUNASR_FINAL_TIMEOUT_SECONDS` | 5 | 发送 stop 后等待 final 的最长时间 |
| `AI_FINAL_ANSWER_FLUSH_TIMEOUT_SECONDS` | 10 | 结束会话前等待已入队最终答案的最长时间 |
| `AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS` | 1 | 等待付费服务并发槽，允许 0.01–60 |

### 10.4 付费服务并发与预算

| 变量 | 默认 |
|---|---|
| `AI_LLM_MAX_CONCURRENCY` / `AI_SEARCH_MAX_CONCURRENCY` / `AI_ASR_MAX_CONCURRENCY` | 4 / 2 / 2 |
| `AI_LLM_TOKENS_PER_MINUTE` | 60,000 |
| `AI_LLM_TOKENS_PER_HOUR` | 300,000 |
| `AI_LLM_TOKENS_PER_DAY` | 1,000,000 |
| `AI_SEARCH_REQUESTS_PER_MINUTE` | 30 |
| `AI_SEARCH_REQUESTS_PER_HOUR` | 300 |
| `AI_SEARCH_REQUESTS_PER_DAY` | 2,000 |
| `AI_ASR_SECONDS_PER_MINUTE` | 300 |
| `AI_ASR_SECONDS_PER_HOUR` | 3,600 |
| `AI_ASR_SECONDS_PER_DAY` | 14,400 |
| `AI_LLM_ANSWER_MAX_COMPLETION_TOKENS` | 512 |
| `AI_LLM_REVIEW_MAX_COMPLETION_TOKENS` | 2,048 |
| `AI_LLM_TRANSCRIBE_MAX_TOKENS` | 2,048 |

变量的允许范围由 `app/security.py` 启动校验；不要只根据表格猜测可配置的最大值。

## 11. 测试与质量状态

~~~powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m pytest --cov=app --cov-report=term-missing -q
python -m ruff check app tests scripts
~~~

2026-08-26 对当前工作树执行完整后端测试：本地 Python `3.10.6` 为 `218 passed`，`ruff check app tests scripts` 通过。本轮没有重新生成覆盖率报告；此前 Python 3.10.6 与 3.12.11 的 `212 passed` 双运行时结果以及更小的测试数字都属于较早快照。正式发布仍需在目标 Python 3.12 复现当前 218 项测试。测试覆盖：

- 匿名健康检查、FunASR readiness 探测、Bearer/OpenAPI、安全响应头和启动失败保护。
- 会话状态机、ended 写屏障、复合游标分页、整数边界和音频状态对账。
- WebSocket 首包认证、Origin、严格协议、`cancel_audio_source`、事件补洞重放、并发发送去重和控制消息。
- 分片幂等、终态不可回退、冲突、乱序、缺口、背压、按来源取消水位、当前任务取消、晚到旧分片拒绝、结束/关闭取消、重启后跳过连续已消费终态和重试。
- `ffprobe` 失败关闭、codec 映射、伪造时长拒绝、FunASR 协议假实现、Groq/Paraformer 旧配置兼容、Windows 系统代理解析、Groq 回退和 ASR 全链路并发限制。
- LLM 输入隔离、输出形状、搜索降级、复盘幂等与输入上限。
- 四类配置的加密/脱敏/激活/删除、SSRF、限流、付费并发/预算、运行态锁释放、容器探针配置和 SQLite 备份恢复。

测试结果不代表真实 FunASR/Groq/LLM/Search、代理、Windows WASAPI/腾讯会议采集、容器镜像或生产网络已完成端到端验证。

## 12. 部署状态

部署资产与未来操作步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。不要把部署文件存在等同于已经上线或已经通过容器审查。依赖三件套（`requirements.in` / `requirements.lock` / `requirements.txt`）已收敛：lock 由 `uv pip compile --universal` 生成，同时携带 Windows 与 Linux 轮子哈希（`uvloop` 带 `sys_platform != 'win32'` 标记，Windows 上不会安装），`python-socks` 已进入 lock，Windows `pip install -r requirements.txt` 可正常解析。FunASR 令牌必须通过 `AI_FUNASR_TOKEN` 外部注入并按供应商流程轮换。
