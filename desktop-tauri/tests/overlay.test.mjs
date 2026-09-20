import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { installWindowStub, registerTsExtensionResolve } from './helpers.mjs'

installWindowStub()
await registerTsExtensionResolve()

const { applyOverlayEvent, initialOverlayFeed, MAX_OVERLAY_STREAM_ENTRIES } = await import(
  '../src/shared/overlay-feed.ts'
)

const overlayPageSource = readFileSync(new URL('../src/pages/OverlayPage.tsx', import.meta.url), 'utf8')
const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
const globalCss = readFileSync(new URL('../src/styles/global.css', import.meta.url), 'utf8')
const markdownSource = readFileSync(new URL('../src/components/Markdown.tsx', import.meta.url), 'utf8')
const overlayControlSource = readFileSync(
  new URL('../src/shared/overlay-control.ts', import.meta.url),
  'utf8'
)
const overlayPanelSource = readFileSync(
  new URL('../src/components/OverlayPanel.tsx', import.meta.url),
  'utf8'
)
const overlayRust = readFileSync(
  new URL('../src-tauri/src/overlay.rs', import.meta.url),
  'utf8'
)
const libRust = readFileSync(new URL('../src-tauri/src/lib.rs', import.meta.url), 'utf8')
const cargoToml = readFileSync(
  new URL('../src-tauri/Cargo.toml', import.meta.url),
  'utf8'
)

function streamEvent(sessionId, requestId, text, overrides = {}) {
  // H1 delta-only 契约:token 帧只带 delta(answer 恒为空串),done 帧携带全文。
  return {
    kind: 'serverMessage',
    type: 'answer_stream',
    request_id: requestId,
    session_id: sessionId,
    question: `问题 ${requestId}`,
    channel: 'answer',
    delta: text,
    answer: '',
    source: 'llm',
    done: false,
    ...overrides
  }
}

function reduce(events, from = initialOverlayFeed) {
  return events.reduce((state, event) => applyOverlayEvent(state, event), from)
}

// ---------- 归约 ----------

test('overlay feed follows the newest session instead of filtering by sessionId', () => {
  // 悬浮窗没有"当前会话"的概念(它不走 REST 也不知道路由参数),
  // 所以第一条载荷就该被接住,不能像主窗口那样因为 sessionId 为 null 全丢掉。
  const state = reduce([streamEvent('s1', 'r1', '答案一')])
  assert.equal(state.sessionId, 's1')
  assert.equal(state.streaming.r1.answer, '答案一')
})

test('switching sessions clears the previous session content', () => {
  const first = reduce([
    streamEvent('s1', 'r1', '上一场的答案'),
    { kind: 'serverMessage', type: 'transcript_partial', session_id: 's1', source: 'pc', text: '上一场的问题' }
  ])
  assert.equal(Object.keys(first.streaming).length, 1)

  const second = applyOverlayEvent(first, streamEvent('s2', 'r9', '新一场'))
  assert.equal(second.sessionId, 's2')
  assert.deepEqual(Object.keys(second.streaming), ['r9'])
  assert.equal(second.partial, null, '上一场的提问不能留在窗口里')
})

test('switching sessions keeps the connection but drops the old session status', () => {
  // 连接状态是全局的,切会话清掉会让顶栏错报"未连接"。
  // 但上一场的"录制中"不能带过来:悬浮窗只跟当前连接的面试走,
  // 新会话会自己报告 sessionState。
  const state = reduce([
    { kind: 'connection', phase: 'ready' },
    { kind: 'sessionState', status: 'recording', radioMode: 'pc' },
    streamEvent('s1', 'r1', 'a'),
    streamEvent('s2', 'r2', 'b')
  ])
  assert.equal(state.phase, 'ready')
  assert.equal(state.sessionStatus, '', '上一场的录制状态不能替新会话报')
})

test('a full disconnect clears the recording badge instead of latching it', () => {
  // 离开实时页时引擎被停掉,ws_client 在循环外补发一条 closed(连接已断开);
  // 归约若不清状态,悬浮窗会永远替已经结束的面试报"录制中"。
  const state = reduce([
    { kind: 'connection', phase: 'ready' },
    { kind: 'sessionState', status: 'recording', radioMode: 'pc' },
    { kind: 'connection', phase: 'closed', note: '连接已断开' }
  ])
  assert.equal(state.phase, 'closed')
  assert.equal(state.sessionStatus, '')
})

test('reconnecting keeps the last session status', () => {
  // 重连中的短暂断线不算断开:状态标了又擦会让"录制中"在顶栏闪烁。
  const state = reduce([
    { kind: 'connection', phase: 'ready' },
    { kind: 'sessionState', status: 'recording', radioMode: 'pc' },
    { kind: 'connection', phase: 'reconnecting' }
  ])
  assert.equal(state.phase, 'reconnecting')
  assert.equal(state.sessionStatus, 'recording')
})

test('stopping the engine emits a closed event after the connection loop', () => {
  // engine.stop() 走的是循环里 `if stopped { break; }` 这条静默路径,
  // 正常结束(close 码 1000)才会发 closed。Rust 侧若不补发,前端归约
  // 永远收不到断开事件,上面的"清状态"测试形同虚设。
  const wsClientRust = readFileSync(
    new URL('../src-tauri/src/ws_client.rs', import.meta.url),
    'utf8'
  )
  assert.match(wsClientRust, /if stopped \{\s*break;\s*\}/)
  const afterLoop = wsClientRust.slice(wsClientRust.lastIndexOf("if stopped {\n            break;"))
  assert.match(
    afterLoop,
    /if stopped \{\s*emit\(EngineEvent::Connection \{\s*phase: engine::phase::CLOSED/,
    '循环结束后必须为 stopped 补发 closed,否则悬浮窗状态卡死在上一场'
  )
})

test('a final transcript clears the partial but keeps the answers', () => {
  const state = reduce([
    streamEvent('s1', 'r1', '答案'),
    { kind: 'serverMessage', type: 'transcript_partial', session_id: 's1', source: 'pc', text: '半句' },
    { kind: 'serverMessage', type: 'transcript', id: 1, session_id: 's1', source: 'pc', text: '整句', timestamp: 'x', seq: 1 }
  ])
  assert.equal(state.partial, null)
  assert.equal(state.streaming.r1.answer, '答案')
})

test('an empty partial is treated as no partial at all', () => {
  const state = reduce([
    { kind: 'serverMessage', type: 'transcript_partial', session_id: 's1', source: 'pc', text: '   ' }
  ])
  assert.equal(state.partial, null)
})

test('the thinking channel is dropped', () => {
  // 思考过程通道已下线,主窗口丢弃,悬浮窗必须一致,否则会多渲染一段空白。
  const state = reduce([streamEvent('s1', 'r1', '想一想', { channel: 'thinking' })])
  assert.deepEqual(state.streaming, {})
})

test('a failed segment keeps the text generated so far', () => {
  const state = reduce([
    streamEvent('s1', 'r1', '已经生成的一半'),
    streamEvent('s1', 'r1', '', { failed: true, done: true, answer: '' })
  ])
  assert.equal(state.streaming.r1.failed, true)
  assert.equal(state.streaming.r1.answer, '已经生成的一半', '失败不该把已生成内容抹掉')
})

test('a newer revision of the same thread keeps the older segment (catch-up swap)', () => {
  // catch-up swap:开新 revision 时旧段**不再被删**——它正流着被删会让用户
  // 眼前的答案凭空消失。两段都保留,渲染层(pickDisplayVersion)挑「未被
  // 取代里答案最长」的展示:新版追平长度即自然接管。
  const state = reduce([
    streamEvent('s1', 'r1', '', { thread_id: 't1', revision: 1, started: true }),
    streamEvent('s1', 'r1', '旧版已经流出很长的一段答案文本', {
      thread_id: 't1',
      revision: 1
    }),
    streamEvent('s1', 'r2', '', { thread_id: 't1', revision: 2, started: true }),
    streamEvent('s1', 'r2', '新版开头', { thread_id: 't1', revision: 2 })
  ])
  assert.deepEqual(Object.keys(state.streaming).sort(), ['r1', 'r2'])
  assert.equal(state.streaming.r1.answer, '旧版已经流出很长的一段答案文本', '旧段必须保留兜底')
  assert.equal(state.streaming.r2.revision, 2)
})

test('a superseded terminal frame freezes the segment instead of removing it', () => {
  // 旧版被取消时后端补一帧 done+superseded(不带 failed)。段冻结而不是删除:
  // 半截答案仍可读,渲染层挑展示版时跳过它;新版失败时它就是最长的可读答案。
  const state = reduce([
    streamEvent('s1', 'r1', '', { thread_id: 't1', revision: 1, started: true }),
    streamEvent('s1', 'r1', '旧版半截', { thread_id: 't1', revision: 1 }),
    streamEvent('s1', 'r1', '', {
      thread_id: 't1',
      revision: 1,
      done: true,
      superseded: true
    })
  ])
  assert.ok(state.streaming.r1, 'superseded 帧不能删段')
  assert.equal(state.streaming.r1.superseded, true)
  assert.equal(state.streaming.r1.done, true)
  assert.equal(state.streaming.r1.failed, false)
  assert.equal(state.streaming.r1.answer, '旧版半截', '半截答案保留兜底')
})

test('token 帧按 delta 累计，全文只在 done 帧可信', () => {
  // H1 delta-only 契约:token 帧 answer 恒为空串,只有 delta 是增量;
  // done 帧携带服务端权威全文。
  const state = reduce([
    streamEvent('s1', 'r1', '缓存穿透是'),
    streamEvent('s1', 'r1', '查询不存在的键'),
    streamEvent('s1', 'r1', '', { done: true, answer: '缓存穿透是查询不存在的键（全文）' })
  ])
  assert.equal(state.streaming.r1.answer, '缓存穿透是查询不存在的键（全文）')
  assert.equal(state.streaming.r1.done, true)
})

test('duplicate persisted answers are not appended twice', () => {
  const answer = {
    kind: 'serverMessage',
    type: 'answer',
    id: 7,
    session_id: 's1',
    question: 'q',
    answer: 'a',
    source: 'llm',
    created_at: 'x'
  }
  const state = reduce([answer, answer])
  assert.equal(state.answers.length, 1)
})

test('streaming segments are capped so a long interview cannot grow without bound', () => {
  const events = []
  for (let i = 0; i < MAX_OVERLAY_STREAM_ENTRIES + 10; i += 1) {
    events.push(streamEvent('s1', `r${i}`, `答案 ${i}`))
  }
  const state = reduce(events)
  assert.equal(Object.keys(state.streaming).length, MAX_OVERLAY_STREAM_ENTRIES)
  // 保留的必须是最近的那批,提词窗只看当下。
  assert.ok(state.streaming[`r${MAX_OVERLAY_STREAM_ENTRIES + 9}`])
  assert.equal(state.streaming.r0, undefined)
})

test('malformed payloads are ignored rather than throwing', () => {
  const state = reduce([
    { kind: 'serverMessage', type: 'answer_stream', session_id: 's1' },
    { kind: 'serverMessage', type: 'transcript_partial', session_id: 's1' },
    { kind: 'serverMessage', type: 'answer', session_id: 's1' },
    { kind: 'serverMessage', type: '未知类型' },
    { kind: 'sessionState', status: '不是合法状态', radioMode: 'pc' }
  ])
  assert.deepEqual(state, initialOverlayFeed)
})

// ---------- 页面接线 ----------

test('overlay page renders one display version per card (pickDisplayVersion)', () => {
  // catch-up swap 渲染侧:一张卡同一时刻只展示一段——未被取代里答案最长的。
  // 标题已是累计问题,段内不再重复「问题：」一行,版与版之间也没有 hr。
  assert.match(overlayPageSource, /pickDisplayVersion/)
  assert.ok(!/>\s*问题：/.test(overlayPageSource), '段内不再重复问题行')
  assert.ok(!overlayPageSource.includes('index > 0 && <hr'), '单版本渲染没有 hr')
  assert.match(overlayPageSource, /buildAnswerFeed/)
})

test('overlay page keeps a regenerate button on every question card', () => {
  // 常驻重新生成:不管答案出来没有、是否还在生成,都能点——生成卡住时这是
  // 唯一的自救手段。走线程 id,答案流回同一张卡。
  assert.match(overlayPageSource, /handleRegenerateThread/)
  assert.match(overlayPageSource, /api\.live\.regenerate\(question, false, key\)/)
})

test('the regenerate button shares the title row instead of occupying its own line', () => {
  // 大宽度悬浮窗下,重新生成按钮单独占卡底一行很浪费。标题行 space-between:
  // 标题 flex-1 可换行,按钮 shrink-0 靠右,gap-2 是默认间距。
  assert.match(overlayPageSource, /items-start justify-between gap-2/)
  assert.match(overlayPageSource, /min-w-0 flex-1/)
  assert.ok(
    !overlayPageSource.includes('mt-2 flex justify-end'),
    '按钮不再挤在卡底单独一行'
  )
})

test('the bottom bar is a quick-question composer, not a hotkey legend', () => {
  // 用户拍板:底部快捷键说明换成 textarea + 右下角图标按钮,想问什么直接敲。
  // 走不带 thread_id 的手动提问路径,答案独立成卡。Enter 发送、Shift+Enter
  // 换行(用户习惯),未录制时按钮禁用。placeholder 只留「向 AI 提问…」,
  // 不再背着键位说明(七轮:键位挪到输入框下方微字,见 footer 分层测试)。
  assert.ok(
    !overlayPageSource.includes('placeholder="快速提问'),
    'placeholder 不得再背键位说明'
  )
  assert.match(overlayPageSource, /<textarea/)
  assert.match(overlayPageSource, /aria-label="向 AI 快速提问"/)
  // Enter(无 Shift)提交表单;Shift+Enter 走默认行为换行。
  assert.match(
    overlayPageSource,
    /event\.key === 'Enter' && !event\.shiftKey/
  )
  assert.match(overlayPageSource, /type="submit"/)
  assert.match(overlayPageSource, /aria-label="发送问题"/)
  assert.match(overlayPageSource, /api\.live\.regenerate\(text, false\)/)
  assert.match(
    overlayPageSource,
    /disabled=\{sending \|\| !question\.trim\(\) \|\| phase !== 'ready' \|\| !recording\}/
  )
  // 三轮拍板"聚焦不改边框颜色"针对的是彩色描边(黑白灰配色下唯一的异色);
  // 七轮拍板改为灰阶提亮:white/35 边框 + white/[0.10] 背景。仍禁彩色 focus 边框。
  assert.ok(
    !/focus:border-(?!white\b)/.test(overlayPageSource),
    '聚焦边框只能灰阶提亮(white/*),不得彩色'
  )
})

test('the overlay palette is strictly black/white/gray', () => {
  // 用户拍板:悬浮窗只要黑白灰。彩色 token(brand/good/warn/bad)和带蓝调的
  // 旧底色 #0B0F1A 都不许回来——状态一律用亮度区分。
  for (const cls of ['brand', 'good', 'warn', 'bad', 'ink-']) {
    assert.ok(
      !new RegExp(`(?:text|bg|border|accent|hover:bg|hover:text|placeholder:text)-(?:${cls})`).test(
        overlayPageSource
      ),
      `悬浮窗不能再用彩色 token: ${cls}`
    )
  }
  assert.ok(!overlayPageSource.includes('0B0F1A'), '旧蓝黑底色应已换成中性黑')
})

test('collapsing scrolls the restored feed back to the tail', () => {
  // 收起/展开会卸载重挂滚动容器,重挂后 scrollTop 归零——依赖里必须有
  // overlay.collapsed,否则展开后聊天记录显示在顶部。feed.pending 也要
  // 贴底:刚发出的"正在思考"卡得出现在视野里。
  assert.match(
    overlayPageSource,
    /\[model\.threads, feed\.partial, feed\.pending, overlay\.collapsed\]/
  )
})

test('overlay page subscribes to engine events itself', () => {
  // 第二个 webview 有独立 JS 上下文,AppLayout 的订阅和 Zustand store 都拿不到。
  assert.match(overlayPageSource, /api\.events\s*\n?\s*\.on\(/)
  assert.doesNotMatch(overlayPageSource, /useLiveStore/)
})

test('overlay page toggles the transparent body class on mount and unmount', () => {
  assert.match(overlayPageSource, /classList\.add\('overlay-root'\)/)
  assert.match(overlayPageSource, /classList\.remove\('overlay-root'\)/)
  assert.match(globalCss, /body\.overlay-root/)
})

test('overlay page has a drag region because the window is frameless', () => {
  assert.match(overlayPageSource, /data-tauri-drag-region/)
  assert.match(overlayRust, /decorations\(false\)/)
})

test('overlay route sits outside the sidebar layout', () => {
  assert.match(appSource, /path="\/overlay" element=\{<OverlayPage \/>\}/)
  // 必须在 AppLayout 的 Route 之外;套进去就会连侧边栏一起渲染。
  const overlayAt = appSource.indexOf('path="/overlay"')
  const layoutAt = appSource.indexOf('element={<AppLayout />}')
  assert.ok(overlayAt > 0 && layoutAt > 0 && overlayAt < layoutAt)
  assert.match(appSource, /<Outlet \/>/)
})

test('rust-emitted toasts reach the same toast outlet as page toasts', () => {
  // 全局热键失败只在 Rust 侧发生,没有可 reject 的 invoke;不桥接就是静默失败。
  assert.match(appSource, /api\.events\s*\n?\s*\.onToast\(/)
  assert.match(appSource, /new CustomEvent\('app-toast', \{ detail: payload \}\)/)
  assert.match(libRust, /"app-toast"/)
})

test('hotkey table matches the rust registration one for one', () => {
  // Rust 用 `Equal`/`Minus` 这种键名，界面上写 `=`/`-`，所以逐个比较要先归一化。
  // 断言的是"两边一一对应"而不是"共有 N 个"：加热键时不该被一个魔法数字挡住，
  // 但漏掉任何一侧必须炸。
  const RUST_TO_LABEL = { Equal: '=', Minus: '-' }
  const specs = [...libRust.matchAll(/"Control\+Alt\+([A-Za-z]+)"/g)].map(
    (m) => `Ctrl+Alt+${RUST_TO_LABEL[m[1]] ?? m[1]}`
  )
  assert.ok(specs.length > 0, 'Rust 侧一个热键都没注册，正则大概过时了')
  const labels = [...overlayControlSource.matchAll(/keys: '([^']+)'/g)].map((m) => m[1])
  assert.deepEqual(
    labels,
    specs,
    '界面热键表必须和 Rust 注册顺序完全一致：热键按下标派发动作，顺序错位会执行错的动作'
  )
})

test('overlay panel states the stealth boundary honestly', () => {
  // 只挡 OS 采集 API。把这条藏起来会让用户以为物理拍摄也挡得住。
  assert.match(overlayPanelSource, /挡不住/)
  assert.match(overlayPanelSource, /采集卡/)
})

test('every overlay command is async so window creation cannot deadlock the main thread', () => {
  // Windows 上同步 `#[tauri::command] fn` 跑在主线程。任何走到
  // `WebviewWindowBuilder::build()` 的同步命令都会和 WebView2 的消息泵互锁，
  // 表现是该 invoke 永不返回、之后所有 IPC 一起超时、整个应用卡死。
  // 这个 bug 真实发生过，而且 overlay.rs 的纯函数单元测试全绿也测不出来。
  const commands = [...libRust.matchAll(/#\[tauri::command\]\s*\n\s*(async\s+)?fn\s+(\w+)/g)]
  const overlayCommands = commands.filter(([, , name]) => name.startsWith('overlay_'))
  assert.ok(overlayCommands.length >= 9, `overlay 命令太少（${overlayCommands.length}），正则大概过时了`)
  const sync = overlayCommands.filter(([, isAsync]) => !isAsync).map(([, , name]) => name)
  assert.deepEqual(sync, [], `这些 overlay 命令是同步的，会和 WebView2 消息泵死锁：${sync.join(', ')}`)
})

test('the hotkey handler spawns instead of building a window on the event loop', () => {
  // 热键 handler 跑在窗口事件循环上，和同步命令是同一个死锁面。
  // handler 只能识别动作，执行必须丢给 async runtime。
  assert.match(libRust, /tauri::async_runtime::spawn/)
  const handler = libRust.slice(libRust.indexOf('fn register_overlay_shortcuts'))
  const runCall = handler.indexOf('run_overlay_shortcut')
  const spawnCall = handler.indexOf('async_runtime::spawn')
  assert.ok(spawnCall > -1 && spawnCall < runCall, 'run_overlay_shortcut 必须在 spawn 内部调用')
})

test('overlay layout persists geometry and switches but never visibility', () => {
  // 启动就自动弹出悬浮窗会盖住用户正在用的窗口，显隐一律由用户触发。
  const layout = overlayRust.slice(
    overlayRust.indexOf('pub struct OverlayLayout'),
    overlayRust.indexOf('impl Default for OverlayLayout')
  )
  for (const field of ['passthrough', 'content_protected', 'always_on_top', 'opacity', 'x', 'y', 'width', 'height']) {
    assert.match(layout, new RegExp(`pub ${field}:`), `落盘布局缺少 ${field}`)
  }
  assert.ok(!/pub visible:/.test(layout), 'visible 不能落盘')
  assert.match(overlayRust, /fn to_state\(self\) -> OverlayState \{\s*OverlayState \{\s*visible: false/)
})

test('a hand-edited or corrupt layout file cannot break the overlay', () => {
  // 这个文件是明文 JSON，用户能改坏，也可能是旧版本写的。
  assert.match(overlayRust, /unwrap_or_default\(\)/)
  assert.match(overlayRust, /opacity: clamp_opacity\(self\.opacity\)/)
  // 几何做有限性和最小尺寸校验，宁可回默认也不要建出 0 宽或 NaN 坐标的窗口。
  assert.match(overlayRust, /is_finite\(\)/)
  assert.match(overlayRust, /w < MIN_WIDTH \|\| h < MIN_HEIGHT/)
})

test('collapse works from anywhere and the strip docks to the top center', () => {
  // 贴边功能已删:收起不再有任何前置条件,细条固定落在当前显示器工作区
  // 顶部居中(而不是吸在窗口原来的边上)。
  assert.ok(!/fn dock\(/.test(overlayRust), '贴边 dock() 应已删除')
  assert.ok(!overlayRust.includes('窗口未贴边'), '收起不该有贴边前置条件')
  assert.match(overlayRust, /pub fn strip_geometry\(work_x: f64, work_y: f64, work_width: f64\)/)
  assert.match(overlayRust, /work_x \+ \(work_width - width\) \/ 2\.0/)
  assert.match(overlayRust, /STRIP_THICKNESS/)
  // 自动收起已删:状态和布局里都不该再有这个字段。
  assert.ok(!overlayRust.includes('auto_hide'), 'autoHide 应已删除')
  assert.ok(!libRust.includes('auto_hide'), 'lib.rs 里的 autoHide 应已删除')
})

test('strip geometry is rejected as recorded layout so expand restores the real window size', () => {
  // 真实 bug:收起的 set_size(细条尺寸)会在 state.set(collapsed=true) 之前
  // 触发 Resized 钩子,细条几何被记进 layout;展开时 geometry() 拒绝
  // (240 < MIN_WIDTH)而回默认尺寸——「调好的宽高一收一展就丢」。记录端
  // 必须拒收细条尺寸的几何。
  assert.match(overlayRust, /pub fn is_full_window_geometry\(width: f64, height: f64\) -> bool/)
  assert.match(libRust, /overlay::is_full_window_geometry\(width, height\)/)
})

test('the collapsed strip shows only a status dot and an expand arrow', () => {
  // 用户拍板:收起条只要圆点 + 箭头,不写字。18px 高的细条放不下也不需要
  // 阅读性内容,它的唯一职责是「还在这儿,点我展开」。
  assert.ok(!overlayPageSource.includes('AI ${answerCount}'), '细条上不再写字')
  assert.match(overlayPageSource, /title="展开悬浮窗（Ctrl\+Alt\+E）"/)
})

test('holding ctrl grants temporary mouse interaction while passthrough is on', () => {
  // 穿透开着时用户一点操作空间都没有;按住 Ctrl 临时可交互、松手恢复穿透。
  // Rust 轮询 GetAsyncKeyState,翻转 set_ignore_cursor_events 并广播状态。
  assert.match(libRust, /fn ctrl_key_pressed\(\) -> bool/)
  assert.match(libRust, /GetAsyncKeyState/)
  assert.match(libRust, /fn sync_ctrl_watch/)
  assert.match(libRust, /set_ignore_cursor_events\(!pressed\)/)
  assert.match(cargoToml, /Win32_UI_Input_KeyboardAndMouse/)
})

test('the overlay window defaults to 560 wide for less scrolling', () => {
  // 420 太窄,答案没几行就要滚动(用户实测反馈)。默认 560,已调过宽高的
  // 用户不受影响(overlay-layout.json 落盘值优先于默认值)。
  assert.match(overlayRust, /DEFAULT_WIDTH: f64 = 560\.0/)
})

test('layout is written on exit because dragging only touches memory', () => {
  // 一次拖动连发几十个 Moved，每条都落盘纯属浪费；只拖过窗口就退出的
  // 那条路径必须靠退出钩子兜住，否则位置白调。
  assert.match(libRust, /RunEvent::ExitRequested \{ \.\. \} \| tauri::RunEvent::Exit/)
  assert.match(libRust, /handle\.persist\(\)/)
})

test('opacity dims only the backdrop, never the text', () => {
  // 整卡套 style={{opacity}} 会把文字一起调淡(CSS 父级 opacity 连带所有子元素,
  // 子元素无法比父级更亮),低档位时提词内容直接看不见——真实用户反馈过。
  // 背景必须走独立的 rgba 颜色层,文字保持全亮。
  assert.ok(
    !overlayPageSource.includes('style={{ opacity: overlay.opacity }}'),
    '不能把不透明度套在整卡根节点上'
  )
  assert.match(overlayPageSource, /backgroundColor: `rgba\(17, 17, 17, \$\{overlay\.opacity\}\)`/)
  // 文字对比兜底:窗口后面是用户桌面,filter: invert() 之类的滤镜管不到页面
  // 之外的堆叠,只能走多向描边——四方向 1px 实描边 + 一圈晕光,等效给每个
  // 字符画轮廓,黑底白字、白底浅字都兜得住。
  assert.match(globalCss, /-1px -1px 0 rgba\(0, 0, 0, 0\.85\)/)
  assert.match(globalCss, /1px 1px 0 rgba\(0, 0, 0, 0\.85\)/)
  assert.match(globalCss, /0 0 4px rgba\(0, 0, 0, 0\.7\)/)
})

test('overlay card and strip carry no box-shadow: it paints gray corners on a transparent window', () => {
  // box-shadow 画在元素矩形之外;悬浮窗是透明窗口,卡片圆角外的四个"窗口
  // 直角"会被阴影填成洗不掉的灰色蒙层(用户实测反馈)。整页禁投影。
  assert.ok(
    !/shadow-\[\d/.test(overlayPageSource),
    'OverlayPage 里不能有任意 shadow-[...] 工具类'
  )
})

test('overlay offers screenshot solving and reports it in place', () => {
  // 悬浮窗是独立 webview，`app-toast` 那条链路挂在主窗口的 AppLayout 上，
  // 从这里派发过去没人接，所以反馈必须就地渲染。
  assert.match(overlayPageSource, /api\.live\.solveScreenshot\(\)/)
  assert.match(overlayPageSource, /solveHint/)
  assert.ok(
    !/dispatchEvent\(\s*new CustomEvent\('app-toast'/.test(overlayPageSource),
    '悬浮窗不能派发 app-toast：那个监听器在主窗口的 AppLayout 里'
  )
})

test('screenshot solving is gated on an active recording session', () => {
  // 没在录制时后端没有会话可挂答案，按钮必须先禁用而不是发出去再报错。
  assert.match(overlayPageSource, /disabled=\{solving \|\| !recording \|\| phase !== 'ready'\}/)
})

// ---------- 状态如实显示（录制中误报修复） ----------

test('captureState tracks the local capture gate separately from session status', () => {
  // 会话 recording 只代表面试开始了,不代表本机在采(切收音模式要求会话
  // 保持 recording)。采集门由 Rust SetCaptureActive 翻转时推送。
  const state = reduce([
    { kind: 'connection', phase: 'ready' },
    { kind: 'sessionState', status: 'recording', radioMode: 'pc' },
    { kind: 'captureState', active: true }
  ])
  assert.equal(state.captureOn, true)
  assert.equal(state.radioMode, 'pc')
  const closed = applyOverlayEvent(state, {
    kind: 'connection',
    phase: 'closed',
    note: '连接已断开'
  })
  assert.equal(closed.sessionStatus, '')
  assert.equal(closed.captureOn, false, '断开后采集门不能残留')
})

test('an invalid sessionState does not clobber the radio mode', () => {
  // 无效状态整个事件被忽略(既有行为),radioMode 不能被顺带覆盖。
  const state = reduce([
    { kind: 'sessionState', status: 'recording', radioMode: 'mobile' },
    { kind: 'sessionState', status: 'bogus', radioMode: 'pc' }
  ])
  assert.equal(state.radioMode, 'mobile')
})

test('the recording badge distinguishes capturing from a recording session', () => {
  // 用户拍板:必须一眼分清"真的在录"与"没在录"。徽标按 会话状态 × 本机
  // 采集门 × 收音模式 三态显示;呼吸点只在真的在采时跳。
  assert.match(overlayPageSource, /const capturing = recording && feed\.captureOn/)
  assert.match(overlayPageSource, /'录制中 · 采集已停'/)
  assert.match(overlayPageSource, /'录制中 · 手机'/)
  assert.ok(
    !overlayPageSource.includes("recording ? 'animate-pulse-dot'"),
    '呼吸点不能只看会话状态'
  )
})

test('the overlay header identifies which session it is following', () => {
  // 悬浮窗必须知道当前跟的是哪场面试(后续手机扫码按会话接入)。
  // 短 id 常驻,完整 id 在 tooltip,点击复制。
  assert.match(overlayPageSource, /function SessionChip/)
  assert.match(overlayPageSource, /sessionId\.slice\(0, 8\)/)
  assert.match(overlayPageSource, /navigator\.clipboard/)
  assert.match(
    overlayPageSource,
    /feed\.sessionId \? <SessionChip sessionId=\{feed\.sessionId\} \/> : null/
  )
})

// ---------- Ctrl+Alt+Z 开启/暂停录制（用户两次纠正后的最终语义） ----------

test('Ctrl+Alt+Z toggles recording without ever ending the session', () => {
  // 用户拍板（2026-09-02 七轮）：这个热键是 开启录制 / 暂停录制，绝不结束
  // 面试。暂停 = 停系统声音 → 关上传门禁（掐断 说话→ASR→LLM 链路，省屏
  // 幕和费用），会话保持 recording，可再按恢复。Rust 只广播事件，三个方向
  // 的动作都在悬浮窗 webview 里执行并就地反馈。
  assert.match(libRust, /"Control\+Alt\+Z"/)
  assert.match(libRust, /if index == 7 \{/)
  assert.match(libRust, /"overlay:toggle-recording"/)
  assert.match(overlayPageSource, /onToggleRecordingHotkey/)

  // 红线：悬浮窗页面不允许出现任何结束会话的调用（结束面试只属于主窗口），
  // 也不允许残留旧的 stopRecording/startRecording 入口。
  assert.ok(
    !overlayPageSource.includes('api.live.endSession'),
    '悬浮窗不得结束会话——Ctrl+Alt+Z 是暂停/开启，不是结束面试'
  )
  assert.ok(!overlayPageSource.includes('const stopRecording'))
  assert.ok(!overlayPageSource.includes('const startRecording'))

  const toggleAt = overlayPageSource.indexOf('const toggleRecording = async')
  const slice = overlayPageSource.slice(
    toggleAt,
    overlayPageSource.indexOf('toggleRecordingRef.current = toggleRecording')
  )
  assert.ok(toggleAt > -1 && slice.length > 0, 'toggleRecording 不见了')

  // 暂停方向：停系统声音（不带 .catch 的主路径）→ 关上传门禁。
  assert.match(
    slice,
    /if \(feed\.captureOn\) \{[\s\S]{0,60}await api\.audio\.stopSystem\(\)(?!\.catch)[\s\S]{0,40}await api\.outbox\.setCaptureActive\(false\)/,
    '暂停必须先停系统声音再关上传门禁'
  )
  // 恢复方向：起系统声音 → 开上传门禁（录制中但采集已停时同键恢复）。
  assert.ok(
    slice.indexOf('await api.audio.startSystem()') > -1 &&
      slice.indexOf('await api.audio.startSystem()') <
        slice.indexOf('await api.outbox.setCaptureActive(true)'),
    '恢复必须先起系统采集再开上传门禁'
  )
  // 开启方向（idle）：开始会话 → 系统声音采集 → 上传门禁，顺序固定
  // （采集起不来就不开门，门开了没声音等于白录）。
  const startAt = slice.indexOf('await api.live.startSession(feed.radioMode || \'pc\')')
  assert.ok(startAt > -1, '开启分支必须走 startSession')
  const sysAt = slice.indexOf('await api.audio.startSystem()', startAt)
  const gateAt = slice.indexOf('await api.outbox.setCaptureActive(true)', sysAt)
  assert.ok(
    startAt < sysAt && sysAt < gateAt,
    '开启必须先起会话和系统采集再开上传门禁'
  )
  // 就地反馈与手机模式提示。
  assert.ok(slice.includes('已暂停录制'))
  assert.ok(slice.includes('已继续录制'))
  assert.ok(slice.includes('已开始录制'))
  assert.ok(slice.includes('手机收音模式，请在手机端暂停'))
})

test('manual question and screenshot gates read session status only, with distinct messages', () => {
  // 用户投诉："textarea 第一次能问，继续输入就提示未在录制中"。根因是
  // 旧的 Z 热键把会话结束了，门禁如实拦截。修复后门禁只看两个状态源：
  // 连接 phase 与会话 sessionStatus；采集开关 captureOn 不进门禁——暂停
  // 只停音频链路，手动提问/截图不走音频，暂停期间照常可问。拦因细分到
  // 每种状态各一句人话（sessionBlockHint），不再一句"未在录制中"盖住
  // 已结束 / 没开始录 / 状态同步中三种完全不同的情况。
  assert.match(
    overlayPageSource,
    /const recording = feed\.sessionStatus === 'recording'/
  )
  const askGate = overlayPageSource.slice(
    overlayPageSource.indexOf('const submitQuestion = async'),
    overlayPageSource.indexOf('setSending(true)')
  )
  assert.ok(askGate.includes('连接未就绪，稍候再试'), '提问门禁要先报连接')
  assert.ok(askGate.includes("sessionBlockHint('提问')"), '会话侧拦因细分通报')
  assert.ok(
    !askGate.includes('captureOn'),
    '提问门禁不得看采集开关——暂停期间照常可问'
  )
  const solveGate = overlayPageSource.slice(
    overlayPageSource.indexOf('const solveScreenshot = async'),
    overlayPageSource.indexOf('setSolving(true)')
  )
  assert.ok(solveGate.includes('连接未就绪，稍候再试'))
  assert.ok(solveGate.includes("sessionBlockHint('解题')"))
  assert.ok(
    !solveGate.includes('captureOn'),
    '截图门禁同样不得看采集开关'
  )
  const hintFn = overlayPageSource.slice(
    overlayPageSource.indexOf('const sessionBlockHint'),
    overlayPageSource.indexOf('const solveScreenshot')
  )
  assert.ok(hintFn.includes('这场面试已结束'))
  assert.ok(hintFn.includes('还没开始录制'))
  assert.ok(hintFn.includes('会话状态同步中'))
})

test('question footer is layered by brightness tiers, not another answer card', () => {
  // 用户拍板的四层方案（2026-09-02 七轮，全黑白灰）：footer 整块抬一档
  // 操作面 white/[0.04]；输入容器再亮一档（white/[0.08] + 边框 white/20 +
  // rounded-lg）；答案卡最暗（white/[0.05]）——三档亮度分清内容层/操作层。
  // 聚焦反馈走灰阶提亮（边框 white/35 + 背景 white/[0.10]），不画彩色。
  // 快捷键说明从 placeholder 挪到输入框下方微字。
  assert.match(
    overlayPageSource,
    /<footer className="shrink-0 border-t border-white\/10 bg-white\/\[0\.04\]/
  )
  assert.match(
    overlayPageSource,
    /rounded-lg border border-white\/20 bg-white\/\[0\.08\][^"]*focus:border-white\/35 focus:bg-white\/\[0\.10\]/
  )
  assert.ok(overlayPageSource.includes('placeholder="向 AI 提问…"'))
  assert.ok(overlayPageSource.includes('Enter 发送 · Shift+Enter 换行'))
  assert.ok(
    !overlayPageSource.includes('快速提问，Enter 发送'),
    'placeholder 不再背快捷键说明'
  )
})

test('every path that can restore passthrough syncs the ctrl watcher', () => {
  // 真实 bug:主窗口面板第一次打开悬浮窗走 overlay_toggle,它原来没有
  // sync_ctrl_watch——toggle 的 show 分支按记忆还原穿透,轮询线程没被拉起,
  // Ctrl 临时交互整个不生效,直到 Ctrl+Alt+E 热键路径碰巧调过一次 sync。
  // 用户实测的"收起再展开才可用"就是它。collapse/expand 同理保活。
  for (const fn of ['fn overlay_toggle', 'fn overlay_collapse', 'fn overlay_expand']) {
    const at = libRust.indexOf(fn)
    assert.ok(at > -1, `${fn} 不见了`)
    const next = libRust.indexOf('#[tauri::command]', at)
    const body = libRust.slice(at, next > at ? next : undefined)
    assert.match(body, /sync_ctrl_watch/, `${fn} 之后必须同步 Ctrl 轮询`)
  }
})

// ---------- 提问框自动增高与焦点样式 ----------

test('the quick-question textarea grows with content instead of a fixed height', () => {
  // 用户拍板:换行要撑高,不要写死高度;封顶约 6 行(max-h-28)后内部滚动,
  // 发送清空后同一个 effect 把高度收回一行。
  assert.match(overlayPageSource, /el\.style\.height = 'auto'/)
  assert.match(overlayPageSource, /el\.scrollHeight/)
  assert.match(overlayPageSource, /rows=\{1\}/)
  assert.match(overlayPageSource, /max-h-28/)
})

test('focused overlay form controls draw no bright outline', () => {
  // 全局 :focus-visible 亮蓝描边写在 utilities 之后,元素上的 outline-none 压不过
  // 它;悬浮窗是黑白灰配色,那道蓝框是唯一会冒出来的彩色。焦点用光标表达。
  assert.match(globalCss, /body\.overlay-root textarea:focus-visible/)
  assert.match(globalCss, /body\.overlay-root input:focus-visible/)
})

// ---------- 晚挂载播种 / pending 提问反馈(2026-09-01 五轮) ----------

test('mount-time seed restores state the overlay missed before it existed', () => {
  // 悬浮窗懒创建,打开前的事件(连接/开始录制/采集门)永远收不到——这正是
  // "明明在录制却提示未在录制中"的根因。挂载时拉 Rust 快照播种。
  const state = applyOverlayEvent(initialOverlayFeed, {
    kind: 'overlay:seed',
    snapshot: {
      phase: 'ready',
      sessionId: 'sx',
      sessionStatus: 'recording',
      radioMode: 'pc',
      captureActive: true
    }
  })
  assert.equal(state.phase, 'ready')
  assert.equal(state.sessionId, 'sx')
  assert.equal(state.sessionStatus, 'recording')
  assert.equal(state.radioMode, 'pc')
  assert.equal(state.captureOn, true)
})

test('seed falls back on unknown values instead of poisoning the state', () => {
  const state = applyOverlayEvent(initialOverlayFeed, {
    kind: 'overlay:seed',
    snapshot: {
      phase: 'bogus',
      sessionId: null,
      sessionStatus: 'weird',
      radioMode: 'nope',
      captureActive: false
    }
  })
  assert.equal(state.phase, 'idle')
  assert.equal(state.sessionStatus, '')
  assert.equal(state.radioMode, '')
})

test('a sent question shows up as a pending card until the stream answers', () => {
  // 用户痛点:发出去没有任何反馈,不知道发没发出去,只能反复发。发送成功
  // 即挂"正在思考"卡;answer_stream 首帧(started 空帧,后端在生成一开始就
  // 发)同文本即摘除,无缝交棒给真卡;失败帧也带 question,同样能收尾。
  const asked = applyOverlayEvent(initialOverlayFeed, {
    kind: 'overlay:asked',
    question: '讲讲 Redis 持久化'
  })
  assert.equal(asked.pending.length, 1)
  assert.equal(asked.pending[0].question, '讲讲 Redis 持久化')

  const started = applyOverlayEvent(
    asked,
    streamEvent('s1', 'r1', '', { question: '讲讲 Redis 持久化', started: true, answer: '' })
  )
  assert.equal(started.pending.length, 0, '流帧到达即摘除 pending')
  assert.ok(started.streaming.r1, '真卡已接管')

  const failed = applyOverlayEvent(
    applyOverlayEvent(initialOverlayFeed, { kind: 'overlay:asked', question: 'Q' }),
    streamEvent('s1', 'r2', '', { question: 'Q', failed: true, done: true, answer: '' })
  )
  assert.equal(failed.pending.length, 0, '失败帧也要收掉 pending')
})

test('a disconnect clears pending questions instead of leaving spinners forever', () => {
  const asked = applyOverlayEvent(initialOverlayFeed, { kind: 'overlay:asked', question: 'Q' })
  const closed = applyOverlayEvent(asked, { kind: 'connection', phase: 'closed' })
  assert.equal(closed.pending.length, 0)
})

test('overlay page seeds runtime state from rust on mount', () => {
  assert.match(overlayPageSource, /runtimeState\(\)/)
  assert.match(overlayPageSource, /kind: 'overlay:seed'/)
})

test('overlay submit dispatches the pending card immediately on success', () => {
  assert.match(overlayPageSource, /kind: 'overlay:asked'/)
  assert.match(overlayPageSource, /正在思考/)
})

test('single-line textarea never shows a scrollbar (fractional-DPI guard)', () => {
  // Windows 125%/150% 缩放下整数 scrollHeight 比小数布局高度少零点几像素,
  // overflow-y-auto 单行也画滚动条。封顶(112px = max-h-28)前藏掉溢出,
  // 到顶后才恢复滚动。
  assert.match(overlayPageSource, /grown >= 112 \? 'auto' : 'hidden'/)
})

test('overlay scrollbar color follows the opacity slider', () => {
  // 全局滚动条是深蓝写死的:半透明悬浮窗上突兀且不随透明度变。thumb 颜色
  // 由悬浮窗根容器按当前 opacity 算好,经 CSS 变量向下级联。
  assert.match(overlayPageSource, /'--overlay-thumb'/)
  assert.match(overlayPageSource, /'--overlay-thumb-hover'/)
  assert.match(globalCss, /body\.overlay-root ::-webkit-scrollbar-thumb/)
  assert.match(globalCss, /var\(--overlay-thumb,/)
})

test('markdown code blocks turn grayscale inside the overlay', () => {
  // 主窗口的实色浅底(bg-surface) + 彩色高亮(绿/琥珀/蓝)在黑白灰悬浮窗里
  // 不对。Markdown 挂稳定钩子类,global.css 只在 overlay-root 里覆盖,
  // 主窗口不受影响。
  assert.match(markdownSource, /'inline-code rounded/)
  assert.match(markdownSource, /code-block group relative/)
  assert.match(markdownSource, /code-lang/)
  assert.match(markdownSource, /code-copy/)
  assert.match(globalCss, /body\.overlay-root \.code-block \{/)
  assert.match(globalCss, /body\.overlay-root \.code-block \.text-good/)
})

test('rust keeps a queryable runtime snapshot updated at the emit funnel', () => {
  // 快照在 emit_engine_event 唯一汇总点更新,晚挂载的悬浮窗经 live_runtime_state
  // 播种;引擎断开时清掉会话字段,不替旧会话报"录制中"。
  assert.match(libRust, /fn live_runtime_state/)
  assert.match(libRust, /live_runtime_state,/)
  assert.match(libRust, /state\.observe\(&event\)/)
  assert.match(libRust, /snap\.capture_active = \*active/)
  assert.ok(libRust.includes('snap.session_id = None;'), 'closed 必须清会话字段')
})

test('stale engine events are dropped by a generation gate', () => {
  // 真实 bug（2026-09-03 九轮）：重开面试时 live_connect 先 stop 旧引擎再
  // spawn 新引擎，旧 ws_client 收尾补发的 Connection closed 可能晚于新引擎
  // 的 ready 到达——快照和两个窗口一起被打回"断开"，而新引擎此后一切正
  // 常、不再发恢复事件，提问/截图从此全部静默（用户实测"发信息给 LLM 完
  // 全没有回应"）。修法：替换引擎前先翻代际，旧引擎闭包持有旧代际，它的
  // 一切事件在唯一出口 emit_engine_event 里整条作废（observe 与广播一并
  // 跳过）。
  assert.match(libRust, /gen: AtomicU64/)
  assert.match(libRust, /gen: AtomicU64::new\(0\)/)
  assert.match(libRust, /fn emit_engine_event\(app: &AppHandle, gen: u64\)/)
  const gate = libRust.indexOf('state.gen.load(Ordering::SeqCst) != gen')
  const observe = libRust.indexOf('state.observe(&event)')
  assert.ok(gate > -1 && gate < observe, '代际检查必须先于 observe 落地')

  const connectAt = libRust.indexOf('fn live_connect')
  const connectNext = libRust.indexOf('#[tauri::command]', connectAt)
  const connect = libRust.slice(connectAt, connectNext)
  const bump = connect.indexOf('gen.fetch_add')
  const take = connect.indexOf('guard.take()')
  assert.ok(
    bump > -1 && take > -1 && bump < take,
    'live_connect 必须先翻代际再取走旧引擎——顺序反了旧闭包就可能拿到新代际'
  )
  assert.match(connect, /emit_engine_event\(&app, engine_gen\)/)

  // live_disconnect 不翻代际：正常离开实时页的 closed 是合法广播，快照与
  // 两个窗口都该收到（这里断开就是用户想要的语义）。
  const disconnectAt = libRust.indexOf('fn live_disconnect')
  const disconnectNext = libRust.indexOf('#[tauri::command]', disconnectAt)
  const disconnect = libRust.slice(disconnectAt, disconnectNext)
  assert.ok(!disconnect.includes('fetch_add'), 'live_disconnect 不该翻代际')

  // Enter 直调 submitQuestion 而非 requestSubmit：发送按钮 disabled 时
  // requestSubmit 对被禁用的默认提交按钮是静默无操作，门禁提示永远亮不
  // 出来（"按了毫无反应"的观感来源之一）。断言匹配的是调用语法——注释
  // 里允许解释来龙去脉。
  assert.ok(
    !/\.requestSubmit\(/.test(overlayPageSource),
    'Enter 必须直调 submitQuestion，不得绕回表单提交'
  )
  assert.match(overlayPageSource, /void submitQuestion\(\)/)
})

test('ctrl+wheel scrolls the overlay instead of being eaten as zoom', () => {
  // 穿透 + Ctrl 临时交互下,滚轮事件带 ctrlKey,WebView2 把它当页面缩放手势
  // 吃掉,滚动容器收不到普通滚动——用户只能手拖滚动条。修法:document 上
  // 捕获 + 非 passive 拦截,preventDefault 阻止缩放,增量应用到光标下最近
  // 的可滚动容器。
  assert.match(overlayPageSource, /if \(!event\.ctrlKey\) return/)
  assert.match(overlayPageSource, /event\.preventDefault\(\)/)
  assert.match(
    overlayPageSource,
    /addEventListener\('wheel', onWheel, \{ passive: false, capture: true \}\)/
  )
  assert.match(overlayPageSource, /node\.scrollTop \+= amount/)
})

test('jumping to tail suppresses the latest-button flash during smooth scroll', () => {
  // 真实竞态:点「最新」→ setAtTail(true) 按钮隐藏 → smooth 动画中间帧
  // "不在底部" → setAtTail(false) 按钮闪出 → 到尾再隐藏。修法:动画期间
  // 抑制 handleScroll 的判定,落底解除;600ms 安全阀兜住没走到尾的极端情况。
  assert.match(overlayPageSource, /jumpingToTailRef\.current = true/)
  assert.match(
    overlayPageSource,
    /if \(jumpingToTailRef\.current\) \{[\s\S]{0,400}setAtTail\(true\)/
  )
  assert.match(overlayPageSource, /window\.setTimeout\(\(\) => \{[\s\S]{0,160}jumpingToTailRef\.current = false/)
})

test('answer text is selectable while chrome stays unselectable', () => {
  // body 默认 user-select:none(chrome 不可选,拖动区不被选字干扰);答案正文
  // 放开:Markdown 根节点与问题行挂 select-text,选中高亮主窗口品牌蓝、
  // 悬浮窗黑白灰。
  assert.match(markdownSource, /select-text/)
  assert.match(overlayPageSource, /min-w-0 flex-1 select-text/)
  assert.match(globalCss, /::selection \{/)
  assert.match(globalCss, /background: rgba\(79, 124, 255, 0\.4\)/)
  assert.match(globalCss, /body\.overlay-root ::selection \{/)
  assert.match(globalCss, /background: rgba\(255, 255, 255, 0\.3\)/)
})
