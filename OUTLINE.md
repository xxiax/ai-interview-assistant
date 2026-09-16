# AI 面试助手：产品大纲与实现边界

本文描述产品目标、当前代码已经实现的能力和下一阶段方向。接口细节以 [backend/README.md](backend/README.md) 和 `backend/app/` 为准。

> 改造前快照保存在 `codex/current-code-snapshot`（`de6b26c`）；当前实现位于 `codex/question-thread-accumulator`。

## 1. 产品定位

AI 面试助手是个人使用的实时辅助工具，目标是：

- 在面试中把电脑或手机采集的音频快速转成文字。
- 在面试官说话期间尽快把累计转写送入 LLM，并生成可立即参考的回答提示；累计 `partial` 按几何倍率节流后开新 revision，各 revision 在会话并发上限内同时执行，前端仍只占一张问题卡。
- 让现行 Tauri 桌面端与未来移动客户端共享同一会话状态、转写和答案。
- 在面试结束后生成复盘报告，帮助回顾回答和改进点。

这里的 `pc` / `mobile` 是音频采集来源，不是说话人标签。当前代码不提供声纹识别或说话人分离。

## 2. 当前实现状态

| 能力 | 当前状态 | 说明 |
|---|---|---|
| 会话管理 | 已实现 | 创建、列表、详情、开始、结束和删除；状态为 `idle -> recording -> ended`，recording 会话不能删除 |
| REST/WebSocket 认证 | 已实现 | 全局 Bearer Token；WebSocket 首包在 5 秒内认证 |
| PC 本地采集 | 已实现，待真机验收 | 只采集 Windows 默认 `eConsole` 播放端点 WASAPI loopback，不请求麦克风；系统声音按 20 ms RMS 窗过滤 |
| 静音门控与语音段边界 | 已实现 | 系统声音按 20 ms RMS 窗判断，至少 3 个有效窗才成片；静音不分配 UUID/序号、不上传；连续 1.6 秒静音冲刷短尾片后发送 `speech_end` |
| 实时音频上传 | 已实现 | Base64 分片，PC 系统声音分片时长由 `AI_AUDIO_CHUNK_MS` 配置（默认 400 ms，夹到 100–2500；语音段边界处可为更短的尾片），支持 PC/移动来源和 4 种编码；PC 持久序号只由 Rust manifest 分配 |
| 音频可靠性 | 已实现 | UUID 幂等、内容冲突检测、按来源连续序号、背压、缺序等待、状态对账、capture gate 和按水位取消 |
| ASR | 已实现 | 默认 `AI_ASR_ENGINE=funasr`；WAV 走 FunASR，设置为 `llm` 才使用激活 LLM 主 `model` 做多模态转写，设置为 `groq` 才走 Groq；统一先经 `ffprobe` 校验 |
| 问题线程 | 已实现 | 客户端 `speech_end` + 后端 `QuestionThread`：一个语音段就是一个 FunASR utterance；累计 `partial` 经几何节流后形成 `revision` 并立即进入 LLM 队列，各 revision 互不取消；宽限期（默认 6 秒）后关闭线程，只落库最高成功版本。不做关键词或问题分类检测 |
| 实时答案 | 已实现 | 默认 FunASR 段末 final 保存为一条转写；LLM 通过 SSE 增量广播 `answer_stream`（带 `thread_id`/`revision`/`started`/`failed`），前端按 `thread_id` 聚合成一张卡并保留全部段、同一时刻只展示未被取代里答案最长的一段（catch-up swap，`pickDisplayVersion`）；线程关闭时保存最终答案但**不清理**实时分段（保留到会话结束）；每张卡常驻「重新生成」（带 `thread_id`，生成中也可点）；默认低思考强度，支持模型时可切换低/中/高；搜索增强可选 |
| 会话级答题背景 | 已实现 | 每场面试可设置岗位 JD 与简历（各 8,000 字符，`PUT /api/sessions/{id}/context`，任何会话状态可改）；非空时注入答案与截图解题提示词，要求贴合该岗位与候选人真实经历、不编造简历外内容；留空回通用答案。桌面端实时页有编辑弹窗，保存后对下一个问题生效 |
| 事件同步 | 已实现 | SQLite 持久化事件、WebSocket 广播、`event_id` 重放和去重水位 |
| 配置管理 | 已实现 | LLM/Search/ASR/Network 四类配置，支持新增、脱敏读取、激活和删除，LLM 支持低/中/高思考强度，秘密使用 Fernet 加密 |
| 会后复盘 | 已实现 | 仅允许 ended 会话；按内容版本幂等，支持显式搜索增强 |
| 成本控制 | 已实现 | LLM/Search/ASR 并发门和分钟/小时/天持久化预算 |
| 桌面客户端 | 已实现 | `desktop-tauri/` 是当前唯一受支持的桌面端；Tauri 2 + React 19 + Rust，可通过类型检查和编译 |
| 悬浮提词窗 | 已实现，真实窗口验证过 | 第二个 WebView 窗口（`#/overlay`），惰性创建。无边框、透明、始终置顶、可从任意位置收起成屏幕顶部居中的 18px 细条、可鼠标穿透（按住 `Ctrl` 临时可交互，所有开窗/还原穿透的路径后都 `sync_ctrl_watch`）、默认对 OS 截屏/录屏/窗口共享隐身；8 个 `Ctrl+Alt+*` 全局热键（含收起/展开、开启/暂停录制——没录就一键走完 开始会话→系统采集→上传门禁；在录就 暂停/恢复 采集链路，会话保持进行中，**不结束面试**）；背景不透明度 25%–100% 滑条调节（只调背景层，文字始终全亮 + 投影兜底；热键按 5% 步进）；位置/尺寸/开关落 `overlay-layout.json` 且重启还原（`visible` 刻意不落盘）；工具条含截图解题；顶栏有当前会话徽片（短 id + 点击复制）；录制徽标按 会话状态 × 本机采集门（`captureState` 事件）× 收音模式 分三态如实显示；底部快速提问 textarea（Enter 发送/Shift+Enter 换行/自动增高，单行不出滚动条；**暂停录制期间照常可问**——门禁只看连接与会话状态，不看采集开关）；按住 Ctrl 时滚轮可正常滚动答案区（WebView2 把 Ctrl+滚轮当缩放手势，悬浮窗自己接管）；「最新」回底按钮有平滑动画中间帧抑制；答案正文可划选复制（chrome 不可选，选中高亮分窗配色）；发送成功即显示「已发送 · 正在思考」pending 卡直到流式帧接管；提问区与答案区靠三档亮度分层（footer 操作面 > 输入容器 > 答案卡）；配色黑白灰（滚动条与 Markdown 代码块颜色均跟随背景不透明度）。**挂载时从 Rust `live_runtime_state` 快照播种**，晚打开的悬浮窗也能立即对齐连接/会话/采集状态（主窗口采集按钮同样跟随 `captureState` 事件，反映悬浮窗热键开的采集）；焦点归属（`focused`）由 Rust 跟踪并实时同步主窗口面板；状态只跟当前连接的面试走（引擎断开会发 `closed` 清掉"录制中"）。所有窗口操作走 Rust `overlay_*` 命令（前端无 `allow-set-*` 权限），且**必须全是 `async fn`**，否则在 Windows 上和 WebView2 消息泵死锁 |
| 截图解题（笔试辅助） | 代码完整，验证不足 | Rust GDI 全屏抓帧 → PNG → 后端多模态 LLM；主窗口按钮、悬浮窗按钮、`Ctrl+Alt+Q` 三个入口都有。答案以 `source="llm"` 入库。按用户要求优先级下调，端到端真机验证未做 |
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
4. 用户点击开始采集后，Tauri 启用 Windows 默认播放设备 WASAPI loopback；不启动麦克风；系统声音按 20 ms RMS 窗过滤，生成 16 kHz 单声道 WAV，分片时长由 `AI_AUDIO_CHUNK_MS` 决定（默认 400 ms）。静音窗口不会上传；连续 1.6 秒静音时先冲刷带 100 ms 尾部静音的短尾片，再发送 `speech_end`（携带当前已分配的 PC 序号水位）。
5. Rust manifest 为有效 PC 分片分配唯一 `chunk_seq`，客户端发送 `audio_chunk`。
6. 后端先持久化分片为 `queued`，然后按 `session_id + source + chunk_seq` 有序处理。
7. `ffprobe` 校验媒体后，默认引擎为 `funasr`：WAV 按 `session + source` 复用 FunASR WebSocket，并按语音段维持**开放式 utterance**——段首一次 `start`，中间分片只推裸 PCM，`speech_end` 才 `stop` 取段末 `final`。整题共享同一份声学上下文，不再出现跨片截断。`AI_ASR_ENGINE=llm` 使用激活 LLM 的主 `model` 走多模态 LLM，`AI_ASR_ENGINE=groq` 或非 WAV 编码走 Groq。
8. 分片推进网关后立刻 ack `done`（不等段末 final，否则长问题会顶满客户端发件箱）。网关每回一版有效累计 `partial`，后端把它作为该 `(session, source)` 问题线程的新 `revision`，并立刻提交 LLM；同一会话最多 3 个 revision 同时生成，超出部分排队，不取消较早 revision。段末 `final` 强制再问一版，并作为一条转写入库。
9. `speech_end` 到达后结束语音段并启动宽限期（默认 6 秒）：期间说话人继续说会开启新语音段，其累计文本拼在已固化的问题前缀后并入同一线程并重排宽限；宽限到期、或会话结束 flush 时关闭线程，把 revision 最高的成功版本写入 `answers` 并广播持久化 `answer`（最高 revision 失败时等其余版本收尾后取其中最高成功版本）。若线程内所有 revision 都失败，则不落库。
10. 转写、分片最终状态和答案写入数据库并广播。
11. 客户端保存最新 `event_id`；断线后继续补拉，但不会自动重新打开采集门或恢复旧采集。
12. 停止采集、切到 `mobile` 或离开实时页时，Tauri 关闭 capture gate，并发送 `cancel_audio_source(source, through_chunk_seq, reason)`，取消水位内的本地 outbox、服务端 queued/retryable failed 和当前处理任务。

### 4.2 会后复盘

1. 会话必须处于 `ended`，并且至少有一条转写。
2. 后端读取完整转写和已生成答案。
3. 如果请求明确设置 `use_search: true`，后端尝试搜索；普通搜索错误会降级为纯 LLM，但预算/并发限制会直接返回可重试的 `429`。
4. 后端限制总输入长度，并把所有会话内容作为不可信数据传给 LLM。
5. 相同内容版本和相同搜索选项命中同一复盘结果，避免重复付费调用。

## 5. 现行桌面端与未来移动端的契约

Tauri 桌面端已经实现以下客户端责任，未来移动端也必须遵守同一协议：

- 会话创建、开始、结束、删除和历史记录。
- 会话级答题背景（岗位 JD 与简历）的编辑；保存在后端 `sessions` 表，跨端共享。
- PC/移动/双端收音模式切换。
- Windows 默认播放设备 loopback 的启动、停止和明确采集状态；当前不启用麦克风。
- 电脑播放声由 WASAPI loopback 单独采集，不依赖麦克风拾取。
- 20 ms RMS 静音门控；静音不得分配 UUID、占用序号或上传。
- 连续静音达到阈值（当前 1.6 秒）时发送 `speech_end{source, through_chunk_seq}`，并与普通音频分片、停止采集和会话结束严格区分：`speech_end` 只标记语音段边界，不冻结 outbox、不取消任何分片。
- 带稳定 UUID、每来源连续序号、准确采集时间和时长的音频分片；PC 的持久序号由 Rust manifest 唯一分配。
- 本地待发送队列、`chunk_ack` 状态跟踪、重传和 REST 对账。
- capture gate 与 `cancel_audio_source` 水位取消；停止、切来源或退出后不得让迟到分片复活。
- `event_id` 持久化、断线重连、事件去重。
- 流式答案按 `request_id` 一版一段保存，并按 `thread_id` 聚合成一张卡；catch-up swap 在各并发版本中展示答案最长的一版，新版追平后自然接管。单个 revision 失败只标记该段，不清空同卡或其他卡内容。持久化 `answer` 到达时不清理实时分段，只作为该卡的「已入库」标记并从独立历史列表剔除。每张卡常驻「重新生成」按钮。
- 实时转写、流式答案、最终答案、错误、用量受限和背压状态的清晰 UI。
- LivePage 必须同时处理 JavaScript `Error` 与 Tauri 直接返回的字符串错误，统一归一化为非空 toast 文本。
- LLM/Search/ASR/Network 四类配置的新增、激活和删除，以及会后复盘。

悬浮提词窗是**桌面端专有**的，不属于跨端契约：它依赖操作系统的窗口层能力（置顶、鼠标穿透、`WDA_EXCLUDEFROMCAPTURE` 类的采集排除），移动端不需要也做不到。它复用同一条 WebSocket 事件流，不引入任何新的后端协议。唯一需要注意的跨端约束是：它是**独立 JS 上下文**，不能复用主窗口的 Zustand store（那份 `sessionId` 永远是 null）和 `app-toast` 事件（监听器在主窗口的 `AppLayout` 里），因此有自己的事件归约 `shared/overlay-feed.ts` 和就地反馈。

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
- 问题线程状态（`thread_id`、`revision`、待完成集合、最新完成结果、宽限定时器）**全部在进程内**，不落库。后端重启会丢失未关闭线程尚未入库的分段，已入库的 `answer` 不受影响。
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
2. 在真机长时间面试中调参 `SPEECH_END_SILENT_WINDOWS`（1.6 秒）、`TRAILING_SILENCE_WINDOWS`（100 ms）和 `AI_QUESTION_THREAD_GRACE_SECONDS`（6 秒）；当前这三个值只有单元测试覆盖，没有真实语速/停顿证据。
3. 扩充现有 Tauri 自动化测试，继续覆盖 UI 模式切换、命令桥、重连和真实 outbox 对账；不要继续维护 Electron 版。
4. 做真实 FunASR 长连接/Groq、LLM 和可选搜索的端到端测试，校准 `AI_AUDIO_CHUNK_MS` 分片时长、语音段结束、问题线程宽限期和 UI 延迟。
5. 在干净 Windows 环境验证 NSIS 安装、升级和卸载，并测试应用重启、服务重启、积压取消和跨网络切换。
6. 实现移动端，并验证桌面/移动双端同时连接与收音切换。
7. 主要功能稳定后，再恢复容器构建、备份恢复演练和生产上线审查。

## 8. 风险

- 录音和转写涉及隐私与当地法律合规，实际使用前需要获得必要同意。
- 第三方 ASR、LLM 和搜索服务有额度、延迟、可用性及数据处理风险。
- 累计 `partial` 可能因上游网络或模型延迟暂时不更新；段末 final 仍以 FunASR 服务端返回为准，客户端保留手动重新生成功能。
- 问题线程会为每个通过节流闸门的累计 `partial` 发一次 LLM 请求（速度优先），费用随 revision 数增长；各版本互不取消，会话并发门、全局并发门、倍率闸门和持久预算共同限制成本。线程只把最高成功 revision 写入历史。
- 双端同时收音可能录到重复声音；当前仅按来源分别排序，不做跨来源去重。

2026-09-14 当前工作树验证为：后端 `260 passed` 且 `ruff` 通过；Tauri 前端 `170 passed` 且生产构建通过；Rust `72 passed`，fmt/check/严格 Clippy 均通过。覆盖开放式 FunASR utterance、`speech_end`、累计 revision 真实并发、最高成功版本落库、一问题一张卡、悬浮提词窗、截图解题和引擎代际门。这些自动化结果仍不能替代真实 WASAPI、腾讯会议和 FunASR 端到端证据。

悬浮提词窗的验证方式与其他模块不同，必须单独说明：它的 Rust 单元测试全是纯函数（`clamp_opacity`、`dock_origin`、布局序列化），**对窗口行为零覆盖**。曾经出现过「68 项测试全绿而悬浮窗因主线程死锁 100% 打不开」，所以这块只能靠真实窗口验证：`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9223" npm run tauri:dev` 起真窗口，用 `playwright-core` 的 `connectOverCDP` 驱动。**不要用普通浏览器打开 `localhost:1421`**，那里 `__TAURI_INTERNALS__` 是 undefined，所有 invoke 都会报错，很容易被误读成桌面端 bug。
- 系统声音只监听默认 `eConsole` Render endpoint；腾讯会议若走通信默认设备或独立输出设备会漏采。后端取消水位和问题线程状态也都只存在于进程内，不跨服务重启；这些都需要真实故障场景验收。
