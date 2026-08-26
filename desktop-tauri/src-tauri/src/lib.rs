//! AI 面试助手 PC 客户端(Tauri 2)— Rust 侧入口。
//! 命令层:设置/会话/历史/配置 REST 透传 + Live 引擎生命周期 + 音频分片。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backoff;
mod engine;
mod outbox;
mod protocol;
mod reconcile;
mod rest;
mod settings;
mod system_audio;
mod ws_client;

use std::path::PathBuf;
use std::sync::Arc;

use tauri::{AppHandle, Emitter, Manager, State};
use tokio::sync::Mutex;

use engine::{Engine, EngineCommand, EngineEvent, NewChunk};
use settings::AppSettings;
use system_audio::SystemAudioState;

/// 全局引擎状态(单活动会话)。
struct EngineState {
    engine: Mutex<Option<Engine>>,
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

/// 构造 REST 上下文;未配置时返回错误消息。
fn make_ctx(app: &AppHandle) -> Result<rest::RestContext, String> {
    let dir = app_data_dir(app);
    let settings = settings::load_settings(&dir);
    if settings.server_url.is_empty() {
        return Err("未配置服务器地址,请先在设置中填写".into());
    }
    let token =
        settings::load_token().ok_or_else(|| "未配置访问令牌,请先在设置中填写".to_string())?;
    Ok(rest::RestContext {
        server_url: settings.server_url,
        token,
    })
}

fn rest_err(e: rest::RestError) -> String {
    e.to_string()
}

fn is_loopback_host(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost")
        || host
            .parse::<std::net::IpAddr>()
            .is_ok_and(|address| address.is_loopback())
}

fn validate_server_url(value: &str) -> Result<String, String> {
    let trimmed = value.trim().trim_end_matches('/').to_string();
    if trimmed.is_empty() {
        return Ok(trimmed);
    }
    let parsed = reqwest::Url::parse(&trimmed)
        .map_err(|_| "服务器地址必须是有效的 http:// 或 https:// URL".to_string())?;
    if parsed.scheme() != "http" && parsed.scheme() != "https" {
        return Err("服务器地址必须以 http:// 或 https:// 开头".into());
    }
    if !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || parsed.path() != "/"
    {
        return Err("服务器地址不能包含凭据、路径、查询参数或片段".into());
    }
    let host = parsed
        .host_str()
        .ok_or_else(|| "服务器地址缺少主机名".to_string())?;
    if parsed.scheme() == "http" && !is_loopback_host(host) {
        return Err("非本机服务器必须使用 https://，避免令牌和音频明文传输".into());
    }
    Ok(trimmed)
}

// ---------- 引擎事件 → 前端 ----------

fn emit_engine_event(app: &AppHandle) -> Arc<dyn Fn(EngineEvent) + Send + Sync> {
    let app = app.clone();
    Arc::new(move |event: EngineEvent| {
        // 序列化后统一以 engine:event 名称广播
        if let Ok(payload) = serde_json::to_value(&event) {
            let _ = app.emit("engine:event", payload);
        }
    })
}

// ---------- 设置命令 ----------

#[tauri::command]
fn settings_get(app: AppHandle) -> Result<AppSettings, String> {
    let dir = app_data_dir(&app);
    let settings = settings::load_settings(&dir);
    Ok(AppSettings {
        server_url: settings.server_url,
        has_token: settings::load_token().is_some(),
    })
}

#[tauri::command]
fn settings_set(
    app: AppHandle,
    server_url: String,
    token: Option<String>,
) -> Result<AppSettings, String> {
    let trimmed = validate_server_url(&server_url)?;
    let dir = app_data_dir(&app);
    settings::save_settings(
        &dir,
        &settings::StoredSettings {
            server_url: trimmed.clone(),
        },
    );
    if let Some(t) = token {
        let t = t.trim().to_string();
        if !t.is_empty() {
            settings::save_token(&t)?;
        }
    }
    Ok(AppSettings {
        server_url: trimmed,
        has_token: settings::load_token().is_some(),
    })
}

#[tauri::command]
async fn settings_check(app: AppHandle) -> Result<serde_json::Value, String> {
    let dir = app_data_dir(&app);
    let settings = settings::load_settings(&dir);
    if settings.server_url.is_empty() {
        return Ok(serde_json::json!({ "ok": false, "reason": "未配置服务器地址" }));
    }
    if let Err(e) = rest::check_health(&settings.server_url).await {
        return Ok(serde_json::json!({ "ok": false, "reason": rest_err(e) }));
    }
    let Some(token) = settings::load_token() else {
        return Ok(serde_json::json!({ "ok": false, "reason": "服务可达,但未配置访问令牌" }));
    };
    let ctx = rest::RestContext {
        server_url: settings.server_url,
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
    let mut guard = state.engine.lock().await;
    let system_stop_result = system_audio_state.stop().await;
    if let Some(old) = guard.take() {
        old.deactivate_capture(protocol::CAPTURE_INTERRUPTED_REASON)
            .await;
        old.stop();
    }
    system_stop_result?;
    let engine = Engine::spawn(app_data_dir(&app), ctx, session_id, emit_engine_event(&app));
    *guard = Some(engine);
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
) -> Result<bool, String> {
    let guard = state.engine.lock().await;
    let Some(engine) = guard.as_ref() else {
        return Ok(false);
    };
    Ok(engine.send(serde_json::json!({
        "v": 1, "type": "regenerate_answer", "question": question, "use_search": use_search
    })))
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
    system_audio_state
        .start(engine.cmd_tx.clone(), emit_engine_event(&app))
        .await
}

/// 停止 WASAPI loopback；不足 2.5 秒的尾片由采集线程直接丢弃。
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

// ---------- 应用入口 ----------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    install_panic_logger();
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
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
        })
        .manage(SystemAudioState::default())
        .invoke_handler(tauri::generate_handler![
            settings_get,
            settings_set,
            settings_check,
            settings_prompt_get,
            settings_prompt_set,
            sessions_create,
            sessions_list,
            sessions_get,
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
            live_start_session,
            live_set_radio_mode,
            live_regenerate,
            live_end_session,
            audio_chunk,
            system_audio_start,
            system_audio_stop,
            outbox_set_capture_active,
            outbox_snapshot,
            outbox_reconcile,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::{capture_gate_without_engine, validate_server_url};
    use std::sync::{Mutex, MutexGuard, OnceLock};

    fn environment_lock() -> MutexGuard<'static, ()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(())).lock().unwrap()
    }

    #[test]
    fn server_url_requires_https_except_for_loopback_development() {
        assert_eq!(
            validate_server_url("http://127.0.0.1:8000"),
            Ok("http://127.0.0.1:8000".into())
        );
        assert_eq!(
            validate_server_url("http://localhost:8000/"),
            Ok("http://localhost:8000".into())
        );
        assert_eq!(
            validate_server_url("https://api.example.com/"),
            Ok("https://api.example.com".into())
        );
        assert!(validate_server_url("http://api.example.com").is_err());
        assert!(validate_server_url("https://user:pass@api.example.com").is_err());
        assert!(validate_server_url("https://api.example.com/v1").is_err());
    }

    #[test]
    fn stopping_capture_without_engine_is_idempotent() {
        assert_eq!(capture_gate_without_engine(false), Ok(()));
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
