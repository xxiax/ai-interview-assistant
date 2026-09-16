//! WebSocket v1 客户端状态机 + 内嵌音频发送泵。
//! 对应 desktop/src/main/net/ws-client.ts + engine/pump.ts。
//!
//! 生命周期:idle → connecting → authenticating(5s) → synchronizing → ready
//!           → { closed | reconnecting }
//!
//! 关键语义:
//! - open 后立即发 authenticate{token,last_event_id},5 秒内必须完成
//! - sync_complete 之前不发任何业务消息
//! - 4401/4403/4404 致命:停止重连,上报
//! - 4429:60s 专用等待;1000:会话结束;1001:服务关闭 → 重连后对账
//! - 20s ping,10s 无 pong 视为半开连接,强制重连
//! - 音频泵:严格按 chunk_seq 串行,在途上限 6,重试带退避

use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message as WsMessage;

use crate::backoff;
use crate::engine::{
    self, now_ms, rand_f64, EngineCommand, EngineEvent, EngineEventEmitter, SharedManifest,
    MAX_INFLIGHT_QUEUED,
};
use crate::outbox::{self, CancelIntent, OutboxState, Transition};
use crate::protocol::{is_fatal_close_code, PROTOCOL_VERSION};

const AUTH_TIMEOUT_MS: u64 = 5_000;
const PING_INTERVAL_MS: u64 = 20_000;
const PONG_TIMEOUT_MS: u64 = 10_000;
/// TCP+TLS 握手硬上限:不设的话 OS 级 TCP 超时可挂 ~2 分钟,期间命令队列
/// 完全不被 poll(含 Stop),表现为「点停止无响应」。
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// sync_complete 后同步对账的上限:对账要「先于泵送」完成(防止旧分片抢发),
/// 但 50 页 × 15s 的 REST 最坏情况不能让 WS 读循环停摆,超时则放弃本次对账。
const RECONCILE_BUDGET: Duration = Duration::from_secs(30);
/// 游标合并写窗口:同一批事件推进多次游标时只落盘一次。
const CURSOR_FLUSH_INTERVAL_MS: u64 = 500;

pub struct WsClientOptions {
    pub server_url: String,
    pub session_id: String,
    pub token: String,
    pub last_event_id: i64,
}

fn ws_url(server_url: &str, session_id: &str) -> Result<String, String> {
    let mut url = reqwest::Url::parse(server_url)
        .map_err(|_| "服务器地址无法转换为 WebSocket URL".to_string())?;
    let websocket_scheme = match url.scheme() {
        "https" => "wss",
        "http" => "ws",
        _ => return Err("服务器地址必须使用 http:// 或 https://".into()),
    };
    url.set_scheme(websocket_scheme)
        .map_err(|_| "服务器地址无法转换为 WebSocket URL".to_string())?;
    url.set_path(&format!("/ws/{session_id}"));
    url.set_query(None);
    url.set_fragment(None);
    Ok(url.to_string())
}

/// 异步对账(spawn 后台任务,不阻塞 WS 循环)。
fn spawn_reconcile(
    ctx: &crate::rest::RestContext,
    session_id: &str,
    manifest: &SharedManifest,
    state_dir: &Path,
    audio_dir: &Path,
    emit: &EngineEventEmitter,
) {
    let ctx = ctx.clone();
    let session_id = session_id.to_string();
    let manifest = manifest.clone();
    let state_dir = state_dir.to_path_buf();
    let audio_dir = audio_dir.to_path_buf();
    let emit = emit.clone();
    tokio::spawn(async move {
        engine::reconcile(&ctx, &session_id, &manifest, &state_dir, &audio_dir, &emit).await;
    });
}

fn pc_upload_allowed(capture_active: bool, session_status: &str, radio_mode: &str) -> bool {
    capture_active && session_status == "recording" && radio_mode != "mobile"
}

fn cancel_audio_source_message(intent: &CancelIntent) -> Value {
    json!({
        "v": PROTOCOL_VERSION,
        "type": "cancel_audio_source",
        "source": intent.source,
        "through_chunk_seq": intent.through_chunk_seq,
        "reason": crate::protocol::normalize_cancel_reason(&intent.reason),
    })
}

fn speech_end_message(source: &str, through_chunk_seq: i64) -> Value {
    json!({
        "v": PROTOCOL_VERSION,
        "type": "speech_end",
        "source": source,
        "through_chunk_seq": through_chunk_seq,
    })
}

/// 发送持久化取消边界。只要还有 CancelPending 就保留意图，供下次重连补发。
async fn send_pending_cancel(
    write: &mut WsWrite,
    manifest: &SharedManifest,
    state_dir: &Path,
    emit: &EngineEventEmitter,
) -> bool {
    let intent = {
        let m = manifest.lock().await;
        m.cancel_intent.clone()
    };
    let Some(intent) = intent else {
        return true;
    };
    if intent.through_chunk_seq < 0 {
        return true;
    }
    if write
        .send(WsMessage::Text(
            cancel_audio_source_message(&intent).to_string(),
        ))
        .await
        .is_err()
    {
        return false;
    }

    let mut m = manifest.lock().await;
    if outbox::clear_cancel_intent_if_resolved(&mut m) {
        let stats = outbox::stats_of(&m);
        engine::persist_manifest(state_dir, &m);
        drop(m);
        emit(EngineEvent::Outbox { stats });
    }
    true
}

/// 引擎主循环:WS 会话 + 泵驱动 + 命令分发,直到致命错误/停止。
pub async fn run_ws(
    opts: WsClientOptions,
    ctx: crate::rest::RestContext,
    mut cmd_rx: mpsc::UnboundedReceiver<EngineCommand>,
    manifest: SharedManifest,
    state_dir: PathBuf,
    audio_dir: PathBuf,
    emit: EngineEventEmitter,
) {
    let mut last_event_id = opts.last_event_id;
    let mut attempt: u32 = 0;
    let mut stopped = false;
    let mut current_session_status = String::new();
    let mut current_radio_mode = String::from("pc");
    // 安全默认：重进页面或应用重启后，不自动恢复旧音频上传。
    let mut capture_active = false;
    let mut capture_inactive_reason = crate::protocol::CAPTURE_INTERRUPTED_REASON.to_string();
    // 重连退避期无法执行的命令必须跨连接保留。尤其 end_session 若被吞，
    // 调用方已收到 channel send 成功，REST 兜底不会再触发。
    let mut pending_commands = VecDeque::new();

    // 引擎启动即推送序号水位(含本地 outbox 积压),前端 sequencer 以此为权威对齐。
    // 水位 = max(本地 manifest, 服务端已见最大 seq + 1),避免重进会话时新分片撞旧 seq。
    {
        let server_next = engine::server_next_chunk_seq(&ctx, &opts.session_id).await;
        let next = {
            let mut m = manifest.lock().await;
            let previous_next = m.next_chunk_seq;
            let previous_intent = m.cancel_intent.clone();
            m.next_chunk_seq = m.next_chunk_seq.max(server_next);
            let cancel_through = m.next_chunk_seq.saturating_sub(1);
            if let Some(intent) = m.cancel_intent.as_mut() {
                intent.through_chunk_seq = intent.through_chunk_seq.max(cancel_through);
            }
            if m.next_chunk_seq != previous_next || m.cancel_intent != previous_intent {
                engine::persist_manifest(&state_dir, &m);
            }
            emit(EngineEvent::Outbox {
                stats: outbox::stats_of(&m),
            });
            m.next_chunk_seq
        };
        emit(EngineEvent::SeqWatermark {
            next_chunk_seq: next,
        });
    }

    'outer: loop {
        if stopped {
            break;
        }
        emit(EngineEvent::Connection {
            phase: engine::phase::CONNECTING,
            note: None,
            retry_after_ms: None,
        });

        let url = match ws_url(&opts.server_url, &opts.session_id) {
            Ok(url) => url,
            Err(message) => {
                emit(EngineEvent::Connection {
                    phase: engine::phase::CLOSED,
                    note: Some(message),
                    retry_after_ms: None,
                });
                break;
            }
        };
        let (ws_stream, _) =
            match tokio::time::timeout(CONNECT_TIMEOUT, tokio_tungstenite::connect_async(&url))
                .await
            {
                // 超时按普通连接失败进入退避;被丢弃的连接由对端/OS 最终回收。
                Err(_elapsed) => {
                    let delay = backoff::backoff_delay_ms(attempt, rand_f64()).max(500);
                    attempt += 1;
                    emit(EngineEvent::Connection {
                        phase: engine::phase::RECONNECTING,
                        note: Some("连接超时(10 秒无响应)".into()),
                        retry_after_ms: Some(delay),
                    });
                    if !sleep_interruptible(
                        &mut cmd_rx,
                        delay,
                        &mut stopped,
                        &mut capture_active,
                        &mut capture_inactive_reason,
                        &mut pending_commands,
                        &opts.session_id,
                        &manifest,
                        &state_dir,
                        &audio_dir,
                        &emit,
                    )
                    .await
                    {
                        break;
                    }
                    continue;
                }
                Ok(Err(e)) => {
                    let delay = backoff::backoff_delay_ms(attempt, rand_f64()).max(500);
                    attempt += 1;
                    emit(EngineEvent::Connection {
                        phase: engine::phase::RECONNECTING,
                        note: Some(format!("连接失败:{e}")),
                        retry_after_ms: Some(delay),
                    });
                    if !sleep_interruptible(
                        &mut cmd_rx,
                        delay,
                        &mut stopped,
                        &mut capture_active,
                        &mut capture_inactive_reason,
                        &mut pending_commands,
                        &opts.session_id,
                        &manifest,
                        &state_dir,
                        &audio_dir,
                        &emit,
                    )
                    .await
                    {
                        break;
                    }
                    continue;
                }
                Ok(Ok(v)) => v,
            };

        emit(EngineEvent::Connection {
            phase: engine::phase::AUTHENTICATING,
            note: None,
            retry_after_ms: None,
        });

        let (mut write, mut read) = ws_stream.split();

        // 首包认证
        let auth_msg = json!({
            "v": PROTOCOL_VERSION,
            "type": "authenticate",
            "token": opts.token,
            "last_event_id": last_event_id,
        });
        if write
            .send(WsMessage::Text(auth_msg.to_string()))
            .await
            .is_err()
        {
            // 写失败也必须退避:立即 continue 会绕过 cmd_rx 轮询并自旋热循环。
            let delay = backoff::backoff_delay_ms(attempt, rand_f64()).max(500);
            attempt += 1;
            if !sleep_interruptible(
                &mut cmd_rx,
                delay,
                &mut stopped,
                &mut capture_active,
                &mut capture_inactive_reason,
                &mut pending_commands,
                &opts.session_id,
                &manifest,
                &state_dir,
                &audio_dir,
                &emit,
            )
            .await
            {
                break;
            }
            continue;
        }

        let mut authenticated = false;
        let auth_deadline = Instant::now() + Duration::from_millis(AUTH_TIMEOUT_MS);
        let mut last_ping_at: Option<Instant> = None;
        let mut pong_deadline: Option<Instant> = None;
        let mut close_code_seen: Option<u16> = None;
        let mut cursor_throttle = CursorThrottle::default();
        // 同类错误码 5s 抑制窗口:批量失败只上报一次,避免前端 toast 刷屏
        let mut last_error_emit: std::collections::HashMap<String, Instant> =
            std::collections::HashMap::new();

        loop {
            let next_timer = if !authenticated {
                Some(auth_deadline)
            } else {
                pong_deadline.or_else(|| {
                    Some(
                        last_ping_at.unwrap_or_else(Instant::now)
                            + Duration::from_millis(PING_INTERVAL_MS),
                    )
                })
            };
            // 心跳/认证定时器与游标节流定时器取更早者;两者都空闲时游标分支禁用。
            let cursor_deadline = cursor_throttle.deadline();
            let next_timer = match (next_timer, cursor_deadline) {
                (Some(a), Some(b)) => Some(a.min(b)),
                (a, b) => a.or(b),
            };

            tokio::select! {
                _ = tokio::time::sleep_until(tokio::time::Instant::from_std(next_timer.unwrap_or_else(|| Instant::now() + Duration::from_secs(3600)))) => {
                    // 游标合并窗口先到期:落盘后继续等心跳/认证定时器
                    cursor_throttle.on_timer(&state_dir, &opts.session_id, last_event_id);
                    if !authenticated {
                        emit(EngineEvent::Connection {
                            phase: engine::phase::RECONNECTING,
                            note: Some("认证超时,重试中".into()),
                            retry_after_ms: None,
                        });
                        break;
                    }
                    if let Some(deadline) = pong_deadline {
                        if Instant::now() >= deadline {
                            emit(EngineEvent::Connection {
                                phase: engine::phase::RECONNECTING,
                                note: Some("心跳超时,重连中".into()),
                                retry_after_ms: None,
                            });
                            break;
                        }
                    }
                    // 发 ping
                    if write.send(WsMessage::Text(json!({"v": PROTOCOL_VERSION, "type": "ping"}).to_string())).await.is_ok() {
                        last_ping_at = Some(Instant::now());
                        pong_deadline = Some(Instant::now() + Duration::from_millis(PONG_TIMEOUT_MS));
                    } else {
                        break;
                    }
                }
                msg = read.next() => {
                    match msg {
                        Some(Ok(WsMessage::Text(text))) => {
                            let parsed: Value = match serde_json::from_str(&text) {
                                Ok(v) => v,
                                Err(_) => continue,
                            };
                            let msg_type = parsed.get("type").and_then(|t| t.as_str()).unwrap_or("").to_string();
                            match msg_type.as_str() {
                                "sync_complete" => {
                                    authenticated = true;
                                    attempt = 0;
                                    last_ping_at = None;
                                    pong_deadline = None;
                                    if let Some(latest) = parsed.get("latest_event_id").and_then(|v| v.as_i64()) {
                                        last_event_id = last_event_id.max(latest);
                                        emit(EngineEvent::SyncComplete { latest_event_id: latest });
                                    }
                                    current_session_status = parsed
                                        .get("status")
                                        .and_then(|v| v.as_str())
                                        .unwrap_or("")
                                        .to_string();
                                    current_radio_mode = parsed
                                        .get("radio_mode")
                                        .and_then(|v| v.as_str())
                                        .unwrap_or("pc")
                                        .to_string();
                                    emit(EngineEvent::SessionState {
                                        status: current_session_status.clone(),
                                        radio_mode: current_radio_mode.clone(),
                                    });
                                    emit(EngineEvent::Connection {
                                        phase: engine::phase::READY,
                                        note: None,
                                        retry_after_ms: None,
                                    });
                                    // 取消意图优先于对账和任何音频发送。服务端可能先重放旧
                                    // queued ack，状态机仍会保留 CancelPending，直到终态 ack。
                                    if !send_pending_cancel(
                                        &mut write,
                                        &manifest,
                                        &state_dir,
                                        &emit,
                                    )
                                    .await
                                    {
                                        break;
                                    }
                                    // 必须先完成对账再发送积压。若先异步启动对账再泵送，旧的冲突
                                    // 分片会抢先发到服务端，造成每次重进都重复报序号占用。
                                    // 但对账同步 await 会让 read/cmd/心跳全部停摆，因此设 30s
                                    // 上限;超时放弃本次对账(cancel 已在上方补发)，进正常泵送，
                                    // 下次重连/手动对账再收敛。
                                    if tokio::time::timeout(
                                        RECONCILE_BUDGET,
                                        engine::reconcile(
                                            &ctx,
                                            &opts.session_id,
                                            &manifest,
                                            &state_dir,
                                            &audio_dir,
                                            &emit,
                                        ),
                                    )
                                    .await
                                    .is_err()
                                    {
                                        eprintln!(
                                            "ws: sync 后对账超过 {} 秒,放弃本次对账",
                                            RECONCILE_BUDGET.as_secs()
                                        );
                                    }
                                    try_pump(
                                        &mut write,
                                        &manifest,
                                        &state_dir,
                                        &audio_dir,
                                        &emit,
                                        pc_upload_allowed(
                                            capture_active,
                                            &current_session_status,
                                            &current_radio_mode,
                                        ),
                                    )
                                    .await;
                                }
                                "pong" => {
                                    pong_deadline = None;
                                }
                                "error" => {
                                    let code = parsed.get("code").and_then(|v| v.as_str()).unwrap_or("").to_string();
                                    let message = parsed.get("message").and_then(|v| v.as_str()).unwrap_or("").to_string();
                                    let retry_after = parsed.get("retry_after_seconds").and_then(|v| v.as_u64());
                                    let chunk_id = parsed.get("chunk_id").and_then(|v| v.as_str()).map(str::to_string);
                                    handle_server_error(
                                        &code,
                                        chunk_id.as_deref(),
                                        retry_after,
                                        &manifest,
                                        &state_dir,
                                        &audio_dir,
                                        &emit,
                                    )
                                    .await;
                                    if code == "audio_sequence_gap" {
                                        spawn_reconcile(&ctx, &opts.session_id, &manifest, &state_dir, &audio_dir, &emit);
                                    }
                                    let resume_after_terminal_error = code == "invalid_audio_chunk"
                                        && pc_upload_allowed(
                                            capture_active,
                                            &current_session_status,
                                            &current_radio_mode,
                                        );
                                    // 抑制窗口:同类错误 5s 内只向前端发一次(outbox 每分片都会触发)
                                    let suppressed = match last_error_emit.get(&code) {
                                        Some(at) => at.elapsed() < Duration::from_secs(5),
                                        None => false,
                                    };
                                    if !suppressed {
                                        last_error_emit.insert(code.clone(), Instant::now());
                                        emit(EngineEvent::ServerError { code, message, retry_after_seconds: retry_after, chunk_id });
                                    }
                                    if resume_after_terminal_error {
                                        try_pump(
                                            &mut write,
                                            &manifest,
                                            &state_dir,
                                            &audio_dir,
                                            &emit,
                                            true,
                                        )
                                        .await;
                                    }
                                }
                                "session_state"
                                | "chunk_ack"
                                | "transcript"
                                | "transcript_partial"
                                | "answer"
                                | "answer_stream" => {
                                    let event_id = parsed.get("event_id").and_then(|v| v.as_i64()).unwrap_or(0);
                                    if event_id > 0 {
                                        if event_id <= last_event_id {
                                            continue; // 去重
                                        }
                                        last_event_id = event_id;
                                        cursor_throttle.advance(&state_dir, &opts.session_id, event_id);
                                    }
                                    let mut should_resume_pump = false;
                                    if msg_type == "chunk_ack" {
                                        apply_chunk_ack(&parsed, &manifest, &state_dir, &audio_dir, &emit).await;
                                        should_resume_pump = true;
                                    } else if msg_type == "session_state" {
                                        current_session_status = parsed
                                            .get("status")
                                            .and_then(|v| v.as_str())
                                            .unwrap_or("")
                                            .to_string();
                                        current_radio_mode = parsed
                                            .get("radio_mode")
                                            .and_then(|v| v.as_str())
                                            .unwrap_or("pc")
                                            .to_string();
                                        emit(EngineEvent::SessionState {
                                            status: current_session_status.clone(),
                                            radio_mode: current_radio_mode.clone(),
                                        });
                                        should_resume_pump = true;
                                    }
                                    emit(EngineEvent::ServerMessage(parsed));
                                    if should_resume_pump {
                                        try_pump(
                                            &mut write,
                                            &manifest,
                                            &state_dir,
                                            &audio_dir,
                                            &emit,
                                            pc_upload_allowed(
                                                capture_active,
                                                &current_session_status,
                                                &current_radio_mode,
                                            ),
                                        )
                                        .await;
                                    }
                                }
                                _ => {}
                            }
                        }
                        Some(Ok(WsMessage::Close(frame))) => {
                            close_code_seen = Some(frame.as_ref().map(|f| u16::from(f.code)).unwrap_or(1005));
                            break;
                        }
                        Some(Ok(_)) => {} // 二进制等忽略
                        Some(Err(_)) | None => {
                            close_code_seen = Some(1006);
                            break;
                        }
                    }
                }
                cmd = recv_engine_command(
                    &mut cmd_rx,
                    &mut pending_commands,
                    authenticated,
                ) => {
                    match cmd {
                        Some(EngineCommand::Send(value)) => {
                            if !authenticated {
                                pending_commands.push_back(EngineCommand::Send(value));
                                continue; // ready 前保留，sync_complete 后发送
                            }
                            let command_type = value
                                .get("type")
                                .and_then(|v| v.as_str())
                                .unwrap_or("")
                                .to_string();
                            let requested_mode = match command_type.as_str() {
                                "start_session" => value
                                    .get("radio_mode")
                                    .and_then(|v| v.as_str())
                                    .map(str::to_string),
                                "set_radio_mode" => value
                                    .get("mode")
                                    .and_then(|v| v.as_str())
                                    .map(str::to_string),
                                _ => None,
                            };
                            if write.send(WsMessage::Text(value.to_string())).await.is_err() {
                                pending_commands.push_front(EngineCommand::Send(value));
                                break;
                            }
                            // 同一 WebSocket 上消息严格有序；本地先切换泵门禁，避免 set_radio_mode
                            // 与录音器 stop() 冲出的尾分片交错后被错误上传。
                            if let Some(mode) = requested_mode {
                                current_radio_mode = mode;
                            }
                            if command_type == "start_session" {
                                current_session_status = "recording".into();
                            } else if command_type == "end_session" {
                                current_session_status = "ended".into();
                            }
                        }
                        Some(EngineCommand::AddChunk(chunk)) => {
                            let accepted = add_chunk(
                                &chunk,
                                &opts.session_id,
                                &manifest,
                                &state_dir,
                                &audio_dir,
                                &emit,
                                capture_active,
                            )
                            .await;
                            if accepted && authenticated {
                                try_pump(
                                    &mut write,
                                    &manifest,
                                    &state_dir,
                                    &audio_dir,
                                    &emit,
                                    pc_upload_allowed(
                                        capture_active,
                                        &current_session_status,
                                        &current_radio_mode,
                                    ),
                                )
                                .await;
                            }
                        }
                        Some(EngineCommand::SpeechEnd { source }) => {
                            if !authenticated {
                                pending_commands.push_back(EngineCommand::SpeechEnd { source });
                                continue;
                            }
                            if !pc_upload_allowed(
                                capture_active,
                                &current_session_status,
                                &current_radio_mode,
                            ) {
                                continue;
                            }
                            let through_chunk_seq = {
                                let m = manifest.lock().await;
                                m.next_chunk_seq - 1
                            };
                            if through_chunk_seq < 0 {
                                continue;
                            }
                            let message = speech_end_message(&source, through_chunk_seq);
                            if write
                                .send(WsMessage::Text(message.to_string()))
                                .await
                                .is_err()
                            {
                                pending_commands.push_front(EngineCommand::SpeechEnd {
                                    source: message["source"]
                                        .as_str()
                                        .unwrap_or("pc")
                                        .to_string(),
                                });
                                break;
                            }
                        }
                        Some(EngineCommand::SetCaptureActive { active, reason, ack }) => {
                            capture_active = active;
                            emit(EngineEvent::CaptureState { active });
                            if !active {
                                capture_inactive_reason = if reason.is_empty() {
                                    "capture_stopped".into()
                                } else {
                                    reason
                                };
                                engine::apply_capture_inactive(
                                    &capture_inactive_reason,
                                    &manifest,
                                    &state_dir,
                                    &audio_dir,
                                    &emit,
                                )
                                .await;
                            }
                            if let Some(ack) = ack {
                                let _ = ack.send(());
                            }
                            if !active {
                                if authenticated
                                    && !send_pending_cancel(
                                        &mut write,
                                        &manifest,
                                        &state_dir,
                                        &emit,
                                    )
                                    .await
                                {
                                    break;
                                }
                            } else if authenticated {
                                try_pump(
                                    &mut write,
                                    &manifest,
                                    &state_dir,
                                    &audio_dir,
                                    &emit,
                                    pc_upload_allowed(
                                        capture_active,
                                        &current_session_status,
                                        &current_radio_mode,
                                    ),
                                )
                                .await;
                            }
                        }
                        Some(EngineCommand::Tick) => {
                            if authenticated {
                                try_pump(
                                    &mut write,
                                    &manifest,
                                    &state_dir,
                                    &audio_dir,
                                    &emit,
                                    pc_upload_allowed(
                                        capture_active,
                                        &current_session_status,
                                        &current_radio_mode,
                                    ),
                                )
                                .await;
                            } else {
                                pending_commands.push_back(EngineCommand::Tick);
                            }
                        }
                        Some(EngineCommand::Reconcile) => {
                            if authenticated {
                                spawn_reconcile(&ctx, &opts.session_id, &manifest, &state_dir, &audio_dir, &emit);
                            } else {
                                pending_commands.push_back(EngineCommand::Reconcile);
                            }
                        }
                        Some(EngineCommand::Snapshot) => {
                            let stats = {
                                let m = manifest.lock().await;
                                outbox::stats_of(&m)
                            };
                            emit(EngineEvent::Outbox { stats });
                        }
                        Some(EngineCommand::Stop) | None => {
                            let _ = write.close().await;
                            cursor_throttle.flush(&state_dir, &opts.session_id, last_event_id);
                            break 'outer;
                        }
                    }
                }
            }
        }

        // 连接层结束:根据 close 码决定后续
        let code = close_code_seen.unwrap_or(1006);
        cursor_throttle.flush(&state_dir, &opts.session_id, last_event_id);
        if stopped {
            break;
        }
        if code == crate::protocol::WS_CLOSE_NORMAL {
            emit(EngineEvent::Connection {
                phase: engine::phase::CLOSED,
                note: Some("会话已结束".into()),
                retry_after_ms: None,
            });
            // ended:所有未完成分片标记终态
            {
                let mut m = manifest.lock().await;
                for i in 0..m.records.len() {
                    if let Some(moved) =
                        outbox::transition(&m.records[i], &Transition::SessionEnded)
                    {
                        m.records[i] = moved;
                    }
                }
                for r in &m.records {
                    if matches!(r.state, OutboxState::Done | OutboxState::TerminalError) {
                        let _ = std::fs::remove_file(audio_dir.join(&r.file));
                    }
                }
                let stats = outbox::stats_of(&m);
                engine::persist_manifest(&state_dir, &m);
                drop(m);
                emit(EngineEvent::Outbox { stats });
            }
            emit(EngineEvent::SessionEnded);
            break;
        }
        if is_fatal_close_code(code) {
            let note = match code {
                crate::protocol::WS_CLOSE_AUTH_FAILED => "认证失败:Token 无效",
                crate::protocol::WS_CLOSE_ORIGIN_DENIED => "连接被 Origin 白名单拒绝",
                _ => "会话不存在",
            };
            emit(EngineEvent::Connection {
                phase: engine::phase::CLOSED,
                note: Some(note.to_string()),
                retry_after_ms: None,
            });
            break;
        }

        // 可重连:常规退避
        let delay = if code == crate::protocol::WS_CLOSE_AUTH_RATE_LIMITED {
            backoff::auth_rate_limit_delay_ms(rand_f64())
        } else {
            let d = backoff::backoff_delay_ms(attempt, rand_f64()).max(500);
            attempt += 1;
            d
        };
        emit(EngineEvent::Connection {
            phase: engine::phase::RECONNECTING,
            note: Some(if code == crate::protocol::WS_CLOSE_GOING_AWAY {
                "服务已关闭,等待恢复".into()
            } else {
                "连接断开,重连中".into()
            }),
            retry_after_ms: Some(delay),
        });
        if !sleep_interruptible(
            &mut cmd_rx,
            delay,
            &mut stopped,
            &mut capture_active,
            &mut capture_inactive_reason,
            &mut pending_commands,
            &opts.session_id,
            &manifest,
            &state_dir,
            &audio_dir,
            &emit,
        )
        .await
        {
            break;
        }
    }

    // 引擎被停掉（离开实时页 / live_disconnect）时上面的循环直接 break，
    // 不经过正常关链路，也就没有任何事件通知前端。悬浮窗会因此永远停在
    // 最后一次会话状态（比如"录制中"）。这里补一条 closed 把状态收干净；
    // 正常结束（close 码 1000）路径在循环里已经发过 closed，不会走到这。
    if stopped {
        emit(EngineEvent::Connection {
            phase: engine::phase::CLOSED,
            note: Some("连接已断开".into()),
            retry_after_ms: None,
        });
    }
}

fn save_cursor(state_dir: &Path, session_id: &str, last_event_id: i64) {
    let path = state_dir.join(format!("{session_id}.json"));
    let body = json!({ "sessionId": session_id, "lastEventId": last_event_id });
    engine::atomic_write(&path, &body.to_string());
}

/// 游标落盘节流:事件流的每个 ServerMessage 都会推进游标,逐事件 atomic_write
/// 会造成每秒数十次全量重写。同一批事件合并进 500ms 窗口,由独立 select 分支
/// 到期落盘;断开/停止路径无条件 flush,保证进程退出时游标不回退。
#[derive(Default)]
struct CursorThrottle {
    dirty_since: Option<Instant>,
}

impl CursorThrottle {
    /// 事件推进了游标:窗口已过则立即落盘并重开窗口,否则只标记脏。
    fn advance(&mut self, state_dir: &Path, session_id: &str, last_event_id: i64) {
        self.dirty_since.get_or_insert_with(Instant::now);
        self.on_timer(state_dir, session_id, last_event_id);
    }

    /// 合并窗口到期检查(select 定时器驱动)。
    fn on_timer(&mut self, state_dir: &Path, session_id: &str, last_event_id: i64) {
        if let Some(since) = self.dirty_since {
            if since.elapsed() >= Duration::from_millis(CURSOR_FLUSH_INTERVAL_MS) {
                save_cursor(state_dir, session_id, last_event_id);
                self.dirty_since = None;
            }
        }
    }

    /// 断开/停止兜底:存在未落盘推进时立即写盘。
    fn flush(&mut self, state_dir: &Path, session_id: &str, last_event_id: i64) {
        if self.dirty_since.take().is_some() {
            save_cursor(state_dir, session_id, last_event_id);
        }
    }

    /// 下次需要唤醒的时间点;游标干净时为 None(select 分支永久挂起)。
    fn deadline(&self) -> Option<Instant> {
        self.dirty_since
            .map(|since| since + Duration::from_millis(CURSOR_FLUSH_INTERVAL_MS))
    }
}

/// 新分片:落盘 → manifest → 泵。
async fn add_chunk(
    chunk: &crate::engine::NewChunk,
    session_id: &str,
    manifest: &SharedManifest,
    state_dir: &Path,
    audio_dir: &Path,
    emit: &EngineEventEmitter,
    capture_active: bool,
) -> bool {
    // SetCaptureActive(false) 处理完成后，renderer invoke 仍可能晚到。
    // 必须在分配序号和持久化 Captured 之前拒绝，否则崩溃窗口会留下一个
    // 高于既有取消水位、下次重连可发送的分片。
    if !capture_active {
        return false;
    }

    let mut sha = Sha256::new();
    sha.update(&chunk.data);
    let sha256 = format!("{:x}", sha.finalize());

    let mut m = manifest.lock().await;
    if m.records.iter().any(|r| r.chunk_id == chunk.chunk_id) {
        // Tauri invoke 的重复投递必须保持幂等，不能为同一 UUID 再占一个序号。
        return false;
    }
    // manifest 是唯一序号分配者。渲染层在页面重进时可能从 0 开始，那个值
    // 只能作为兼容字段，绝不能参与持久化序号分配。
    let assigned_seq = m.next_chunk_seq;
    m.next_chunk_seq = m.next_chunk_seq.saturating_add(1);
    let record = outbox::make_record(
        &chunk.chunk_id,
        session_id,
        &chunk.source,
        &chunk.codec,
        assigned_seq,
        &chunk.captured_at,
        chunk.duration_ms,
        &sha256,
        chunk.data.len() as u64,
        now_ms(),
    );
    let write_failed = std::fs::write(audio_dir.join(&record.file), &chunk.data).is_err();
    let failed_file = if write_failed {
        Some(record.file.clone())
    } else {
        None
    };
    m.records.push(record);
    let next_chunk_seq = m.next_chunk_seq;
    let stats = outbox::stats_of(&m);
    engine::persist_manifest(state_dir, &m);
    drop(m);
    if let Some(file) = failed_file {
        // WAV 落盘失败:分片已进 manifest(泵读到文件缺失会转终态),但要告知
        // 前端磁盘异常;每次失败都发会刷屏,进程生命周期内只发一次。
        // 锁外 emit,遵循本文件「先 drop 锁再发事件」的约定。
        use std::sync::atomic::{AtomicBool, Ordering};
        static ALERTED: AtomicBool = AtomicBool::new(false);
        if !ALERTED.swap(true, Ordering::Relaxed) {
            emit(EngineEvent::ServerError {
                code: "audio_write_failed".into(),
                message: format!("音频分片写入失败,请检查磁盘空间:{file}"),
                retry_after_seconds: None,
                chunk_id: None,
            });
        }
    }
    emit(EngineEvent::Outbox { stats });
    emit(EngineEvent::SeqWatermark { next_chunk_seq });
    true
}

/// chunk_ack → outbox 状态迁移。
async fn apply_chunk_ack(
    parsed: &Value,
    manifest: &SharedManifest,
    state_dir: &Path,
    audio_dir: &Path,
    emit: &EngineEventEmitter,
) {
    let chunk_id = parsed
        .get("chunk_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if chunk_id.is_empty() {
        return;
    }
    let status = parsed
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let event_id = parsed.get("event_id").and_then(|v| v.as_i64());
    let duplicate = parsed
        .get("duplicate")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let error_code = parsed
        .get("error_code")
        .and_then(|v| v.as_str())
        .map(str::to_string);

    let mut m = manifest.lock().await;
    let Some(idx) = m.records.iter().position(|r| r.chunk_id == chunk_id) else {
        return;
    };
    let input = if duplicate {
        Transition::AckDuplicate { status }
    } else {
        Transition::AckStatus {
            status,
            error_code,
            event_id,
            now: now_ms(),
        }
    };
    let Some(moved) = outbox::transition(&m.records[idx], &input) else {
        return;
    };
    let reached_terminal = matches!(moved.state, OutboxState::Done | OutboxState::TerminalError);
    m.records[idx] = moved;
    if reached_terminal {
        let _ = std::fs::remove_file(audio_dir.join(&m.records[idx].file));
    }
    outbox::clear_cancel_intent_if_resolved(&mut m);
    // 终态收敛点顺带淘汰超额旧记录(与本次 persist 合并,不增加写次数)
    outbox::evict_terminal_records(&mut m);
    let stats = outbox::stats_of(&m);
    engine::persist_manifest(state_dir, &m);
    drop(m);
    emit(EngineEvent::Outbox { stats });
}

/// 服务端 error → outbox 状态迁移(paid_usage_limited 暂停等)。
async fn handle_server_error(
    code: &str,
    chunk_id: Option<&str>,
    retry_after_seconds: Option<u64>,
    manifest: &SharedManifest,
    state_dir: &Path,
    audio_dir: &Path,
    emit: &EngineEventEmitter,
) {
    let mut m = manifest.lock().await;
    let mut changed = false;
    if code == "paid_usage_limited" {
        let until = now_ms() + retry_after_seconds.unwrap_or(30) * 1000;
        m.paused_until = m.paused_until.max(until);
        changed = true;
    }
    if let Some(cid) = chunk_id {
        if let Some(idx) = m.records.iter().position(|r| r.chunk_id == cid) {
            if let Some(moved) = outbox::transition(
                &m.records[idx],
                &Transition::ErrorCode {
                    code: code.to_string(),
                    now: now_ms(),
                },
            ) {
                let reached_terminal = matches!(moved.state, OutboxState::TerminalError);
                m.records[idx] = moved;
                if reached_terminal {
                    let _ = std::fs::remove_file(audio_dir.join(&m.records[idx].file));
                }
                changed = true;
            }
        }
    }
    if outbox::clear_cancel_intent_if_resolved(&mut m) {
        changed = true;
    }
    if changed {
        let stats = outbox::stats_of(&m);
        engine::persist_manifest(state_dir, &m);
        drop(m);
        emit(EngineEvent::Outbox { stats });
    }
}

/// 泛型 sink 写半边(WebSocket 写端)。
type WsWrite = futures_util::stream::SplitSink<
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
    WsMessage,
>;

/// 尝试推进发送(幂等,可频繁调用;严格按 seq,受在途上限约束)。
async fn try_pump(
    write: &mut WsWrite,
    manifest: &SharedManifest,
    state_dir: &Path,
    audio_dir: &Path,
    emit: &EngineEventEmitter,
    pc_upload_allowed: bool,
) {
    if !pc_upload_allowed {
        return;
    }
    let mut guard = manifest.lock().await;
    let now = now_ms();
    if guard.paused_until > now {
        return;
    }
    let inflight = guard
        .records
        .iter()
        .filter(|r| {
            matches!(
                r.state,
                OutboxState::Queued | OutboxState::Sending | OutboxState::CancelPending
            )
        })
        .count();
    if inflight >= MAX_INFLIGHT_QUEUED {
        return;
    }
    // 严格顺序:只发送最小未完成 seq
    let Some(idx) = guard
        .records
        .iter()
        .enumerate()
        .filter(|(_, r)| {
            r.state == OutboxState::Captured
                || (r.state == OutboxState::RetryableFailed && r.next_attempt_at <= now)
        })
        .min_by_key(|(_, r)| r.chunk_seq)
        .map(|(i, _)| i)
    else {
        return;
    };

    let record = guard.records[idx].clone();
    let path = audio_dir.join(&record.file);
    let Ok(bytes) = std::fs::read(&path) else {
        // 文件丢失:终态(不可恢复)
        if let Some(moved) = outbox::transition(
            &guard.records[idx],
            &Transition::ErrorCode {
                code: "invalid_audio_chunk".into(),
                now,
            },
        ) {
            guard.records[idx] = moved;
        }
        let stats = outbox::stats_of(&guard);
        engine::persist_manifest(state_dir, &guard);
        drop(guard);
        emit(EngineEvent::Outbox { stats });
        return;
    };
    // 标记 sending
    if let Some(moved) = outbox::transition(&guard.records[idx], &Transition::SendStarted { now }) {
        guard.records[idx] = moved;
    }
    let stats = outbox::stats_of(&guard);
    engine::persist_manifest(state_dir, &guard);
    drop(guard);

    let payload = json!({
        "v": 1,
        "type": "audio_chunk",
        "chunk_id": record.chunk_id,
        "source": record.source,
        "codec": record.codec,
        "chunk_seq": record.chunk_seq,
        "captured_at": record.captured_at,
        "duration_ms": record.duration_ms,
        "data": base64::engine::general_purpose::STANDARD.encode(&bytes),
    });
    if write
        .send(WsMessage::Text(payload.to_string()))
        .await
        .is_err()
    {
        // socket 不可用:回到 captured 等待重连后
        let mut m = manifest.lock().await;
        if let Some(idx) = m.records.iter().position(|r| r.chunk_id == record.chunk_id) {
            if m.records[idx].state == OutboxState::Sending {
                m.records[idx].state = OutboxState::Captured;
            }
            engine::persist_manifest(state_dir, &m);
        }
    }
    emit(EngineEvent::Outbox { stats });

    // 发送后短促推进后续(受 inflight 限制)
    tokio::time::sleep(Duration::from_millis(30)).await;
    Box::pin(try_pump(
        write,
        manifest,
        state_dir,
        audio_dir,
        emit,
        pc_upload_allowed,
    ))
    .await;
}

/// ready 后优先取退避期缓存的命令；认证前仍接收即时控制命令。
async fn recv_engine_command(
    cmd_rx: &mut mpsc::UnboundedReceiver<EngineCommand>,
    pending_commands: &mut VecDeque<EngineCommand>,
    ready: bool,
) -> Option<EngineCommand> {
    if ready {
        if let Some(command) = pending_commands.pop_front() {
            return Some(command);
        }
    }
    cmd_rx.recv().await
}

/// 可中断的 sleep:期间收到 Stop 立即返回 false；音频分片必须先落盘再重连。
#[allow(clippy::too_many_arguments)]
async fn sleep_interruptible(
    cmd_rx: &mut mpsc::UnboundedReceiver<EngineCommand>,
    ms: u64,
    stopped: &mut bool,
    capture_active: &mut bool,
    capture_inactive_reason: &mut String,
    pending_commands: &mut VecDeque<EngineCommand>,
    session_id: &str,
    manifest: &SharedManifest,
    state_dir: &Path,
    audio_dir: &Path,
    emit: &EngineEventEmitter,
) -> bool {
    tokio::select! {
        _ = tokio::time::sleep(Duration::from_millis(ms)) => true,
        cmd = cmd_rx.recv() => {
            match cmd {
                Some(EngineCommand::Stop) | None => {
                    *stopped = true;
                    false
                }
                Some(EngineCommand::SetCaptureActive { active, reason, ack }) => {
                    *capture_active = active;
                    emit(EngineEvent::CaptureState { active });
                    if !active {
                        *capture_inactive_reason = if reason.is_empty() {
                            "capture_stopped".into()
                        } else {
                            reason
                        };
                        engine::apply_capture_inactive(
                            capture_inactive_reason,
                            manifest,
                            state_dir,
                            audio_dir,
                            emit,
                        )
                        .await;
                    }
                    if let Some(ack) = ack {
                        let _ = ack.send(());
                    }
                    true
                }
                Some(EngineCommand::AddChunk(chunk)) => {
                    add_chunk(
                        &chunk,
                        session_id,
                        manifest,
                        state_dir,
                        audio_dir,
                        emit,
                        *capture_active,
                    )
                    .await;
                    true
                }
                Some(command) => {
                    pending_commands.push_back(command);
                    true
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::NewChunk;
    use crate::outbox::{CancelIntent, OutboxManifest};
    use std::sync::Arc;
    use tokio::sync::Mutex;

    #[test]
    fn pc_pump_requires_active_capture_recording_session_and_allowed_mode() {
        assert!(pc_upload_allowed(true, "recording", "pc"));
        assert!(pc_upload_allowed(true, "recording", "both"));
        assert!(!pc_upload_allowed(false, "recording", "pc"));
        assert!(!pc_upload_allowed(true, "idle", "pc"));
        assert!(!pc_upload_allowed(true, "recording", "mobile"));
    }

    #[test]
    fn websocket_url_preserves_host_and_switches_transport_scheme() {
        assert_eq!(
            ws_url("https://api.example.com", "session-1").unwrap(),
            "wss://api.example.com/ws/session-1"
        );
        assert_eq!(
            ws_url("http://127.0.0.1:8000", "session-1").unwrap(),
            "ws://127.0.0.1:8000/ws/session-1"
        );
        assert!(ws_url("not-a-url", "session-1").is_err());
    }

    #[test]
    fn cancel_message_carries_persisted_boundary_and_reason() {
        let message = cancel_audio_source_message(&CancelIntent {
            source: "pc".into(),
            through_chunk_seq: 17,
            reason: "capture_stopped".into(),
        });
        assert_eq!(message["v"], PROTOCOL_VERSION);
        assert_eq!(message["type"], "cancel_audio_source");
        assert_eq!(message["source"], "pc");
        assert_eq!(message["through_chunk_seq"], 17);
        assert_eq!(message["reason"], "capture_stopped");
    }

    #[test]
    fn speech_end_message_carries_current_sequence_boundary() {
        let message = speech_end_message("pc", 23);
        assert_eq!(message["v"], PROTOCOL_VERSION);
        assert_eq!(message["type"], "speech_end");
        assert_eq!(message["source"], "pc");
        assert_eq!(message["through_chunk_seq"], 23);
    }

    #[tokio::test]
    async fn add_chunk_allocates_from_manifest_instead_of_renderer_hint() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-ws-client-{}",
            crate::engine::now_ms()
        ));
        let state_dir = root.join("state");
        let audio_dir = root.join("audio");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        std::fs::create_dir_all(&audio_dir).expect("create audio dir");

        let mut initial = OutboxManifest::empty("session-1");
        initial.next_chunk_seq = 7;
        let manifest = Arc::new(Mutex::new(initial));
        let emit: crate::engine::EngineEventEmitter = Arc::new(|_| {});
        let chunk = NewChunk {
            chunk_id: "00000000-0000-4000-8000-000000000001".into(),
            chunk_seq: 0,
            captured_at: "2026-08-20T00:00:00Z".into(),
            duration_ms: 1_000,
            codec: "wav_pcm_s16le".into(),
            source: "pc".into(),
            data: vec![0, 1, 2, 3],
        };

        add_chunk(
            &chunk,
            "session-1",
            &manifest,
            &state_dir,
            &audio_dir,
            &emit,
            true,
        )
        .await;
        add_chunk(
            &chunk,
            "session-1",
            &manifest,
            &state_dir,
            &audio_dir,
            &emit,
            true,
        )
        .await;

        let guard = manifest.lock().await;
        assert_eq!(guard.records.len(), 1);
        assert_eq!(guard.records[0].chunk_seq, 7);
        assert_eq!(guard.next_chunk_seq, 8);
        drop(guard);
        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[tokio::test]
    async fn inactive_add_chunk_is_dropped_before_it_can_enter_the_pump() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-ws-client-inactive-{}",
            crate::engine::now_ms()
        ));
        let state_dir = root.join("state");
        let audio_dir = root.join("audio");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        std::fs::create_dir_all(&audio_dir).expect("create audio dir");

        let mut initial = OutboxManifest::empty("session-1");
        initial.next_chunk_seq = 7;
        let manifest = Arc::new(Mutex::new(initial));
        let emit: crate::engine::EngineEventEmitter = Arc::new(|_| {});
        let chunk = NewChunk {
            chunk_id: "00000000-0000-4000-8000-000000000002".into(),
            chunk_seq: 0,
            captured_at: "2026-08-20T00:00:00Z".into(),
            duration_ms: 1_000,
            codec: "wav_pcm_s16le".into(),
            source: "pc".into(),
            data: vec![0, 1, 2, 3],
        };

        let accepted = add_chunk(
            &chunk,
            "session-1",
            &manifest,
            &state_dir,
            &audio_dir,
            &emit,
            false,
        )
        .await;

        assert!(!accepted);
        let guard = manifest.lock().await;
        assert!(guard.records.is_empty());
        assert_eq!(guard.next_chunk_seq, 7);
        drop(guard);
        assert!(!audio_dir.join(format!("{}.wav", chunk.chunk_id)).exists());
        assert!(!state_dir.join("outbox-session-1.json").exists());
        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[tokio::test]
    async fn reconnect_sleep_persists_active_add_chunk_before_retrying() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-ws-client-reconnect-active-{}",
            crate::engine::now_ms()
        ));
        let state_dir = root.join("state");
        let audio_dir = root.join("audio");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        std::fs::create_dir_all(&audio_dir).expect("create audio dir");

        let manifest = Arc::new(Mutex::new(OutboxManifest::empty("session-1")));
        let emit: crate::engine::EngineEventEmitter = Arc::new(|_| {});
        let (tx, mut rx) = mpsc::unbounded_channel();
        let chunk_id = "00000000-0000-4000-8000-000000000003";
        tx.send(EngineCommand::AddChunk(Box::new(NewChunk {
            chunk_id: chunk_id.into(),
            chunk_seq: 0,
            captured_at: "2026-08-20T00:00:00Z".into(),
            duration_ms: 1_000,
            codec: "wav_pcm_s16le".into(),
            source: "pc".into(),
            data: vec![0, 1, 2, 3],
        })))
        .expect("queue chunk during reconnect sleep");

        let mut stopped = false;
        let mut capture_active = true;
        let mut inactive_reason = "capture_stopped".to_string();
        let mut pending_commands = VecDeque::new();
        assert!(
            sleep_interruptible(
                &mut rx,
                60_000,
                &mut stopped,
                &mut capture_active,
                &mut inactive_reason,
                &mut pending_commands,
                "session-1",
                &manifest,
                &state_dir,
                &audio_dir,
                &emit,
            )
            .await
        );

        let guard = manifest.lock().await;
        assert_eq!(guard.records.len(), 1);
        assert_eq!(guard.records[0].state, OutboxState::Captured);
        assert_eq!(guard.next_chunk_seq, 1);
        drop(guard);
        assert!(audio_dir.join(format!("{chunk_id}.wav")).exists());
        assert!(state_dir.join("outbox-session-1.json").exists());
        assert!(!stopped);
        assert!(pending_commands.is_empty());
        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[tokio::test]
    async fn reconnect_sleep_acknowledges_deactivation_after_older_chunk_is_frozen() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-ws-client-reconnect-deactivate-{}-{}",
            std::process::id(),
            crate::engine::now_ms()
        ));
        let state_dir = root.join("state");
        let audio_dir = root.join("audio");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        std::fs::create_dir_all(&audio_dir).expect("create audio dir");

        let manifest = Arc::new(Mutex::new(OutboxManifest::empty("session-1")));
        let emit: crate::engine::EngineEventEmitter = Arc::new(|_| {});
        let (tx, mut rx) = mpsc::unbounded_channel();
        let chunk_id = "00000000-0000-4000-8000-000000000005";
        tx.send(EngineCommand::AddChunk(Box::new(NewChunk {
            chunk_id: chunk_id.into(),
            chunk_seq: 0,
            captured_at: "2026-08-20T00:00:00Z".into(),
            duration_ms: 1_000,
            codec: "wav_pcm_s16le".into(),
            source: "pc".into(),
            data: vec![0, 1, 2, 3],
        })))
        .expect("queue chunk before deactivation");
        let (ack_tx, ack_rx) = tokio::sync::oneshot::channel();
        tx.send(EngineCommand::SetCaptureActive {
            active: false,
            reason: crate::protocol::CAPTURE_INTERRUPTED_REASON.into(),
            ack: Some(ack_tx),
        })
        .expect("queue capture deactivation");

        let mut stopped = false;
        let mut capture_active = true;
        let mut inactive_reason = "capture_stopped".to_string();
        let mut pending_commands = VecDeque::new();
        for _ in 0..2 {
            assert!(
                sleep_interruptible(
                    &mut rx,
                    60_000,
                    &mut stopped,
                    &mut capture_active,
                    &mut inactive_reason,
                    &mut pending_commands,
                    "session-1",
                    &manifest,
                    &state_dir,
                    &audio_dir,
                    &emit,
                )
                .await
            );
        }

        tokio::time::timeout(Duration::from_secs(1), ack_rx)
            .await
            .expect("deactivation ack should not wait for reconnect")
            .expect("deactivation handler should send ack");
        assert!(!capture_active);
        let guard = manifest.lock().await;
        assert_eq!(guard.records.len(), 1);
        assert_eq!(guard.records[0].state, OutboxState::TerminalError);
        assert_eq!(
            guard
                .cancel_intent
                .as_ref()
                .map(|intent| intent.through_chunk_seq),
            Some(0)
        );
        drop(guard);
        assert!(!audio_dir.join(format!("{chunk_id}.wav")).exists());

        let persisted: OutboxManifest = serde_json::from_str(
            &std::fs::read_to_string(state_dir.join("outbox-session-1.json"))
                .expect("read persisted manifest after ack"),
        )
        .expect("parse persisted manifest after ack");
        assert_eq!(persisted.records[0].state, OutboxState::TerminalError);
        assert!(persisted.cancel_intent.is_some());
        assert!(pending_commands.is_empty());
        assert!(!stopped);
        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[tokio::test]
    async fn reconnect_sleep_rejects_inactive_add_chunk() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-ws-client-reconnect-inactive-{}",
            crate::engine::now_ms()
        ));
        let state_dir = root.join("state");
        let audio_dir = root.join("audio");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        std::fs::create_dir_all(&audio_dir).expect("create audio dir");

        let manifest = Arc::new(Mutex::new(OutboxManifest::empty("session-1")));
        let emit: crate::engine::EngineEventEmitter = Arc::new(|_| {});
        let (tx, mut rx) = mpsc::unbounded_channel();
        let chunk_id = "00000000-0000-4000-8000-000000000004";
        tx.send(EngineCommand::AddChunk(Box::new(NewChunk {
            chunk_id: chunk_id.into(),
            chunk_seq: 0,
            captured_at: "2026-08-20T00:00:00Z".into(),
            duration_ms: 1_000,
            codec: "wav_pcm_s16le".into(),
            source: "pc".into(),
            data: vec![0, 1, 2, 3],
        })))
        .expect("queue chunk during reconnect sleep");

        let mut stopped = false;
        let mut capture_active = false;
        let mut inactive_reason = "capture_stopped".to_string();
        let mut pending_commands = VecDeque::new();
        assert!(
            sleep_interruptible(
                &mut rx,
                60_000,
                &mut stopped,
                &mut capture_active,
                &mut inactive_reason,
                &mut pending_commands,
                "session-1",
                &manifest,
                &state_dir,
                &audio_dir,
                &emit,
            )
            .await
        );

        let guard = manifest.lock().await;
        assert!(guard.records.is_empty());
        assert_eq!(guard.next_chunk_seq, 0);
        drop(guard);
        assert!(!audio_dir.join(format!("{chunk_id}.wav")).exists());
        assert!(!state_dir.join("outbox-session-1.json").exists());
        assert!(!stopped);
        assert!(pending_commands.is_empty());
        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[tokio::test]
    async fn reconnect_sleep_buffers_send_until_connection_loop_is_ready() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-ws-client-reconnect-send-{}",
            crate::engine::now_ms()
        ));
        let state_dir = root.join("state");
        let audio_dir = root.join("audio");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        std::fs::create_dir_all(&audio_dir).expect("create audio dir");

        let manifest = Arc::new(Mutex::new(OutboxManifest::empty("session-1")));
        let emit: crate::engine::EngineEventEmitter = Arc::new(|_| {});
        let (tx, mut rx) = mpsc::unbounded_channel();
        tx.send(EngineCommand::Send(json!({
            "v": PROTOCOL_VERSION,
            "type": "end_session",
        })))
        .expect("queue end_session during reconnect sleep");

        let mut stopped = false;
        let mut capture_active = false;
        let mut inactive_reason = "capture_stopped".to_string();
        let mut pending_commands = VecDeque::new();
        assert!(
            sleep_interruptible(
                &mut rx,
                60_000,
                &mut stopped,
                &mut capture_active,
                &mut inactive_reason,
                &mut pending_commands,
                "session-1",
                &manifest,
                &state_dir,
                &audio_dir,
                &emit,
            )
            .await
        );

        assert_eq!(pending_commands.len(), 1);
        let command = recv_engine_command(&mut rx, &mut pending_commands, true)
            .await
            .expect("ready connection should receive buffered command");
        match command {
            EngineCommand::Send(value) => assert_eq!(value["type"], "end_session"),
            other => panic!("expected buffered Send, got {other:?}"),
        }
        assert!(pending_commands.is_empty());
        assert!(!stopped);
        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[test]
    fn cancel_message_normalizes_legacy_local_reason() {
        let message = cancel_audio_source_message(&CancelIntent {
            source: "pc".into(),
            through_chunk_seq: 17,
            reason: crate::protocol::CAPTURE_INTERRUPTED_REASON.into(),
        });
        assert_eq!(message["reason"], crate::protocol::CAPTURE_STOPPED_REASON);

        let source_disabled = cancel_audio_source_message(&CancelIntent {
            source: "pc".into(),
            through_chunk_seq: 18,
            reason: crate::protocol::SOURCE_DISABLED_REASON.into(),
        });
        assert_eq!(
            source_disabled["reason"],
            crate::protocol::SOURCE_DISABLED_REASON
        );
    }

    #[tokio::test]
    async fn cursor_throttle_batches_bursts_and_flushes_on_disconnect() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-ws-cursor-{}-{}",
            std::process::id(),
            crate::engine::now_ms()
        ));
        std::fs::create_dir_all(&root).expect("create state dir");
        let cursor_path = root.join("session-1.json");

        let mut throttle = CursorThrottle::default();
        // 突发推进:窗口未到期,不落盘
        throttle.advance(&root, "session-1", 1);
        throttle.advance(&root, "session-1", 2);
        throttle.advance(&root, "session-1", 3);
        assert!(throttle.dirty_since.is_some());
        assert!(!cursor_path.exists());

        // 窗口到期后 on_timer 落盘最新游标(中间值被合并)
        tokio::time::sleep(Duration::from_millis(CURSOR_FLUSH_INTERVAL_MS + 50)).await;
        throttle.on_timer(&root, "session-1", 3);
        assert!(throttle.dirty_since.is_none());
        let body: Value =
            serde_json::from_str(&std::fs::read_to_string(&cursor_path).expect("cursor flushed"))
                .expect("parse cursor");
        assert_eq!(body["lastEventId"], 3);

        // 新一轮推进后直接断开:flush 保证最终游标落盘
        throttle.advance(&root, "session-1", 4);
        throttle.flush(&root, "session-1", 4);
        let body: Value = serde_json::from_str(
            &std::fs::read_to_string(&cursor_path).expect("cursor flushed on disconnect"),
        )
        .expect("parse cursor");
        assert_eq!(body["lastEventId"], 4);
        assert!(throttle.dirty_since.is_none());

        // 干净状态 flush 是 no-op,游标不会回退
        throttle.flush(&root, "session-1", 4);
        let body: Value =
            serde_json::from_str(&std::fs::read_to_string(&cursor_path).expect("cursor unchanged"))
                .expect("parse cursor");
        assert_eq!(body["lastEventId"], 4);

        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[test]
    fn connect_timeout_is_bounded_and_reconcile_budget_capped() {
        // 回归护栏:无上限的 connect/对账会让 WS 循环长时间不可中断
        assert_eq!(CONNECT_TIMEOUT, Duration::from_secs(10));
        assert_eq!(RECONCILE_BUDGET, Duration::from_secs(30));
    }
}
