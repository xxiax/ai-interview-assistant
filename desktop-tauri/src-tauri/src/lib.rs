//! AI 面试助手 PC 客户端(Tauri 2)— Rust 侧入口。
//! 命令层:设置/会话/历史/配置 REST 透传 + Live 引擎生命周期 + 音频分片。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backoff;
mod engine;
mod outbox;
mod overlay;
mod protocol;
mod reconcile;
mod rest;
mod screenshot;
mod settings;
mod system_audio;
mod ws_client;

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use base64::Engine as _;
use tauri::{AppHandle, Emitter, Manager, State};
use tokio::sync::Mutex;

use engine::{Engine, EngineCommand, EngineEvent, NewChunk};
use overlay::{OverlayLayout, OverlayState};
use settings::AppSettings;
use system_audio::SystemAudioState;

/// 全局引擎状态(单活动会话)。
struct EngineState {
    engine: Mutex<Option<Engine>>,
    /// 实时链路运行时快照。引擎事件只在发生瞬间广播,悬浮窗是懒创建的,
    /// 它打开前的事件(比如已经开始的录制)永远收不到——晚挂载的 webview
    /// 用 live_runtime_state 命令拉这份快照播种。快照在 emit_engine_event
    /// 这个唯一汇总点更新,和广播出去的内容天然一致。
    runtime: std::sync::Mutex<RuntimeSnapshot>,
    /// 引擎代际:每次 live_connect 替换引擎前 fetch_add 翻新。事件闭包在
    /// 创建时定格自己那一代,之后凡当前代际 != 闭包代际的事件整条丢弃。
    /// 这堵的是一个真实竞态(用户连续两轮" textarea 提问毫无回应"的根因):
    /// 替换引擎时,旧引擎 ws_client 循环收尾补发的 Connection closed 可能
    /// **晚于新引擎的 ready 到达**——快照与两个窗口会先看到 ready/recording,
    /// 又被这条迟到的 closed 打回"断开",而新引擎此后不再发任何事件,
    /// 没有人来纠正,状态就永久卡死:悬浮窗与主窗口的提问门禁全部静默拦截。
    /// 代际门保证被替换引擎的收尾事件(含 closed/SessionEnded)出不了 Rust。
    /// 注意 live_disconnect 不翻代际:正常离开实时页的收尾 closed 要照发。
    gen: AtomicU64,
}

/// live_runtime_state 命令的返回体(serde camelCase,对齐前端 LiveRuntimeState)。
#[derive(Default, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeSnapshot {
    /// 连接阶段(connecting/ready/reconnecting/closed…);引擎从未启动过为 None。
    phase: Option<String>,
    session_id: Option<String>,
    session_status: Option<String>,
    radio_mode: Option<String>,
    capture_active: bool,
}

impl EngineState {
    /// 引擎事件流的唯一汇总点顺带维护快照(见字段注释)。只记"晚挂载的
    /// 悬浮窗补状态"需要的字段,别的不复制。
    fn observe(&self, event: &EngineEvent) {
        let Ok(mut snap) = self.runtime.lock() else {
            return;
        };
        match event {
            EngineEvent::Connection { phase, .. } => {
                snap.phase = Some((*phase).to_string());
                if *phase == engine::phase::CLOSED {
                    // 引擎停了:会话/采集状态随之作废,别让快照替旧会话报"录制中"。
                    snap.session_id = None;
                    snap.session_status = None;
                    snap.radio_mode = None;
                    snap.capture_active = false;
                }
            }
            EngineEvent::SessionState { status, radio_mode } => {
                snap.session_status = Some(status.clone());
                snap.radio_mode = Some(radio_mode.clone());
            }
            EngineEvent::SessionEnded => {
                snap.session_status = Some("ended".to_string());
                snap.capture_active = false;
            }
            EngineEvent::CaptureState { active } => {
                snap.capture_active = *active;
            }
            _ => {}
        }
    }
}
/// 悬浮提词窗状态。窗口本身不存 alpha/穿透之类的可读回值,
/// 因此这里记一份权威副本,创建/重开窗口时照它还原。
///
/// 同时兼作"跨重启记忆"的内存缓存:`layout` 是要落盘的那份(开关 + 几何),
/// `state` 是本次运行时的实际状态(多一个 `visible`,它不落盘)。
/// 两者共用一把锁,避免出现"开关已改、落盘的还是旧值"的中间态。
#[derive(Default)]
struct OverlayStateHandle {
    inner: std::sync::Mutex<OverlayMemory>,
}

#[derive(Default)]
struct OverlayMemory {
    state: OverlayState,
    layout: OverlayLayout,
    /// 落盘目录。启动时 setup 里填,填之前 persist 静默跳过。
    dir: Option<PathBuf>,
}

impl OverlayStateHandle {
    fn get(&self) -> OverlayState {
        self.inner.lock().map(|m| m.state).unwrap_or_default()
    }

    fn layout(&self) -> OverlayLayout {
        self.inner.lock().map(|m| m.layout).unwrap_or_default()
    }

    /// 记下新状态,并同步进待落盘的 layout(几何信息保持不动)。
    fn set(&self, next: OverlayState) -> OverlayState {
        if let Ok(mut guard) = self.inner.lock() {
            guard.state = next;
            guard.layout = guard.layout.with_state(&next);
        }
        next
    }

    /// 启动时把落盘值装回内存。`visible` 不恢复,见 `OverlayLayout` 的说明。
    fn restore(&self, dir: PathBuf, layout: OverlayLayout) -> OverlayState {
        let state = layout.to_state();
        if let Ok(mut guard) = self.inner.lock() {
            guard.dir = Some(dir);
            guard.layout = layout;
            guard.state = state;
        }
        state
    }

    /// 记录用户拖动/缩放后的几何。只写内存:拖动期间 Moved 会连发几十次,
    /// 每次都落盘纯属浪费。真正写盘由 `persist` 在动作结束时做。
    ///
    /// 收起期间不记:那时的几何是顶部细条,写进去展开时就没有原尺寸可恢复了。
    /// 细条尺寸(240×18)还要单独拒收:收起路径自己 `set_size` 触发的 Resized
    /// 回调在 Windows 上是同步重入的,可能赶在 `collapsed` 置位之前进来。
    fn record_geometry(&self, x: f64, y: f64, width: f64, height: f64) {
        let Ok(mut guard) = self.inner.lock() else {
            return;
        };
        if guard.state.collapsed {
            return;
        }
        if !overlay::is_full_window_geometry(width, height) {
            return;
        }
        guard.layout.x = Some(x);
        guard.layout.y = Some(y);
        guard.layout.width = Some(width);
        guard.layout.height = Some(height);
    }

    /// 落盘。失败只吞掉:摆窗口的偏好丢了是小事,不该把用户的操作变成报错。
    fn persist(&self) {
        let Ok(guard) = self.inner.lock() else {
            return;
        };
        let Some(dir) = guard.dir.clone() else {
            return;
        };
        let layout = guard.layout;
        drop(guard);
        let _ = overlay::save_layout(&dir, &layout);
    }
}

#[derive(Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
#[allow(dead_code)]
struct ToastPayload {
    kind: String, // success | error | warning | info
    message: String,
}

fn app_data_dir(app: &AppHandle) -> PathBuf {
    app.path()
        .app_data_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("ai-interview-desktop"))
}

/// release 下 panic=abort 会直接闪退且无任何痕迹。启动时安装 hook,把 panic
/// 位置/消息落盘到应用数据目录 panic.log(hook 内不能 panic,目录拿不到就只 stderr)。
/// 注意:这只保证退出前留下诊断信息,不改变 abort 策略。
fn install_panic_logger() {
    std::panic::set_hook(Box::new(|info| {
        let location = info
            .location()
            .map(|l| format!("{}:{}:{}", l.file(), l.line(), l.column()))
            .unwrap_or_else(|| "<unknown>".into());
        let payload = if let Some(s) = info.payload().downcast_ref::<&str>() {
            (*s).to_string()
        } else if let Some(s) = info.payload().downcast_ref::<String>() {
            s.clone()
        } else {
            "<non-string panic payload>".into()
        };
        let line = format!("[{}] panic at {location}: {payload}\n", chrono_like_now());
        eprintln!("{line}");
        let _ = append_panic_log(&line);
    }));
}

/// 追加写入 panic.log;失败(目录不可写等)时已由 eprintln 兜底。
fn append_panic_log(line: &str) -> std::io::Result<()> {
    use std::io::Write;
    let path = panic_log_path()?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    file.write_all(line.as_bytes())
}

/// panic.log 路径:Tauri app data 目录需要 AppHandle,而 hook 在任意线程触发;
/// 用 identifier 推导 %APPDATA% 路径,失败则放弃(仅 stderr)。
fn panic_log_path() -> std::io::Result<PathBuf> {
    let base = if cfg!(target_os = "windows") {
        std::env::var("APPDATA").map(PathBuf::from).ok()
    } else if cfg!(target_os = "macos") {
        std::env::var("HOME")
            .ok()
            .map(|home| PathBuf::from(home).join("Library/Application Support"))
    } else {
        std::env::var("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .ok()
            .or_else(|| {
                std::env::var("HOME")
                    .ok()
                    .map(|h| PathBuf::from(h).join(".config"))
            })
    };
    let Some(base) = base else {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "无法定位应用数据目录",
        ));
    };
    Ok(base.join("com.aiinterview.desktop").join("panic.log"))
}

/// 无 chrono 依赖的本地时间戳(hook 内只用于排序,精度足够)。
fn chrono_like_now() -> String {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| format!("{}.{:03}s", d.as_secs(), d.subsec_millis()))
        .unwrap_or_else(|_| "pre-epoch".into())
}

/// 构造 REST 上下文;未配置令牌时返回错误消息。
fn make_ctx(_app: &AppHandle) -> Result<rest::RestContext, String> {
    let token =
        settings::load_token().ok_or_else(|| "未配置访问令牌,请先在设置中填写".to_string())?;
    Ok(rest::RestContext {
        server_url: settings::SERVER_URL.to_string(),
        token,
    })
}

fn rest_err(e: rest::RestError) -> String {
    e.to_string()
}

// ---------- 引擎事件 → 前端 ----------

fn emit_engine_event(app: &AppHandle, gen: u64) -> Arc<dyn Fn(EngineEvent) + Send + Sync> {
    let app = app.clone();
    Arc::new(move |event: EngineEvent| {
        // 代际门:闭包创建时定格 gen(见 EngineState.gen 注释)。被替换的
        // 旧引擎事件先在这丢弃——observe 与广播一并跳过,快照和两个窗口
        // 都不会被迟到的 closed 打回"断开"。
        if let Some(state) = app.try_state::<EngineState>() {
            if state.gen.load(Ordering::SeqCst) != gen {
                return;
            }
            // 先把快照记上再广播:快照和事件走同一份数据,必然一致。
            state.observe(&event);
        }
        // 序列化后统一以 engine:event 名称广播
        if let Ok(payload) = serde_json::to_value(&event) {
            let _ = app.emit("engine:event", payload);
        }
    })
}

// ---------- 设置命令 ----------

#[tauri::command]
fn settings_get() -> Result<AppSettings, String> {
    Ok(AppSettings {
        has_token: settings::load_token().is_some(),
    })
}

#[tauri::command]
fn settings_set(token: Option<String>) -> Result<AppSettings, String> {
    if let Some(t) = token {
        let t = t.trim().to_string();
        if !t.is_empty() {
            settings::save_token(&t)?;
        }
    }
    Ok(AppSettings {
        has_token: settings::load_token().is_some(),
    })
}

#[tauri::command]
async fn settings_check() -> Result<serde_json::Value, String> {
    if let Err(e) = rest::check_health(settings::SERVER_URL).await {
        return Ok(serde_json::json!({ "ok": false, "reason": rest_err(e) }));
    }
    let Some(token) = settings::load_token() else {
        return Ok(serde_json::json!({ "ok": false, "reason": "服务可达,但未配置访问令牌" }));
    };
    let ctx = rest::RestContext {
        server_url: settings::SERVER_URL.to_string(),
        token,
    };
    match rest::probe_auth(&ctx).await {
        Ok(_) => Ok(serde_json::json!({ "ok": true })),
        Err(e) => Ok(serde_json::json!({ "ok": false, "reason": rest_err(e) })),
    }
}

#[tauri::command]
async fn settings_prompt_get(app: AppHandle) -> Result<protocol::PromptResponse, String> {
    let ctx = make_ctx(&app)?;
    rest::settings_prompt_get(&ctx).await.map_err(rest_err)
}

#[tauri::command]
async fn settings_prompt_set(
    app: AppHandle,
    prompt: String,
) -> Result<protocol::PromptResponse, String> {
    let ctx = make_ctx(&app)?;
    rest::settings_prompt_set(&ctx, &prompt)
        .await
        .map_err(rest_err)
}

// ---------- 会话命令 ----------

#[tauri::command]
async fn sessions_create(app: AppHandle, title: String) -> Result<protocol::Session, String> {
    let ctx = make_ctx(&app)?;
    rest::sessions_create(&ctx, &title).await.map_err(rest_err)
}

#[tauri::command]
async fn sessions_list(
    app: AppHandle,
    limit: u32,
    offset: u32,
) -> Result<Vec<protocol::Session>, String> {
    let ctx = make_ctx(&app)?;
    rest::sessions_list(&ctx, limit, offset)
        .await
        .map_err(rest_err)
}

#[tauri::command]
async fn sessions_get(app: AppHandle, id: String) -> Result<protocol::Session, String> {
    let ctx = make_ctx(&app)?;
    rest::sessions_get(&ctx, &id).await.map_err(rest_err)
}

#[tauri::command]
async fn sessions_set_context(
    app: AppHandle,
    id: String,
    job_description: String,
    resume: String,
) -> Result<protocol::Session, String> {
    let ctx = make_ctx(&app)?;
    rest::sessions_set_context(&ctx, &id, &job_description, &resume)
        .await
        .map_err(rest_err)
}

#[tauri::command]
async fn sessions_end(app: AppHandle, id: String) -> Result<protocol::Session, String> {
    let ctx = make_ctx(&app)?;
    rest::sessions_end(&ctx, &id).await.map_err(rest_err)
}

#[tauri::command]
async fn sessions_delete(app: AppHandle, id: String) -> Result<(), String> {
    let ctx = make_ctx(&app)?;
    rest::sessions_delete(&ctx, &id).await.map_err(rest_err)
}

// ---------- 历史命令 ----------

#[tauri::command]
async fn history_transcripts(
    app: AppHandle,
    session_id: String,
) -> Result<Vec<protocol::Transcript>, String> {
    let ctx = make_ctx(&app)?;
    rest::transcripts(&ctx, &session_id).await.map_err(rest_err)
}

#[tauri::command]
async fn history_answers(
    app: AppHandle,
    session_id: String,
) -> Result<Vec<protocol::Answer>, String> {
    let ctx = make_ctx(&app)?;
    rest::answers(&ctx, &session_id).await.map_err(rest_err)
}

#[tauri::command]
async fn history_reviews(
    app: AppHandle,
    session_id: String,
) -> Result<Vec<protocol::Review>, String> {
    let ctx = make_ctx(&app)?;
    rest::reviews(&ctx, &session_id).await.map_err(rest_err)
}

#[tauri::command]
async fn history_generate_review(
    app: AppHandle,
    session_id: String,
    use_search: bool,
) -> Result<protocol::Review, String> {
    let ctx = make_ctx(&app)?;
    rest::generate_review(&ctx, &session_id, use_search)
        .await
        .map_err(rest_err)
}

// ---------- 配置命令 ----------

#[tauri::command]
async fn configs_list(
    app: AppHandle,
    config_type: String,
) -> Result<Vec<protocol::ConfigItem>, String> {
    let ctx = make_ctx(&app)?;
    rest::configs_list(&ctx, &config_type)
        .await
        .map_err(rest_err)
}

#[tauri::command]
async fn configs_save(
    app: AppHandle,
    config_type: String,
    body: serde_json::Value,
) -> Result<protocol::ConfigItem, String> {
    let ctx = make_ctx(&app)?;
    rest::configs_save(&ctx, &config_type, &body)
        .await
        .map_err(rest_err)
}

#[tauri::command]
async fn configs_activate(
    app: AppHandle,
    config_type: String,
    id: i64,
) -> Result<protocol::ConfigItem, String> {
    let ctx = make_ctx(&app)?;
    rest::configs_activate(&ctx, &config_type, id)
        .await
        .map_err(rest_err)
}

#[tauri::command]
async fn configs_delete(app: AppHandle, config_type: String, id: i64) -> Result<(), String> {
    let ctx = make_ctx(&app)?;
    rest::configs_delete(&ctx, &config_type, id)
        .await
        .map_err(rest_err)
}

/// 拉取 LLM 模型列表。api_key 允许为空串(编辑模式不修改 key):
/// 后端只在 Base URL 未改变时复用已保存密钥。
#[tauri::command]
async fn llm_fetch_models(
    app: AppHandle,
    base_url: String,
    api_key: String,
    auth_field: String,
) -> Result<protocol::LlmModelsResponse, String> {
    let ctx = make_ctx(&app)?;
    rest::llm_fetch_models(&ctx, &base_url, &api_key, &auth_field)
        .await
        .map_err(rest_err)
}

// ---------- Live 引擎命令 ----------

#[tauri::command]
async fn live_connect(
    app: AppHandle,
    state: State<'_, EngineState>,
    system_audio_state: State<'_, SystemAudioState>,
    session_id: String,
) -> Result<bool, String> {
    let ctx = make_ctx(&app)?;
    // 先翻引擎代际再动旧引擎(顺序不能反):旧引擎闭包持有旧代际,此后它的
    // 一切事件——最要命的是 ws_client 收尾补发的 Connection closed,它可能
    // 晚于新引擎的 ready 到达——整条作废,见 EngineState.gen 注释。
    let engine_gen = state.gen.fetch_add(1, Ordering::SeqCst).wrapping_add(1);
    let mut guard = state.engine.lock().await;
    let system_stop_result = system_audio_state.stop().await;
    if let Some(old) = guard.take() {
        old.deactivate_capture(protocol::CAPTURE_INTERRUPTED_REASON)
            .await;
        old.stop();
    }
    system_stop_result?;
    let engine = Engine::spawn(
        app_data_dir(&app),
        ctx,
        session_id.clone(),
        emit_engine_event(&app, engine_gen),
    );
    *guard = Some(engine);
    // 快照记下新会话 id:SessionState 事件不带 id,悬浮窗晚挂载时会话徽片/
    // 提问门禁的播种靠这里。capture 同步归零,新引擎的采集门从关开始。
    if let Ok(mut snap) = state.runtime.lock() {
        snap.session_id = Some(session_id);
        snap.capture_active = false;
    }
    Ok(true)
}

#[tauri::command]
async fn live_disconnect(
    state: State<'_, EngineState>,
    system_audio_state: State<'_, SystemAudioState>,
) -> Result<(), String> {
    let mut guard = state.engine.lock().await;
    let system_stop_result = system_audio_state.stop().await;
    if let Some(engine) = guard.take() {
        engine
            .deactivate_capture(protocol::CAPTURE_INTERRUPTED_REASON)
            .await;
        engine.stop();
    }
    system_stop_result?;
    Ok(())
}

/// 当前实时链路快照:晚挂载的悬浮窗播种用(见 EngineState.runtime 注释)。
/// 返回 Result 是 Tauri 硬性要求:带 `State<'_, _>` 借用参数的 async 命令
/// 必须返回 Result(裸类型过不了 generate_handler 的 'static 约束)。
/// 锁中毒只会发生在上游 panic 之后,给默认值(全空)即可。
#[tauri::command]
async fn live_runtime_state(state: State<'_, EngineState>) -> Result<RuntimeSnapshot, String> {
    Ok(state.runtime.lock().map(|s| s.clone()).unwrap_or_default())
}

#[tauri::command]
async fn live_start_session(
    state: State<'_, EngineState>,
    radio_mode: String,
) -> Result<bool, String> {
    let guard = state.engine.lock().await;
    let Some(engine) = guard.as_ref() else {
        return Ok(false);
    };
    Ok(engine.send(serde_json::json!({
        "v": 1, "type": "start_session", "radio_mode": radio_mode
    })))
}

#[tauri::command]
async fn live_set_radio_mode(state: State<'_, EngineState>, mode: String) -> Result<bool, String> {
    let guard = state.engine.lock().await;
    let Some(engine) = guard.as_ref() else {
        return Ok(false);
    };
    Ok(engine.send(serde_json::json!({
        "v": 1, "type": "set_radio_mode", "mode": mode
    })))
}

#[tauri::command]
async fn live_regenerate(
    state: State<'_, EngineState>,
    question: String,
    use_search: bool,
    thread_id: Option<String>,
) -> Result<bool, String> {
    let guard = state.engine.lock().await;
    let Some(engine) = guard.as_ref() else {
        return Ok(false);
    };
    // 带 thread_id = 重问某张问题卡：后端 revision+1，答案流回同一张卡。
    // 不带 = 手动提问式的重新生成，独立成卡立即入库。
    let mut message = serde_json::json!({
        "v": 1, "type": "regenerate_answer", "question": question, "use_search": use_search
    });
    if let Some(id) = thread_id {
        message["thread_id"] = serde_json::Value::String(id);
    }
    Ok(engine.send(message))
}

/// 抓屏 + 发送的公共实现。Tauri 命令和全局热键共用一份，避免两条路径
/// 在压缩参数或消息形状上分叉。
///
/// 抓屏放在阻塞线程池：GDI BitBlt 在 4K 多屏上要几十毫秒，占住 async
/// 运行时会让悬浮窗的热键响应卡顿。截图字节不落盘、不进设置，用完即丢
/// ——题面留在用户机器上是隐私风险。
async fn solve_screenshot_now(app: &AppHandle) -> Result<bool, String> {
    solve_screenshot_with_note(app, String::new()).await
}

async fn solve_screenshot_with_note(app: &AppHandle, note: String) -> Result<bool, String> {
    let png = tokio::task::spawn_blocking(screenshot::capture_screen_png)
        .await
        .map_err(|err| format!("截图任务失败：{err}"))??;
    let state = app
        .try_state::<EngineState>()
        .ok_or_else(|| "引擎未初始化".to_string())?;
    let guard = state.engine.lock().await;
    let Some(engine) = guard.as_ref() else {
        return Ok(false);
    };
    Ok(engine.send(serde_json::json!({
        "v": 1,
        "type": "solve_screenshot",
        "image": base64::engine::general_purpose::STANDARD.encode(&png),
        "mime": "image/png",
        "note": note,
    })))
}

/// 笔试辅助：抓当前屏幕，编 PNG，以 Base64 走 WS 交给后端多模态解题。
#[tauri::command]
async fn live_solve_screenshot(app: AppHandle, note: Option<String>) -> Result<bool, String> {
    solve_screenshot_with_note(&app, note.unwrap_or_default()).await
}

#[tauri::command]
async fn live_end_session(app: AppHandle, state: State<'_, EngineState>) -> Result<bool, String> {
    let session_id = {
        let guard = state.engine.lock().await;
        let Some(engine) = guard.as_ref() else {
            return Ok(false);
        };
        let ok = engine.send(serde_json::json!({ "v": 1, "type": "end_session" }));
        if ok {
            return Ok(true);
        }
        engine.session_id.clone()
    };
    // socket 不通:REST 兜底(幂等)
    let ctx = make_ctx(&app)?;
    rest::sessions_end(&ctx, &session_id)
        .await
        .map(|_| true)
        .map_err(rest_err)
}

/// 渲染进程采集的分片:落盘 → outbox → 泵(严格按序发送)。
#[tauri::command]
async fn audio_chunk(
    state: State<'_, EngineState>,
    chunk_id: String,
    chunk_seq: i64,
    captured_at: String,
    duration_ms: i64,
    data: Vec<u8>, // 前端传 number[](WAV 字节)
) -> Result<bool, String> {
    let guard = state.engine.lock().await;
    let Some(engine) = guard.as_ref() else {
        return Ok(false);
    };
    Ok(engine
        .cmd_tx
        .send(EngineCommand::AddChunk(Box::new(NewChunk {
            chunk_id,
            chunk_seq,
            captured_at,
            duration_ms,
            codec: "wav_pcm_s16le".into(),
            source: "pc".into(),
            data,
        })))
        .is_ok())
}

/// 启动默认 Windows 播放设备的 WASAPI loopback 采集。
#[tauri::command]
async fn system_audio_start(
    app: AppHandle,
    engine_state: State<'_, EngineState>,
    system_audio_state: State<'_, SystemAudioState>,
) -> Result<bool, String> {
    // 持有引擎生命周期锁直到采集线程完成启动，避免 live_connect/disconnect
    // 在 start 取出旧 sender 后先完成 stop，随后旧采集线程又被装回全局状态。
    let guard = engine_state.engine.lock().await;
    let Some(engine) = guard.as_ref() else {
        return Err("Live 引擎尚未连接，无法启动系统音频采集".into());
    };
    // 采集线程只发 ServerError,跟着当前代际走:引擎被替换后它若还残留,
    // 迟到的报错也会被代际门拦下,不再打到新会话头上。
    let audio_gen = engine_state.gen.load(Ordering::SeqCst);
    system_audio_state
        .start(engine.cmd_tx.clone(), emit_engine_event(&app, audio_gen))
        .await
}

/// 停止 WASAPI loopback；凑不满一个分片的尾片由采集线程直接丢弃。
#[tauri::command]
async fn system_audio_stop(
    system_audio_state: State<'_, SystemAudioState>,
) -> Result<bool, String> {
    system_audio_state.stop().await
}

/// 前端启动或停止系统声音后，统一控制 PC 音频上传门禁。
#[tauri::command]
async fn outbox_set_capture_active(
    state: State<'_, EngineState>,
    active: bool,
    reason: Option<String>,
) -> Result<(), String> {
    let guard = state.engine.lock().await;
    let Some(engine) = guard.as_ref() else {
        return capture_gate_without_engine(active);
    };
    let reason = reason.unwrap_or_else(|| {
        if active {
            "capture_started".into()
        } else {
            "capture_stopped".into()
        }
    });
    if active {
        engine.activate_capture(&reason).await
    } else {
        engine.deactivate_capture(&reason).await;
        Ok(())
    }
}

/// 停止一个尚未创建或已经销毁的引擎天然是幂等成功；页面首次挂载时
/// React StrictMode 会先执行一次清理，不能因此向用户展示伪故障。
fn capture_gate_without_engine(active: bool) -> Result<(), String> {
    if active {
        Err("Live 引擎尚未连接，无法开启音频采集".into())
    } else {
        Ok(())
    }
}

#[tauri::command]
async fn outbox_snapshot(state: State<'_, EngineState>) -> Result<outbox::OutboxStats, String> {
    let guard = state.engine.lock().await;
    let Some(engine) = guard.as_ref() else {
        return Ok(Default::default());
    };
    Ok(engine.stats().await)
}

#[tauri::command]
async fn outbox_reconcile(state: State<'_, EngineState>) -> Result<(), String> {
    let guard = state.engine.lock().await;
    let Some(engine) = guard.as_ref() else {
        return Ok(());
    };
    engine.reconcile();
    Ok(())
}

// ---------- 悬浮提词窗 ----------

// 这一组命令全部是 `async fn`，一个都不能退回同步。
//
// 同步命令在主线程上跑，而 `WebviewWindowBuilder::build()` 在 Windows 上从主
// 线程调用会和 WebView2 的消息循环互锁：`overlay_show` 永不返回，之后所有
// invoke 一起卡死，表现就是"点了悬浮窗按钮什么都没发生，然后整个应用不响应"。
// tauri 自己的文档写得很直白：创建窗口要用 async 命令和独立线程。
// `async fn` 让命令落到 tokio 运行时线程，绕开这个死锁。
//
// 不只 show 这个建窗口的要 async：设 setter 也走事件循环派发，
// 全组统一 async 才不会有人后来"顺手"把某一个改回同步又踩回来。

#[tauri::command]
async fn overlay_snapshot(
    app: AppHandle,
    state: State<'_, OverlayStateHandle>,
) -> Result<OverlayState, String> {
    Ok(state.set(overlay::snapshot(&app, &state.get())))
}

#[tauri::command]
async fn overlay_show(
    app: AppHandle,
    state: State<'_, OverlayStateHandle>,
) -> Result<OverlayState, String> {
    let next = state.set(overlay::show(
        &app,
        &state.get(),
        &state.layout(),
        overlay_geometry_hook(&app),
        overlay_focus_hook(&app),
    )?);
    // show 会按记忆还原穿透;穿透开着就得有 Ctrl 临时交互的轮询。
    sync_ctrl_watch(&app, &next);
    state.persist();
    Ok(next)
}

#[tauri::command]
async fn overlay_hide(
    app: AppHandle,
    state: State<'_, OverlayStateHandle>,
) -> Result<OverlayState, String> {
    // 隐藏前把窗口现在的位置/大小记下来：用户可能拖过又直接关掉。
    capture_overlay_geometry(&app, &state);
    let next = state.set(overlay::hide(&app, &state.get())?);
    state.persist();
    Ok(next)
}

#[tauri::command]
async fn overlay_toggle(
    app: AppHandle,
    state: State<'_, OverlayStateHandle>,
) -> Result<OverlayState, String> {
    let current = overlay::snapshot(&app, &state.get());
    let next = if current.visible {
        capture_overlay_geometry(&app, &state);
        overlay::hide(&app, &current)?
    } else {
        overlay::show(
            &app,
            &current,
            &state.layout(),
            overlay_geometry_hook(&app),
            overlay_focus_hook(&app),
        )?
    };
    let next = state.set(next);
    // toggle 的 show 分支会按记忆还原穿透;漏了 sync 的话,第一次从主窗口面板
    // 打开悬浮窗(走 toggle 而不是 show)时 Ctrl 临时交互整个不生效,直到某个
    // 热键路径碰巧调过一次 sync——用户实测的"收起再展开才可用"就是它。
    sync_ctrl_watch(&app, &next);
    state.persist();
    Ok(next)
}

#[tauri::command]
async fn overlay_set_passthrough(
    app: AppHandle,
    state: State<'_, OverlayStateHandle>,
    passthrough: bool,
) -> Result<OverlayState, String> {
    let next = state.set(overlay::set_passthrough(&app, &state.get(), passthrough)?);
    sync_ctrl_watch(&app, &next);
    state.persist();
    Ok(next)
}

#[tauri::command]
async fn overlay_set_content_protected(
    app: AppHandle,
    state: State<'_, OverlayStateHandle>,
    protected: bool,
) -> Result<OverlayState, String> {
    let next = state.set(overlay::set_content_protected(
        &app,
        &state.get(),
        protected,
    )?);
    state.persist();
    Ok(next)
}

#[tauri::command]
async fn overlay_set_always_on_top(
    app: AppHandle,
    state: State<'_, OverlayStateHandle>,
    on_top: bool,
) -> Result<OverlayState, String> {
    let next = state.set(overlay::set_always_on_top(&app, &state.get(), on_top)?);
    state.persist();
    Ok(next)
}

#[tauri::command]
async fn overlay_set_opacity(
    state: State<'_, OverlayStateHandle>,
    opacity: f64,
) -> Result<OverlayState, String> {
    let next = state.set(overlay::set_opacity(&state.get(), opacity));
    state.persist();
    Ok(next)
}

/// 收起成屏幕顶部居中的细条。收起前先把完整几何记下来，
/// 否则展开时不知道恢复成多大、恢复到哪里。
#[tauri::command]
async fn overlay_collapse(
    app: AppHandle,
    state: State<'_, OverlayStateHandle>,
) -> Result<OverlayState, String> {
    capture_overlay_geometry(&app, &state);
    let next = state.set(overlay::collapse(&app, &state.get())?);
    // 收起不改穿透标志,但 watch 可能早已死掉(见 toggle 注释),顺手保活。
    sync_ctrl_watch(&app, &next);
    state.persist();
    Ok(next)
}

/// 从细条展开回完整窗口。
#[tauri::command]
async fn overlay_expand(
    app: AppHandle,
    state: State<'_, OverlayStateHandle>,
) -> Result<OverlayState, String> {
    let next = state.set(overlay::expand(&app, &state.get(), &state.layout())?);
    sync_ctrl_watch(&app, &next);
    state.persist();
    Ok(next)
}

/// 把窗口当前的实际几何读回 `OverlayStateHandle`。读不到就保留上一次的值，
/// 不要覆盖成空：窗口关掉之后读失败是正常的。
fn capture_overlay_geometry(app: &AppHandle, state: &OverlayStateHandle) {
    if let Some((x, y, width, height)) = overlay::read_geometry(app) {
        state.record_geometry(x, y, width, height);
    }
}
/// 构造"用户拖动/缩放悬浮窗"的回调。只写内存，落盘由显式动作和退出时做。
///
/// 闭包里通过 `AppHandle` 现取 state 而不是捕获 `State<'_, _>`：后者带生命周期，
/// 塞不进 `'static` 的窗口事件回调。
fn overlay_geometry_hook(app: &AppHandle) -> Option<overlay::GeometryHook> {
    let app = app.clone();
    Some(Box::new(move |x, y, width, height| {
        let Some(handle) = app.try_state::<OverlayStateHandle>() else {
            return;
        };
        handle.record_geometry(x, y, width, height);
    }))
}

/// 构造"悬浮窗获得/失去键盘焦点"的回调。
///
/// 焦点事件到达在窗口事件循环上，回调里只识别，状态更新和广播丢给
/// 异步运行时去做（`overlay::GeometryHook` 只写内存是同一个原因）。
fn overlay_focus_hook(app: &AppHandle) -> Option<overlay::FocusHook> {
    let app = app.clone();
    Some(Box::new(move |focused| {
        // 闭包是 `Fn`，焦点事件会触发多次，不能把捕获的 `app` move 进 future；
        // 每次调用先克隆一份再带走（`AppHandle` 是 Arc 语义，克隆廉价），
        // 和 `register_overlay_shortcuts` 的 handler 同一个模式。
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            let Some(handle) = app.try_state::<OverlayStateHandle>() else {
                return;
            };
            // 焦点事件很频繁（点一下窗口、Alt-Tab、最小化都会发），
            // 值没变就不打扰两个 webview 重新渲染。
            let current = handle.get();
            if current.focused == focused {
                return;
            }
            let next = handle.set(OverlayState { focused, ..current });
            // 焦点只影响界面提示，不落盘（布局里根本没有这个字段）。
            let _ = app.emit("overlay:state", next);
        });
    }))
}

/// 悬浮窗全局快捷键规格。
///
/// 统一用 Ctrl+Alt 前缀：避开 Win+Shift+S（系统截图）和各会议软件惯用的
/// Ctrl+Shift+* 静音/摄像头热键，也避开 Ctrl+Alt+方向键（部分 Intel 显卡
/// 驱动拿它转屏）。透明度用 = / - 而不是上下键就是这个原因。
/// 下标即动作号：`run_overlay_shortcut` 和前端 `OVERLAY_HOTKEYS`
/// （shared/overlay-control.ts）都按这个顺序排，两边必须一一对应。
const OVERLAY_SHORTCUTS: [&str; 8] = [
    "Control+Alt+O",     // 显隐
    "Control+Alt+P",     // 鼠标穿透
    "Control+Alt+S",     // 共享隐身
    "Control+Alt+Equal", // 更不透明
    "Control+Alt+Minus", // 更透明
    "Control+Alt+Q",     // 截图解题（笔试辅助）
    "Control+Alt+E",     // 收起成细条 / 展开
    "Control+Alt+Z",     // 开启/暂停录制（动作在悬浮窗 webview 里执行）
];

// ---------- 按住 Ctrl 临时交互（配合鼠标穿透） ----------

/// 穿透开启后悬浮窗一个像素都不吃事件,用户连拖动/滚动都做不了。补偿方案:
/// 按住 Ctrl = 临时恢复交互(可拖动、可滚动),松开 = 回到穿透。全局热键拿不到
/// "单独的 Ctrl",所以用 `GetAsyncKeyState` 轮询(30ms,开销可忽略)。
#[derive(Default)]
struct CtrlWatchHandle {
    inner: std::sync::Mutex<CtrlWatchInner>,
}

#[derive(Default)]
struct CtrlWatchInner {
    stop: Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
    handle: Option<std::thread::JoinHandle<()>>,
}

#[cfg(windows)]
fn ctrl_key_pressed() -> bool {
    use windows::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_CONTROL};
    // 最高位表示"自上次调用以来按过/正按着";配合 30ms 轮询等价于"现在按着"。
    unsafe { (GetAsyncKeyState(i32::from(VK_CONTROL.0)) as u16 & 0x8000) != 0 }
}

#[cfg(not(windows))]
fn ctrl_key_pressed() -> bool {
    // 非 Windows 不是打包目标;返回 false 等于该功能不存在,不影响穿透本身。
    false
}

fn ctrl_watch_loop(app: AppHandle, stop: std::sync::Arc<std::sync::atomic::AtomicBool>) {
    use std::sync::atomic::Ordering;
    use std::time::Duration;

    let mut pressed_last = false;
    loop {
        if stop.load(Ordering::Relaxed) {
            break;
        }
        let Some(window) = app.get_webview_window(overlay::OVERLAY_LABEL) else {
            // 窗口已销毁(理论路径),没有可恢复交互的对象了。
            break;
        };
        let pressed = ctrl_key_pressed();
        if pressed != pressed_last {
            pressed_last = pressed;
            let Some(handle) = app.try_state::<OverlayStateHandle>() else {
                break;
            };
            let current = handle.get();
            // 穿透已被关掉:另一条路径负责窗口事件,轮询退出。
            if !current.passthrough {
                break;
            }
            let next = handle.set(OverlayState {
                ctrl_interactive: pressed,
                ..current
            });
            // 按下 = 临时不穿透;松开 = 恢复穿透。
            let _ = window.set_ignore_cursor_events(!pressed);
            let _ = app.emit("overlay:state", next);
        }
        std::thread::sleep(Duration::from_millis(30));
    }
}

/// 穿透开着 → 保证轮询线程在跑;穿透关了/线程已死 → 清理并复位临时交互标记。
/// 每条可能改 passthrough 的路径(命令、热键、show 还原)之后都要调。
fn sync_ctrl_watch(app: &AppHandle, overlay_state: &OverlayState) {
    use std::sync::atomic::{AtomicBool, Ordering};

    let Some(watch) = app.try_state::<CtrlWatchHandle>() else {
        return;
    };
    let Ok(mut guard) = watch.inner.lock() else {
        return;
    };
    if overlay_state.passthrough {
        let running = guard
            .handle
            .as_ref()
            .map(|thread| !thread.is_finished())
            .unwrap_or(false);
        if !running {
            if let Some(finished) = guard.handle.take() {
                let _ = finished.join();
            }
            let stop = std::sync::Arc::new(AtomicBool::new(false));
            let thread_app = app.clone();
            let thread_stop = stop.clone();
            guard.stop = Some(stop);
            guard.handle = Some(std::thread::spawn(move || {
                ctrl_watch_loop(thread_app, thread_stop)
            }));
        }
        return;
    }
    let Some(stop) = guard.stop.take() else {
        return;
    };
    guard.handle = None; // JoinHandle 直接丢弃 = 分离,线程最迟 30ms 后自己退。
    drop(guard);
    stop.store(true, Ordering::Relaxed);
    // 关穿透时 Ctrl 可能正被按着:把"临时交互"标记复位,免得界面一直亮着。
    let Some(handle) = app.try_state::<OverlayStateHandle>() else {
        return;
    };
    let current = handle.get();
    if current.ctrl_interactive {
        let next = handle.set(OverlayState {
            ctrl_interactive: false,
            ..current
        });
        let _ = app.emit("overlay:state", next);
    }
}

/// 判断实际按下的热键是否就是某个规格串。
///
/// 不能拿 `shortcut.into_string()` 去和规格串比：它输出的是规范化小写
/// （`control+alt+keyo`），和这里写的 `Control+Alt+O` 对不上。改成把规格串
/// 解析成 `Shortcut` 再比 id，`Ctrl` / `Control`、`O` / `KeyO` 这类写法差异
/// 就都被解析器吸收掉了。
fn shortcut_matches(shortcut: &tauri_plugin_global_shortcut::Shortcut, spec: &str) -> bool {
    spec.parse::<tauri_plugin_global_shortcut::Shortcut>()
        .map(|parsed| parsed.id() == shortcut.id())
        .unwrap_or(false)
}

/// 悬浮窗全局快捷键。
///
/// 为什么必须是全局热键：悬浮窗以 `focused(false)` 创建且可能处于鼠标穿透状态，
/// 它自己收不到键盘事件；面试时焦点又在会议软件里，普通页面级快捷键一样收不到。
///
/// 注册失败只记日志、不阻断启动：热键被别的程序占了是常态，界面上的开关仍可用。
fn register_overlay_shortcuts(app: &AppHandle) {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutEvent, ShortcutState};

    let handler = |app: &AppHandle, shortcut: &Shortcut, event: ShortcutEvent| {
        // 只响应按下：不过滤的话一次按键会触发按下+抬起两次，开关等于没动。
        if event.state() != ShortcutState::Pressed {
            return;
        }
        // 先把是哪个动作认出来，再整体丢到异步运行时里执行。
        //
        // 为什么必须离开这个回调线程：热键回调跑在窗口事件循环上，而
        // `WebviewWindowBuilder::build()`（首次 show 会建窗口）从事件循环
        // 里调用会和 WebView2 的消息泵互锁——按下 Ctrl+Alt+O 之后整个应用
        // 卡死。和 overlay_* 命令都写成 async 是同一个原因。
        let Some(index) = OVERLAY_SHORTCUTS
            .iter()
            .position(|spec| shortcut_matches(shortcut, spec))
        else {
            return;
        };
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            run_overlay_shortcut(&app, index).await;
        });
    };

    if let Err(e) = app
        .global_shortcut()
        .on_shortcuts(OVERLAY_SHORTCUTS, handler)
    {
        let _ = append_panic_log(&format!("悬浮窗快捷键注册失败：{e}\n"));
    }
}

/// 执行第 `index` 个悬浮窗热键动作。已在异步运行时线程上，可以安全建窗口。
async fn run_overlay_shortcut(app: &AppHandle, index: usize) {
    // 截图解题不改悬浮窗状态，走自己的分支。悬浮窗开了共享隐身时
    // （WDA_EXCLUDEFROMCAPTURE）它自己不会出现在抓到的帧里，正好不挡题面。
    if index == 5 {
        let payload = match solve_screenshot_now(app).await {
            Ok(true) => ToastPayload {
                kind: "info".into(),
                message: "截图已发送，正在解题".into(),
            },
            Ok(false) => ToastPayload {
                kind: "error".into(),
                message: "未连接到服务，无法解题".into(),
            },
            Err(message) => ToastPayload {
                kind: "error".into(),
                message,
            },
        };
        let _ = app.emit("app-toast", payload);
        return;
    }

    // 开启/暂停录制（用户拍板：绝不结束面试）。动作本体在前端：悬浮窗
    // webview 订阅本事件后按会话与采集状态三选一——没录就走 开始会话 →
    // 系统声音采集 → 打开上传门禁；在录且本机在采就 暂停（停系统声音 →
    // 关上传门禁，会话保持 recording）；在录但采集已停就 恢复（起采集 →
    // 开门禁）。结束会话只属于主窗口「结束面试」（它读得到会话状态，
    // Rust 这边没有这份信息）。悬浮窗从未创建过时没有监听者，按键无效
    // ——那种场景主窗口才是操作入口。
    if index == 7 {
        let _ = app.emit("overlay:toggle-recording", ());
        return;
    }

    let Some(handle) = app.try_state::<OverlayStateHandle>() else {
        return;
    };
    let current = overlay::snapshot(app, &handle.get());
    let result = match index {
        0 => {
            if current.visible {
                capture_overlay_geometry(app, &handle);
                overlay::hide(app, &current)
            } else {
                overlay::show(
                    app,
                    &current,
                    &handle.layout(),
                    overlay_geometry_hook(app),
                    overlay_focus_hook(app),
                )
            }
        }
        1 => overlay::set_passthrough(app, &current, !current.passthrough),
        2 => overlay::set_content_protected(app, &current, !current.content_protected),
        3 => Ok(overlay::step_opacity(&current, true)),
        4 => Ok(overlay::step_opacity(&current, false)),
        6 => {
            // 收起/展开开关。收起前记下完整几何，展开时才知道恢复成多大、恢复到哪里。
            if current.collapsed {
                overlay::expand(app, &current, &handle.layout())
            } else {
                capture_overlay_geometry(app, &handle);
                overlay::collapse(app, &handle.get())
            }
        }
        _ => return,
    };
    match result {
        Ok(next) => {
            let next = handle.set(next);
            // 热键也可能切穿透/显隐(例如重启后 show 还原穿透态),同步轮询。
            sync_ctrl_watch(app, &next);
            handle.persist();
            // 广播给所有 webview：主窗口的开关面板和悬浮窗自身都要跟着变。
            let _ = app.emit("overlay:state", next);
        }
        Err(message) => {
            let _ = app.emit(
                "app-toast",
                ToastPayload {
                    kind: "error".into(),
                    message,
                },
            );
        }
    }
}

// ---------- 应用入口 ----------

/// 启动时把上次的悬浮窗布局装回内存。窗口此刻还不存在（惰性创建），
/// 所以这里只恢复状态，不建窗口，也不会碰到 WebView2 死锁。
fn restore_overlay_layout(app: &AppHandle) {
    let Some(handle) = app.try_state::<OverlayStateHandle>() else {
        return;
    };
    let dir = app_data_dir(app);
    let layout = overlay::load_layout(&dir);
    handle.restore(dir, layout);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    install_panic_logger();
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        // 悬浮窗热键：窗口不接受焦点、还可能鼠标穿透，只有系统级热键能控制它。
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        // 双开实例会各自分配 chunk_seq,服务端对账会把另一实例的分片判为
        // 序号占用并 BurnSeq,静默丢音频。第二个实例启动时聚焦已有主窗口
        // 并退出自身。该插件只需 Rust 侧注册,无需前端配合。
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .manage(EngineState {
            engine: Mutex::new(None),
            runtime: std::sync::Mutex::new(RuntimeSnapshot::default()),
            gen: AtomicU64::new(0),
        })
        .manage(SystemAudioState::default())
        .manage(OverlayStateHandle::default())
        .manage(CtrlWatchHandle::default())
        .setup(|app| {
            register_overlay_shortcuts(app.handle());
            restore_overlay_layout(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            settings_get,
            settings_set,
            settings_check,
            settings_prompt_get,
            settings_prompt_set,
            sessions_create,
            sessions_list,
            sessions_get,
            sessions_set_context,
            sessions_end,
            sessions_delete,
            history_transcripts,
            history_answers,
            history_reviews,
            history_generate_review,
            configs_list,
            configs_save,
            configs_activate,
            configs_delete,
            llm_fetch_models,
            live_connect,
            live_disconnect,
            live_runtime_state,
            live_start_session,
            live_set_radio_mode,
            live_regenerate,
            live_solve_screenshot,
            live_end_session,
            audio_chunk,
            system_audio_start,
            system_audio_stop,
            outbox_set_capture_active,
            outbox_snapshot,
            outbox_reconcile,
            overlay_snapshot,
            overlay_show,
            overlay_hide,
            overlay_toggle,
            overlay_set_passthrough,
            overlay_set_content_protected,
            overlay_set_always_on_top,
            overlay_set_opacity,
            overlay_collapse,
            overlay_expand,
        ])
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        // 退出前把悬浮窗布局落盘：拖动/缩放只写内存（Moved 一次拖动发几十条，
        // 每条都写盘纯属浪费），只拖过窗口就退出的那条路径靠这里兜住。
        .run(|app, event| {
            if matches!(
                event,
                tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
            ) {
                if let Some(handle) = app.try_state::<OverlayStateHandle>() {
                    handle.persist();
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::capture_gate_without_engine;
    use std::sync::{Mutex, MutexGuard, OnceLock};

    fn environment_lock() -> MutexGuard<'static, ()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(())).lock().unwrap()
    }

    #[test]
    fn server_url_is_hardcoded_to_local_loopback() {
        // 后端与客户端同机运行,地址不可配置:必须是 loopback 且不带凭据/路径/查询。
        let parsed = reqwest::Url::parse(crate::settings::SERVER_URL).expect("固定地址必须可解析");
        assert_eq!(parsed.scheme(), "http");
        assert_eq!(parsed.host_str(), Some("127.0.0.1"));
        assert_eq!(parsed.port(), Some(8000));
        assert_eq!(parsed.path(), "/");
        assert!(parsed.username().is_empty());
        assert!(parsed.password().is_none());
        assert!(parsed.query().is_none());
        assert!(parsed.fragment().is_none());
        assert!(!crate::settings::SERVER_URL.ends_with('/'));
    }

    #[test]
    fn stopping_capture_without_engine_is_idempotent() {
        assert_eq!(capture_gate_without_engine(false), Ok(()));
    }

    #[test]
    fn every_overlay_shortcut_spec_parses_and_is_unique() {
        use tauri_plugin_global_shortcut::Shortcut;

        let mut ids = Vec::new();
        for spec in super::OVERLAY_SHORTCUTS {
            let parsed: Shortcut = spec
                .parse()
                .unwrap_or_else(|e| panic!("热键 {spec} 解析失败：{e}"));
            ids.push(parsed.id());
        }
        // 重复的 id 会让后注册的那个静默顶掉前一个,对用户表现为"某个热键没反应"。
        let mut unique = ids.clone();
        unique.sort_unstable();
        unique.dedup();
        assert_eq!(unique.len(), ids.len(), "悬浮窗热键存在重复：{ids:?}");
    }

    #[test]
    fn shortcut_matching_ignores_spelling_differences() {
        use tauri_plugin_global_shortcut::Shortcut;

        let toggle: Shortcut = super::OVERLAY_SHORTCUTS[0].parse().expect("可解析");
        // Ctrl / Control、O / KeyO 都该落到同一个热键,否则 handler 会漏判。
        assert!(super::shortcut_matches(&toggle, "Ctrl+Alt+KeyO"));
        // 少一个修饰键就不是同一个热键,不能误触。
        assert!(!super::shortcut_matches(&toggle, "Alt+O"));
        // 无法解析的规格串只能返回 false,不许 panic 掉热键回调。
        assert!(!super::shortcut_matches(&toggle, "这不是热键"));
    }

    #[test]
    fn starting_capture_without_engine_still_fails() {
        assert!(capture_gate_without_engine(true).is_err());
    }

    #[test]
    fn panic_log_path_points_into_app_data_directory() {
        let _guard = environment_lock();
        let previous_appdata = std::env::var_os("APPDATA");
        if previous_appdata.is_none() {
            std::env::set_var("APPDATA", std::env::temp_dir());
        }
        let path = super::panic_log_path().expect("APPDATA should be set on Windows CI");
        assert!(path.components().any(|c| c
            .as_os_str()
            .to_string_lossy()
            .contains("com.aiinterview.desktop")));
        assert_eq!(path.file_name().unwrap(), "panic.log");
        if previous_appdata.is_none() {
            std::env::remove_var("APPDATA");
        }
    }

    #[test]
    fn append_panic_log_creates_file_and_appends_lines() {
        let _guard = environment_lock();
        let root = std::env::temp_dir().join(format!(
            "ai-interview-panic-log-{}-{}",
            std::process::id(),
            crate::engine::now_ms()
        ));
        let previous_appdata = std::env::var_os("APPDATA");
        std::env::set_var("APPDATA", &root);
        super::append_panic_log("first\n").expect("first append should succeed");
        super::append_panic_log("second\n").expect("second append should succeed");
        if let Some(previous) = previous_appdata {
            std::env::set_var("APPDATA", previous);
        } else {
            std::env::remove_var("APPDATA");
        }

        let path = root.join("com.aiinterview.desktop").join("panic.log");
        let body = std::fs::read_to_string(&path).expect("panic log written");
        assert_eq!(body, "first\nsecond\n");
        std::fs::remove_dir_all(root).expect("remove test dir");
    }
}
