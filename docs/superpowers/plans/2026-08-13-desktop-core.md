# PC 客户端实施计划（归档：Electron 方案）

> 原计划日期：2026-08-13
>
> 最近按代码复核：2026-08-26
>
> 状态：**历史资料，不得作为现行实施计划执行。**

这份文件记录早期 Electron 客户端方案中的产品目标、协议约束和验收思路。原 Electron 实现已经退役并删除，不再受支持，也不是当前代码的参考运行入口。

项目现行且唯一受支持的桌面客户端是 [`desktop-tauri/`](../../../desktop-tauri/README.md)：Tauri 2 + React + Rust。所有当前功能、目录结构、开发命令、存储方式和验证状态必须以该目录源码与 README 为准。

以下内容仅保留为历史决策背景；其中的 Electron、Node 主进程、TypeScript 网络层、阶段状态和“建议实施”表述均不代表当前路线或待办。

## 1. 历史产品目标

早期方案提出的产品目标与壳技术无关，现已由 Tauri 客户端承接：

- 创建、开始、结束和删除面试会话。
- 当前 Tauri 只采集 Windows 默认 `eConsole` 播放设备的 WASAPI loopback，不启用电脑麦克风。
- `pc/mobile/both` 收音模式切换。
- 实时显示转写和 AI 答案。
- 手动重新生成答案，并可显式选择搜索增强。
- 历史会话、转写、答案和复盘。
- ASR、网络代理、LLM、搜索配置管理及删除。
- 与未来移动端共享服务端会话状态。

## 2. 历史设计决策

### 2.1 Token 与本地安全

- REST 统一附加 Bearer Token。
- WebSocket 建连后在 5 秒内发送 `authenticate`，不依赖 URL 参数。
- Token 不进入前端源码、普通 localStorage 或日志。
- 当前 Tauri 实现使用 Rust `keyring` 的 Windows Credential Manager 后端；不是 Electron `safeStorage`，也不是 Stronghold 文件库。

### 2.2 音频格式

后端支持的候选格式包括：

- `webm_opus`
- `ogg_opus`
- `m4a_aac`
- `wav_pcm_s16le`

当前 Tauri 客户端选择 `wav_pcm_s16le`。每个分片需要：

- 新 UUID `chunk_id`。
- PC 来源独立、连续递增的 `chunk_seq`。
- 带时区的采集开始时间。
- 真实 `duration_ms`，范围 100–10,000。
- 可被后端 `ffprobe` 验证的真实容器、编码、单音轨和时长。

现行 Tauri 的标准分片是 16 kHz、单声道、40,000 帧（2.5 秒）。当前只采集电脑播放声，由 WASAPI loopback 处理；WASAPI 线程按 20 ms RMS 窗判断声音，至少 3 个窗口过阈值才成片；静音不会创建 UUID、占用序号或上传，也不会额外发送 `speech_end`。React 中的 `chunk_seq` 仅作兼容展示，持久 PC 序号只由 Rust manifest 原子分配。

### 2.3 本地可靠队列

客户端不能把 WebSocket 写入成功视为服务端已经处理。历史状态设计为：

~~~text
captured -> sending -> queued -> done -> released
                   └-> retryable_failed
                   └-> terminal_error
~~~

持久化记录至少包含：

- `session_id`
- `chunk_id`
- `source`
- `codec`
- `chunk_seq`
- `captured_at`
- `duration_ms`
- 原始音频文件
- 最新服务端状态和错误码

重试必须复用完全相同的内容与元数据。队列满、网络断开、`missing_predecessor`、`service_restart`、`service_shutdown`、`processing_failed` 和 `usage_limited` 需要明确状态迁移。

当前实现补充了原计划没有定义清楚的停止语义：关闭 capture gate 后，迟到的候选分片在分配序号前被拒绝；本地 outbox 持久化 `cancel_audio_source(source, through_chunk_seq, reason)`，让后端取消水位以内的 queued、可重试 failed 和当前 ASR 任务。停止使用 `capture_stopped`，切到手机来源使用 `source_disabled`。

同一分片累计 3 次 `processing_failed` 后停止自动重试，且 `queued` ACK 不清空失败次数。LivePage 收到首个 ASR 处理故障会熔断本轮采集并取消积压。页面重进、重连或应用重启不会自动恢复旧采集。

现行 LivePage 还会把标准 `Error` 与 Tauri 直接返回的字符串错误归一化为显示文本；字符串不再被强制当成 `Error` 读取 `.message`，因此失败 toast 不会为空。

### 2.4 事件游标与对账

每个会话持久化最大已处理 `event_id`：

1. WebSocket 认证时传 `last_event_id`。
2. 逐条应用重放事件并去重。
3. 收到 `sync_complete` 后完成当前快照同步。
4. 使用 `GET /api/sessions/{id}/audio-chunks?source=pc` 对账音频状态。

### 2.5 会话状态

客户端 UI 以服务端 `session_state` 为准：

- `idle`：可以 start，不能上传音频。
- `recording`：可以收音、切换模式、重新生成和 end。
- `ended`：只读历史和复盘，停止录音与重试。

## 3. 历史分层思路

~~~text
UI pages
  ├─ Interview
  ├─ History / Review
  └─ Settings

Application services
  ├─ SessionController
  ├─ RealtimeSync
  ├─ AudioOutbox
  └─ ConfigService

Infrastructure
  ├─ REST client
  ├─ WebSocket v1 client
  ├─ Recorder / Encoder
  ├─ Local persistence
  └─ OS secure storage
~~~

现行 Tauri 代码将 UI 和系统声音启动/停止编排放在 React，将 WASAPI 系统声音、REST、WebSocket、outbox、对账、事件游标和 OS 安全存储放在 Rust；不再采用 Electron 主进程/preload/IPC 分层。

## 4. 历史实施阶段

以下阶段只说明当时的依赖顺序，不代表当前完成度或现行 roadmap。

### 阶段 A：协议和无音频会话

- Bearer REST 客户端。
- WebSocket 首包认证、`sync_complete`、`ping/pong` 和关闭码。
- 创建、开始、结束会话和状态展示。
- 重放和去重验证。

### 阶段 B：音频 outbox

- codec、UUID、序号、时间与时长。
- 本地持久队列。
- `chunk_ack` 状态机、背压、缺序和 REST 对账。
- 断网、应用重启和服务重启验证。

### 阶段 C：实时转写与答案

- 展示 `transcript` 和 `answer` 持久化事件。
- `regenerate_answer` 与 `use_search`。
- 额度、背压和上游失败提示。

### 阶段 D：历史、配置与复盘

- 会话列表、删除和详情。
- 转写、答案和复盘。
- ASR、网络代理、LLM、搜索配置的保存、激活和删除。
- 只展示 `secret_configured`，不期待后端返回 API Key。

### 阶段 E：真实端到端

- 真麦克风、真实 ASR、LLM 和搜索服务。
- 长时间面试、系统休眠、网络切换和输入设备变化。
- 性能、内存、磁盘、安装包和隐私检查。

## 5. 历史最低测试矩阵

该矩阵仍可用于扩展 Tauri 自动化测试。当前 `npm test` 有 92 个测试、Rust 有 42 个测试，但真实 Windows 音频设备与腾讯会议场景仍未由这些测试覆盖。

| 类别 | 必测场景 |
|---|---|
| 认证 | REST 401、WS 4401、Token 轮换后的提示 |
| 状态机 | idle/recording/ended、重复 end、远端结束 |
| 同步 | 首次连接、断线重放、重复事件、游标超前 |
| 音频 | 正常、重复、冲突、缺序、背压、超大、codec 不符 |
| 恢复 | 客户端重启、后端重启、服务关闭、网络切换 |
| 双端 | pc/mobile 独立序号、both 模式、来源不允许 |
| 系统声音 | 默认 `eConsole` 播放设备 loopback、腾讯会议/媒体播放、通信默认或独立端点漏采、静音不成片 |
| 重复收音 | 当前只上传系统播放声，避免麦克风和系统声音混在同一 `source=pc` |
| 停止取消 | 停止、切 mobile、退出页面、迟到 invoke、取消水位幂等、当前 ASR task 中止 |
| 错误提示 | JavaScript `Error`、Tauri 字符串错误、非空 toast 和用户可理解的失败原因 |
| 成本 | ASR/LLM/Search 预算和并发上限 |
| 配置 | 密钥不回显、URL 被拒绝、激活切换、删除 |
| 复盘 | 非 ended 拒绝、空转写拒绝、幂等、搜索开关 |

## 6. 已废弃的历史假设

- `POST /api/sessions` 无 Authorization。
- WebSocket 连接后直接发送 `start_session`。
- 音频消息只有 `source` 和 `data`。
- 用本地 `Date.now()` 伪造服务端记录 ID。
- `chunk_ack` 不持久化。
- 读取配置时可以取得 `api_key`。
- 创建会话后本地直接进入 recording 而不等待服务端状态。
- 服务重启后的在途分片不可重试。
- Electron 仍是受支持的桌面壳或未来实施目标。

## 7. 归档结论

本文件只用于解释早期方案为什么强调认证、游标、音频幂等、durable outbox 和服务端权威状态。它不是可执行计划，也不代表 Electron 仍存在或受支持。

后续桌面端开发、修复、测试、构建和发布一律在 `desktop-tauri/` 中进行；需要新路线图时，应基于当前 Tauri 源码、[后端协议](../../../backend/README.md)和实际验证缺口另建计划。

截至 2026-08-26，当前自动化证据为前端 `92 passed`、Rust `42 passed`；前端测试包含系统声音采集停止、答案优先布局、手动提问框、partial 转写、并发流式答案、Tauri 字符串错误非空显示和全局提示词回归。本次发布仍需重新执行 `npm run build`、`cargo check --locked`、`cargo fmt --check` 和严格 Clippy。WASAPI 默认 `eConsole` 播放设备、腾讯会议对方声音和真实 FunASR 链路仍待真实设备结果补充；若会议使用通信默认设备或独立端点，当前实现不会捕获。本归档不得把自动化或编译结果表述为真机场景已经验收。
