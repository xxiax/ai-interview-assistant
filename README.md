# AI 面试助手

这是一个面向个人使用的 AI 面试辅助系统：客户端把电脑或手机采集的音频分片发送到后端，后端完成实时语音转写、答案生成、双端事件同步，并在会话结束后生成复盘报告。

## 当前状态

| 部分 | 状态 | 代码位置 |
|---|---|---|
| 后端 | 已实现；FastAPI + SQLite，提供 REST、WebSocket v1、ASR、答案、搜索增强和复盘 | `backend/` |
| 桌面客户端 | 已实现；当前唯一受支持的客户端为 Tauri 2 + React 19 + Rust | `desktop-tauri/` |
| 旧 Electron 客户端 | 已废弃并从当前工作树删除，不再维护或作为实现依据 | `desktop/`（已不存在） |
| 移动客户端 | 未实现 | 仓库中没有 Flutter 工程 |
| 容器部署 | 资产已准备，当前阶段暂缓；尚未完成真实容器构建与上线验证 | `backend/Dockerfile`、`backend/compose.prod.yml` |

后端契约以 [backend/README.md](backend/README.md) 和 `backend/app/` 为准；桌面端行为以 [desktop-tauri/README.md](desktop-tauri/README.md)、`desktop-tauri/src/` 和 `desktop-tauri/src-tauri/src/` 为准。历史设计和计划只用于理解背景，一旦与现行代码冲突，以代码为准。

## 已实现的端到端结构

1. Tauri 桌面端保存服务器地址和访问令牌，使用全局 Bearer Token 创建、查看、结束和删除会话。
2. 点击“开始采集”后，Tauri 只启动 Windows 默认播放设备的 WASAPI loopback，不请求麦克风权限。系统声音统一生成 16 kHz、单声道、PCM s16le WAV，正常分片长度为 2.5 秒，以片级低延迟方式触发转写和答案流程。
3. WASAPI 采集线程按 20 ms RMS 窗做静音门控；一个分片至少有 3 个过阈值窗口才入队。静音/底噪不会分配 UUID、不会占用 `chunk_seq`，也不会上传或触发 ASR。当前客户端不会为静音额外发送 `speech_end`，因此后端不知道“静音持续了多久”。
4. PC 分片的持久 `chunk_seq` 只由 Rust outbox manifest 原子分配。停止采集、切到 `mobile` 或退出实时页时，客户端先停止系统声音线程，再关闭 capture gate；同一停止命令会冻结 outbox，并用 `cancel_audio_source(source, through_chunk_seq, reason)` 收敛服务端水位内的排队、可重试失败及当前 ASR 任务。
5. 后端持久化分片状态，按 `session + source` 排序、背压和对账。默认 `AI_ASR_ENGINE=funasr`：WAV 走 FunASR 转写；设置为 `llm` 才走激活 LLM 配置主 `model` 的多模态转写，设置为 `groq` 才走 Groq。受支持的非 WAV 编码走 Groq 兼容路径。
6. 默认 FunASR WAV 按 `session + source` 复用同一条 WebSocket，但每个 2.5 秒切片独立执行 `start -> PCM -> stop -> final`。每个片级 final 都会创建独立 LLM 请求，同一会话默认最多 3 条并发生成；前端通过 `request_id` 同时展示多张流式答案卡片，不会互相替换问题标题。相邻 final 只在转写显示层合并，当前还没有跨切片的问题线程累积器或“整个问题 final”。
7. `session_state`、`chunk_ack`、`transcript`、`answer` 写入 SQLite 事件流并通过 WebSocket 广播；LLM 生成期间的 `answer_stream` 增量只实时广播、不入库；客户端持久化 `event_id`，支持断线重放和音频 outbox 对账。
8. 会话结束后，可以基于完整转写和答案生成幂等复盘；搜索增强默认关闭，只有显式启用时才查询 Google Custom Search。历史 Bing 配置会被拒绝或按不可用降级。
9. Google 结果只是清洗后的标题和摘要上下文，不包含文档摄取、Embedding、向量数据库或召回器，因此不是向量 RAG。

后端还实现了音频幂等与对账、队列背压、缺序等待、服务重启后重试、配置密钥加密、SSRF 防护、Origin/Host/速率限制、付费服务并发门和持久化用量预算。配置 API 支持 `llm`、`search`、`asr`、`network` 四类配置的新增、脱敏读取、激活和删除；LLM 配置支持默认低、可选中/高的思考强度，普通模型会自动忽略不兼容的 `reasoning_effort`；全局提示词通过独立的设置 API 读取和修改；会话 API 也支持删除 idle/ended 会话并级联清理数据。

同一分片累计收到 3 次 `processing_failed` 后，Tauri 会停止自动重试；中间收到 `queued` ACK 不会把失败计数清零。LivePage 收到本轮首个 ASR 处理故障时会熔断采集并取消积压，避免出现 `failed -> queued -> failed` 的永久循环。页面重进、WebSocket 重连或应用重启都不会自动恢复旧采集，用户需要重新点击“开始采集”。

Tauri `invoke` 失败既可能抛出 `Error`，也可能直接返回字符串。LivePage 会统一提取并字符串化错误后再显示 toast，不再因为读取不存在的 `.message` 而出现空提示。

ASR 出网代理优先级为 `AI_OUTBOUND_PROXY`、激活的 Network 配置、Windows 当前用户系统代理；Windows 系统代理兼容单地址及 `http=...;https=...` 写法。

## 仓库结构

| 路径 | 用途 |
|---|---|
| [backend/app](backend/app) | FastAPI 应用、REST/WebSocket、实时流水线、SQLite、安全和外部服务适配 |
| [backend/tests](backend/tests) | 后端自动化测试 |
| [backend/scripts](backend/scripts) | SQLite 在线备份与校验恢复脚本 |
| [backend/README.md](backend/README.md) | 当前后端权威说明、API、WebSocket 和环境变量 |
| [desktop-tauri](desktop-tauri) | 当前唯一受支持的桌面客户端；React 前端、Rust core、Tauri 构建配置 |
| [desktop-tauri/README.md](desktop-tauri/README.md) | 桌面端架构、功能、数据目录、开发和验证边界 |
| 旧 Electron `desktop/` | 已从当前工作树删除；不要恢复或继续演进 |
| [OUTLINE.md](OUTLINE.md) | 产品目标、已实现范围和后续路线 |
| [docs/superpowers/specs/2026-08-13-ai-interview-assistant-design.md](docs/superpowers/specs/2026-08-13-ai-interview-assistant-design.md) | 历史设计及按当前代码修订的架构边界 |
| [docs/superpowers/plans/2026-08-13-backend-core.md](docs/superpowers/plans/2026-08-13-backend-core.md) | 已归档的后端原始实施计划及现状对照 |
| [docs/superpowers/plans/2026-08-13-desktop-core.md](docs/superpowers/plans/2026-08-13-desktop-core.md) | 已归档的 Electron 桌面计划及 Tauri 现状对照 |
| [backend/DEPLOYMENT.md](backend/DEPLOYMENT.md) | 暂缓的后续部署参考 |

## 本地运行后端

目标生产运行时是 Python 3.12。`ffprobe` 必须可执行。

~~~powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
~~~

在 `.env` 中至少设置：

- `AI_AUTH_TOKEN`：至少 32 个字符。
- `AI_CONFIG_ENCRYPTION_KEY`：有效 Fernet 密钥。
- `AI_ASR_ENGINE`：默认 `funasr`，当前 WAV 使用 FunASR 转写；可切换为 `llm` 或 `groq`。
- `AI_FUNASR_TOKEN`：使用 FunASR 时必填；必须通过环境变量或 Secret Manager 注入，源码不再提供回退值。
- LLM Base URL 默认要求 HTTPS、无凭据/query/fragment，并拒绝解析到本机、私网或保留地址；本地调试可显式设置 `AI_ALLOW_INSECURE_HTTP` 或 `AI_ALLOW_PRIVATE_LLM_HOSTS`。
- `GROQ_API_KEY`：仅在 `AI_ASR_ENGINE=groq` 或非 WAV Groq 路径且没有通过 active ASR 配置保存密钥时需要。

然后启动单 worker：

~~~powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
~~~

健康检查：

- `GET /health`
- `GET /health/live`
- `GET /health/ready`

完整运行说明见 [backend/README.md](backend/README.md)。

## 本地运行桌面端

先启动后端，再在另一终端运行：

~~~powershell
cd desktop-tauri
npm install
npm test
npm run typecheck
npm run tauri:dev
~~~

完整桌面应用需要 Node.js 20+、Rust stable、MSVC target、Visual Studio C++ Build Tools 和 Windows SDK。生产构建命令为 `npm run tauri:build`，目标是 Windows NSIS 安装包。

## 验证状态

2026-08-26 当前工作树的后端验证为 `218 passed`；`ruff check app tests scripts` 已通过。此前 Python 3.10.6 与 3.12.11 的双运行时结果属于较早的 `212 passed` 快照，正式发布仍需在目标 Python 3.12 重新复现当前 218 项测试。
Tauri 前端 `npm test` 为 `92 passed`，覆盖系统声音采集停止边界、答案优先布局、手动提问框、partial 转写、并发流式答案、事件/历史去重、设置表单、全局提示词、Markdown、静音门控和错误归一化；`npm run build` 已通过。
- Rust `cargo test --locked` 为 `42 passed`；`cargo check --locked`、`cargo fmt --check` 和 `cargo clippy --locked -- -D warnings` 已通过。
- 上述结果验证了协议、状态机、静音门控、取消水位、有限重试和构建，但不等于 Windows 默认播放设备或腾讯会议场景已经真机可用。
- 真实 Windows WASAPI 系统声音、腾讯会议对方声音和真实 FunASR 转写结果，待网络可达后补充端到端验证。
- NSIS 安装包尚未在干净 Windows 环境完成安装、升级和卸载冒烟；容器镜像和公网部署也未验证。

## 当前限制

- 这是单用户、单 Token、单进程系统，不是多租户账号平台。
- WebSocket 连接、广播、队列和部分限流保存在进程内，必须使用一个 Uvicorn worker。
- SQLite 适合当前单实例规模；扩展多实例前必须引入共享数据库、跨实例消息总线和用户级授权。
- 当前已有可操作的 Tauri 桌面 UI，但移动端仍未实现。
- 旧 Electron `desktop/` 已退役并从当前工作树删除，任何桌面端改动都应落在 `desktop-tauri/`。
- 音频来源只区分 `pc` 与 `mobile`，不做说话人分离；当前 PC 只上传系统播放声音并标记为 `source=pc`。
- 仓库仍保留未被 LivePage 导入的麦克风 recorder/worklet 与对应单元测试，但当前生产实时流程不创建该 recorder，不会采集或上传用户回答的麦克风声音。
- 当前静音门控只负责“不上传静音分片”，没有 `speech_end` 控制消息；后端也没有逻辑问题边界。一次面试问题被切成多片时，每片会独立触发 LLM。
- Windows 系统声音当前只采集默认 `eConsole` Render endpoint 的 WASAPI loopback。腾讯会议若使用“默认通信设备”、独立指定声卡或蓝牙通话端点而不是同一个默认播放设备，当前实现会采集不到；真实设备兼容性仍待本轮真机结果补充。
- 后端取消水位本身保存在进程内；已经落库为 cancelled 的分片可恢复，但尚未 reserve 的空洞取消范围不会跨后端重启。客户端会持久化取消意图并在重连时补发，但极端的“取消后端随即重启、旧包随后迟到”仍需真机/故障测试。
- 容器文件目前只作为后续上线基线，不是当前主要开发路径。

## 给后续 AI 的阅读顺序

1. 先读本文件，确认项目范围。
2. 读 [backend/README.md](backend/README.md)，获取当前 API 和运行契约。
3. 读 [desktop-tauri/README.md](desktop-tauri/README.md)，获取现行桌面端边界、运行方式和验证状态。
4. 读 [历史设计与现状修订](docs/superpowers/specs/2026-08-13-ai-interview-assistant-design.md)，理解模块和数据流。
5. 修改行为前直接核对 `backend/app/`、`backend/tests/`、`desktop-tauri/src/` 和 `desktop-tauri/src-tauri/src/`。
6. 不要继续实现旧 Electron 计划；`desktop/` 已删除，现行桌面端只在 `desktop-tauri/` 演进。
