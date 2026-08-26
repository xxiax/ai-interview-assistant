# AI 面试助手：历史设计与现状修订

> 初始日期：2026-08-13
> 最近按代码修订：2026-08-26
> 状态：这是历史设计文档的现状修订版；后端和 Tauri 桌面端已实现，移动端未实现，旧 Electron 客户端已废弃并从当前工作树删除，容器上线验证暂缓。

本文最初记录 2026-08-13 的方案，现已按当前代码纠正实现状态和技术边界。它不是逐步执行计划：后端接口以 [后端权威文档](../../../backend/README.md) 和 `backend/app/` 为准，桌面端以 [Tauri 客户端文档](../../../desktop-tauri/README.md)、`desktop-tauri/src/` 和 `desktop-tauri/src-tauri/src/` 为准。

## 1. 目标与范围

产品目标是让个人用户在面试中获得实时转写和 AI 回答提示，并在结束后得到复盘。

当前仓库实际实现：

- 单用户、单 worker 的 FastAPI + SQLite 后端。
- SQLite 会话、转写、答案、音频分片、事件、配置、复盘和预算数据。
- REST 会话/配置/复盘接口，包括会话删除和配置删除。
- 可认证、可恢复的 WebSocket v1。
- 默认 `AI_ASR_ENGINE=funasr`：WAV 走 FunASR；设置为 `llm` 才使用激活 LLM 配置的主 `model` 做多模态转写，设置为 `groq` 或使用非 WAV 编码时走 Groq。
- OpenAI-compatible LLM，以及 Google 可选搜索增强；Bing 新配置已退役，搜索不是向量 RAG。
- `llm`、`search`、`asr`、`network` 四类服务端配置的新增、脱敏读取、激活和删除。
- Tauri 2 + React 19 + TypeScript + Rust 的 Windows 桌面客户端，开始采集时只使用默认播放设备 WASAPI loopback，并包含静音门控、本地 outbox、取消水位、断线恢复、历史和设置界面。
- 后端测试、SQLite 备份恢复脚本和未完成实测的生产容器资产。

当前仓库没有：

- Flutter iOS/Android 客户端。
- 多用户账号、权限或租户系统。
- 跨实例消息总线或分布式任务队列。
- 生产监控、集中日志和已验证的公网部署。

早期 Electron `desktop/` 实现已经退役并从当前工作树删除。现行唯一桌面客户端是 `desktop-tauri/`。

## 2. 系统上下文

~~~text
┌──────────────────────┐       ┌──────────────────────┐
│ Tauri 2 桌面客户端    │       │ 未来移动客户端        │
│ React / TypeScript   │       │ Flutter              │
│ Rust core            │       └──────────┬───────────┘
└──────────┬───────────┘                  │
           └────── REST + WebSocket v1 ───┘
                           │
                ┌──────────▼───────────┐       ┌──────────────────────┐
                │ FastAPI 单进程后端    │──────>│ FunASR / Groq        │
                │ 会话 / 实时流水线     │──────>│ OpenAI-compatible LLM│
                │ 安全 / 成本控制       │──────>│ Google 搜索          │
                │ 配置 / 删除 / 复盘    │──────>│ 可选出网代理          │
                └──────────┬───────────┘       └──────────────────────┘
                           │
                ┌──────────▼───────────┐
                │ SQLite               │
                │ WAL + 持久化事件流    │
                └──────────────────────┘
~~~

Tauri 桌面端和后端方框代表已有代码；移动客户端仍是未来目标。旧 Electron 客户端不属于现行架构。

## 3. 后端组件

| 组件 | 代码 | 设计责任 |
|---|---|---|
| 应用边界 | `main.py` | 环境加载、启动校验、数据库初始化、中间件、健康检查、优雅关闭 |
| REST 模型 | `models.py` | 严格字段、长度、枚举、SecretStr 和响应类型 |
| WS 协议 | `protocol.py` | `v: 1`、首包认证、`cancel_audio_source`、业务消息分派、拒绝未知字段 |
| 实时入口 | `ws.py` | Origin/Token、快照同步、事件重放、连接广播、错误和关闭码 |
| 实时流水线 | `realtime.py` | 按来源排序、背压、取消水位、当前任务中止、默认 FunASR 连接复用、片级 final、答案并发、结束/关闭清理 |
| 持久层 | `db.py` | 状态机、事务、幂等、取消终态、事件、秘密、复盘和预算 |
| 外部服务 | `asr.py`、`llm.py`、`search.py` | 媒体校验、第三方请求、输出验证和降级 |
| 防护 | `security.py`、`cost_control.py` | 认证、加密、SSRF、限流、并发和预算 |

同步 SQLite 操作通过 `asyncio.to_thread` 移出事件循环。写事务使用进程内可重入锁和 `BEGIN IMMEDIATE`。

### 3.1 Tauri 桌面端组件

| 组件 | 代码 | 设计责任 |
|---|---|---|
| React 界面 | `desktop-tauri/src/pages/`、`src/stores/` | 会话列表、实时转写与答案、历史复盘、四类配置、删除交互和 Tauri 字符串错误归一化 |
| 音频采集 | `desktop-tauri/src-tauri/src/system_audio.rs` | Windows 默认播放设备 WASAPI loopback、16 kHz 单声道 PCM、2.5 秒 WAV 和 20 ms RMS 静音门控；麦克风模块保留但当前不启用 |
| 系统声音采集 | `desktop-tauri/src-tauri/src/system_audio.rs` | Windows 默认 `eConsole` Render endpoint WASAPI loopback、16 kHz 单声道 PCM、2.5 秒组帧、静音门控和停止尾片丢弃 |
| 前后端桥 | `desktop-tauri/src/api/bridge.ts` | Tauri `invoke` / `listen` 的类型化封装 |
| Tauri 命令 | `desktop-tauri/src-tauri/src/lib.rs` | 设置、会话、历史、配置、live 引擎和音频命令注册 |
| 网络与恢复 | `rest.rs`、`ws_client.rs`、`engine.rs`、`outbox.rs`、`reconcile.rs` | REST/WS、capture gate、Rust 唯一序号分配、取消意图、有限重试、游标、重连、落盘 outbox 和对账 |
| 本地秘密 | `settings.rs` | 服务器地址文件和 Windows Credential Manager 访问令牌 |

## 4. 核心领域模型

### 4.1 会话

~~~text
idle --start(pc|mobile|both)--> recording --end--> ended
~~~

没有暂停或重新打开 ended 会话的状态。会话结束是写屏障：后续音频、转写和答案写入被拒绝。

`radio_mode`：

- `pc`：只允许 `source=pc`。
- `mobile`：只允许 `source=mobile`。
- `both`：两种来源都允许。

来源表示采集设备，不表示说话人。

### 4.2 音频分片

分片身份与顺序由以下字段构成：

- `chunk_id`：全局 UUID 幂等键。
- `session_id`。
- `source`：`pc/mobile`。
- `chunk_seq`：每个会话/来源从 0 开始的连续序号。
- `codec`。
- `captured_at`：带时区时间。
- `duration_ms`。
- 解码后音频内容 SHA-256。

对于当前 PC 客户端，Rust outbox manifest 是持久 `chunk_seq` 的唯一分配者。WASAPI 线程只提交候选分片；静音门控未通过时不会创建 UUID、不会占用序号，也不会写入 outbox。当前 PC 只上传系统播放声音并标记为 `source=pc`，不是说话人标签。

持久化状态：

~~~text
queued -> done
       -> failed
       -> cancelled
~~~

可重试错误码：

- `missing_predecessor`
- `processing_failed`
- `service_restart`
- `service_shutdown`
- `usage_limited`

客户端重试必须发送完全相同的元数据和音频内容，并复用原 `chunk_id`。`session_ended` 和 `session_not_recording` 不是恢复实时处理的条件。

同一 Tauri 分片累计 3 次 `processing_failed` 后进入本地终态；中间 `queued` ACK 不清空累计失败次数。停止采集、切到 `mobile` 或退出实时页会冻结 outbox，并通过 `cancel_audio_source(source, through_chunk_seq, reason)` 收敛水位以内的工作。

### 4.3 转写、答案和复盘

- 每条转写有会话内单调 `seq`，并可关联音频 `chunk_id/chunk_seq`。
- 答案记录问题、答案正文和实际来源 `llm/search+llm`；生成中的流式内容只通过 `answer_stream` 消息展示，完成后才写入历史。
- 复盘只属于 ended 会话，不写入实时事件流。
- 复盘请求键包含转写、答案、协议版本和 `use_search`；相同内容版本复用结果。

## 5. 实时处理流程

~~~text
audio_chunk
  │
  ├─ Base64/大小/模式校验
  ├─ 持久化 audio_chunks(queued) + chunk_ack 事件
  ├─ 每 session/source 有界队列
  ├─ 按 chunk_seq 等待和排序
  ├─ ffprobe 校验实际媒体
  ├─ AI_ASR_ENGINE=llm + WAV + active LLM model → 多模态 LLM
  │    └─ 激活 Whisper 或名称为 Groq 的兼容配置时改走 Groq
  ├─ 非 WAV 编码当前走 Groq
  ├─ transcript 事件 + chunk_ack(done) 事件
  ├─ 每个片级 final 独立创建 AnswerWork
  └─ 每 session 答案队列与并发任务
       ├─ 最近转写上下文
       ├─ 可选搜索
       ├─ LLM
       └─ answer 事件
~~~

`audio_chunk` 之前的当前 PC 采集链路为：

~~~text
用户点击开始采集
  └─ 电脑播放声：Windows 默认 eConsole 播放设备 WASAPI loopback
       │
       ├─ 转换为 16 kHz / mono / PCM s16le
       ├─ 每 2.5 秒形成候选窗口
       ├─ 20 ms RMS 窗，至少 3 个有效窗
       ├─ 非静音候选 → UUID → Rust manifest 分配唯一 chunk_seq
       └─ 静音候选直接丢弃；当前不发送 speech_end
~~~

系统声音初始化失败时，LivePage 停止启动并显示具体错误。真实 Windows 设备和腾讯会议声音采集结果仍待本轮真机验证，不能从编译或单元测试推断已可用。

系统播放声由 WASAPI loopback 单独采集，不依赖麦克风拾取扬声器里的对方声音；后端将其标记为 `source=pc`，不负责说话人分离。

LivePage 的命令错误边界接受 `unknown`：标准 `Error` 读取非空 `message`，Tauri 直接返回的字符串则通过 `String(error)` 保留。两种形状最终都进入同一 toast 路径，不再出现字符串错误被误当成对象后显示空白的情况。

### 5.1 排序与背压

- 音频队列按 `session + source` 隔离，默认最多 8 个在途分片。
- `pc` 与 `mobile` 独立排序，不保证两个来源之间的全局音频顺序。
- 后序分片先到时进入 pending；默认等待 5 秒。
- 缺失前序超时后，pending 分片进入 `failed/missing_predecessor`，客户端补齐缺口后可以重发。
- 队列容量在持久化新分片前检查；返回 `audio_backpressure` 时新分片尚未预留。

### 5.2 媒体校验

ASR 前通过 `ffprobe` 失败关闭：

- codec 元数据必须与实际容器和音频编码匹配。
- 必须恰好有一条音频流。
- 实际时长必须在 100 毫秒到 `AI_MAX_AUDIO_DURATION_MS + 250` 毫秒之间。
- 声明/实际时长误差不能超过 `max(AI_AUDIO_DURATION_TOLERANCE_MS, 真实时长的 20%)`。

通过后按 `AI_ASR_ENGINE` 路由：`llm` 使用激活 LLM 的主 `model` 处理 WAV；默认 `funasr` 的 WAV 按 `session_id + source` 复用 WebSocket，但无论连接是否复用，每个切片都独立执行 `start -> PCM -> stop -> final`；`AI_FUNASR_STREAM=false` 只会退回“每片重新建连”的兼容路径。`groq` 和非 WAV 编码走 Groq。默认路径收到片级 final 后新增 transcript 并触发答案；不再做问题关键词、标点、长度或静音结束检测。

### 5.3 实时转写和答案

- 默认 FunASR 每个 `session + source` 复用一条 WebSocket；每个切片独立执行 `start -> PCM -> stop -> final` 并创建独立 LLM 请求。会话内答案有界并发，`request_id` 用于隔离交错到达的流式事件；相邻 final 只在转写 UI 显示层合并。
- `partial` 只通过非持久化 `transcript_partial` 事件显示在实时转写区，不触发 LLM。
- `final` 到达后才写入历史，清除临时转写，更新最终答案。
- LLM 使用 OpenAI-compatible SSE；每段上游增量立即通过非持久化 `answer_stream` 广播，不增加逐字符人工延迟，流结束后才写入 `answers` 并广播持久化 `answer`。
- 当前没有客户端 `speech_end`、服务端 endpoint gap 或逻辑问题线程；“没有新分片”不会触发任何控制消息。停止整个会话前仍会 flush 已在处理的任务。
- 客户端保留手动 `regenerate_answer`，用于用户主动重算答案。

## 6. 同步与恢复协议

### 6.1 持久化事件

`session_events` 保存：

- `session_state`
- `chunk_ack`
- `transcript`
- `answer`

每个事件有数据库自增 `event_id`。客户端可以通过 REST 或 WebSocket 重放游标后的事件。

### 6.2 建连同步

1. WebSocket 接受连接。
2. 客户端在 5 秒内发送 `authenticate` 和 `last_event_id`。
3. 服务端校验 Origin、Token 和会话。
4. 在会话同步锁内取得状态 + 最新事件 ID 的一致快照。
5. 重放游标之后且不超过快照上界的事件。
6. 发送 `sync_complete`。
7. 同步期间产生的新事件随后广播。

连接管理器维护每个连接的事件水位，避免同一连接重复发送已确认的事件。客户端仍要持久化最大 `event_id` 并自行去重。

### 6.3 音频恢复

事件恢复不能替代音频对账：

- 新建/重试/状态变化产生持久化 `chunk_ack`。
- 完全重复且没有状态变化的分片可能只收到没有新 `event_id` 的即时 `chunk_ack`。
- 客户端应维护本地分片表，并调用 `GET /api/sessions/{id}/audio-chunks` 对账。
- 服务启动时把旧 `queued` 改为 `failed/service_restart` 并写事件；优雅关闭把未完成分片改为 `failed/service_shutdown`。
- 两种情况都允许相同分片重试。

当前 Tauri 不会在页面重进、WebSocket 重连或应用重启时自动重新打开采集门或恢复旧采集。停止链路先使旧回调失效并停止 WASAPI，再等待已经开始的 renderer invoke，最后关闭 capture gate；门关闭后的迟到 `AddChunk` 在分配序号和落盘之前被拒绝。

`cancel_audio_source` 的协议字段为 `source`、包含式 `through_chunk_seq` 和 `reason`。原因只允许 `capture_stopped`、`source_disabled`；内部 `capture_interrupted` 发送前规范化为 `capture_stopped`。后端在线性化锁内取消水位以内的 queued、可重试 failed、pending/queue 项和当前 ASR task，广播持久化 cancelled ACK，并拒绝水位内迟到旧片；更高序号的新一轮采集不受影响。取消水位本身仍是进程内状态：已落库 cancelled 分片可恢复，但未 reserve 的空洞范围不会跨后端重启。

## 7. 外部服务与信任边界

### 7.1 LLM

- 配置为 OpenAI-compatible Base URL、模型和认证 Header。
- 固定请求 `{base_url}/chat/completions`。
- LLM 配置支持 `reasoning_effort=low|medium|high`，默认 `low`；只有 GPT-5、o1、o3、o4 系列模型发送该参数，普通 OpenAI-compatible 模型省略它并保留 `temperature`。
- 只允许 `Authorization`、`X-API-Key`、`API-Key`。
- 问题、上下文、转写、答案和搜索结果都序列化为不可信数据。
- 答案和复盘分别有可配置的最大 completion tokens。
- 严格检查上游 JSON 和返回文本。

### 7.2 搜索

- 支持 Google Custom Search；Bing Web Search API 已退役，新配置会被拒绝，遗留配置按不可用降级。
- 没有活动配置时返回空结果。
- 普通搜索故障降级为空结果。
- 最多 5 条标题和摘要在清洗、限长后送入 LLM。
- 自动问题答案默认不搜索；手动重生成和复盘必须显式 `use_search=true`。
- 没有文档摄取、Embedding、向量数据库、召回器或引用生成，因此这不是向量 RAG。

### 7.3 ASR

- `AI_ASR_ENGINE=funasr` 时，`wav_pcm_s16le` 走自部署 FunASR WebSocket并从 WAV 中提取裸 PCM；`AI_ASR_ENGINE=llm` 时才使用激活 LLM 的主 `model`。
- `asr` 配置可保存 Groq API Key 和模型；模型以 `whisper` 开头或配置名称包含 `groq` 时选择 Groq。旧 Groq 配置若误存 Paraformer 等非 Whisper 模型，会修正为 `whisper-large-v3`；保存的密钥优先于 `GROQ_API_KEY` 环境变量。
- 受支持的非 WAV 编码当前走 Groq。
- ASR 出网代理优先级为 `AI_OUTBOUND_PROXY`、active `network` 配置、Windows 当前用户系统代理；系统代理支持单地址及 `http=...;https=...`。当前接口和代理秘密模型仍需在生产化前收敛。
- 媒体内容、声明元数据和第三方响应都不被信任。
- 调用消耗按音频声明秒数预留持久化预算。

## 8. 安全模型

当前安全边界是“持有全局 Token 的单一可信用户”：

- REST Bearer Token 和 WebSocket 首包 Token 使用常量时间比较。
- 配置 API Key 用 Fernet 加密，响应只显示 `secret_configured`。
- Tauri 桌面端访问令牌保存在 Windows Credential Manager，不写入 `settings.json`。
- LLM URL 默认只允许 HTTPS、无凭据、无 query/fragment 和非私网 DNS 结果；当前不再维护额外的主机白名单。
- 浏览器 Origin 精确白名单且禁止 `*`；原生客户端可以省略 Origin。
- 可选 Trusted Host、HTTPS 重定向和 HSTS。
- REST、WS 认证、WS 消息和答案重新生成分别限流。
- 验证错误脱敏，安全响应头默认关闭缓存和嵌入。

这个模型不适合多个互不信任用户。引入用户前必须重新设计数据所有权、鉴权、审计和预算主体。

## 9. 成本与容量

- LLM、Search、ASR 各有全局 `asyncio.Semaphore`。
- 等待并发槽超时会返回/广播可重试的繁忙错误。
- SQLite `usage_buckets` 按分钟、小时、天窗口原子预留。
- 同时按服务 Token 指纹和供应商凭据指纹计数，避免跨会话绕过。
- LLM 以估算输入 Token + 最大输出 Token 预留。
- Search 以请求次数预留。
- ASR 以声明音频秒数预留。

当前是保护性上限，不是精确账单系统；预算不会在上游实际消耗低于预留时返还。

## 10. 持久化设计

~~~text
sessions
  ├─ transcripts
  ├─ answers
  ├─ audio_chunks
  ├─ session_events
  └─ reviews

configs
  └─ config_secrets

usage_buckets
~~~

关键唯一性：

- `transcripts(session_id, seq)`。
- 非空 `transcripts.chunk_id`。
- `audio_chunks.chunk_id`。
- `audio_chunks(session_id, source, chunk_seq)`。
- 每种 `llm/search/asr/network` config 只有一个活动项，并可删除配置及对应密文。
- `reviews(session_id, request_key)`。

## 11. 已验证与未验证

已验证：

- 2026-08-26 使用本地 Python `3.10.6` 执行后端当前测试集，结果为 `218 passed`；`ruff` 通过，本轮没有重新生成覆盖率报告。此前 Python 3.10.6 与 3.12.11 的 `212 passed` 属于较早快照；目标 Python 3.12 仍需重新复现当前测试集。
- 后端自动化测试覆盖状态机、认证、幂等、乱序、重放、关闭恢复、FunASR 协议假实现、Groq 回退、四类配置、删除、成本限制、密钥加密、SSRF、复盘和备份恢复。
- Tauri 前端 `npm test` 为 `92 passed`，覆盖系统声音采集、答案优先布局、手动提问框、partial 转写、并发流式答案、事件去重、配置表单、全局提示词、Markdown、静音门控和错误显示；本次发布仍需重新执行 `npm run build`。
- Rust `cargo test --locked` 为 `42 passed`；`cargo check --locked`、`cargo fmt --check` 与严格 Clippy 通过。

未验证：

- 真实 Windows 默认播放设备 WASAPI loopback、腾讯会议对方声音，以及静音/停止/切换在真实进程中的结果；待本轮真实设备验证结果补充。
- WASAPI 当前只选择默认 `eConsole` Render endpoint；腾讯会议若使用默认通信设备、独立声卡或蓝牙通话端点会漏采。
- 取消后端与后端重启并发时，未 reserve 空洞水位不持久化的极端迟到包行为。
- 真实系统声音到 FunASR/Groq/LLM、Google、代理的完整用户流程。
- 桌面端断线、应用重启、服务重启、积压发送和对账的端到端恢复结果。
- NSIS 安装包在干净 Windows 上的安装、升级和卸载冒烟。
- 真实移动客户端。
- Docker 镜像、Compose、Caddy TLS 和公网 WSS。
- 多日稳定性、负载、磁盘满、第三方大规模故障。
- 多实例和多用户。

## 12. 后续设计要求

### 桌面与移动客户端

现行桌面端只在 `desktop-tauri/` 演进；不要恢复已经删除的 Electron `desktop/`。下一步应扩充现有 Tauri 自动化测试并完成真实设备端到端验证。未来移动客户端必须复用 WebSocket v1 约束，并至少设计：

- Token 的平台安全存储。
- 每来源连续 `chunk_seq` 和稳定 UUID。
- 本地 durable outbox、重传、`chunk_ack` 和 REST 对账。
- `event_id` 游标持久化、重放、去重。
- 录音 codec 与后端媒体校验匹配。
- 网络切换、权限拒绝、后台限制、背压和预算耗尽 UX。

### 已确认的下一阶段实验（尚未实现）

当前方案把“2.5 秒有效音频片”“FunASR final”“完整面试问题”错误地绑定在同一层：每片都有自己的 ASR final 和独立 LLM 请求，但客户端静音门控又不向后端报告静音边界。下一阶段应把三者拆开：

1. 客户端继续只采集系统播放声；静音音频不上传，但在连续静音达到阈值后发送轻量 `speech_end` 控制消息。
2. 短静音只结束一个 ASR 语音片段并尽快产出草稿答案，不立刻关闭逻辑问题线程。
3. 同一问题后续片段到达时，把累计文本作为新的问题修订再次发送给 LLM，以速度优先的草稿逐步提高准确率。
4. 因为当前不会采集面试者麦克风，面试官问完后用户回答期间的长静音可以作为问题线程硬关闭信号；仍需为会议停顿、网络抖动和面试官思考留出宽限时间。
5. 该设计必须保持多个 LLM 请求可并发、通过稳定线程/修订 ID 隔离输出，并避免旧修订完成后覆盖新修订。

这些条目是下一提交的设计目标，不属于本快照已经实现的协议。

### 多实例

扩展前需要：

- PostgreSQL 或等价共享事务数据库。
- Redis/NATS 等跨实例广播与任务所有权。
- 分布式限流/预算。
- 连接路由或可跨实例重放的统一事件分发。

### 部署

主要客户端功能稳定后，再按 [部署参考](../../../backend/DEPLOYMENT.md) 完成真实容器、备份恢复、TLS、监控和灾难演练。
