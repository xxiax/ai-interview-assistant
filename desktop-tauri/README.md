# AI 面试助手桌面客户端（Tauri 2）

本目录是项目当前唯一受支持的桌面客户端：Tauri 2（Rust core）+ React 19 + TypeScript + Tailwind CSS 3 + Zustand。客户端对接[后端](../backend/README.md)的 REST API 与 WebSocket v1 协议。

早期 Electron 实现已经退役并删除；当前功能、构建和验证均以 `desktop-tauri/` 为准。

## 职责边界

- **Rust 侧**（`src-tauri/src/`）负责后端通信和本地可靠性逻辑：
  - `ws_client.rs`：WebSocket 认证、事件重放与去重、关闭码处理、重连退避、心跳和音频发送泵。
  - `rest.rs`：Bearer REST 客户端、超时、429 `Retry-After` 和错误翻译。
  - `outbox.rs`：音频分片状态机、重试状态和统计。
  - `reconcile.rs`：本地 outbox 与服务端 `audio-chunks` 的对账 diff。
  - `engine.rs`：活动会话引擎、capture gate、事件游标、manifest、音频文件和对账调度。
  - `system_audio.rs`：Windows 默认 `eConsole` Render endpoint 的 WASAPI loopback、16 kHz 单声道 PCM、2.5 秒组帧和静音门控。
  - `settings.rs`：服务器地址文件与 Windows Credential Manager 令牌存储。
  - `lib.rs`：Tauri 命令注册、单活动会话生命周期和前端事件广播。
- **React 前端**（`src/`）负责界面、状态和系统声音采集编排：
  - `api/bridge.ts`：Tauri `invoke` / `listen` 桥接。
  - `audio/` 与 `public/worklets/`：未被 LivePage 导入的历史麦克风采集模块和对应测试；当前生产实时流程不创建它们，系统声音由 Rust WASAPI loopback 采集并执行 20 ms RMS 静音门控。
  - `stores/`：设置、会话和实时事件状态。
  - `pages/LivePage.tsx`：启动系统声音采集、停止顺序、音频故障熔断、Tauri 字符串错误归一化和收音模式 UI。
  - `pages/`：会话列表、实时面试、历史详情和设置。

REST 与 WebSocket 只从 Rust 侧发起；前端不直接连接后端。

## 功能范围

- 创建、列出、查看、结束和删除面试会话。
- WebSocket 实时会话：开始面试、切换 `pc` / `mobile` / `both` 收音模式、重新生成答案和结束面试。
- PC 采集：只使用 Windows 默认 `eConsole` 播放设备 WASAPI loopback，不采集本机麦克风，避免面试官与面试者声音混在同一来源。
- 实时显示转写、LLM 流式答案与最终答案、连接状态和本地音频队列统计；流式内容会在最终答案到达后自动替换为历史答案。
- 查看历史转写、答案与复盘，并生成普通或搜索增强复盘。
- 管理 `asr`、`network`、`llm`、`search` 四类服务端配置：列表、新增、编辑、激活和删除；LLM 配置支持低/中/高思考强度（默认低，普通模型自动忽略）；设置页另有独立的全局提示词，可读取、修改和清除。
- 保存服务器地址、保存访问令牌并执行健康与鉴权探针。

## 会话与音频流程

1. 前端创建或打开会话，并通过 Tauri 命令启动 Rust live 引擎。
2. Rust 在进入实时面试页时连接 `/ws/{session_id}`，在 5 秒认证窗口内发送 Token 和本地 `last_event_id`；该连接在页面存活期间保持长连接并按退避策略重连，不会在每次转写后重新建连。
3. 收到 `sync_complete` 后进入 ready 状态，并将服务端会话状态和音频序号水位发给前端。
4. 用户点击“开始采集”时，LivePage 启动 Windows 默认播放设备的 WASAPI loopback；本机麦克风保持关闭，系统声音包含腾讯会议对方声音。
5. 系统声音输出 `wav_pcm_s16le`：16 kHz、单声道、正常 40,000 帧（2.5 秒）。WASAPI 停止时直接丢弃不足 2.5 秒尾片。
6. 系统声音按 20 ms RMS 窗检测，一个 2.5 秒分片至少有 3 个过阈值窗口才被视为有效。静音或当前底噪不会生成分片，因此不会分配 UUID、占用 `chunk_seq`、写 WAV 或调用 ASR。当前不会额外发送 `speech_end`，所以后端无法仅凭“没有新分片”精确知道静音从何时开始。
7. 有效分片携带 UUID、采集时间和真实时长交给 Rust；outbox manifest 是 PC 持久 `chunk_seq` 的唯一分配者，React 中的序号只作 UI/兼容水位，不参与落盘身份。
8. Rust 将 WAV 和 manifest 落盘；重发时从同一 WAV 文件读取，保持 `chunk_id`、持久序号、元数据和字节内容不变。
9. 重连 `sync_complete`、`audio_sequence_gap` 或手动请求会触发 `source=pc` 的 keyset 对账，但页面重进、WebSocket 重连或应用重启不会自动重新打开 capture gate，也不会恢复旧采集。

## 停止、切换与失败收敛

停止采集、切到 `mobile`、离开实时页或连接中断时，LivePage 按以下顺序收敛本机音频：

1. 递增 capture epoch，使旧 AudioWorklet 回调立即失效。
2. 停止 WASAPI 线程并等待其退出，避免停止边界后又落入一个完整系统音频片。
3. 等待已经发起的 `audio_chunk` invoke 完成。
4. 关闭 Rust capture gate。门关闭后迟到的 `AddChunk` 会在分配序号和落盘前被拒绝。
5. 冻结本地 outbox，并持久化 `cancel_audio_source` 意图：`source`、`through_chunk_seq`、`reason`。
6. 后端取消该来源水位以内的 `queued`、可重试 `failed` 和当前 ASR 任务，写入并广播 `chunk_ack: cancelled`；更高序号的新一轮采集仍可继续。

协议允许的取消原因是 `capture_stopped` 和 `source_disabled`。页面退出或连接被替换的内部 `capture_interrupted` 会在发送到后端前规范化为 `capture_stopped`。

`processing_failed` 仍是协议层可重试错误，但同一分片累计 3 次处理失败后，Tauri 会转为终态并停止自动重试。收到中间 `queued` ACK 不会清空失败次数，因此不会形成无限的 `failed -> queued -> failed` 循环。LivePage 收到本轮首个 `audio_processing_failed` 时会熔断系统声音采集并取消积压；系统音频线程故障不会降级到麦克风。

Tauri 命令拒绝 Promise 时，错误值可能是标准 `Error`，也可能是 Rust 侧直接传回的字符串。LivePage 通过统一的 `errorMessage(error: unknown)` 归一化两种形状，再写入 toast；不会再对字符串读取 `.message` 而显示空白提示。

实现中的可重试错误包括 `missing_predecessor`、`processing_failed`、`service_restart`、`service_shutdown` 和 `usage_limited`，但自动重试受上述停止门禁和失败次数上限约束。终态包括完成、显式取消、会话结束取消、无效音频和来源不允许等情况。

## 本地设置与数据

Tauri identifier 为 `com.aiinterview.desktop`。Windows 上的应用数据位于 Tauri 返回的 app data 目录，通常对应 `%APPDATA%\com.aiinterview.desktop`。

| 路径或存储 | 用途 |
|---|---|
| `<appData>/settings.json` | 服务器地址 |
| Windows Credential Manager | 访问令牌；服务名 `com.aiinterview.desktop`，用户名 `auth-token` |
| `<appData>/state/<sessionId>.json` | WebSocket 事件游标 |
| `<appData>/state/outbox-<sessionId>.json` | 音频 outbox manifest、唯一 PC 序号水位和待补发取消意图 |
| `<appData>/audio/*.wav` | 待发送或等待重试的音频分片 |

令牌由 Rust `keyring` 的 `windows-native` 后端保存，不会写入 `settings.json`，也不存在 `token.hold` 或 `token.salt` 文件。

outbox manifest 的终态记录（`done` / `terminal_error` / `released`，WAV 均已删除）只保留最近 200 条用于去重与对账，超出部分在 ack 收敛点淘汰；`next_chunk_seq` 水位与取消意图永不随淘汰回退，未决分片（captured/sending/queued/cancel_pending/retryable_failed）永不淘汰。

## 已知限制（安全与协议）

以下行为经评审标记为已知限制，暂不修复，仅在此说明：

- TLS 证书校验依赖 `webpki-roots` 内置根证书集合，不读取 Windows 系统证书存储；企业内网自签 CA（如 MITM 代理）会导致连接失败。`https://` 场景受此影响。
- 配置为 `http://` 的服务器地址会使用明文 `ws://`，认证 Token 与音频数据不加密，仅适合本机或可信内网调试，生产应使用 `https://`。
- WebSocket 断开时不主动发送 Close 帧（依赖 TCP/TLS 层关闭），服务端会以异常断开记录；不影响重连与事件游标恢复。
- 默认后端 FunASR 按 `session + source` 复用长连接，但每个切片都立即执行自己的 `start/PCM/stop/final` 并触发独立 LLM 请求；相邻 final 仅在客户端显示层按来源、连续序号和最多 6 秒间隔合并，历史数据和 LLM 触发时机不变。
- 当前没有客户端 `speech_end`、后端问题线程累积器或天然的“整个问题 final”。面试官一句话跨多个 2.5 秒切片时，会出现多个独立答案。
- 实时转写栏底部提供 Agent 风格手动提问框：Enter 发送、Shift+Enter 换行，提交后复用当前 WebSocket 的并发答案通道。

## 窗口与打包

- 单主窗口，默认 1280×820，最小 1024×680。
- 通过 `tauri-plugin-single-instance` 阻止双开：第二个实例启动时聚焦已有主窗口并退出。双开会导致两个实例各自分配 `chunk_seq`，服务端对账将另一实例的分片判为序号占用并烧毁，表现为静默丢音频，因此该限制必须保留。
- 前端 CSP 只允许自身资源和 Tauri IPC；后端网络请求由 Rust 完成。
- 当前生产 bundle 目标仅为 Windows NSIS 安装包。
- Rust release profile 启用 `panic = "abort"`、LTO、单 codegen unit、体积优化和 strip。`panic = "abort"` 前会先经过全局 panic hook，把 panic 位置与消息追加写入 `<appData>/panic.log`，用于闪退后排查。

## 开发与构建

~~~bash
cd desktop-tauri
npm install
npm run dev          # 仅启动 Vite 前端
npm test             # Node 测试：AudioWorklet 静音门控
npm run typecheck    # tsc --noEmit
npm run build        # 类型检查 + Vite 生产构建
npm run tauri:dev    # 完整 Tauri 应用（HMR）
npm run tauri:build  # 前端 + Rust release + NSIS
cd src-tauri
cargo fmt --check
cargo test --locked
cargo check --locked
~~~

前置要求：

- Node.js 20+
- Rust stable，安装 MSVC target
- Visual Studio C++ Build Tools 与 Windows SDK

首次 `tauri:dev` 或 Rust 构建需要冷编译 Tauri 依赖，耗时会明显长于后续增量构建。

## 当前验证边界

当前已验证：

- `npm test`：`92 passed`，覆盖系统声音采集停止边界、答案优先布局、手动提问框、partial 转写、并发流式答案、事件/历史去重、设置表单、全局提示词、保留的历史音频模块和 Tauri 字符串错误非空显示。
- `npm run build` 通过，包含 TypeScript 类型检查和 Vite 生产构建。
- `cargo test --locked`：`42 passed`，覆盖 outbox 状态迁移、HTTPS 服务器地址、WebSocket URL、3 次处理失败上限、取消意图、迟到分片门禁、WAV 头、系统音频组帧、重连缓冲和静音判定等纯逻辑。
- `cargo check --locked`、`cargo fmt --check` 与 `cargo clippy --locked -- -D warnings` 通过。

仍未验证：

- 真实 Windows 默认播放设备的 WASAPI loopback 是否能在目标机器稳定采集声音。
- 腾讯会议对方声音、普通媒体播放和真实 FunASR 转写结果；待网络可达后补充端到端验证。
- WASAPI 当前不枚举或选择多端点。腾讯会议必须输出到同一个 Windows 默认 `eConsole` 播放设备；如果它使用“默认通信设备”、独立声卡或蓝牙通话端点，当前实现不会捕获那一路声音。
- 静音、停止采集、切 `mobile` 和退出页面在真实运行进程中的 SQLite/网络行为。
- 断线、应用重启、服务重启、积压发送和音频对账的端到端恢复结果。
- 系统声音长时间采集、默认播放设备变化和跨网络切换。
- 真实 ASR、LLM、搜索服务及代理配置。
- NSIS 安装包在干净 Windows 环境中的安装、升级和卸载冒烟。

因此，当前代码目标是能够采集腾讯会议等应用经 Windows 默认播放设备输出的声音，但在本轮真机结果补充前，不能把“编译和单元测试通过”表述为该场景已经验收。
