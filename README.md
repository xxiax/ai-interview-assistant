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

> 改造前快照已保存于分支 `codex/current-code-snapshot`（commit `de6b26c`）。当前 `codex/question-thread-accumulator` 分支实现 `speech_end`、开放式 FunASR utterance、累计问题修订和悬浮提词窗。

## 已实现的端到端结构

一句话链路：**腾讯会议里对方的声音 → Windows loopback → 语音段切分 → FunASR 转写 → 问题线程累积 → LLM 流式答案 → 面试后复盘**。

~~~text
Windows 默认 eConsole 播放设备（WASAPI loopback，无麦克风）
  │  16 kHz / mono / s16le
  ├─ 20 ms RMS 窗静音门控（≥3 个有声窗才成片；静音不成片、不占序号）
  ├─ 满一个分片（AI_AUDIO_CHUNK_MS，默认 400 ms）→ audio_chunk；连续 1.6 秒静音 → 冲刷短尾片 + speech_end
  ▼
Rust outbox manifest（唯一 chunk_seq 分配者，WAV 落盘）
  ▼  WebSocket v1
FastAPI 单进程后端
  ├─ 幂等/顺序/背压/缺序等待/取消水位
  ├─ ffprobe 媒体校验
  ├─ FunASR（默认）| 多模态 LLM | Groq Whisper
  ├─ 累计 partial → 问题线程 revision++ → 并发 LLM 答案（answer_stream）
  ├─ speech_end + 宽限期到 → 关闭线程 → 只落库最高成功 revision
  └─ SQLite（WAL）：session_events 事件流 + event_id 重放
  ▼
Tauri 前端：一个问题一张卡（卡内每版「问题：partialN + 答案」一段、hr 分隔）、实时转写、会后复盘
~~~

1. Tauri 桌面端只保存访问令牌（后端地址写死为同机 `http://127.0.0.1:8000`），使用全局 Bearer Token 创建、查看、结束和删除会话。
2. 点击“开始采集”后，Tauri 只启动 Windows 默认播放设备的 WASAPI loopback，不请求麦克风权限。系统声音统一生成 16 kHz、单声道、PCM s16le WAV，正常分片长度由 `AI_AUDIO_CHUNK_MS` 决定（默认 400 ms，取值夹到 100–2500 并向下取整到 20 ms RMS 窗的整数倍），以片级低延迟方式触发转写和答案流程。分片越短，FunASR 首个累计 partial 出现得越早，第一版答案也来得越快。
3. WASAPI 采集线程按 20 ms RMS 窗做静音门控；一个分片至少有 3 个过阈值窗口才入队。静音/底噪不会分配 UUID、不会占用 `chunk_seq`，也不会上传或触发 ASR。连续静音达到 1.6 秒（80 个静音窗）时，客户端先冲刷携带 100 ms 尾部静音的短尾片，再发送轻量 `speech_end` 控制消息，把语音段边界告知后端。
4. PC 分片的持久 `chunk_seq` 只由 Rust outbox manifest 原子分配。停止采集、切到 `mobile` 或退出实时页时，客户端先停止系统声音线程，再关闭 capture gate；同一停止命令会冻结 outbox，并用 `cancel_audio_source(source, through_chunk_seq, reason)` 收敛服务端水位内的排队、可重试失败及当前 ASR 任务。
5. 后端持久化分片状态，按 `session + source` 排序、背压和对账。默认 `AI_ASR_ENGINE=funasr`：WAV 走 FunASR 转写；设置为 `llm` 才走激活 LLM 配置主 `model` 的多模态转写，设置为 `groq` 才走 Groq。受支持的非 WAV 编码走 Groq 兼容路径。
6. 默认 FunASR WAV 按 `session + source` 复用同一条 WebSocket，并且**一个语音段就是一个 utterance**：段首发一次 `start`，中间每个分片只推裸 PCM，直到客户端 `speech_end` 才发一次 `stop` 取段末 `final`。网关返回的 `partial` 是当前语音段的累计全文；后端经过几何节流后，把每个有效版本作为新 `revision` 立即送入 LLM。各 revision 不互相取消：同一会话最多 3 个真实并发生成，全局最多 4 个，超出部分在答案队列等待。只有线程关闭时才把最高成功 revision 写入 `answers`。前端按 `thread_id` 聚合成一张卡，并用 catch-up swap 保留旧答案直到新版内容追上，因此既能先看到前半句答案，也不会出现一个问题堆出多张大卡。分片推进网关后立即 ack `done`，不等段末 `final`；`speech_end` 后开启默认 6 秒宽限期，宽限内续说会把新语音段拼回同一问题线程。
7. `session_state`、`chunk_ack`、`transcript`、`answer` 写入 SQLite 事件流并通过 WebSocket 广播；LLM 生成期间的 `answer_stream` 增量只实时广播、不入库（其中的 `thread_id`、`revision`、`started`、`failed` 字段供前端按 `request_id` 区分卡内分段、按 `thread_id` 聚合成一张卡）；客户端持久化 `event_id`，支持断线重放和音频 outbox 对账。
8. 会话结束后，可以基于完整转写和答案生成幂等复盘；搜索增强默认关闭，只有显式启用时才查询 Google Custom Search。历史 Bing 配置会被拒绝或按不可用降级。
9. Google 结果只是清洗后的标题和摘要上下文，不包含文档摄取、Embedding、向量数据库或召回器，因此不是向量 RAG。
10. 桌面端另有一个可选的**悬浮提词窗**（第二个 WebView 窗口，惰性创建）：无边框、透明、始终置顶、可从任意位置收起成屏幕顶部居中的细条、可鼠标穿透（按住 `Ctrl` 临时可交互），并默认对操作系统的截屏/录屏/窗口共享隐身，用于在会议软件旁边实时读答案。8 个 `Ctrl+Alt+*` 全局热键覆盖显隐、穿透、隐身、透明度、截图解题、收起/展开和**开启/暂停录制**（最终语义：没录就一键走完 开始会话 → 系统采集 → 上传门禁；在录就 暂停/恢复 采集链路——停/起系统声音与上传门禁、会话保持进行中，**不结束面试**；聊到不需要找答案的内容时暂停，可省屏幕空间和调用费用）；顶栏有当前会话徽片（短 id + 点击复制），录制徽标按 会话状态 × 本机采集门 × 收音模式 分三态如实显示；窗口底部有**快速提问输入框**（Enter 发送、Shift+Enter 换行、随内容自动增高，答案独立成卡；暂停录制期间照常可问——门禁只看连接与会话状态，不看采集开关；按住 Ctrl 时滚轮可正常滚动答案区）；**背景不透明度**由 25%–100% 的滑条调节（只影响背景层，文字始终保持全亮可读；热键按 5% 步进）；答案正文可划选复制（chrome 不可选）；配色只有黑白灰（状态用亮度区分，提问区与答案区靠三档亮度分层）；位置、尺寸和开关落在 `overlay-layout.json`，重启后还原（但**不**自动弹窗）。细节见 [desktop-tauri/README.md](desktop-tauri/README.md#悬浮提词窗)。

后端还实现了音频幂等与对账、队列背压、缺序等待、服务重启后重试、配置密钥加密、SSRF 防护、Origin/Host/速率限制、付费服务并发门和持久化用量预算。配置 API 支持 `llm`、`search`、`asr`、`network` 四类配置的新增、脱敏读取、激活和删除；LLM 配置支持默认低、可选中/高的思考强度，普通模型会自动忽略不兼容的 `reasoning_effort`；全局提示词通过独立的设置 API 读取和修改；会话 API 也支持删除 idle/ended 会话并级联清理数据。每场面试还可以通过 `PUT /api/sessions/{id}/context` 设置**会话级答题背景**（岗位 JD + 简历，各 8,000 字符）：非空时注入答案与截图解题的提示词，让回答贴合该岗位和候选人真实经历；桌面端实时页有对应的编辑弹窗，面试中途修改保存后对下一个问题立即生效。

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

2026-09-14 当前工作树的实测结果：

- 后端 `python -m pytest -q`：**`260 passed`**；`python -m ruff check app tests scripts` 通过。覆盖 `speech_end`、开放式 utterance、累计 revision 真实并发、只落库最高成功版本、单 revision 失败隔离、重新生成失败不重复历史、JD/简历上下文、截图解题和启动恢复。
- Tauri 前端 `npm test`：**`170 passed` / `0 failed`**；`npm run build` 通过（包含 TypeScript 检查与 Vite 生产构建）。覆盖一问题一张卡、catch-up swap、悬浮窗、pending 状态、快捷键、截图解题和事件代际门。
- Rust `cargo test --locked`：**`72 passed` / `0 failed`**；`cargo fmt --check`、`cargo check --locked` 与 `cargo clippy --locked -- -D warnings` 均通过。
- 悬浮提词窗做过**真实窗口**验证（WebView2 CDP + `playwright-core`，含杀进程重启后布局还原）。注意 `overlay.rs` 的单元测试都是纯函数，对窗口行为零覆盖，曾经出现过「测试全绿而功能因主线程死锁 100% 挂」，验证悬浮窗只能靠真实窗口。
- 上述结果验证了协议、状态机、静音门控、语音段切分、问题线程累积、取消水位、有限重试等纯逻辑，但不等于 Windows 默认播放设备或腾讯会议场景已经真机可用。
- 真实 Windows WASAPI 系统声音、腾讯会议对方声音和真实 FunASR 转写结果，待网络可达后补充端到端验证。
- NSIS 安装包尚未在干净 Windows 环境完成安装、升级和卸载冒烟；容器镜像和公网部署也未验证。

## 当前限制

- 这是单用户、单 Token、单进程系统，不是多租户账号平台。
- WebSocket 连接、广播、队列和部分限流保存在进程内，必须使用一个 Uvicorn worker。
- SQLite 适合当前单实例规模；扩展多实例前必须引入共享数据库、跨实例消息总线和用户级授权。
- 当前已有可操作的 Tauri 桌面 UI，但移动端仍未实现。
- 悬浮提词窗的「共享隐身」只挡**操作系统的**截屏、录屏和窗口共享 API（Windows 上是 `WDA_EXCLUDEFROMCAPTURE`）。它挡不住采集卡、外接摄像头，也挡不住有人用手机拍屏幕，且依赖显卡驱动实现，个别环境可能失效。当前只在本机 Windows 上冒烟过，没有逐个验证腾讯会议 / Zoom / OBS，macOS 完全未验证。
- 旧 Electron `desktop/` 已退役并从当前工作树删除，任何桌面端改动都应落在 `desktop-tauri/`。
- 音频来源只区分 `pc` 与 `mobile`，不做说话人分离；当前 PC 只上传系统播放声音并标记为 `source=pc`。
- 仓库仍保留未被 LivePage 导入的麦克风 recorder/worklet 与对应单元测试，但当前生产实时流程不创建该 recorder，不会采集或上传用户回答的麦克风声音。
- 静音门控除了“不上传静音分片”，还会在连续 1.6 秒静音后发送 `speech_end`，后端据此结束 FunASR 语音段并在宽限期后关闭问题线程。同一个问题的累计 `partial` 经几何节流后形成多个 LLM revision；这些 revision 真实并发、互不取消，前端聚合成一张卡，线程关闭后只落库最高成功版本。宽限期、静音阈值、节流倍率和并发成本仍需在真实长时间面试中调参验证。
- Windows 系统声音当前只采集默认 `eConsole` Render endpoint 的 WASAPI loopback。腾讯会议若使用“默认通信设备”、独立指定声卡或蓝牙通话端点而不是同一个默认播放设备，当前实现会采集不到；真实设备兼容性仍待本轮真机结果补充。
- 后端取消水位本身保存在进程内；已经落库为 cancelled 的分片可恢复，但尚未 reserve 的空洞取消范围不会跨后端重启。客户端会持久化取消意图并在重连时补发，但极端的“取消后端随即重启、旧包随后迟到”仍需真机/故障测试。
- 容器文件目前只作为后续上线基线，不是当前主要开发路径。

## 给后续 AI 的阅读顺序

先记住这 11 条事实，可以避免绝大多数误判：

1. 这是**个人单用户**工具：一个全局 Bearer Token，一个 Uvicorn worker，SQLite 单文件。没有账号、权限、租户。
2. 音频**只来自 Windows 默认播放设备的 WASAPI loopback**（面试官的声音），**不采集麦克风**。`pc`/`mobile` 是采集来源标签，不是说话人。
3. 生产采集链路完全在 Rust (`src-tauri/src/system_audio.rs`)。`desktop-tauri/src/audio/`、`public/worklets/` 是保留的历史麦克风模块，**LivePage 不导入**，只被单元测试引用。
4. 静音门控 → 语音段切分 → `speech_end` → 后端问题线程，是当前最新、最容易被旧文档误导的一条链路。改这里前先读 `system_audio.rs` 的 `SpeechChunker` 和 `realtime.py` 的 `QuestionThread`。
5. 持久 `chunk_seq` **只由 Rust outbox manifest 分配**；React 里的 `chunk_seq` 只是 UI 占位。
6. Google 搜索增强**不是向量 RAG**：没有 Embedding、向量库、召回器。全仓库无相关代码。
7. `answer_stream` / `transcript_partial` / `error` / `pong` / `sync_complete` **不入库**；只有 `session_state` / `chunk_ack` / `transcript` / `answer` 进 `session_events`。
8. 会话状态严格 `idle -> recording -> ended`，由 SQLite 触发器强制；`recording` 会话不能删除。
9. 后端所有出网调用（LLM/Search/ASR）都过并发门 + 持久化预算 + SSRF 校验；LLM Base URL 默认强制 HTTPS 且拒绝私网。
10. 旧 Electron `desktop/` **已删除**，不要恢复。桌面端改动只落在 `desktop-tauri/`。
11. 悬浮提词窗的所有窗口操作都走 Rust `overlay_*` 命令，而且**每个都必须是 `async fn`**。Windows 上同步命令跑在主线程，走到 `WebviewWindowBuilder::build()` 会和 WebView2 的消息泵互锁，整个应用永久卡死；全局热键 handler 同理跑在事件循环上，只能 `spawn` 出去执行。这个 bug 真实发生过，而且纯函数单元测试全绿也测不出来——验证悬浮窗只能起真实窗口。

然后按顺序读：

1. 本文件，确认项目范围。
2. [backend/README.md](backend/README.md)，当前 API、WebSocket v1 协议和环境变量的权威契约。
3. [desktop-tauri/README.md](desktop-tauri/README.md)，现行桌面端边界、采集链路、运行方式和验证状态。
4. [历史设计与现状修订](docs/superpowers/specs/2026-08-13-ai-interview-assistant-design.md)，理解模块划分和完整数据流图。
5. 修改行为前直接核对 `backend/app/`、`backend/tests/`、`desktop-tauri/src/` 和 `desktop-tauri/src-tauri/src/`。
6. 不要继续实现旧 Electron 计划；`desktop/` 已删除，现行桌面端只在 `desktop-tauri/` 演进。
