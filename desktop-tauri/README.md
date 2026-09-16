# AI 面试助手桌面客户端（Tauri 2）

本目录是项目当前唯一受支持的桌面客户端：Tauri 2（Rust core）+ React 19 + TypeScript + Tailwind CSS 3 + Zustand。客户端对接[后端](../backend/README.md)的 REST API 与 WebSocket v1 协议。

早期 Electron 实现已经退役并删除；当前功能、构建和验证均以 `desktop-tauri/` 为准。

> 改造前快照保存在 `codex/current-code-snapshot`（`de6b26c`）；当前实现位于 `codex/question-thread-accumulator`。

## 职责边界

- **Rust 侧**（`src-tauri/src/`）负责后端通信和本地可靠性逻辑：
  - `ws_client.rs`：WebSocket 认证、事件重放与去重、关闭码处理、重连退避、心跳和音频发送泵。
  - `rest.rs`：Bearer REST 客户端、超时、429 `Retry-After` 和错误翻译。
  - `outbox.rs`：音频分片状态机、重试状态和统计。
  - `reconcile.rs`：本地 outbox 与服务端 `audio-chunks` 的对账 diff。
  - `engine.rs`：活动会话引擎、capture gate、事件游标、manifest、音频文件和对账调度。
  - `system_audio.rs`：Windows 默认 `eConsole` Render endpoint 的 WASAPI loopback、16 kHz 单声道 PCM、`SpeechChunker` 语音段切分（20 ms RMS 窗静音门控、按 `AI_AUDIO_CHUNK_MS` 成片（默认 400 ms）、连续静音冲刷短尾片并产出 `SpeechEnd`）。
  - `settings.rs`：写死的本机后端地址常量 `SERVER_URL`（`http://127.0.0.1:8000`）与 Windows Credential Manager 令牌存储。
  - `overlay.rs`：悬浮提词窗的创建与全部窗口操作（无边框 / 透明 / 置顶 / 任意位置收起成顶部细条 / 鼠标穿透 / 共享隐身），以及 `overlay-layout.json` 的读写。
  - `screenshot.rs`：GDI 全屏抓帧并编码 PNG，供截图解题使用。
  - `lib.rs`：Tauri 命令注册、单活动会话生命周期、前端事件广播、悬浮窗权威状态（`OverlayStateHandle`）与全局热键注册。
- **React 前端**（`src/`）负责界面、状态和系统声音采集编排：
  - `api/bridge.ts`：Tauri `invoke` / `listen` 桥接。
  - `audio/` 与 `public/worklets/`：未被 LivePage 导入的历史麦克风采集模块和对应测试；当前生产实时流程不创建它们，系统声音由 Rust WASAPI loopback 采集并执行 20 ms RMS 静音门控。
  - `stores/`：设置、会话和实时事件状态；`stores/live.ts` 按 `request_id` 保存并发 revision，再由 `thread_id` 聚合为一张问题卡。catch-up swap 保留旧答案，直到新版内容长度追上后再接管；落库答案不清流式分段。
  - `pages/LivePage.tsx`：启动系统声音采集、停止顺序、音频故障熔断、Tauri 字符串错误归一化和收音模式 UI；用 `shared/answer-threads.ts` 的 `buildAnswerFeed` 把流式分段聚合成「一个问题一张卡」。
  - `shared/answer-threads.ts`：纯聚合函数，把 `StreamingAnswer[]` 按 `thread_id ?? request_id` 分组为卡片、按 `revision` 排序、卡片标题取最长的累计问题，每一段保留该 revision 的问题文本（`AnswerVersion.question`），并把已落库的 `answer` 挂到对应卡片上（同时从历史列表剔除，避免重复渲染）；`pickDisplayVersion` 从卡内多段中挑展示版：未被取代里答案最长的（同长取 revision 高者），全部被取代时退回 revision 最高的一段。
  - `pages/feeds/AnswerFeed.tsx`：一个问题一张卡，卡内同一时刻只展示 `pickDisplayVersion` 挑出的一段（标题已是累计问题，段内不再重复问题行、段间没有 `<hr>`）；每张卡常驻「重新生成」按钮（带 `thread_id`，生成中也可点，答案流回同一张卡，6.5 秒防抖冷却）。没有「草稿」概念，也没有思考过程展示。
  - `pages/`：会话列表、实时面试、历史详情和设置。
  - `pages/OverlayPage.tsx`：悬浮窗自己的界面（`#/overlay`），工具条含截图解题、收起、穿透、隐身、置顶、透明度滑条；底部是**快速提问 textarea**（Enter 发送、Shift+Enter 换行，右下角图标按钮提交；走手动提问路径、答案独立成卡；未录制时按钮禁用；聚焦走灰阶提亮不画彩色）；反馈就地显示而不发 toast。整个悬浮窗只用**黑白灰**配色：文字走 white/透明度灰阶、背景是纯中性黑 `rgba(17,17,17,·)`，状态（连接点、模式角标、提示条）一律用亮度区分，不用彩色。提问区与答案区靠**三档亮度分层**（用户拍板 2026-09-02）：footer 操作面 `white/[0.04]` > 输入容器 `white/[0.08]` + 边框 `white/20` + `rounded-lg` > 答案卡 `white/[0.05]`；聚焦时边框提亮到 `white/35`、背景到 `white/[0.10]`；placeholder 只留「向 AI 提问…」，键位说明（Enter 发送 · Shift+Enter 换行）缩小放输入框下方做微字。
  - `shared/overlay-control.ts`：主窗口面板与悬浮窗共用的控制 hook，含热键表 `OVERLAY_HOTKEYS` 和 `overlay:state` 订阅。
  - `shared/overlay-feed.ts`：悬浮窗自己的事件归约。不复用 `stores/live.ts`（第二个 webview 是独立 JS 上下文，那份 store 的 `sessionId` 永远是 null，跨会话过滤会把事件全丢掉），规则是「跟随最新会话」，并只保留最近 `MAX_OVERLAY_STREAM_ENTRIES = 60` 段流式答案；连接彻底断开（引擎停止后 Rust 补发的 `closed`）或切换会话时清掉「录制中」标记——悬浮窗只跟当前连接的面试走。

REST 与 WebSocket 只从 Rust 侧发起；前端不直接连接后端。

## 功能范围

- 创建、列出、查看、结束和删除面试会话。
- WebSocket 实时会话：开始面试、切换 `pc` / `mobile` / `both` 收音模式、重新生成答案和结束面试。
- PC 采集：只使用 Windows 默认 `eConsole` 播放设备 WASAPI loopback，不采集本机麦克风，避免面试官与面试者声音混在同一来源。
- 实时显示转写、LLM 流式答案与最终答案、连接状态和本地音频队列统计。同一问题的多个累计 revision 在后端真实并发，但前端按 `thread_id` 聚合成一张卡；catch-up swap 在卡内展示当前答案最长的版本，新版追平后自然接管，因此不会闪断或堆出多张大卡。单个 revision 失败只标记自己的版本，每张卡保留重新生成按钮。
- 查看历史转写、答案与复盘，并生成普通或搜索增强复盘。
- **会话级答题背景**：实时页（`LivePage`）的「答题背景」弹窗（`SessionContextModal`）可为当前这场面试粘贴岗位 JD 与简历（各 8,000 字符、带字数统计），经 `PUT /api/sessions/{id}/context` 存到后端 `sessions` 表。非空时后端把字段注入答案与截图解题的提示词；留空回通用答案。任何会话状态都能改，面试中途保存后对下一个问题立即生效；每次打开弹窗都从最新会话快照回填。
- 管理 `asr`、`network`、`llm`、`search` 四类服务端配置：列表、新增、编辑、激活和删除；LLM 配置支持低/中/高思考强度（默认低，普通模型自动忽略）；设置页另有独立的全局提示词，可读取、修改和清除。
- 保存访问令牌并执行健康与鉴权探针。后端地址不可配置：客户端与后端始终同机运行，地址写死在 `src-tauri/src/settings.rs` 的 `SERVER_URL`（`http://127.0.0.1:8000`），改端口需要改代码并重新构建。
- 悬浮提词窗（第二个窗口，`#/overlay`）：无边框、透明、始终置顶、可从任意位置收起成屏幕顶部居中的细条、可鼠标穿透、对系统截屏/录屏/窗口共享隐身，实时显示累计问题与分段答案，可直接截图解题，也可在底部 textarea **快速提问**（Enter 发送、Shift+Enter 换行、随内容自动增高、聚焦灰阶提亮，走手动提问路径；暂停录制期间照常可问——门禁只看连接与会话状态，不看采集开关；被拦时按真实拦因细分提示：连接未就绪 / 面试已结束 / 还没开始录制 / 会话状态同步中，不再一句「未在录制中」盖住四种情况）。问题卡的「重新生成」按钮与标题同行靠右（space-between）。配色只有黑白灰。顶栏有**当前会话徽片**（短 id + 点击复制完整 id）和**录制徽标**（录制中 / 录制中 · 手机 / 录制中 · 采集已停 三态，只有本机真的在采时呼吸点才跳）。详见下方「悬浮提词窗」。

## 悬浮提词窗

面试时压在会议软件旁边用。窗口是**惰性创建**的：只有第一次显示时才建，多数会话不会多付一个 WebView2 进程。

所有窗口操作都走 Rust 命令（`src-tauri/src/overlay.rs`），前端不直接调 `@tauri-apps/api/window`：`capabilities/default.json` 只授予 `core:window:default`（一组 getter），没有任何 `allow-set-*`；`capabilities/overlay.json` 只额外给 `overlay` 窗口一个 `core:window:allow-start-dragging`，用于标题条的 `data-tauri-drag-region`。

权威状态在 Rust 的 `OverlayStateHandle`。全局热键会在前端不知情的情况下改它，因此两个 webview 都订阅 `overlay:state` 事件。

### 全局热键

与 Rust `OVERLAY_SHORTCUTS` 和前端 `OVERLAY_HOTKEYS` 一一对应（含顺序），改一边必须改另一边。

| 热键 | 动作 |
|---|---|
| `Ctrl+Alt+O` | 显示 / 隐藏 |
| `Ctrl+Alt+P` | 鼠标穿透开关 |
| `Ctrl+Alt+S` | 共享隐身开关 |
| `Ctrl+Alt+=` | 更清晰（不透明度 +5%） |
| `Ctrl+Alt+-` | 更透明（不透明度 −5%） |
| `Ctrl+Alt+Q` | 截图解题 |
| `Ctrl+Alt+E` | 收起成屏幕顶部居中的细条 / 展开 |
| `Ctrl+Alt+Z` | 开启/暂停录制（用户两次纠正后的最终语义：**绝不结束面试**。没录 = 悬浮窗一键走完 开始会话 → 系统采集 → 上传门禁；在录且本机在采 = 暂停（停系统采集 → 关门禁，掐断 说话→ASR→LLM 链路，会话保持 recording，可再按恢复）；在录但采集已停 = 恢复采集。结束会话只属于主窗口「结束面试」） |

热键是全局注册的，面试时焦点在会议软件里也能用。悬浮窗自己 `.focused(false)` 创建，显示时不抢焦点，否则打字会打进悬浮窗。焦点归属由 Rust 跟踪（`OverlayState.focused`，经 `overlay:state` 事件广播）：用户点了一下悬浮窗、键盘输入转进去时，主窗口面板会实时标出来。

**穿透开着时按住 `Ctrl` 临时可交互。** 鼠标穿透一旦开启，悬浮窗就点不到自己了，用户没有任何操作空间。Rust 起一条 30 ms 轮询线程（`ctrl_key_pressed` 用 `GetAsyncKeyState`），检测到 Ctrl 按下就临时 `set_ignore_cursor_events(false)`，松手立即恢复穿透；状态经 `OverlayState.ctrl_interactive` 广播，悬浮窗在穿透开启时显示「临时交互」角标提示当前可点。穿透关闭/窗口隐藏时轮询线程自动停，不需要一直空转。**每条可能开窗或还原穿透的路径（`overlay_show` / `overlay_toggle` / `overlay_collapse` / `overlay_expand` / `overlay_set_passthrough` / 热键）之后都必须调 `sync_ctrl_watch`**——主窗口面板第一次打开悬浮窗走的是 `toggle`，漏了 sync 轮询线程就不会被拉起，Ctrl 临时交互整个不生效，直到某条热键路径碰巧调过一次（用户实测的「收起再展开才可用」就是它）。

### 几个刻意的设计

- **透明度走 CSS，不走 Win32；且只调背景层。** `overlay_set_opacity` 只记录数值，前端把它拼进卡片背景的 `rgba(17,17,17,·)`（纯中性黑，黑白灰配色的一部分）。**不能**把 opacity 套在卡片根节点上：父级 opacity 会连带所有子元素（文字无法比父级更亮），低档位时提词内容直接看不清。滑条 25%–100%、每 5% 一格，热键也是每按 5% 一格（不在格点上的旧值先对齐到最近格点再走），鼠标和键盘调出来的值落在同一套格点上。文字对比靠**多向描边**：四方向 1px 实描边 + 一圈 4px 晕光（`.overlay-card`/`.overlay-strip` 的 `text-shadow`），等效给每个字符画轮廓——窗口后面是用户桌面，`filter: invert()` 之类的滤镜管不到页面之外的堆叠，只能靠描边让黑底白字、白底浅字都兜得住；悬浮窗 UI 全页禁 `box-shadow`（阴影画在元素矩形外，透明窗口的圆角外直角区会被填成洗不掉的灰色蒙层，用户实测反馈过）。`WS_EX_LAYERED` + alpha 与 `WDA_EXCLUDEFROMCAPTURE` 叠加时在部分显卡驱动上会让窗口彻底不可见，所以也不用 Win32 alpha。窗口本体不用 `backdrop-blur`（半透明窗口体上的 backdrop-filter 在 WebView2 上经常渲染成全透明），用户要的"透明度"就是这层背景 rgba。取值夹在 `0.25`–`1.0`，再低就等于把窗口弄丢了。
- **共享隐身默认开。** 面试场景里「忘记开」的代价远大于「忘记关」。显示顺序也固定：先 `set_content_protected` 再 `show`，窗口一旦出现在共享画面里就已经泄露了。
- **`visible` 不落盘。** 启动就自动弹出悬浮窗会盖住别的窗口，显隐一律由用户触发。
- **所有建窗口的命令都是 `async fn`。** Windows 上同步命令跑在主线程，走到 `WebviewWindowBuilder::build()` 会和 WebView2 的消息泵互锁，整个应用永久卡死。全局热键回调同理跑在事件循环上，只能先识别动作再 `tauri::async_runtime::spawn`。改动 `overlay_*` 命令时不要把它们退回同步。
- **悬浮窗里的反馈就地显示。** 两个 webview 是独立 JS 上下文，`app-toast` 那条链路挂在主窗口的 `AppLayout` 上，从 `#/overlay` 派发过去没人接。截图解题的提示因此长在工具条下方，4 秒后自动消失。
- **截图解题不需要先隐藏窗口。** 悬浮窗本身开着共享隐身，抓到的帧里没有它自己。该按钮只在会话 ready 且正在录制时可用。
- **录制徽标分三态，呼吸点只在本机真的在采时跳。** 会话状态 `recording` 只代表这场面试开始了（切收音模式要求会话保持 recording），不代表本机在采。徽标按 会话状态 × 本机采集门 × 收音模式 显示：`录制中`（本机采集门开着）、`录制中 · 手机`（收音走手机，本机不采）、`录制中 · 采集已停`（会话还开着但本机门关了，主窗口同样有此提示）。采集门状态由 Rust 在 `SetCaptureActive` 翻转时经 `captureState` 引擎事件推送；与「后端启动时把僵尸 `recording` 收回 `idle`」一起，构成「悬浮窗永远如实报告在不在录」的两半。
- **顶栏带当前会话徽片。** 悬浮窗必须知道自己跟的是哪场面试（后续按会话把手机扫码接进来）。平时显示前 8 位短 id，完整 id 在 tooltip，点击复制（复制走 `navigator.clipboard`，非安全上下文 reject 就不装成功，与 Markdown 代码块同款防御）。
- **悬浮窗挂载时从 Rust 播种，不指望事件。** 悬浮窗是惰性创建的第二个 webview，feed 只能靠订阅 `engine:event` 增量更新——晚打开就错过挂载前的一切 `connection`/`sessionState`/`captureState`，于是「明明在录制却提示未在录制」「会话徽片空着」这类状态错乱。修法是在 Rust 侧 `EngineState` 挂一份 `RuntimeSnapshot`（在 `emit_engine_event` 这个唯一出口顺带 `observe()` 更新，连接/会话/采集三个维度都在里面），新增 `live_runtime_state` 命令查询；`OverlayPage` 挂载后先拉快照 `overlay:seed` 播种，再开始吃事件。事件丢失不可恢复，但快照保证「晚来的窗口也能对齐当下」。
- **重开面试时旧引擎的收尾事件按引擎代际整条丢弃。** 真实竞态（用户实测「重开一场面试后给 LLM 发消息完全没回应」）：`live_connect` 替换引擎是先 `stop()` 旧引擎再 `spawn` 新引擎，旧 `ws_client` 循环收尾补发的 `Connection closed` 可能**晚于新引擎的 `ready` 到达**——快照与两个窗口先看到 ready/recording，又被这条迟到的 closed 打回「断开」，而新引擎此后一切正常、不再发任何恢复事件，没人来纠正，提问/截图门禁从此全部静默拦截（「第一次可以，有时再问就不行」的偶发即此竞态的概率性落地）。修法：`EngineState` 挂 `gen: AtomicU64`，`live_connect` 在触碰旧引擎**之前** `fetch_add` 翻代际并把新代际传进新引擎的事件闭包；`emit_engine_event` 出口先比代际，不匹配就整条丢弃（observe 与广播一并跳过）。`live_disconnect` **不**翻代际——正常离开实时页的收尾 closed 是合法广播，该照发。配套：提问框 Enter 直调 `submitQuestion()` 而非 `requestSubmit()`——发送按钮 disabled 时后者是静默无操作，门禁提示亮不出来。
- **提问后有「已发送 · 正在思考」占位卡，主窗口和悬浮窗都有。** 手动提问从发出到第一个流式帧回来之间原本是黑盒：输入框清空、答案区不动，用户不知道发没发出去。现在发送成功即落一张 pending 卡（带旋转图标），`answer_stream` 的 `started`/`failed` 帧带着原问题文本回来时按文本匹配移除（同文本全部移除），连接彻底断开清空。主窗口那张真卡的「正在生成…」态在 `started` 帧到达后无缝接管。
- **主窗口的采集按钮跟随 `captureState` 事件，不再只信本地变量。** 采集可以从悬浮窗热键 `Ctrl+Alt+Z` 开起来，主窗口原来只看自己发起的 `systemAudioOn`，于是「悬浮窗开的采集，主窗口按钮没反应」。现在 `stores/live.ts` 从 `captureState` 事件维护 `captureOn`，按钮显示与切换判定都用「本地开过 ∪ Rust 报告在采」。
- **悬浮窗内滚动条与 Markdown 代码块都跟着背景不透明度走黑白灰。** 全局滚动条 thumb 是深蓝写死的，在半透明悬浮窗上既突兀也不随透明度变化：`OverlayPage` 按当前不透明度算出 `--overlay-thumb`/`--overlay-thumb-hover` 挂在根容器上，`global.css` 在 `body.overlay-root` 作用域里用变量覆盖（底越不透明 thumb 越亮）。Markdown 代码块同理：`Markdown.tsx` 给代码块/语言标签/复制按钮/行内代码挂稳定钩子类（`code-block`/`code-lang`/`code-copy`/`inline-code`），悬浮窗作用域里覆盖成半透明白底 + 降饱和高亮（只保亮度分级），主窗口观感不变。
- **按住 Ctrl 时的滚轮要自己接管。** 穿透 + Ctrl 临时交互下用户要滚答案区，但 Chromium/WebView2 把 Ctrl+滚轮当页面缩放手势吃掉，滚动容器收不到普通滚动——用户只能手拖滚动条。悬浮窗在 document 上以捕获 + 非 passive 挂 wheel 监听：带 ctrlKey 就 `preventDefault()`（阻止缩放）并把增量应用到光标下最近的可滚动容器（多行 textarea / 答案区 / partial 区）；不带 Ctrl 的滚轮不碰，走原生滚动。
- **「最新」按钮的平滑回底要抑制中间帧。** `scrollToTail` 用 `behavior:'smooth'`，动画期间每个中间 scroll 事件的滚动位置都离底部超过 `TAIL_SLACK`，照常判定会把刚隐藏的按钮又闪出来（先消失→一闪→再消失的竞态）。动画期间 `jumpingToTailRef` 抑制 `handleScroll` 的判定，落到底部即解除；600ms 安全阀兜住动画没走到尾的极端情况，不许把按钮永久藏住。
- **答案正文可选中复制，chrome 保持不可选。** body 默认 `user-select:none`（工具条/标题栏/按钮不可选，拖动区不被选字干扰）；Markdown 根节点与问题行挂 `select-text` 放开划选。选中高亮主窗口品牌蓝、悬浮窗 30% 白（黑白灰），配合多向描边在亮暗背景上都可辨。

## 会话与音频流程

1. 前端创建或打开会话，并通过 Tauri 命令启动 Rust live 引擎。
2. Rust 在进入实时面试页时连接 `/ws/{session_id}`，在 5 秒认证窗口内发送 Token 和本地 `last_event_id`；该连接在页面存活期间保持长连接并按退避策略重连，不会在每次转写后重新建连。
3. 收到 `sync_complete` 后进入 ready 状态，并将服务端会话状态和音频序号水位发给前端。
4. 用户点击“开始采集”时，LivePage 启动 Windows 默认播放设备的 WASAPI loopback；本机麦克风保持关闭，系统声音包含腾讯会议对方声音。
5. 系统声音输出 `wav_pcm_s16le`：16 kHz、单声道。常规分片帧数由 `AI_AUDIO_CHUNK_MS` 决定：默认 400 ms 即 6,400 帧；取值先夹到 `MIN_CHUNK_MS = 100` 与 `MAX_CHUNK_MS = 2500`，再向下取整到 `RMS_WINDOW_FRAMES`（20 ms = 320 帧）的整数倍，保证分片边界正好落在 RMS 窗上。解析只做一次，结果缓存在 `static CHUNK_FRAMES: OnceLock<usize>`，所以运行期改环境变量不生效，要重启进程。语音段边界处会额外冲刷一个更短的尾片（携带 `TRAILING_SILENCE_WINDOWS = 5` 即 100 ms 尾部静音，不足 `MIN_CHUNK_FRAMES = SAMPLE_RATE/10`（100 ms）时零填充）。`duration_ms` 按实际采样数计算并夹到 100–2500，`captured_at` 由当前时间回退该时长得到；WASAPI 停止时直接丢弃未成片的残留缓冲。分片调小的收益是首字更快：FunASR 更早吐出第一版累计 `partial`，后端也就更早发出第一次 LLM 请求。
6. `SpeechChunker` 按 20 ms RMS 窗判断有声/静音：一个候选分片至少需要 `MIN_VOICED_WINDOWS = 3` 个过阈值窗口才成片，静音或底噪不会分配 UUID、占用 `chunk_seq`、写 WAV 或调用 ASR。语音段进行中累计 `SPEECH_END_SILENT_WINDOWS = 80` 个静音窗（约 1.6 秒）时，先冲刷短尾片，再产出 `SegmentOutput::SpeechEnd`。
7. `SpeechEnd` 经 `EngineCommand::SpeechEnd { source }` 进入 WS 任务，转成 `{"v":1,"type":"speech_end","source":"pc","through_chunk_seq":<manifest.next_chunk_seq - 1>}`。发送门禁：未完成认证时缓存进 `pending_commands`；`pc_upload_allowed(capture_active, session_status, radio_mode)` 为假时直接跳过；水位为负（还没有任何分片）时跳过；发送失败则把命令压回队首并断开重连补发。后端据此启动问题线程宽限期。
8. 有效分片携带 UUID、采集时间和真实时长交给 Rust；outbox manifest 是 PC 持久 `chunk_seq` 的唯一分配者，React 中的序号只作 UI/兼容水位，不参与落盘身份。
9. Rust 将 WAV 和 manifest 落盘；重发时从同一 WAV 文件读取，保持 `chunk_id`、持久序号、元数据和字节内容不变。
10. 重连 `sync_complete`、`audio_sequence_gap` 或手动请求会触发 `source=pc` 的 keyset 对账，但页面重进、WebSocket 重连或应用重启不会自动重新打开 capture gate，也不会恢复旧采集。

`speech_end` 与 `cancel_audio_source` 必须严格区分：`speech_end` 只标记语音段边界，**不冻结 outbox、不取消任何分片、不影响 ASR**；`cancel_audio_source` 才是取消水位。

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
| Windows Credential Manager | 访问令牌；服务名 `com.aiinterview.desktop`，用户名 `auth-token`。这是唯一的凭据存储，不存在 `settings.json` |
| `<appData>/overlay-layout.json` | 悬浮窗布局：穿透 / 共享隐身 / 置顶 / 透明度，以及逻辑像素的位置和尺寸。明文 JSON，无敏感信息 |
| `<appData>/state/<sessionId>.json` | WebSocket 事件游标 |
| `<appData>/state/outbox-<sessionId>.json` | 音频 outbox manifest、唯一 PC 序号水位和待补发取消意图 |
| `<appData>/audio/*.wav` | 待发送或等待重试的音频分片 |

令牌由 Rust `keyring` 的 `windows-native` 后端保存，不会明文落盘；`settings.json`、`token.hold` 和 `token.salt` 都不存在。

`overlay-layout.json` 的写入时机：每个 `overlay_*` 命令、每次热键动作，以及 `RunEvent::ExitRequested | Exit`。拖动和缩放窗口**只写内存**，一次拖动会连发几十个 `Moved` 事件，每条都落盘纯属浪费；只拖过窗口就退出的路径靠退出钩子兜住。这个文件被当作不可信输入读取：缺字段、坏 JSON、`NaN`、小于最小尺寸的几何一律丢弃回默认值，透明度在装载时重新夹一次，所以手改成 `0` 也不会让窗口彻底看不见。`visible` 不在文件里，见上文「悬浮提词窗」。

outbox manifest 的终态记录（`done` / `terminal_error` / `released`，WAV 均已删除）只保留最近 200 条用于去重与对账，超出部分在 ack 收敛点淘汰；`next_chunk_seq` 水位与取消意图永不随淘汰回退，未决分片（captured/sending/queued/cancel_pending/retryable_failed）永不淘汰。

## 已知限制（安全与协议）

以下行为经评审标记为已知限制，暂不修复，仅在此说明：

- TLS 证书校验依赖 `webpki-roots` 内置根证书集合，不读取 Windows 系统证书存储；企业内网自签 CA（如 MITM 代理）会导致连接失败。`https://` 场景受此影响。
- 后端地址写死为 `http://127.0.0.1:8000`，因此 WebSocket 使用明文 `ws://`，认证 Token 与音频数据不加密。这只在客户端与后端同机（loopback 不出网卡）时成立；如果未来把后端搬到另一台机器，必须先改成 `https://`/`wss://` 而不是直接改常量的主机名。
- WebSocket 断开时不主动发送 Close 帧（依赖 TCP/TLS 层关闭），服务端会以异常断开记录；不影响重连与事件游标恢复。
- 默认后端 FunASR 按 `session + source` 复用长连接，一个语音段对应一个 utterance：段首一次 `start`，中间分片只推裸 PCM，`speech_end` 才 `stop`。累计 `partial` 经几何节流后触发 LLM revision；各 revision 互不取消，在会话并发上限内同时生成。分片推流后立即 ack `done`，前端 outbox 不会因为等待段末 final 而顶满；一个语音段最终只落一条转写。
- 问题线程状态（`thread_id`、`revision`、宽限定时器）完全保存在后端进程内存，不落库。后端重启会丢失未关闭线程尚未入库的分段，客户端只会看到卡片停止更新。
- 实时转写栏底部提供 Agent 风格手动提问框：Enter 发送、Shift+Enter 换行，提交后复用当前 WebSocket 的并发答案通道。手动提问没有 `thread_id`，答案立即入库。
- 悬浮窗的「共享隐身」边界必须说清楚：Windows 的 `WDA_EXCLUDEFROMCAPTURE` 只让**操作系统的**截屏、录屏和窗口共享 API 拿不到这块画面。它挡不住采集卡、挡不住外接摄像头，也挡不住有人用手机拍屏幕。同时它依赖显卡驱动实现，个别环境下可能失效，重要场合请自己先录一段确认。
- 悬浮窗的位置和尺寸落在 `overlay-layout.json`，但显示器拓扑变化（拔掉副屏、改缩放比例）后恢复出来的坐标可能落在屏幕外。用 `Ctrl+Alt+E` 收起再展开即可找回窗口（收起会把窗口移到当前显示器工作区顶部居中；展开则回到**收起前**的位置和宽高——记录端拒收细条尺寸的几何，收起瞬间的 `set_size` 不会污染落盘布局），程序不做主动纠偏。

## 窗口与打包

- 单主窗口，默认 1280×820，最小 1024×680。
- 另有一个可选的第二窗口 `overlay`（悬浮提词窗，默认 560×560，最小 280×200；已调过宽高的用户以 `overlay-layout.json` 落盘值为准），不在 `tauri.conf.json` 里声明，由 Rust 在首次显示时惰性创建，入口是 `index.html#/overlay`（`HashRouter`，不需要额外构建产物）。它 `skip_taskbar`、无边框、无阴影、透明背景。
- 通过 `tauri-plugin-single-instance` 阻止双开：第二个实例启动时聚焦已有主窗口并退出。双开会导致两个实例各自分配 `chunk_seq`，服务端对账将另一实例的分片判为序号占用并烧毁，表现为静默丢音频，因此该限制必须保留。`single_instance` 聚焦的是 `main`，悬浮窗不受影响。
- 前端 CSP 只允许自身资源和 Tauri IPC；后端网络请求由 Rust 完成。
- 当前生产 bundle 目标仅为 Windows NSIS 安装包。
- Rust release profile 启用 `panic = "abort"`、LTO、单 codegen unit、体积优化和 strip。`panic = "abort"` 前会先经过全局 panic hook，把 panic 位置与消息追加写入 `<appData>/panic.log`，用于闪退后排查。

## 开发与构建

~~~bash
cd desktop-tauri
npm install
npm run dev          # 仅启动 Vite 前端
npm test             # Node 内置测试：语音段切分、问题卡片聚合、UI 与错误显示等
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

- `npm test`：**`170 passed` / `0 failed`（2026-09-14 实测）**。覆盖语音段切分、一问题一张卡、并发 revision 的 catch-up swap、重新生成、pending 状态、主窗口与悬浮窗事件同步、截图解题、快捷键、布局持久化、Markdown 安全渲染和错误边界。
  - `tests/live-page-stop.test.mjs:55` 此前断言 LivePage 含 `[&::placeholder]:text-center`，而实际代码用 `text-left ... placeholder:text-ink-faint`。单行 composer 的占位文字跟正文左对齐才对，所以已按代码修正断言，不是改代码去迁就过时断言。
  - `tests/overlay.test.mjs` 的热键断言此前写死「Rust 侧应有 6 个热键」，加了 `Ctrl+Alt+Q` 截图解题后失败。已改成两侧归一化后 `deepEqual`（Rust 的 `Equal`/`Minus` 映射到 `=`/`-`），断言的是一一对应且**顺序一致**——热键按下标派发动作，顺序错位会执行错的动作——而不是某个固定数量。
  - 新增针对悬浮窗的源码级断言，专门覆盖单元测试测不到的运行期陷阱：`overlay_*` 命令（现为 10 个）必须全是 `async fn`、热键 handler 必须 `spawn` 后才碰窗口（这两条对应真实发生过的 WebView2 主线程死锁）、`OverlayLayout` 落盘字段齐全且**不含 `visible`**、坏掉的布局文件必须回默认值、退出钩子必须落盘、截图解题必须就地反馈而不是派发 `app-toast`、按钮必须先按录制状态禁用；2026-08-31 又补了「引擎停止必须补发 `closed` 否则悬浮窗永远报录制中」和「收起无前置条件、细条落工作区顶部居中」。
- `cargo test --locked`：**`72 passed` / `0 failed`（2026-09-14 实测）**；包含系统音频 400 ms 分片、1.6 秒静音边界、悬浮窗布局、截图编码、outbox、重连和协议测试。
- 悬浮提词窗的**真实窗口**验证（2026-08-29，非单元测试）：`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9223" npm run tauri:dev` 起真窗口，用 `playwright-core` 的 `connectOverCDP` 驱动。确认显示/隐藏/穿透/隐身/置顶/透明度/贴边全部生效，工具条 8 个按钮、未录制时截图解题按钮为 `disabled`；随后 `taskkill` 杀进程重启，`overlay_snapshot` 恢复出 `opacity: 0.81` 且 `visible: false`（正确地没有自动弹窗），`overlay_show` 后窗口回到 `420×560 @ (1484, 456)`。**注意：该记录早于 2026-08-31 的改动（贴边与自动收起已删、收起改为顶部居中细条、透明度改 5% 滑条、断开补发 `closed`），也早于同日第二轮修复（Ctrl 临时交互轮询、细条几何拒收后原地展开、默认宽度 560、细条极简为圆点+箭头）与 2026-09-01 第三轮（toggle/collapse/expand 补 `sync_ctrl_watch`、第 8 个热键 `Ctrl+Alt+Z` 当时广播 `overlay:start-recording`、`SetCaptureActive` 后补发 `captureState` 事件），更早于同日第五轮（`EngineState` 增加 `RuntimeSnapshot` 与 `live_runtime_state` 命令、悬浮窗挂载播种）与 2026-09-02 第六、七轮（第六轮曾把 Z 热键改成「在录即结束会话」的开关语义并更名事件 `overlay:toggle-recording`，第七轮经用户纠正改回最终语义：开启/暂停采集链路、**不结束会话**，悬浮窗侧不再调用 `endSession`），以及 2026-09-03 第九轮（`EngineState` 增加引擎代际 `gen`：重开面试时被替换旧引擎的迟到事件在 `emit_engine_event` 出口整条丢弃；`live_connect` 先翻代际再取旧引擎）——这些 Rust 改动只做了静态验证，沙箱无 cargo，尚未 `cargo test`，也未做真窗口回归；第五轮的 `live_runtime_state` 在用户真机首次编译时实锤过一条 E0277（带 `State<'_, _>` 借用参数的 async 命令必须返回 `Result`，已改为 `Result<RuntimeSnapshot, String>`），这正是静态验证漏掉的错误类型；`overlay.rs` 的纯函数单元测试对窗口行为零覆盖**，之前正是这类「测试全绿而功能 100% 挂」的死锁 bug，验证悬浮窗只能用真实窗口。
> 2026-09-14 更新：上一条真窗口记录中的“后续 Rust 改动尚未测试”只描述当时状态；当前 Rust 测试与严格检查均已重新执行通过。真窗口仍需按最新行为复测。

- `npm run build`（含 `tsc --noEmit`）通过（2026-09-14 实测）。
- `cargo fmt --check`、`cargo check --locked` 与 `cargo clippy --locked -- -D warnings` 均通过（2026-09-14 实测）。

仍未验证：

- 悬浮窗的共享隐身只在本机 Windows 上用系统截屏 API 做过冒烟，**没有**在腾讯会议、Zoom、OBS 和录屏软件里逐个确认，也没有跨显卡驱动验证。重要场合请自己先录一段确认。
- 悬浮窗在 macOS 上未验证。`set_content_protected` 在 Tauri 里是跨平台 API，但 macOS 的实现路径与 `WDA_EXCLUDEFROMCAPTURE` 不同，当前打包目标也只有 Windows NSIS。
- 悬浮窗在多显示器 + 混合 DPI 缩放下的收起细条落点（应落在窗口当前所在显示器的工作区顶部居中），以及拔掉副屏后从 `overlay-layout.json` 恢复的坐标是否仍在可见区域。
- 全局热键与其他软件的冲突面（`Ctrl+Alt+*` 相对干净，但输入法和显卡驱动工具也常占这一段）。
- 真实 Windows 默认播放设备的 WASAPI loopback 是否能在目标机器稳定采集声音。
- 腾讯会议对方声音、普通媒体播放和真实 FunASR 转写结果；待网络可达后补充端到端验证。
- WASAPI 当前不枚举或选择多端点。腾讯会议必须输出到同一个 Windows 默认 `eConsole` 播放设备；如果它使用“默认通信设备”、独立声卡或蓝牙通话端点，当前实现不会捕获那一路声音。
- 静音、停止采集、切 `mobile` 和退出页面在真实运行进程中的 SQLite/网络行为。
- `SPEECH_END_SILENT_WINDOWS`（1.6 秒）、`TRAILING_SILENCE_WINDOWS`（100 ms）与后端 `AI_QUESTION_THREAD_GRACE_SECONDS`（6 秒）这三个阈值只有单元测试覆盖，没有真实语速/停顿证据，需在真机长时间面试中调参。
- 断线、应用重启、服务重启、积压发送和音频对账的端到端恢复结果。
- 系统声音长时间采集、默认播放设备变化和跨网络切换。
- 真实 ASR、LLM、搜索服务及代理配置。
- NSIS 安装包在干净 Windows 环境中的安装、升级和卸载冒烟。

因此，当前代码目标是能够采集腾讯会议等应用经 Windows 默认播放设备输出的声音，但在本轮真机结果补充前，不能把“编译和单元测试通过”表述为该场景已经验收。
