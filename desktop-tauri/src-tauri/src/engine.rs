//! Live 引擎:一个活动会话的 WS 连接 + 内嵌发送泵 + 对账 + 游标持久化。
//! 对应 desktop/src/main/engine/(engine.ts + pump.ts) 的合并移植。
//!
//! 架构:WS 任务持有写半边,发送泵内嵌其中(严格顺序、背压、重试全部事件驱动);
//! 命令层通过 unbounded channel 与 WS 任务通信;manifest 由 Arc<Mutex> 共享。

use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::Mutex;

use crate::outbox::{self, OutboxManifest, OutboxState, OutboxStats};
use crate::reconcile::{diff_reconcile, ReconcileAction};
use crate::rest::RestContext;

// ---------- 对外事件(序列化后发给前端,tag=kind) ----------

#[derive(Debug, Clone, Serialize)]
#[serde(
    tag = "kind",
    rename_all = "camelCase",
    rename_all_fields = "camelCase"
)]
pub enum EngineEvent {
    Connection {
        phase: &'static str,
        note: Option<String>,
        retry_after_ms: Option<u64>,
    },
    SyncComplete {
        latest_event_id: i64,
    },
    SessionState {
        status: String,
        radio_mode: String,
    },
    SessionEnded,
    /// 服务端持久化事件原始消息(前端解析 transcript/answer/chunk_ack/session_state)
    ServerMessage(Value),
    ServerError {
        code: String,
        message: String,
        retry_after_seconds: Option<u64>,
        chunk_id: Option<String>,
    },
    Outbox {
        stats: OutboxStats,
    },
    /// 序号水位(引擎启动时/对账后推送;前端 sequencer 对齐用,防双重分配)
    SeqWatermark {
        next_chunk_seq: i64,
    },
    /// 本机采集门翻转。悬浮窗据此区分「会话在录」与「本机真的在采」:
    /// 停止采集后后端会话状态仍是 recording(切收音模式要求如此),
    /// 只报 sessionState 的话徽标会一直亮"录制中"。
    CaptureState {
        active: bool,
    },
}

/// WS 阶段字符串(与前端 ConnectionPhase 一致)。
pub mod phase {
    pub const CONNECTING: &str = "connecting";
    pub const AUTHENTICATING: &str = "authenticating";
    pub const READY: &str = "ready";
    pub const RECONNECTING: &str = "reconnecting";
    pub const CLOSED: &str = "closed";
}

/// 服务端每会话/来源队列默认 8;客户端保守在途上限。
pub const MAX_INFLIGHT_QUEUED: usize = 6;

#[cfg(not(test))]
const CAPTURE_GATE_ACK_TIMEOUT: Duration = Duration::from_secs(2);
#[cfg(test)]
const CAPTURE_GATE_ACK_TIMEOUT: Duration = Duration::from_millis(50);

// ---------- 引擎命令 ----------

#[derive(Debug)]
pub enum EngineCommand {
    /// 直接发送已构造好的业务消息(仅 ready 后)
    Send(Value),
    /// 渲染进程采集的新分片(落盘后由泵按序发送)
    AddChunk(Box<NewChunk>),
    /// 客户端 VAD 检测到连续静音；WS 任务会附带当前已分配的 PC 序号水位。
    SpeechEnd {
        source: String,
    },
    /// 控制本机采集门禁；关闭时同时持久化并补发服务端取消意图。
    SetCaptureActive {
        active: bool,
        reason: String,
        /// 调用方可等待门禁更新完成；关闭时 ACK 只在 manifest 持久化之后发送。
        ack: Option<tokio::sync::oneshot::Sender<()>>,
    },
    /// 驱动泵(尝试推进发送)
    #[allow(dead_code)]
    Tick,
    /// 手动/自动对账
    Reconcile,
    /// 读取统计快照(回复通过 Outbox 事件)
    #[allow(dead_code)]
    Snapshot,
    Stop,
}

#[derive(Debug)]
pub struct NewChunk {
    pub chunk_id: String,
    /// 渲染层的临时序号提示。真正的持久序号由 Rust outbox 在落盘时原子分配。
    #[allow(dead_code)]
    pub chunk_seq: i64,
    pub captured_at: String,
    pub duration_ms: i64,
    pub codec: String,
    pub source: String,
    pub data: Vec<u8>,
}

pub type EngineEventEmitter = Arc<dyn Fn(EngineEvent) + Send + Sync>;
pub type SharedManifest = Arc<Mutex<OutboxManifest>>;
/// 引擎停止标记:Stop 后置位。persist_manifest 检查它,让引擎停止后旧任务
/// (收尾/残留对账)的落盘变成 no-op,不盖掉接替引擎写入的文件(E1c)。
pub type SharedStopped = Arc<AtomicBool>;

// ---------- 时间与持久化工具 ----------

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// 无 rand 依赖的简易随机(xorshift,种子取自时间)。outbox 重试退避与
/// WS 重连退避共用;取值范围 [0, 1)。
pub fn rand_f64() -> f64 {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos() as u64 ^ d.as_secs())
        .unwrap_or(0x9E37_79B9);
    let mut x = nanos | 1;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    (x & 0xFFFF_FFFF) as f64 / 0x1_0000_0000u64 as f64
}

/// 原子写:tmp + rename,Windows 失败时删除重命名,最终降级 copy+delete。
pub fn atomic_write(path: &Path, data: &str) {
    use std::io::Write;
    let tmp = path.with_extension("tmp");
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let Ok(mut f) = std::fs::File::create(&tmp) else {
        return;
    };
    if f.write_all(data.as_bytes()).is_err() {
        return;
    }
    // rename 前先 fsync:否则断电窗口内目录项可能先于数据落盘,
    // 目标文件变成空壳/半截,下次启动按损坏 manifest 处理(E4)。
    if f.sync_all().is_err() {
        return;
    }
    drop(f);
    match std::fs::rename(&tmp, path) {
        Ok(()) => {}
        Err(_) => match (std::fs::remove_file(path), std::fs::rename(&tmp, path)) {
            (Ok(()), Ok(())) => {}
            _ => {
                let _ = std::fs::copy(&tmp, path).map(|_| ());
                let _ = std::fs::remove_file(&tmp);
            }
        },
    }
}

fn load_cursor(state_dir: &Path, session_id: &str) -> i64 {
    let path = state_dir.join(format!("{session_id}.json"));
    let Ok(raw) = std::fs::read_to_string(path) else {
        return 0;
    };
    #[derive(serde::Deserialize)]
    struct Cursor {
        #[serde(rename = "lastEventId", alias = "last_event_id")]
        last_event_id: i64,
    }
    serde_json::from_str::<Cursor>(&raw)
        .map(|c| c.last_event_id)
        .unwrap_or(0)
}

/// 查询服务端该会话 pc 源最大 chunk_seq + 1;查询失败返回 0(退回本地水位,由对账兜底)。
/// 必须在异步上下文中调用(无嵌套 block_on)。
/// 整体受 deadline 约束:50 页 × 15s 的 REST 最坏情况不能让启动扫描分钟级
/// 停摆(E1a);到限即返回已见页面的最大值。
pub async fn server_next_chunk_seq(
    ctx: &RestContext,
    session_id: &str,
    deadline: std::time::Instant,
) -> i64 {
    let deadline = tokio::time::Instant::from_std(deadline);
    let mut cursor: i64 = -1;
    let mut max_seq: i64 = -1;
    for _ in 0..50 {
        match tokio::time::timeout_at(
            deadline,
            crate::rest::audio_chunks(ctx, session_id, "pc", cursor),
        )
        .await
        {
            Err(_elapsed) => break,
            Ok(Err(_)) => return 0,
            Ok(Ok(page)) => {
                if page.is_empty() {
                    break;
                }
                max_seq = max_seq.max(page.iter().map(|c| c.chunk_seq).max().unwrap_or(-1));
                cursor = max_seq;
                if page.len() < 200 {
                    break;
                }
            }
        }
    }
    max_seq + 1
}

/// 读取 manifest。返回 (manifest, quarantined):文件存在但读取/解析失败时,
/// 原文件改名 `<name>.corrupt` 保留并返回 quarantined=true,调用方本次启动
/// 不得清孤儿 WAV,否则等于把积压音频全部删光(E4)。
fn load_manifest(state_dir: &Path, session_id: &str) -> (OutboxManifest, bool) {
    let path = state_dir.join(format!("outbox-{session_id}.json"));
    let raw = match std::fs::read_to_string(&path) {
        Ok(raw) => raw,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return (OutboxManifest::empty(session_id), false);
        }
        Err(e) => {
            eprintln!("engine: outbox manifest 读取失败({e}),保留音频并跳过孤儿清理");
            return (OutboxManifest::empty(session_id), true);
        }
    };
    match serde_json::from_str(&raw) {
        Ok(manifest) => (manifest, false),
        Err(e) => {
            let quarantine = path.with_extension("corrupt");
            // 之前启动已留下隔离文件时 rename 会失败:先删旧的再改。
            if std::fs::rename(&path, &quarantine).is_err() {
                let _ = std::fs::remove_file(&quarantine);
                let _ = std::fs::rename(&path, &quarantine);
            }
            eprintln!(
                "engine: outbox manifest 解析失败({e}),已隔离为 {}",
                quarantine.display()
            );
            (OutboxManifest::empty(session_id), true)
        }
    }
}

pub fn persist_manifest(state_dir: &Path, manifest: &OutboxManifest, stopped: &AtomicBool) {
    // 引擎停止后的落盘一律跳过:旧引擎收尾/残留对账任务的写入会盖掉接替
    // 引擎已写入的新文件(E1c)。
    if stopped.load(Ordering::SeqCst) {
        return;
    }
    let path = state_dir.join(format!("outbox-{}.json", manifest.session_id));
    if let Ok(body) = serde_json::to_string(manifest) {
        atomic_write(&path, &body);
    }
}

/// Engine::spawn 的同步装载:读 manifest(损坏则隔离)→ 冻结遗留未终态分片 →
/// 清孤儿 WAV(仅当 manifest 可信;不可信时跳过,音频保留待恢复,E4)。
fn startup_manifest(
    state_dir: &Path,
    audio_dir: &Path,
    session_id: &str,
    stopped: &AtomicBool,
) -> OutboxManifest {
    let (mut manifest, quarantined) = load_manifest(state_dir, session_id);

    // 引擎启动时采集默认关闭。上次遗留的非终态分片视为采集已中断：
    // 本地未发送项直接终态，服务端可能已接收的项保留 CancelPending，
    // 等 sync_complete 后先补发取消，绝不自动恢复旧音频上传。
    let cancellation =
        outbox::cancel_for_capture_stop(&mut manifest, crate::protocol::CAPTURE_INTERRUPTED_REASON);
    for file in &cancellation.files_to_delete {
        let _ = std::fs::remove_file(audio_dir.join(file));
    }
    if cancellation.changed {
        persist_manifest(state_dir, &manifest, stopped);
    }
    // 序号水位对齐挪到 run_ws 内(异步上下文):此处同步 block_on 会与 tauri 的
    // tokio runtime 嵌套冲突。本地水位先保留,连上服务端后立即对齐。
    if quarantined {
        eprintln!("engine: manifest 不可信,本次启动跳过孤儿 WAV 清理(音频保留待恢复)");
    } else {
        sweep_orphans(&audio_dir, &manifest);
    }
    manifest
}

/// 关闭采集门禁并同步冻结当前 outbox。该函数只在命令队列处理完成或引擎任务
/// 已退出后的兜底路径调用，确保取消水位包含此前已经接收的所有分片。
pub async fn apply_capture_inactive(
    reason: &str,
    manifest: &SharedManifest,
    state_dir: &Path,
    audio_dir: &Path,
    emit: &EngineEventEmitter,
    stopped: &AtomicBool,
) {
    let mut m = manifest.lock().await;
    let result = outbox::cancel_for_capture_stop(&mut m, reason);
    if !result.changed {
        return;
    }
    for file in result.files_to_delete {
        let _ = std::fs::remove_file(audio_dir.join(file));
    }
    let stats = outbox::stats_of(&m);
    persist_manifest(state_dir, &m, stopped);
    drop(m);
    emit(EngineEvent::Outbox { stats });
}

fn sweep_orphans(audio_dir: &Path, manifest: &OutboxManifest) {
    let keep: std::collections::HashSet<String> = manifest
        .records
        .iter()
        .map(|r| r.chunk_id.clone())
        .collect();
    let Ok(entries) = std::fs::read_dir(audio_dir) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if let Some(chunk_id) = name.strip_suffix(".wav") {
            if !keep.contains(chunk_id) {
                let _ = std::fs::remove_file(entry.path());
            }
        }
    }
}

// ---------- 引擎句柄 ----------

pub struct Engine {
    pub cmd_tx: tokio::sync::mpsc::UnboundedSender<EngineCommand>,
    pub manifest: SharedManifest,
    pub state_dir: PathBuf,
    pub audio_dir: PathBuf,
    pub emit: EngineEventEmitter,
    pub session_id: String,
    /// 本引擎的停止标记:run_ws 收到 Stop 置位,所有 manifest 落盘路径共享。
    stopped: SharedStopped,
}

impl Engine {
    /// 启动引擎任务。emit 在 tokio 工作线程执行,调用方负责转发前端。
    pub fn spawn(
        app_data_dir: PathBuf,
        ctx: RestContext,
        session_id: String,
        emit: EngineEventEmitter,
    ) -> Self {
        let (cmd_tx, cmd_rx) = tokio::sync::mpsc::unbounded_channel();
        let state_dir = app_data_dir.join("state");
        let audio_dir = app_data_dir.join("audio");
        let _ = std::fs::create_dir_all(&state_dir);
        let _ = std::fs::create_dir_all(&audio_dir);

        let cursor = load_cursor(&state_dir, &session_id);
        let stopped: SharedStopped = Arc::new(AtomicBool::new(false));
        let manifest = startup_manifest(&state_dir, &audio_dir, &session_id, &stopped);
        let manifest: SharedManifest = Arc::new(Mutex::new(manifest));

        tokio::spawn(super::ws_client::run_ws(
            crate::ws_client::WsClientOptions {
                server_url: ctx.server_url.clone(),
                session_id: session_id.clone(),
                token: ctx.token.clone(),
                last_event_id: cursor,
            },
            ctx,
            cmd_rx,
            manifest.clone(),
            state_dir.clone(),
            audio_dir.clone(),
            emit.clone(),
            stopped.clone(),
        ));

        Self {
            cmd_tx,
            manifest,
            state_dir,
            audio_dir,
            emit,
            session_id,
            stopped,
        }
    }

    pub fn send(&self, value: Value) -> bool {
        self.cmd_tx.send(EngineCommand::Send(value)).is_ok()
    }

    #[allow(dead_code)]
    pub fn tick(&self) {
        let _ = self.cmd_tx.send(EngineCommand::Tick);
    }

    pub fn reconcile(&self) {
        let _ = self.cmd_tx.send(EngineCommand::Reconcile);
    }

    pub fn stop(&self) {
        let _ = self.cmd_tx.send(EngineCommand::Stop);
    }

    /// 将采集关闭命令排在此前所有分片之后，并等待 manifest/cancel intent 落盘。
    /// 若 WS 任务已退出，则直接对共享 manifest 做幂等兜底。
    pub async fn deactivate_capture(&self, reason: &str) {
        let (ack_tx, ack_rx) = tokio::sync::oneshot::channel();
        let queued = self
            .cmd_tx
            .send(EngineCommand::SetCaptureActive {
                active: false,
                reason: reason.to_string(),
                ack: Some(ack_tx),
            })
            .is_ok();
        if queued {
            if let Ok(Ok(())) = tokio::time::timeout(CAPTURE_GATE_ACK_TIMEOUT, ack_rx).await {
                return;
            }
        }

        // connect_async/TLS 阶段暂不轮询命令。短超时后先幂等持久化，已排队的
        // SetCaptureActive 仍位于随后 Stop 之前，恢复轮询时会再次覆盖晚到分片。
        apply_capture_inactive(
            reason,
            &self.manifest,
            &self.state_dir,
            &self.audio_dir,
            &self.emit,
            &self.stopped,
        )
        .await;
    }

    /// 开启采集必须等 WS 任务实际更新门禁；仅成功写入 channel 不代表分片已可接收。
    pub async fn activate_capture(&self, reason: &str) -> Result<(), String> {
        let (ack_tx, ack_rx) = tokio::sync::oneshot::channel();
        self.cmd_tx
            .send(EngineCommand::SetCaptureActive {
                active: true,
                reason: reason.to_string(),
                ack: Some(ack_tx),
            })
            .map_err(|_| "Live 引擎已停止，无法开启音频采集".to_string())?;

        match tokio::time::timeout(CAPTURE_GATE_ACK_TIMEOUT, ack_rx).await {
            Ok(Ok(())) => Ok(()),
            Ok(Err(_)) => Err("Live 引擎已停止，无法开启音频采集".into()),
            Err(_) => Err("Live 引擎正忙于建立连接，音频采集尚未开启".into()),
        }
    }

    pub async fn stats(&self) -> OutboxStats {
        let m = self.manifest.lock().await;
        outbox::stats_of(&m)
    }
}

/// 手动/自动对账:keyset 分页拉全量 → diff → 应用。
pub async fn reconcile(
    ctx: &RestContext,
    session_id: &str,
    manifest: &SharedManifest,
    state_dir: &Path,
    audio_dir: &Path,
    emit: &EngineEventEmitter,
    stopped: &AtomicBool,
) {
    let mut all = Vec::new();
    let mut cursor: i64 = -1;
    for _ in 0..50 {
        match crate::rest::audio_chunks(ctx, session_id, "pc", cursor).await {
            Ok(page) => {
                let len = page.len();
                if len == 0 {
                    break;
                }
                cursor = page[len - 1].chunk_seq;
                all.extend(page);
                if len < 200 {
                    break;
                }
            }
            Err(_) => return,
        }
    }

    let mut m = manifest.lock().await;
    let result = diff_reconcile(&m, &all);
    let mut changed = false;
    for action in result.actions {
        match action {
            ReconcileAction::Resend { chunk_id } => {
                if let Some(idx) = m.records.iter().position(|r| r.chunk_id == chunk_id) {
                    if m.records[idx].state == OutboxState::CancelPending {
                        // 服务端暂时查不到不能证明取消已经完成。保留持久取消意图，
                        // 下次 sync 继续补发 cancel，绝不能重新变回 Captured。
                        continue;
                    }
                    if !matches!(
                        m.records[idx].state,
                        OutboxState::Done | OutboxState::Released | OutboxState::TerminalError
                    ) {
                        m.records[idx].state = OutboxState::Captured;
                        m.records[idx].next_attempt_at = now_ms();
                        changed = true;
                    }
                }
            }
            ReconcileAction::AdoptStatus {
                chunk_id,
                status,
                error_code,
            } => {
                if let Some(idx) = m.records.iter().position(|r| r.chunk_id == chunk_id) {
                    if let Some(moved) = outbox::transition(
                        &m.records[idx],
                        &crate::outbox::Transition::AckStatus {
                            status,
                            error_code,
                            event_id: None,
                            now: now_ms(),
                        },
                    ) {
                        m.records[idx] = moved;
                        changed = true;
                    }
                }
            }
            ReconcileAction::BurnSeq { chunk_id, .. } => {
                if let Some(idx) = m.records.iter().position(|r| r.chunk_id == chunk_id) {
                    if let Some(moved) = outbox::transition(
                        &m.records[idx],
                        &crate::outbox::Transition::ErrorCode {
                            code: "invalid_audio_chunk".into(),
                            now: now_ms(),
                        },
                    ) {
                        m.records[idx] = moved;
                        changed = true;
                    }
                }
            }
        }
    }
    if outbox::clear_cancel_intent_if_resolved(&mut m) {
        changed = true;
    }
    m.next_chunk_seq = m.next_chunk_seq.max(result.next_chunk_seq);
    let watermark = m.next_chunk_seq;
    // 对账烧毁后水位前进,推给前端 sequencer
    emit(EngineEvent::SeqWatermark {
        next_chunk_seq: watermark,
    });
    // 对账 warning 聚合为单条,避免大量冲突时 toast 刷屏
    if !result.warnings.is_empty() {
        let summary = if result.warnings.len() == 1 {
            result.warnings[0].clone()
        } else {
            format!(
                "{}(共 {} 个分片序号冲突,已自动放弃本地旧分片)",
                result.warnings[0],
                result.warnings.len()
            )
        };
        emit(EngineEvent::ServerError {
            code: "reconcile_warning".into(),
            message: summary,
            retry_after_seconds: None,
            chunk_id: None,
        });
    }
    if changed {
        // 终态记录清理音频文件
        for r in &m.records {
            if matches!(r.state, OutboxState::Done | OutboxState::TerminalError) {
                let _ = std::fs::remove_file(audio_dir.join(&r.file));
            }
        }
        let stats = outbox::stats_of(&m);
        persist_manifest(state_dir, &m, stopped);
        drop(m);
        emit(EngineEvent::Outbox { stats });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::outbox::{make_record, OutboxState};
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn engine_event_payload_fields_are_camel_case() {
        let session_state = serde_json::to_value(EngineEvent::SessionState {
            status: "recording".into(),
            radio_mode: "both".into(),
        })
        .expect("session state should serialize");
        assert_eq!(session_state["kind"], "sessionState");
        assert_eq!(session_state["radioMode"], "both");
        assert!(session_state.get("radio_mode").is_none());

        let watermark = serde_json::to_value(EngineEvent::SeqWatermark { next_chunk_seq: 17 })
            .expect("watermark should serialize");
        assert_eq!(watermark["kind"], "seqWatermark");
        assert_eq!(watermark["nextChunkSeq"], 17);
        assert!(watermark.get("next_chunk_seq").is_none());
    }

    #[tokio::test]
    async fn capture_deactivation_deletes_unsent_audio_and_persists_cancel_intent() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-engine-capture-stop-{}-{}",
            std::process::id(),
            now_ms()
        ));
        let state_dir = root.join("state");
        let audio_dir = root.join("audio");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        std::fs::create_dir_all(&audio_dir).expect("create audio dir");

        let mut captured = make_record(
            "captured-chunk",
            "session-1",
            "pc",
            "wav_pcm_s16le",
            0,
            "2026-08-20T00:00:00Z",
            3_000,
            "sha",
            4,
            0,
        );
        captured.state = OutboxState::Captured;
        let mut queued = make_record(
            "queued-chunk",
            "session-1",
            "pc",
            "wav_pcm_s16le",
            1,
            "2026-08-20T00:00:03Z",
            3_000,
            "sha",
            4,
            0,
        );
        queued.state = OutboxState::Queued;
        std::fs::write(audio_dir.join(&captured.file), b"wav").expect("write captured wav");
        std::fs::write(audio_dir.join(&queued.file), b"wav").expect("write queued wav");

        let mut initial = OutboxManifest::empty("session-1");
        initial.next_chunk_seq = 2;
        initial.records = vec![captured, queued];
        let manifest = Arc::new(Mutex::new(initial));
        let outbox_events = Arc::new(AtomicUsize::new(0));
        let event_counter = Arc::clone(&outbox_events);
        let emit: EngineEventEmitter = Arc::new(move |event| {
            if matches!(event, EngineEvent::Outbox { .. }) {
                event_counter.fetch_add(1, Ordering::SeqCst);
            }
        });

        apply_capture_inactive(
            crate::protocol::CAPTURE_INTERRUPTED_REASON,
            &manifest,
            &state_dir,
            &audio_dir,
            &emit,
            &AtomicBool::new(false),
        )
        .await;

        let guard = manifest.lock().await;
        assert_eq!(guard.records[0].state, OutboxState::TerminalError);
        assert_eq!(guard.records[1].state, OutboxState::CancelPending);
        assert_eq!(
            guard
                .cancel_intent
                .as_ref()
                .map(|intent| intent.through_chunk_seq),
            Some(1)
        );
        drop(guard);
        assert!(!audio_dir.join("captured-chunk.wav").exists());
        assert!(audio_dir.join("queued-chunk.wav").exists());

        let persisted: OutboxManifest = serde_json::from_str(
            &std::fs::read_to_string(state_dir.join("outbox-session-1.json"))
                .expect("read persisted manifest"),
        )
        .expect("parse persisted manifest");
        assert_eq!(persisted.records[0].state, OutboxState::TerminalError);
        assert_eq!(persisted.records[1].state, OutboxState::CancelPending);
        assert!(persisted.cancel_intent.is_some());
        assert_eq!(outbox_events.load(Ordering::SeqCst), 1);

        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[tokio::test]
    async fn capture_deactivation_falls_back_when_command_consumer_never_acknowledges() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-engine-capture-timeout-{}-{}",
            std::process::id(),
            now_ms()
        ));
        let state_dir = root.join("state");
        let audio_dir = root.join("audio");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        std::fs::create_dir_all(&audio_dir).expect("create audio dir");

        let mut record = make_record(
            "timeout-chunk",
            "session-1",
            "pc",
            "wav_pcm_s16le",
            0,
            "2026-08-20T00:00:00Z",
            3_000,
            "sha",
            4,
            0,
        );
        record.state = OutboxState::Captured;
        std::fs::write(audio_dir.join(&record.file), b"wav").expect("write wav");
        let mut initial = OutboxManifest::empty("session-1");
        initial.next_chunk_seq = 1;
        initial.records = vec![record];

        // 保留 receiver 但不消费命令，模拟 run_ws 卡在 connect_async/TLS。
        let (cmd_tx, _cmd_rx) = tokio::sync::mpsc::unbounded_channel();
        let engine = Engine {
            cmd_tx,
            manifest: Arc::new(Mutex::new(initial)),
            state_dir: state_dir.clone(),
            audio_dir: audio_dir.clone(),
            emit: Arc::new(|_| {}),
            session_id: "session-1".into(),
            stopped: Arc::new(AtomicBool::new(false)),
        };

        tokio::time::timeout(
            Duration::from_secs(1),
            engine.deactivate_capture(crate::protocol::CAPTURE_INTERRUPTED_REASON),
        )
        .await
        .expect("deactivation should use the timeout fallback");

        let guard = engine.manifest.lock().await;
        assert_eq!(guard.records[0].state, OutboxState::TerminalError);
        assert!(guard.cancel_intent.is_some());
        drop(guard);
        assert!(!audio_dir.join("timeout-chunk.wav").exists());
        assert!(state_dir.join("outbox-session-1.json").exists());

        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[tokio::test]
    async fn capture_activation_reports_when_command_consumer_never_acknowledges() {
        let (cmd_tx, _cmd_rx) = tokio::sync::mpsc::unbounded_channel();
        let engine = Engine {
            cmd_tx,
            manifest: Arc::new(Mutex::new(OutboxManifest::empty("session-1"))),
            state_dir: PathBuf::new(),
            audio_dir: PathBuf::new(),
            emit: Arc::new(|_| {}),
            session_id: "session-1".into(),
            stopped: Arc::new(AtomicBool::new(false)),
        };

        let error = tokio::time::timeout(Duration::from_secs(1), engine.activate_capture("start"))
            .await
            .expect("activation should not hang")
            .expect_err("activation without an ack must fail");
        assert!(error.contains("尚未开启"));
    }

    #[test]
    fn corrupt_manifest_is_quarantined_and_wavs_survive_startup_sweep() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-engine-corrupt-{}-{}",
            std::process::id(),
            now_ms()
        ));
        let state_dir = root.join("state");
        let audio_dir = root.join("audio");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        std::fs::create_dir_all(&audio_dir).expect("create audio dir");
        // 半截 JSON:解析必败;同时留一个 manifest 未引用的 WAV,
        // 修复前会被 startup sweep 当孤儿删光(E4)。
        std::fs::write(state_dir.join("outbox-session-1.json"), "{\"records\": [")
            .expect("write corrupt manifest");
        std::fs::write(audio_dir.join("pending-chunk.wav"), b"wav").expect("write pending wav");

        let manifest =
            startup_manifest(&state_dir, &audio_dir, "session-1", &AtomicBool::new(false));

        assert!(manifest.records.is_empty());
        assert!(
            audio_dir.join("pending-chunk.wav").exists(),
            "manifest 不可信时孤儿 WAV 必须保留"
        );
        let quarantine = state_dir.join("outbox-session-1.corrupt");
        assert!(quarantine.exists(), "损坏文件必须隔离保留");
        assert!(!state_dir.join("outbox-session-1.json").exists());
        assert_eq!(
            std::fs::read_to_string(&quarantine).expect("read quarantined file"),
            "{\"records\": [",
            "隔离文件应保留原始字节供恢复"
        );

        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[test]
    fn valid_manifest_still_sweeps_orphan_wavs() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-engine-sweep-{}-{}",
            std::process::id(),
            now_ms()
        ));
        let state_dir = root.join("state");
        let audio_dir = root.join("audio");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        std::fs::create_dir_all(&audio_dir).expect("create audio dir");
        let valid = serde_json::to_string(&OutboxManifest::empty("session-1")).expect("serialize");
        std::fs::write(state_dir.join("outbox-session-1.json"), valid).expect("write manifest");
        std::fs::write(audio_dir.join("orphan-chunk.wav"), b"wav").expect("write orphan wav");

        let manifest =
            startup_manifest(&state_dir, &audio_dir, "session-1", &AtomicBool::new(false));

        assert!(manifest.records.is_empty());
        // manifest 可信时清理照常:跳过只发生在损坏/不可读的场景
        assert!(!audio_dir.join("orphan-chunk.wav").exists());
        assert!(state_dir.join("outbox-session-1.json").exists());

        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[test]
    fn persist_manifest_after_stop_is_skipped() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-engine-stop-persist-{}-{}",
            std::process::id(),
            now_ms()
        ));
        let state_dir = root.join("state");
        std::fs::create_dir_all(&state_dir).expect("create state dir");
        let manifest = OutboxManifest::empty("session-1");
        let path = state_dir.join("outbox-session-1.json");

        let running = AtomicBool::new(false);
        persist_manifest(&state_dir, &manifest, &running);
        assert!(path.exists(), "运行中持久化照常");

        std::fs::remove_file(&path).expect("remove manifest");
        running.store(true, Ordering::SeqCst);
        persist_manifest(&state_dir, &manifest, &running);
        assert!(!path.exists(), "Stop 后的落盘必须是 no-op(E1c)");

        std::fs::remove_dir_all(root).expect("remove test dir");
    }

    #[test]
    fn atomic_write_roundtrips_and_overwrites() {
        let root = std::env::temp_dir().join(format!(
            "ai-interview-engine-atomic-{}-{}",
            std::process::id(),
            now_ms()
        ));
        std::fs::create_dir_all(&root).expect("create dir");
        let path = root.join("outbox-session-1.json");

        atomic_write(&path, "{\"nextChunkSeq\":1}");
        assert_eq!(
            std::fs::read_to_string(&path).expect("read after write"),
            "{\"nextChunkSeq\":1}"
        );

        // 覆盖写:rename 替换既有文件后读到的必须是新内容,tmp 不残留
        atomic_write(&path, "{\"nextChunkSeq\":2}");
        assert_eq!(
            std::fs::read_to_string(&path).expect("read after overwrite"),
            "{\"nextChunkSeq\":2}"
        );
        assert!(!root.join("outbox-session-1.tmp").exists());

        std::fs::remove_dir_all(root).expect("remove test dir");
    }
}
