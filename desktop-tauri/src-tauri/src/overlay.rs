//! 悬浮提词窗：无边框、透明、始终置顶、可鼠标穿透、对屏幕共享隐身。
//!
//! 为什么全部逻辑放 Rust 而不是前端调 `@tauri-apps/api/window`：
//! `capabilities/default.json` 只授予 `core:window:default`（一组 getter），
//! 没有任何 `allow-set-*`。走 Rust 命令就不必逐个枚举 setter 权限，
//! 攻击面也更小（前端只能调这里暴露的几个动作，不能任意改窗口）。

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tauri::{
    AppHandle, LogicalPosition, LogicalSize, Manager, PhysicalPosition, PhysicalSize, WebviewUrl,
    WebviewWindow, WebviewWindowBuilder,
};

/// 悬浮窗 label；`single_instance` 聚焦的 "main" 不受影响。
pub const OVERLAY_LABEL: &str = "overlay";

/// 逻辑像素下的默认尺寸。窄长条形状是刻意的：面试时它压在会议窗口边上，
/// 遮挡越少越好，答案区靠纵向滚动而不是横向铺开。
///
/// 2026-08-31 用户反馈 420 太窄：答案要滚很久才能看完，宽度调到 560，
/// 首屏能装下的答案行数更多，滚动次数显著减少。
const DEFAULT_WIDTH: f64 = 560.0;
const DEFAULT_HEIGHT: f64 = 560.0;
/// `lib.rs` 的 record_geometry 用最小尺寸拒收细条几何（收起时程序自己
/// set_size(240×18) 触发的 Resized 会在 collapsed 置位前重入回调），
/// 因此对 crate 内可见。
pub(crate) const MIN_WIDTH: f64 = 280.0;
pub(crate) const MIN_HEIGHT: f64 = 200.0;

/// 收起成细条后的厚度与长度（逻辑像素）。
///
/// 厚度取 18 而不是更细的 8-10：这条细条是唯一的展开入口，鼠标得能稳定命中，
/// 太细在高 DPI 下几乎点不到。长度 240 够放一行提示，又不至于挡住半屏。
const STRIP_THICKNESS: f64 = 18.0;
const STRIP_LENGTH: f64 = 240.0;

/// 透明度下限：再低就会看不见自己写的字，等于把窗口弄丢了。
const MIN_OPACITY: f64 = 0.25;

/// 热键步进的透明度粒度。与前端滑条的 step=5% 一致：鼠标拖出来的值和
/// 热键按出来的值落在同一套格点上，不会互相打乱。
const OPACITY_STEP: f64 = 0.05;

/// 前端读到的悬浮窗状态；每次动作后回传，避免前端自己猜。
///
/// 刻意保持 `Copy`：`OverlayStateHandle` 在 Mutex 里存的就是这个值，
/// 取用时按值拷出来立刻放锁，不让锁跨越窗口调用（窗口 API 会阻塞）。
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct OverlayState {
    pub visible: bool,
    /// 键盘焦点是否在悬浮窗上。窗口以 `focused(false)` 创建、内容更新不抢焦，
    /// 只有用户点它才变 true。运行时态，和 `visible` 一样不落盘：
    /// 重启后窗口都没建，恢复"上次有焦点"没有意义。
    pub focused: bool,
    /// 鼠标穿透：true 时点击穿到底下的会议窗口，悬浮窗完全不吃事件。
    pub passthrough: bool,
    /// 穿透开启期间用户正按住 Ctrl（临时交互模式）。运行时态，不落盘：
    /// 由 lib.rs 的按键轮询线程维护，重启后自然是 false。
    pub ctrl_interactive: bool,
    /// 对 OS 截屏/录屏/共享隐身（Windows 上是 WDA_EXCLUDEFROMCAPTURE）。
    pub content_protected: bool,
    pub always_on_top: bool,
    /// 0.25 - 1.0。CSS 侧生效，因此穿透关闭时仍可点击半透明区域。
    pub opacity: f64,
    /// 是否已收起成屏幕顶部居中的细条。从任意窗口位置都能收起。
    pub collapsed: bool,
}

impl Default for OverlayState {
    fn default() -> Self {
        Self {
            visible: false,
            focused: false,
            passthrough: false,
            ctrl_interactive: false,
            // 默认开隐身：面试场景下"忘记开"的代价远大于"忘记关"。
            content_protected: true,
            always_on_top: true,
            opacity: 0.92,
            collapsed: false,
        }
    }
}

// ---------- 跨重启记忆 ----------

/// 落盘的悬浮窗布局。面试前调好的位置/大小/透明度，重启后必须还在，
/// 否则每次开会都要重新摆一遍窗口。
///
/// 只记"用户摆过的样子"，不记 `visible`：启动就自动弹出悬浮窗会盖住别的窗口，
/// 显隐一律由用户主动触发（侧边栏按钮或 Ctrl+Alt+O）。
///
/// `collapsed` 同理不落盘：重启后先给完整窗口，否则用户只看到一条 18px 的细条，
/// 很难猜到那是自己的应用。历史版本写进这个文件的 `docked` / `autoHide`
/// 字段已随贴边/自动收起功能删除；serde 默认忽略未知字段，老文件仍能加载。
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct OverlayLayout {
    pub passthrough: bool,
    pub content_protected: bool,
    pub always_on_top: bool,
    pub opacity: f64,
    /// 逻辑像素的窗口位置/尺寸；从未摆过时为 None，走默认尺寸 + 系统定位。
    /// 收起期间不更新（那时的几何是细条，不是用户摆的样子）。
    pub x: Option<f64>,
    pub y: Option<f64>,
    pub width: Option<f64>,
    pub height: Option<f64>,
}

impl Default for OverlayLayout {
    fn default() -> Self {
        let state = OverlayState::default();
        Self {
            passthrough: state.passthrough,
            content_protected: state.content_protected,
            always_on_top: state.always_on_top,
            opacity: state.opacity,
            x: None,
            y: None,
            width: None,
            height: None,
        }
    }
}

impl OverlayLayout {
    /// 恢复成运行时状态。`visible` / `collapsed` / `focused` 永远从 false 起，
    /// 见上面的说明。
    pub fn to_state(self) -> OverlayState {
        OverlayState {
            visible: false,
            focused: false,
            passthrough: self.passthrough,
            // Ctrl 临时交互是纯运行时态，永远不从盘上恢复。
            ctrl_interactive: false,
            content_protected: self.content_protected,
            always_on_top: self.always_on_top,
            // 存盘值也要过 clamp：文件可能被手改成 0 或 NaN。
            opacity: clamp_opacity(self.opacity),
            collapsed: false,
        }
    }

    /// 合并一份新的开关状态，保留已记录的几何信息。
    pub fn with_state(self, state: &OverlayState) -> Self {
        Self {
            passthrough: state.passthrough,
            content_protected: state.content_protected,
            always_on_top: state.always_on_top,
            opacity: state.opacity,
            ..self
        }
    }

    /// 几何信息是否可用。非有限值或非正尺寸一律丢弃，宁可回默认也不要建出
    /// 一个 0 宽或坐标是 NaN 的窗口。
    fn geometry(&self) -> Option<(f64, f64, f64, f64)> {
        let (x, y, w, h) = (self.x?, self.y?, self.width?, self.height?);
        if ![x, y, w, h].iter().all(|v| v.is_finite()) {
            return None;
        }
        if w < MIN_WIDTH || h < MIN_HEIGHT {
            return None;
        }
        Some((x, y, w, h))
    }
}

fn layout_path(dir: &Path) -> PathBuf {
    dir.join("overlay-layout.json")
}

/// 读取落盘布局。任何失败（首次运行、文件损坏）都回默认值：
/// 悬浮窗是可选功能，不该因为一个坏掉的 JSON 就无法打开。
pub fn load_layout(dir: &Path) -> OverlayLayout {
    std::fs::read(layout_path(dir))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<OverlayLayout>(&bytes).ok())
        .unwrap_or_default()
}

pub fn save_layout(dir: &Path, layout: &OverlayLayout) -> Result<(), String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("创建配置目录失败：{e}"))?;
    let bytes =
        serde_json::to_vec_pretty(layout).map_err(|e| format!("序列化悬浮窗布局失败：{e}"))?;
    std::fs::write(layout_path(dir), bytes).map_err(|e| format!("写入悬浮窗布局失败：{e}"))
}

/// 读回窗口当前的实际位置/尺寸（逻辑像素）。窗口不存在或读失败时返回 None，
/// 调用方应保留上一次记录的几何信息，不要覆盖成空。
pub fn read_geometry(app: &AppHandle) -> Option<(f64, f64, f64, f64)> {
    let window = overlay_window(app)?;
    let scale = window.scale_factor().ok()?;
    let pos: LogicalPosition<f64> = window.outer_position().ok()?.to_logical(scale);
    let size: LogicalSize<f64> = window.outer_size().ok()?.to_logical(scale);
    Some((pos.x, pos.y, size.width, size.height))
}

/// 判断一段尺寸是否可能是"用户摆出来的完整窗口"。
///
/// 收起成细条时窗口是 `STRIP_LENGTH × STRIP_THICKNESS`（240×18），远小于最小
/// 窗口尺寸。收起路径自己 `set_size` 触发的 `Resized` 回调可能赶在
/// `collapsed` 置位之前重入到几何记录里（Windows 的 `SetWindowPos` 是同步的），
/// 把细条几何写进 layout 后，展开时 `geometry()` 校验失败就会回默认值——
/// 这正是"调过宽高、收起再展开却恢复默认"的根因。细条尺寸永远过不了
/// 最小尺寸校验，在记录入口直接拒收。
pub fn is_full_window_geometry(width: f64, height: f64) -> bool {
    width.is_finite() && height.is_finite() && width >= MIN_WIDTH && height >= MIN_HEIGHT
}

/// 把任意输入夹到可用区间，并保留 2 位小数避免浮点噪声写回前端。
pub fn clamp_opacity(value: f64) -> f64 {
    if !value.is_finite() {
        return 1.0;
    }
    let clamped = value.clamp(MIN_OPACITY, 1.0);
    (clamped * 100.0).round() / 100.0
}

/// 收起成细条后的几何（逻辑像素）：横条压在当前显示器工作区顶边、水平居中。
///
/// 顶部不留缝：留了缝之后鼠标"移到屏幕顶边"就碰不到细条了。
pub fn strip_geometry(work_x: f64, work_y: f64, work_width: f64) -> (f64, f64, f64, f64) {
    let width = STRIP_LENGTH.min(work_width);
    let x = work_x + (work_width - width) / 2.0;
    (x, work_y, width, STRIP_THICKNESS)
}

/// 应用一段目标几何时窗口应配合的最小尺寸（S1 的"最小尺寸之舞"）。
///
/// 窗口以 `min_inner_size(280, 200)` 建出来，tao 在 set_size 触发的
/// WM_GETMINMAXINFO 里会用当前最小值把请求夹回去：目标比正常最小值还小
/// （收起细条 240×18）时，必须先把最小尺寸降到目标本身，否则 Ctrl+Alt+E
/// 收到的不是细条而是一个 280×200 的色块。目标回到正常最小值及以上时返回
/// 正常值——展开路径据此把收起期间降下去的最小尺寸放回去。
fn min_size_for_target(width: f64, height: f64) -> (f64, f64) {
    if is_full_window_geometry(width, height) {
        (MIN_WIDTH, MIN_HEIGHT)
    } else {
        (width, height)
    }
}

fn overlay_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window(OVERLAY_LABEL)
}

/// 惰性创建悬浮窗。放在运行时创建而不是 tauri.conf.json，是因为
/// 大多数会话根本不开悬浮窗，没必要为它常驻一个 WebView2 进程。
///
/// `layout` 里若有上次记录的几何信息，在 build 时就带上（而不是建完再挪）：
/// 窗口是 `.visible(false)` 建出来的，先摆好再 show 才不会先在默认位置闪一下。
/// 窗口几何变化的回调（逻辑像素 x, y, width, height）。
/// 由 lib.rs 传进来写 `OverlayStateHandle`，overlay.rs 自己不持有那份状态。
pub type GeometryHook = Box<dyn Fn(f64, f64, f64, f64) + Send + Sync + 'static>;

/// 窗口焦点变化的回调（参数 = 是否获得焦点）。与 `GeometryHook` 同一套约束。
pub type FocusHook = Box<dyn Fn(bool) + Send + Sync + 'static>;

fn ensure_window(
    app: &AppHandle,
    layout: &OverlayLayout,
    on_geometry: Option<GeometryHook>,
    on_focus: Option<FocusHook>,
) -> Result<WebviewWindow, String> {
    if let Some(window) = overlay_window(app) {
        return Ok(window);
    }
    let mut builder = WebviewWindowBuilder::new(
        app,
        OVERLAY_LABEL,
        // HashRouter：#/overlay 无需额外构建产物即可作为第二窗口入口。
        WebviewUrl::App("index.html#/overlay".into()),
    )
    .title("AI 提词")
    .min_inner_size(MIN_WIDTH, MIN_HEIGHT)
    .decorations(false)
    .transparent(true)
    .always_on_top(true)
    .skip_taskbar(true)
    .shadow(false)
    .resizable(true)
    // 不抢焦点：面试时焦点必须留在会议软件/编辑器里，否则打字会打到悬浮窗。
    .focused(false)
    .visible(false);

    builder = match layout.geometry() {
        Some((x, y, width, height)) => builder.inner_size(width, height).position(x, y),
        None => builder.inner_size(DEFAULT_WIDTH, DEFAULT_HEIGHT),
    };

    let window = builder
        .build()
        .map_err(|e| format!("创建悬浮窗失败：{e}"))?;

    // 用户拖动/缩放后要记住新几何；点击/切走窗口要跟上焦点。只在建窗口时挂
    // 一次，`hide`/`collapse` 那种程序主动改位置的路径不依赖它（那边直接读回几何）。
    //
    // 回调跑在事件循环上，所以里面只允许写内存，绝对不能建窗口或做文件 IO：
    // 拖动一次会连发几十个 Moved，落盘留给退出和显式动作。
    let geometry_hook = on_geometry;
    let focus_hook = on_focus;
    if geometry_hook.is_some() || focus_hook.is_some() {
        let scale = window.scale_factor().unwrap_or(1.0);
        let geometry_window = window.clone();
        window.on_window_event(move |event| match event {
            tauri::WindowEvent::Focused(focused) => {
                if let Some(hook) = focus_hook.as_ref() {
                    hook(*focused);
                }
            }
            tauri::WindowEvent::Moved(_) | tauri::WindowEvent::Resized(_) => {
                let Some(hook) = geometry_hook.as_ref() else {
                    return;
                };
                // Moved 只带位置、Resized 只带尺寸，两个都要用当前值补齐，
                // 所以统一从窗口读回来而不是解事件里的那一半。
                let Ok(pos) = geometry_window.outer_position() else {
                    return;
                };
                let Ok(size) = geometry_window.outer_size() else {
                    return;
                };
                let pos: LogicalPosition<f64> = pos.to_logical(scale);
                let size: LogicalSize<f64> = size.to_logical(scale);
                hook(pos.x, pos.y, size.width, size.height);
            }
            _ => {}
        });
    }

    let restored = layout.to_state();
    // 先设隐身再显示：窗口一旦出现在共享画面里就已经泄露了，顺序不能反。
    if let Err(e) = window.set_content_protected(restored.content_protected) {
        return Err(format!("开启共享隐身失败：{e}"));
    }
    let _ = window.set_ignore_cursor_events(restored.passthrough);
    Ok(window)
}

fn read_state(window: &WebviewWindow, known: &OverlayState) -> OverlayState {
    OverlayState {
        // 可见性/焦点都问系统要权威值：用户可能用系统手势关掉了窗口，
        // 焦点也可能被别的窗口抢走，拿记忆值硬顶会让界面说谎。
        visible: window.is_visible().unwrap_or(known.visible),
        focused: window.is_focused().unwrap_or(known.focused),
        ..*known
    }
}

/// 显示悬浮窗（必要时创建），并按当前记忆的状态还原隐身/穿透/置顶。
///
/// `layout` 只在"这次调用需要新建窗口"时用来定位；窗口已存在时它不生效
/// （不能反过来把用户刚拖到的位置又拽回存盘值）。
pub fn show(
    app: &AppHandle,
    state: &OverlayState,
    layout: &OverlayLayout,
    on_geometry: Option<GeometryHook>,
    on_focus: Option<FocusHook>,
) -> Result<OverlayState, String> {
    let window = ensure_window(app, layout, on_geometry, on_focus)?;
    window
        .set_content_protected(state.content_protected)
        .map_err(|e| format!("设置共享隐身失败：{e}"))?;
    let _ = window.set_always_on_top(state.always_on_top);
    let _ = window.set_ignore_cursor_events(state.passthrough);
    window.show().map_err(|e| format!("显示悬浮窗失败：{e}"))?;
    Ok(read_state(&window, state))
}

pub fn hide(app: &AppHandle, state: &OverlayState) -> Result<OverlayState, String> {
    if let Some(window) = overlay_window(app) {
        window.hide().map_err(|e| format!("隐藏悬浮窗失败：{e}"))?;
        return Ok(read_state(&window, state));
    }
    Ok(OverlayState {
        visible: false,
        focused: false,
        ..*state
    })
}

/// 穿透开关。开启后窗口不再接收任何鼠标事件，等于变成纯提词投影。
pub fn set_passthrough(
    app: &AppHandle,
    state: &OverlayState,
    passthrough: bool,
) -> Result<OverlayState, String> {
    let next = OverlayState {
        passthrough,
        ..*state
    };
    if let Some(window) = overlay_window(app) {
        window
            .set_ignore_cursor_events(passthrough)
            .map_err(|e| format!("设置鼠标穿透失败：{e}"))?;
        return Ok(read_state(&window, &next));
    }
    Ok(next)
}

/// 共享隐身开关。
///
/// 边界要说清楚：Windows 的 `WDA_EXCLUDEFROMCAPTURE` 只让 OS 截屏/录屏/
/// 窗口共享 API 拿不到这块画面。它挡不住采集卡，也挡不住有人用手机拍屏幕。
pub fn set_content_protected(
    app: &AppHandle,
    state: &OverlayState,
    protected: bool,
) -> Result<OverlayState, String> {
    let next = OverlayState {
        content_protected: protected,
        ..*state
    };
    if let Some(window) = overlay_window(app) {
        window
            .set_content_protected(protected)
            .map_err(|e| format!("设置共享隐身失败：{e}"))?;
        return Ok(read_state(&window, &next));
    }
    Ok(next)
}

pub fn set_always_on_top(
    app: &AppHandle,
    state: &OverlayState,
    on_top: bool,
) -> Result<OverlayState, String> {
    let next = OverlayState {
        always_on_top: on_top,
        ..*state
    };
    if let Some(window) = overlay_window(app) {
        window
            .set_always_on_top(on_top)
            .map_err(|e| format!("设置始终置顶失败：{e}"))?;
        return Ok(read_state(&window, &next));
    }
    Ok(next)
}

/// 透明度只记在状态里，由前端用 CSS 应用。
///
/// 不用 Win32 的 `WS_EX_LAYERED` + alpha：那会连带降低整窗 alpha，和
/// `WDA_EXCLUDEFROMCAPTURE` 叠加时在部分驱动上会让窗口彻底不可见。
pub fn set_opacity(state: &OverlayState, opacity: f64) -> OverlayState {
    OverlayState {
        opacity: clamp_opacity(opacity),
        ..*state
    }
}

/// 按 5% 步进透明度（热键用）。`clearer = true` 更不透明。
///
/// 当前值不在 5% 的格点上（比如旧 `overlay-layout.json` 读回来的 0.92）时，
/// 先落到最近的格点再走，避免第一次按键像是没反应。
pub fn step_opacity(state: &OverlayState, clearer: bool) -> OverlayState {
    let current = clamp_opacity(state.opacity);
    let snapped = (current / OPACITY_STEP).round() * OPACITY_STEP;
    let aligned = (current - snapped).abs() < 1e-9;
    let next = if !aligned {
        snapped
    } else if clearer {
        snapped + OPACITY_STEP
    } else {
        snapped - OPACITY_STEP
    };
    set_opacity(state, next)
}

/// 读取窗口所在显示器的工作区（逻辑像素）。
///
/// 用 `work_area` 而不是 `monitor.size()`：后者含任务栏。多显示器下也必须用
/// 窗口当前所在的那块，否则收起时会把细条拽回主屏。
fn work_area_of(window: &WebviewWindow) -> Result<(f64, f64, f64, f64), String> {
    let monitor = window
        .current_monitor()
        .map_err(|e| format!("读取显示器信息失败：{e}"))?
        .or_else(|| window.primary_monitor().ok().flatten())
        .ok_or_else(|| "找不到可用显示器".to_string())?;
    let scale = monitor.scale_factor();
    let work = monitor.work_area();
    let pos: LogicalPosition<f64> =
        PhysicalPosition::new(work.position.x as f64, work.position.y as f64).to_logical(scale);
    let size: LogicalSize<f64> =
        PhysicalSize::new(work.size.width as f64, work.size.height as f64).to_logical(scale);
    Ok((pos.x, pos.y, size.width, size.height))
}

fn logical_geometry(window: &WebviewWindow) -> Result<(f64, f64, f64, f64), String> {
    let scale = window
        .scale_factor()
        .map_err(|e| format!("读取缩放比例失败：{e}"))?;
    let pos: LogicalPosition<f64> = window
        .outer_position()
        .map_err(|e| format!("读取窗口位置失败：{e}"))?
        .to_logical(scale);
    let size: LogicalSize<f64> = window
        .outer_size()
        .map_err(|e| format!("读取窗口尺寸失败：{e}"))?
        .to_logical(scale);
    Ok((pos.x, pos.y, size.width, size.height))
}

/// 收起成细条。没有前置条件：从任意窗口位置都能收起，
/// 细条固定落在当前显示器工作区的顶部居中。
///
/// 收起前先把完整几何记进 layout（调用方负责 persist），否则展开时不知道
/// 该恢复回哪里。
pub fn collapse(app: &AppHandle, state: &OverlayState) -> Result<OverlayState, String> {
    let Some(window) = overlay_window(app) else {
        return Ok(OverlayState {
            collapsed: true,
            ..*state
        });
    };
    if state.collapsed {
        return Ok(read_state(&window, state));
    }
    let (work_x, work_y, work_w, _) = work_area_of(&window)?;
    let (x, y, w, h) = strip_geometry(work_x, work_y, work_w);
    // 先降最小尺寸再缩小（顺序不能反）：tao 的 set_min_size 会拿当前尺寸
    // 重走一遍 WM_GETMINMAXINFO，set_size(240×18) 撞上 280×200 的最小值只能
    // 收到一个色块。降完再 set_size 才是真正的细条（S1）。
    let (min_w, min_h) = min_size_for_target(w, h);
    window
        .set_min_size(Some(LogicalSize::new(min_w, min_h)))
        .map_err(|e| format!("放宽悬浮窗最小尺寸失败：{e}"))?;
    // 先缩小再挪位：反过来的话在部分驱动上会先看到窗口跑到顶上再变形。
    window
        .set_size(LogicalSize::new(w, h))
        .map_err(|e| format!("收起悬浮窗失败：{e}"))?;
    window
        .set_position(LogicalPosition::new(x, y))
        .map_err(|e| format!("移动悬浮窗失败：{e}"))?;
    Ok(read_state(
        &window,
        &OverlayState {
            collapsed: true,
            ..*state
        },
    ))
}

/// 从细条展开回完整窗口，恢复收起前记录的位置和尺寸。
///
/// 没有记录时回默认尺寸并原地向下展开（坐标夹回工作区内），而不是保持
/// 细条大小——否则展开后还是一条看不了内容的横条。
pub fn expand(
    app: &AppHandle,
    state: &OverlayState,
    layout: &OverlayLayout,
) -> Result<OverlayState, String> {
    let Some(window) = overlay_window(app) else {
        return Ok(OverlayState {
            collapsed: false,
            ..*state
        });
    };
    if !state.collapsed {
        return Ok(read_state(&window, state));
    }
    let (work_x, work_y, work_w, work_h) = work_area_of(&window)?;
    let (cur_x, cur_y, _, _) = logical_geometry(&window)?;
    let (x, y, width, height) = match layout.geometry() {
        Some((x, y, w, h)) => (x, y, w, h),
        None => {
            // 没摆过就回默认尺寸，以细条当前位置为左上角向下展开。
            let max_x = (work_w - DEFAULT_WIDTH).max(0.0);
            let max_y = (work_h - DEFAULT_HEIGHT).max(0.0);
            (
                (cur_x - work_x).clamp(0.0, max_x) + work_x,
                (cur_y - work_y).clamp(0.0, max_y) + work_y,
                DEFAULT_WIDTH,
                DEFAULT_HEIGHT,
            )
        }
    };
    window
        .set_size(LogicalSize::new(width, height))
        .map_err(|e| format!("展开悬浮窗失败：{e}"))?;
    window
        .set_position(LogicalPosition::new(x, y))
        .map_err(|e| format!("移动悬浮窗失败：{e}"))?;
    // 尺寸落定后再放回正常最小尺寸（S1）：最小值先升的话，tao 的 set_min_size
    // 会拿细条当前尺寸重走 WM_GETMINMAXINFO，把 240×18 先夹成 280×200 的色块
    // 再展开，等于闪一次错误状态。此刻窗口已不小于正常最小值，升回去不会动它。
    let (min_w, min_h) = min_size_for_target(width, height);
    window
        .set_min_size(Some(LogicalSize::new(min_w, min_h)))
        .map_err(|e| format!("恢复悬浮窗最小尺寸失败：{e}"))?;
    Ok(read_state(
        &window,
        &OverlayState {
            collapsed: false,
            ..*state
        },
    ))
}

/// 当前状态（窗口不存在时返回记忆值，visible / focused 归 false：
/// 不存在的窗口既不可见也没有焦点）。
pub fn snapshot(app: &AppHandle, state: &OverlayState) -> OverlayState {
    match overlay_window(app) {
        Some(window) => read_state(&window, state),
        None => OverlayState {
            visible: false,
            focused: false,
            ..*state
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opacity_is_clamped_into_the_usable_band() {
        assert_eq!(clamp_opacity(1.5), 1.0);
        // 低于下限会让提词看不见，等于把窗口弄丢。
        assert_eq!(clamp_opacity(0.0), MIN_OPACITY);
        assert_eq!(clamp_opacity(-3.0), MIN_OPACITY);
        assert_eq!(clamp_opacity(0.5), 0.5);
        // 浮点噪声截到 2 位，避免状态在前端来回抖动。
        assert_eq!(clamp_opacity(0.917_3), 0.92);
        // NaN 不该把窗口变透明。
        assert_eq!(clamp_opacity(f64::NAN), 1.0);
    }

    #[test]
    fn opacity_steps_by_five_percent_and_stop_at_both_ends() {
        let base = OverlayState::default();
        // 0.92 不在 5% 格点上：第一次按键先对齐到最近的格点（0.90），不越过它。
        let aligned = step_opacity(&base, false);
        assert_eq!(aligned.opacity, 0.90);
        let aligned_up = step_opacity(&base, true);
        assert_eq!(aligned_up.opacity, 0.90, "对齐不看方向，只落到最近格点");

        // 从格点上出发才真的走一格。期望值用整数百分比算,避免浮点累加噪声
        // (0.95 - 0.05 在 f64 里并不精确等于 0.90)。
        let mut state = set_opacity(&base, 1.0);
        let mut percent: i32 = 95;
        while percent >= (MIN_OPACITY * 100.0) as i32 {
            state = step_opacity(&state, false);
            assert_eq!(state.opacity, f64::from(percent) / 100.0);
            percent -= 5;
        }
        // 触底后夹住：热键按到几乎看不见会被当成崩了。
        state = step_opacity(&state, false);
        assert_eq!(state.opacity, MIN_OPACITY);

        let state = step_opacity(&state, true);
        assert_eq!(state.opacity, MIN_OPACITY + OPACITY_STEP);
        // 顶部同样夹住在 1.0。
        let mut top = set_opacity(&base, 1.0);
        top = step_opacity(&top, true);
        assert_eq!(top.opacity, 1.0);
    }

    #[test]
    fn stepping_opacity_keeps_every_other_field() {
        let other = OverlayState {
            visible: true,
            passthrough: true,
            collapsed: true,
            ..OverlayState::default()
        };
        let next = step_opacity(&other, false);
        assert!(next.collapsed && next.passthrough && next.visible);
    }

    #[test]
    fn default_state_hides_from_capture_before_first_show() {
        let state = OverlayState::default();
        // "忘记开隐身"在面试里是致命的，所以默认必须是开。
        assert!(state.content_protected);
        assert!(state.always_on_top);
        assert!(!state.visible);
        assert!(!state.passthrough);
        assert_eq!(state.opacity, clamp_opacity(state.opacity));
    }

    #[test]
    fn restored_layout_never_auto_shows_the_window() {
        // 启动就自动弹悬浮窗会盖住别的窗口，显隐一律由用户触发。
        let layout = OverlayLayout {
            passthrough: true,
            content_protected: false,
            always_on_top: false,
            opacity: 0.5,
            x: Some(10.0),
            y: Some(20.0),
            width: Some(400.0),
            height: Some(500.0),
        };
        let state = layout.to_state();
        assert!(!state.visible);
        assert!(state.passthrough);
        assert!(!state.content_protected);
        assert!(!state.always_on_top);
        assert_eq!(state.opacity, 0.5);
        // 绝不能恢复成"已收起"：重启后只看到一条 18px 细条，
        // 用户很难认出那是自己的应用。
        assert!(!state.collapsed);
    }

    #[test]
    fn restored_layout_clamps_a_hand_edited_opacity() {
        // overlay-layout.json 是明文，被改成 0 之后不能让窗口彻底看不见。
        let layout = OverlayLayout {
            opacity: 0.0,
            ..OverlayLayout::default()
        };
        assert_eq!(layout.to_state().opacity, MIN_OPACITY);
    }

    #[test]
    fn broken_geometry_falls_back_to_default_size() {
        // 缺字段、NaN、比最小尺寸还小：一律丢弃，宁可回默认也不要建畸形窗口。
        let partial = OverlayLayout {
            x: Some(10.0),
            y: None,
            width: Some(400.0),
            height: Some(500.0),
            ..OverlayLayout::default()
        };
        assert!(partial.geometry().is_none());

        let nan = OverlayLayout {
            x: Some(f64::NAN),
            y: Some(0.0),
            width: Some(400.0),
            height: Some(500.0),
            ..OverlayLayout::default()
        };
        assert!(nan.geometry().is_none());

        let too_small = OverlayLayout {
            x: Some(0.0),
            y: Some(0.0),
            width: Some(10.0),
            height: Some(10.0),
            ..OverlayLayout::default()
        };
        assert!(too_small.geometry().is_none());

        let good = OverlayLayout {
            x: Some(-5.0),
            y: Some(30.0),
            width: Some(MIN_WIDTH),
            height: Some(MIN_HEIGHT),
            ..OverlayLayout::default()
        };
        // 负 x 是合法的：副屏可能在主屏左边。
        assert_eq!(good.geometry(), Some((-5.0, 30.0, MIN_WIDTH, MIN_HEIGHT)));
    }

    #[test]
    fn merging_state_into_layout_keeps_recorded_geometry() {
        // 改透明度不该把记好的位置抹掉。
        let layout = OverlayLayout {
            x: Some(100.0),
            y: Some(200.0),
            width: Some(420.0),
            height: Some(560.0),
            ..OverlayLayout::default()
        };
        let next = layout.with_state(&OverlayState {
            opacity: 0.4,
            passthrough: true,
            ..OverlayState::default()
        });
        assert_eq!(next.opacity, 0.4);
        assert!(next.passthrough);
        assert_eq!(next.geometry(), layout.geometry());
    }

    #[test]
    fn layout_round_trips_through_disk_and_survives_a_corrupt_file() {
        let dir = std::env::temp_dir().join(format!("overlay-layout-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);

        // 首次运行：文件不存在，必须给默认值而不是报错。
        assert_eq!(load_layout(&dir), OverlayLayout::default());

        let layout = OverlayLayout {
            passthrough: true,
            content_protected: false,
            always_on_top: false,
            opacity: 0.66,
            x: Some(-1920.0),
            y: Some(40.0),
            width: Some(380.0),
            height: Some(620.0),
        };
        save_layout(&dir, &layout).expect("写入布局应成功");
        assert_eq!(load_layout(&dir), layout);

        // 文件损坏也不能挡住悬浮窗打开。
        std::fs::write(layout_path(&dir), b"{ not json").expect("写入坏文件");
        assert_eq!(load_layout(&dir), OverlayLayout::default());

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_old_layout_file_with_removed_fields_still_loads() {
        // 删字段不能让老用户的文件整份作废：贴边时代的 docked/autoHide 键
        // 已经没有对应字段，serde 忽略未知键，位置和透明度必须照常读出来。
        let dir = std::env::temp_dir().join(format!("overlay-legacy-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("建目录");
        std::fs::write(
            layout_path(&dir),
            br#"{"passthrough":false,"contentProtected":true,"alwaysOnTop":true,
                 "opacity":0.7,"x":12.0,"y":34.0,"width":400.0,"height":500.0,
                 "docked":"right","autoHide":true}"#,
        )
        .expect("写老版本文件");
        let layout = load_layout(&dir);
        assert_eq!(layout.opacity, 0.7);
        assert_eq!(layout.geometry(), Some((12.0, 34.0, 400.0, 500.0)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_collapsed_strip_sits_top_center_of_the_work_area() {
        // 收起后是顶部居中的横条：贴住工作区顶边，不留缝（留缝鼠标碰不到）。
        let (x, y, w, h) = strip_geometry(0.0, 0.0, 1920.0);
        assert_eq!(y, 0.0);
        assert_eq!(h, STRIP_THICKNESS);
        assert_eq!(w, STRIP_LENGTH);
        assert_eq!(x, (1920.0 - STRIP_LENGTH) / 2.0);
    }

    #[test]
    fn strip_geometry_is_rejected_as_recorded_window_geometry() {
        // 收起时程序自己 set_size(240×18) 触发的 Resized 回调可能赶在 collapsed
        // 置位前重入几何记录;细条尺寸必须被拒收,否则展开时恢复的就是细条
        // (或因校验失败回默认值)——这是"收起再展开宽高回默认"的根因。
        let (x, y, w, h) = strip_geometry(0.0, 0.0, 1920.0);
        assert!(!is_full_window_geometry(w, h), "细条尺寸不能记进布局");
        // 窗口被 OS 限制在最小尺寸时(280×200)仍是合法的完整窗口几何。
        assert!(is_full_window_geometry(MIN_WIDTH, MIN_HEIGHT));
        assert!(is_full_window_geometry(DEFAULT_WIDTH, DEFAULT_HEIGHT));
        assert!(!is_full_window_geometry(f64::NAN, 500.0));
        assert!(!is_full_window_geometry(500.0, f64::INFINITY));
        let _ = (x, y);
    }

    #[test]
    fn a_collapsed_strip_stays_on_its_monitor_and_shrinks_on_a_narrow_one() {
        // 副屏工作区原点不为 0：细条必须居中在那块屏上，不是主屏。
        let (x, _, w, _) = strip_geometry(1920.0, 0.0, 1920.0);
        assert_eq!(x, 1920.0 + (1920.0 - w) / 2.0);
        // 工作区比细条还窄：长度夹到工作区宽，别把细条推出屏幕。
        let (x, _, w, _) = strip_geometry(0.0, 0.0, 100.0);
        assert_eq!((x, w), (0.0, 100.0));
    }

    #[test]
    fn strip_geometry_lowers_the_min_size_and_expanding_restores_the_normal_one() {
        // S1 的"最小尺寸之舞"：细条目标必须先把窗口最小尺寸降到目标本身，
        // 否则 tao 在 set_size 的 WM_GETMINMAXINFO 里会用 280×200 的最小值把
        // 240×18 的请求夹回去——Ctrl+Alt+E 只能收到一个色块而不是细条。
        let (_, _, w, h) = strip_geometry(0.0, 0.0, 1920.0);
        assert_eq!(
            min_size_for_target(w, h),
            (w, h),
            "细条目标配细条自己的最小尺寸"
        );
        // 窄屏上细条长度被工作区夹短，最小尺寸必须跟着目标走，不能还是 240。
        let (_, _, w, h) = strip_geometry(0.0, 0.0, 100.0);
        assert_eq!(min_size_for_target(w, h), (100.0, h));
        // 展开路径：任何完整窗口几何都配回正常最小值，收起期间降下去的值
        // 不能泄漏成常驻的最小尺寸。
        assert_eq!(
            min_size_for_target(DEFAULT_WIDTH, DEFAULT_HEIGHT),
            (MIN_WIDTH, MIN_HEIGHT)
        );
        assert_eq!(
            min_size_for_target(MIN_WIDTH, MIN_HEIGHT),
            (MIN_WIDTH, MIN_HEIGHT)
        );
    }

    #[test]
    fn focus_starts_false_and_never_comes_back_from_disk() {
        // 窗口以 focused(false) 创建且从不抢焦；焦点是运行时态，
        // 落盘布局里没有这个字段，恢复时必须从 false 起。
        assert!(!OverlayState::default().focused);
        assert!(!OverlayLayout::default().to_state().focused);
        // with_state 只合并开关偏好，不含焦点：改透明度不能把焦点写进布局。
        let merged = OverlayLayout::default().with_state(&OverlayState {
            focused: true,
            ..OverlayState::default()
        });
        assert!(!merged.to_state().focused);
    }
}
