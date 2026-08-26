# AI 面试助手：产品大纲与实现边界

本文描述产品目标、当前代码已经实现的能力和下一阶段方向。接口细节以 [backend/README.md](backend/README.md) 和 `backend/app/` 为准。

## 1. 产品定位

AI 面试助手是个人使用的实时辅助工具，目标是：

- 在面试中把电脑或手机采集的音频快速转成文字。
- 在面试官说话期间尽快把片级转写送入 LLM，并生成可立即参考的回答提示。
- 让现行 Tauri 桌面端与未来移动客户端共享同一会话状态、转写和答案。
- 在面试结束后生成复盘报告，帮助回顾回答和改进点。

这里的 `pc` / `mobile` 是音频采集来源，不是说话人标签。当前代码不提供声纹识别或说话人分离。

## 2. 当前实现状态

| 能力 | 当前状态 | 说明 |
|---|---|---|
| 会话管理 | 已实现 | 创建、列表、详情、开始、结束和删除；状态为 `idle -> recording -> ended`，recording 会话不能删除 |
| REST/WebSocket 认证 | 已实现 | 全局 Bearer Token；WebSocket 首包在 5 秒内认证 |
| PC 本地采集 | 已实现，待真机验收 | 只采集 Windows 默认 `eConsole` 播放端点 WASAPI loopback，不请求麦克风；系统声音按 20 ms RMS 窗过滤 |
| 静音门控 | 已实现 | 系统声音按 20 ms RMS 窗判断，至少 3 个有效窗才成片；静音不分配 UUID/序号、不上传 |
| 实时音频上传 | 已实现 | Base64 分片，PC 系统声音正常切为 2.5 秒，支持 PC/移动来源和 4 种编码；PC 持久序号只由 Rust manifest 分配 |
| 音频可靠性 | 已实现 | UUID 幂等、内容冲突检测、按来源连续序号、背压、缺序等待、状态对账、capture gate 和按水位取消 |
| ASR | 已实现 | 默认 `AI_ASR_ENGINE=funasr`；WAV 走 FunASR，设置为 `llm` 才使用激活 LLM 主 `model` 做多模态转写，设置为 `groq` 才走 Groq；统一先经 `ffprobe` 校验 |
| 问题线程 | 未实现 | 不做关键词、标点、长度或停顿判断；partial 只显示，每个片级 final 独立调用 AI，尚未把同一问题的多个切片累积起来 |
| 实时答案 | 已实现 | 默认 FunASR final 直接保存转写；LLM 通过 SSE 增量广播 `answer_stream`，结束后保存最终答案；不做问题检测；默认低思考强度，支持模型时可切换低/中/高；搜索增强可选 |
| 事件同步 | 已实现 | SQLite 持久化事件、WebSocket 广播、`event_id` 重放和去重水位 |
| 配置管理 | 已实现 | LLM/Search/ASR/Network 四类配置，支持新增、脱敏读取、激活和删除，LLM 支持低/中/高思考强度，秘密使用 Fernet 加密 |
| 会后复盘 | 已实现 | 仅允许 ended 会话；按内容版本幂等，支持显式搜索增强 |
| 成本控制 | 已实现 | LLM/Search/ASR 并发门和分钟/小时/天持久化预算 |
| 桌面客户端 | 已实现 | `desktop-tauri/` 是当前唯一受支持的桌面端；Tauri 2 + React 19 + Rust，可通过类型检查和编译 |
| 旧 Electron 客户端 | 已废弃并删除 | `desktop/` 已从当前工作树移除，不再演进 |
| 移动客户端 | 未实现 | 仓库没有 Flutter 工程 |
| 生产容器验证 | 暂缓 | 文件已存在，但当前未完成真实构建、启动和公网验证 |

## 3. 当前架构

~~~text
Tauri 2 桌面端 ─┐
                ├─ REST + WebSocket v1 ─> FastAPI + SQLite
未来移动客户端 ─┘                           │
                                            ├─ FunASR（默认 WAV）
                                            ├─ 多模态 LLM 主 model（可选）
                                            ├─ Groq Whisper（配置或非 WAV）
                                            ├─ OpenAI-compatible LLM
                                            └─ Google 搜索增强（非向量 RAG；历史 Bing 配置不可用）
~~~

Tauri 桌面端与后端均已有代码。移动端仍只是目标组件。旧 Electron 工程已经退役，不属于现行架构。

## 4. 核心数据流

### 4.1 实时会话

1. 客户端通过 REST 创建会话。
2. 客户端连接 `/ws/{session_id}`，用协议版本 `v: 1` 和 Token 认证。
3. 后端重放客户端游标之后的持久化事件，并发送 `sync_complete`。
4. 用户点击开始采集后，Tauri 启用 Windows 默认播放设备 WASAPI loopback；不启动麦克风；系统声音按 20 ms RMS 窗过滤，生成 16 kHz 单声道 2.5 秒 WAV。静音窗口不会上传，但当前也不会发送 `speech_end`。
5. Rust manifest 为有效 PC 分片分配唯一 `chunk_seq`，客户端发送 `audio_chunk`。
6. 后端先持久化分片为 `queued`，然后按 `session_id + source + chunk_seq` 有序处理。
7. `ffprobe` 校验媒体后，默认引擎为 `funasr`：WAV 按 `session + source` 复用 FunASR WebSocket，每个切片独立 `start -> PCM -> stop -> final` 并创建自己的 LLM 请求；同一会话默认并发 3 条，通过 `request_id` 隔离流式答案。当前连接复用只减少握手，不代表整道问题是一个 ASR utterance。`AI_ASR_ENGINE=llm` 使用激活 LLM 的主 `model` 走多模态 LLM，`AI_ASR_ENGINE=groq` 或非 WAV 编码走 Groq。
8. 转写、分片最终状态和可能生成的答案写入数据库并广播。
9. 客户端保存最新 `event_id`；断线后继续补拉，但不会自动重新打开采集门或恢复旧采集。
10. 停止采集、切到 `mobile` 或离开实时页时，Tauri 关闭 capture gate，并发送 `cancel_audio_source(source, through_chunk_seq, reason)`，取消水位内的本地 outbox、服务端 queued/retryable failed 和当前处理任务。

### 4.2 会后复盘

1. 会话必须处于 `ended`，并且至少有一条转写。
2. 后端读取完整转写和已生成答案。
3. 如果请求明确设置 `use_search: true`，后端尝试搜索；普通搜索错误会降级为纯 LLM，但预算/并发限制会直接返回可重试的 `429`。
4. 后端限制总输入长度，并把所有会话内容作为不可信数据传给 LLM。
5. 相同内容版本和相同搜索选项命中同一复盘结果，避免重复付费调用。

## 5. 现行桌面端与未来移动端的契约

Tauri 桌面端已经实现以下客户端责任，未来移动端也必须遵守同一协议：

- 会话创建、开始、结束、删除和历史记录。
- PC/移动/双端收音模式切换。
- Windows 默认播放设备 loopback 的启动、停止和明确采集状态；当前不启用麦克风。
- 电脑播放声由 WASAPI loopback 单独采集，不依赖麦克风拾取。
- 20 ms RMS 静音门控；静音不得分配 UUID、占用序号或上传。
- 当前没有 `speech_end` 或逻辑问题终止消息；未来如果增加，必须与普通音频分片、停止采集和会话结束区分。
- 带稳定 UUID、每来源连续序号、准确采集时间和时长的音频分片；PC 的持久序号由 Rust manifest 唯一分配。
- 本地待发送队列、`chunk_ack` 状态跟踪、重传和 REST 对账。
- capture gate 与 `cancel_audio_source` 水位取消；停止、切来源或退出后不得让迟到分片复活。
- `event_id` 持久化、断线重连、事件去重。
- 实时转写、流式答案、最终答案、错误、用量受限和背压状态的清晰 UI。
- LivePage 必须同时处理 JavaScript `Error` 与 Tauri 直接返回的字符串错误，统一归一化为非空 toast 文本。
- LLM/Search/ASR/Network 四类配置的新增、激活和删除，以及会后复盘。

客户端不得假设：

- WebSocket 建连后可以直接发业务消息。
- 音频只有 `source + data` 两个字段。
- API Key 会从后端读取接口返回。
- 所有 `chunk_ack` 都是瞬时消息。
- 会话结束后还能继续写入音频、转写或答案。
- 页面重进或 WebSocket 重连会自动恢复旧采集。

仓库仍保留未被 LivePage 导入的麦克风 recorder/worklet 和对应测试，作为历史模块；现行实时页只调用 Rust WASAPI 系统声音命令，因此用户回答的麦克风声音不会进入当前后端链路。

Google 搜索增强只是把最多 5 条清洗后的标题和摘要加入 LLM 上下文，没有文档摄取、Embedding、向量数据库、召回器或引用生成，不应描述为向量 RAG。Bing Web Search API 已退役；新 Bing 配置会被拒绝，历史配置按不可用降级。

## 6. 非功能边界

### 安全

- 所有业务 REST API 都需要 Bearer Token。
- 第三方密钥加密存储，校验错误会脱敏。
- LLM Base URL 默认只允许 HTTPS、无凭据且解析到公网地址，并继续执行 SSRF 防护；当前不再维护额外的主机白名单。
- 浏览器 Origin 不允许通配符。
- 付费调用和 REST/WS 消息均有限流或容量边界。

### 一致性与恢复

- SQLite 使用 WAL、外键、busy timeout 和单进程写锁。
- 持久化事件类型为 `session_state`、`chunk_ack`、`transcript`、`answer`。
- 新分片和状态变更会留下 `chunk_ack` 事件；重复分片的即时状态回复可能没有新的 `event_id`。
- `service_restart`、`service_shutdown`、`processing_failed`、`missing_predecessor` 和 `usage_limited` 属于协议层可用相同 `chunk_id` 重试的失败原因；Tauri 对同一分片累计 3 次 `processing_failed` 后停止自动重试，且 `queued` ACK 不清零失败计数。
- 停止采集使用 `capture_stopped`，切到手机来源使用 `source_disabled`。取消水位内的 queued、可重试 failed 和当前处理任务都会收敛为持久化 `chunk_ack: cancelled`。
- LivePage 收到首个 ASR 处理故障后会熔断本轮采集并取消积压，避免错误持续刷屏。
- Tauri 字符串错误会被归一化显示，不再因读取不存在的 `.message` 产生空 toast。

### 扩展限制

- 当前仅支持单 Uvicorn worker。
- 当前只有全局 Token，没有用户、权限和租户隔离。
- 多实例需要共享数据库、跨实例广播/队列、分布式限流和任务所有权。

## 7. 下一阶段建议

优先级按“先验证现有桌面主链路，再扩展平台和部署”排列：

1. 在真实 Windows 设备验证默认播放设备 WASAPI loopback、腾讯会议对方声音，以及静音时不产生新分片；本轮结果出来前不要宣称场景已验收。
2. 扩充现有 Tauri 自动化测试，继续覆盖 UI 模式切换、命令桥、重连和真实 outbox 对账；不要继续维护 Electron 版。
3. 做真实 FunASR 长连接/Groq、LLM 和可选搜索的端到端测试，校准 2.5 秒分片、语音段结束和 UI 延迟。
4. 在干净 Windows 环境验证 NSIS 安装、升级和卸载，并测试应用重启、服务重启、积压取消和跨网络切换。
5. 实现移动端，并验证桌面/移动双端同时连接与收音切换。
6. 主要功能稳定后，再恢复容器构建、备份恢复演练和生产上线审查。

## 8. 风险

- 录音和转写涉及隐私与当地法律合规，实际使用前需要获得必要同意。
- 第三方 ASR、LLM 和搜索服务有额度、延迟、可用性及数据处理风险。
- partial 可能因上游网络或模型延迟暂时不更新；final 仍以 FunASR 服务端返回为准，客户端保留手动重新生成功能。
- 双端同时收音可能录到重复声音；当前仅按来源分别排序，不做跨来源去重。
2026-08-26 当前工作树自动化结果为后端 `218 passed`、Tauri 前端 `92 passed`、Rust `42 passed`；`ruff` 与现有前端构建已通过。目标 Python 3.12、`cargo check --locked`、`cargo fmt --check` 和严格 Clippy 仍需在本次发布门禁重新执行并记录最新结果。这些仍不能替代真实 WASAPI/腾讯会议/FunASR 端到端证据。
- 系统声音只监听默认 `eConsole` Render endpoint；腾讯会议若走通信默认设备或独立输出设备会漏采。后端取消水位也只存在于进程内，未 reserve 的空洞范围不会跨服务重启；这两项都需要真实故障场景验收。
