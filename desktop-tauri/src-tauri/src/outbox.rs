//! 音频 outbox:分片状态机(纯逻辑,与 desktop/src/main/engine/outbox.ts 一致)。
//!
//! 状态:captured → sending → queued → done → released
//!       sending|queued → retryable_failed → sending
//!       sending → captured(audio_backpressure:新分片未预留可原样重发)
//!       sending|queued → cancel_pending → terminal_error
//!       any → terminal_error → released

use serde::{Deserialize, Serialize};

use crate::engine::rand_f64;
use crate::protocol::{is_retryable_error, normalize_cancel_reason};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OutboxState {
    Captured,
    Sending,
    Queued,
    CancelPending,
    Done,
    RetryableFailed,
    TerminalError,
    Released,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OutboxRecord {
    pub chunk_id: String,
    pub session_id: String,
    pub source: String,
    pub codec: String,
    pub chunk_seq: i64,
    pub captured_at: String,
    pub duration_ms: i64,
    /// wav 文件名(audio/<chunk_id>.wav)
    pub file: String,
    pub sha256: String,
    pub byte_size: u64,
    pub state: OutboxState,
    pub attempts: u32,
    pub next_attempt_at: u64,
    pub server_status: Option<String>,
    pub server_error: Option<String>,
    pub last_event_id: Option<i64>,
}

/// 需要在 WebSocket 就绪后补发的服务端取消意图。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CancelIntent {
    pub source: String,
    pub through_chunk_seq: i64,
    pub reason: String,
}

/// paid_usage_limited 连续受限达到该次数后终止:配额阻塞不会自愈,无上限
/// 重试等于每 ~30s 一次的永动循环(与 processing_failed 的上限对齐)。
pub const MAX_PAID_USAGE_LIMITED_ATTEMPTS: u32 = 3;

/// 退避:min(30s, 1s·2^attempts) ±20% 抖动。
pub fn retry_delay_ms(attempts: u32, random: f64) -> u64 {
    let base = (30_000u64).min(1000u64 << attempts.min(5)) as f64;
    let jitter = base * 0.2;
    (base - jitter / 2.0 + random * jitter) as u64
}

/// 迁移输入
#[derive(Debug, Clone)]
pub enum Transition {
    SendStarted {
        #[allow(dead_code)]
        now: u64,
    },
    #[allow(dead_code)]
    AckQueued {
        event_id: Option<i64>,
    },
    AckDuplicate {
        status: String,
    },
    AckStatus {
        status: String,
        error_code: Option<String>,
        event_id: Option<i64>,
        now: u64,
    },
    ErrorCode {
        code: String,
        now: u64,
    },
    SessionEnded,
}

fn is_terminal(state: OutboxState) -> bool {
    matches!(
        state,
        OutboxState::Done | OutboxState::TerminalError | OutboxState::Released
    )
}

/// 即时 ack / 事件 → 记录迁移(返回 None 表示无变化)。
pub fn transition(record: &OutboxRecord, input: &Transition) -> Option<OutboxRecord> {
    if is_terminal(record.state) && !matches!(input, Transition::SessionEnded) {
        return None;
    }
    let mut next = record.clone();
    match input {
        Transition::SendStarted { .. } => {
            if !matches!(
                record.state,
                OutboxState::Captured | OutboxState::RetryableFailed
            ) {
                return None;
            }
            next.state = OutboxState::Sending;
        }
        Transition::AckQueued { event_id } => {
            if record.state != OutboxState::Sending {
                return None;
            }
            next.state = OutboxState::Queued;
            next.server_status = Some("queued".into());
            next.server_error = None;
            if let Some(e) = event_id {
                next.last_event_id = Some(*e);
            }
        }
        Transition::AckDuplicate { status } => {
            if record.state == OutboxState::CancelPending {
                match status.as_str() {
                    "done" => {
                        next.state = OutboxState::Done;
                        next.server_status = Some("done".into());
                        next.server_error = None;
                    }
                    "cancelled" | "failed" => {
                        next.state = OutboxState::TerminalError;
                        next.server_status = Some(status.clone());
                    }
                    "queued" => return None,
                    _ => return None,
                }
                return Some(next);
            }
            match status.as_str() {
                "done" => {
                    next.state = OutboxState::Done;
                    next.server_status = Some("done".into());
                    next.server_error = None;
                }
                "queued" => {
                    next.state = OutboxState::Queued;
                    next.server_status = Some("queued".into());
                }
                "cancelled" => {
                    next.state = OutboxState::TerminalError;
                    next.server_status = Some("cancelled".into());
                }
                "failed" => {
                    // 重发遇到 failed:等待事件/对账给出错误码;先标记待重试
                    next.state = OutboxState::Queued;
                    next.server_status = Some("failed".into());
                }
                other => {
                    next.state = OutboxState::Queued;
                    next.server_status = Some(other.to_string());
                }
            }
        }
        Transition::AckStatus {
            status,
            error_code,
            event_id,
            now,
        } => {
            if record.state == OutboxState::CancelPending {
                match status.as_str() {
                    "done" => {
                        next.state = OutboxState::Done;
                        next.server_status = Some("done".into());
                        next.server_error = None;
                    }
                    "cancelled" | "failed" => {
                        next.state = OutboxState::TerminalError;
                        next.server_status = Some(status.clone());
                        next.server_error = error_code.clone().or(next.server_error);
                    }
                    // 取消请求尚未生效时，服务端可能重放旧 queued；必须保留取消意图。
                    "queued" => return None,
                    _ => return None,
                }
                if let Some(e) = event_id {
                    next.last_event_id = Some(*e);
                }
                return Some(next);
            }
            match status.as_str() {
                "done" => {
                    next.state = OutboxState::Done;
                    next.server_status = Some("done".into());
                    next.server_error = None;
                    if let Some(e) = event_id {
                        next.last_event_id = Some(*e);
                    }
                }
                "cancelled" => {
                    next.state = OutboxState::TerminalError;
                    next.server_status = Some("cancelled".into());
                    next.server_error = error_code.clone();
                }
                "failed" => {
                    let code = error_code.as_deref().unwrap_or("");
                    if is_retryable_error(code) {
                        let attempts = record.attempts + 1;
                        next.attempts = attempts;
                        next.server_status = Some("failed".into());
                        next.server_error = error_code.clone();
                        if code == "processing_failed"
                            && attempts >= crate::protocol::MAX_PROCESSING_FAILED_ATTEMPTS
                        {
                            next.state = OutboxState::TerminalError;
                        } else {
                            next.state = OutboxState::RetryableFailed;
                            next.next_attempt_at = now + retry_delay_ms(attempts, rand_f64());
                        }
                    } else {
                        next.state = OutboxState::TerminalError;
                        next.server_status = Some("failed".into());
                        next.server_error = error_code.clone();
                    }
                }
                "queued" => {
                    next.state = OutboxState::Queued;
                    next.server_status = Some("queued".into());
                }
                _ => return None,
            }
        }
        Transition::ErrorCode { code, now } => match code.as_str() {
            "audio_backpressure" => {
                // 未预留,回到 captured 原样重发
                if record.state == OutboxState::CancelPending {
                    next.state = OutboxState::TerminalError;
                    next.server_status = Some("cancelled".into());
                    next.server_error = Some(code.clone());
                } else if record.state == OutboxState::Sending {
                    next.state = OutboxState::Captured;
                } else {
                    return None;
                }
            }
            "invalid_audio_chunk" | "source_not_allowed" | "config_missing" => {
                next.state = OutboxState::TerminalError;
                next.server_error = Some(code.clone());
            }
            "invalid_session_state" => {
                // 会话状态与本地失步(如服务重启后会话回到 idle):
                // 不再重发该分片,标记终态等待用户重新开始会话,避免逐分片报错级联
                next.state = OutboxState::TerminalError;
                next.server_error = Some(code.clone());
            }
            "paid_usage_limited" if record.state == OutboxState::Sending => {
                let attempts = record.attempts + 1;
                next.attempts = attempts;
                next.server_error = Some(code.clone());
                if attempts >= MAX_PAID_USAGE_LIMITED_ATTEMPTS {
                    next.state = OutboxState::TerminalError;
                } else {
                    next.state = OutboxState::RetryableFailed;
                    next.next_attempt_at = now + retry_delay_ms(attempts, rand_f64());
                }
            }
            "audio_sequence_gap" if record.state == OutboxState::Sending => {
                // 等待重发(missing_predecessor 事件或对账驱动)
                next.state = OutboxState::Queued;
            }
            "audio_sequence_gap" => return None,
            _ => return None,
        },
        Transition::SessionEnded => {
            if matches!(
                record.state,
                OutboxState::Done | OutboxState::Released | OutboxState::TerminalError
            ) {
                return Some(next);
            }
            // ended 后未完成的分片被服务端 cancelled,本地标记终态
            next.state = OutboxState::TerminalError;
            next.server_error = Some("session_ended".into());
        }
    }
    Some(next)
}

// ---------- manifest ----------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OutboxManifest {
    pub session_id: String,
    pub records: Vec<OutboxRecord>,
    pub next_chunk_seq: i64,
    pub paused_until: u64,
    #[serde(default)]
    pub cancel_intent: Option<CancelIntent>,
}

impl OutboxManifest {
    pub fn empty(session_id: &str) -> Self {
        Self {
            session_id: session_id.to_string(),
            records: Vec::new(),
            next_chunk_seq: 0,
            paused_until: 0,
            cancel_intent: None,
        }
    }
}

#[derive(Debug, Default)]
pub struct CaptureCancelResult {
    pub changed: bool,
    pub files_to_delete: Vec<String>,
}

/// 停止采集时冻结本地 outbox，并持久化需要服务端完成的取消边界。
pub fn cancel_for_capture_stop(manifest: &mut OutboxManifest, reason: &str) -> CaptureCancelResult {
    let mut result = CaptureCancelResult::default();
    let mut had_cancellable = false;
    let protocol_reason = normalize_cancel_reason(reason);

    for record in &mut manifest.records {
        match record.state {
            OutboxState::Captured | OutboxState::RetryableFailed => {
                had_cancellable = true;
                record.state = OutboxState::TerminalError;
                record.server_status = Some("cancelled".into());
                record.server_error = Some(reason.to_string());
                result.files_to_delete.push(record.file.clone());
                result.changed = true;
            }
            OutboxState::Sending | OutboxState::Queued => {
                had_cancellable = true;
                record.state = OutboxState::CancelPending;
                record.server_error = Some(reason.to_string());
                result.changed = true;
            }
            OutboxState::CancelPending => {
                had_cancellable = true;
                if record.server_error.as_deref() != Some(reason) {
                    record.server_error = Some(reason.to_string());
                    result.changed = true;
                }
            }
            OutboxState::Done | OutboxState::TerminalError | OutboxState::Released => {}
        }
    }

    if had_cancellable && manifest.next_chunk_seq > 0 {
        let through_chunk_seq = manifest.next_chunk_seq - 1;
        let next_intent = match manifest.cancel_intent.as_ref() {
            Some(existing) => CancelIntent {
                source: "pc".into(),
                through_chunk_seq: existing.through_chunk_seq.max(through_chunk_seq),
                reason: protocol_reason.to_string(),
            },
            None => CancelIntent {
                source: "pc".into(),
                through_chunk_seq,
                reason: protocol_reason.to_string(),
            },
        };
        if manifest.cancel_intent.as_ref() != Some(&next_intent) {
            manifest.cancel_intent = Some(next_intent);
            result.changed = true;
        }
    }

    result
}

/// 所有属于当前取消边界的 CancelPending 均已收敛后，清除持久取消意图。
pub fn clear_cancel_intent_if_resolved(manifest: &mut OutboxManifest) -> bool {
    let Some(intent) = manifest.cancel_intent.as_ref() else {
        return false;
    };
    let unresolved = manifest.records.iter().any(|record| {
        record.state == OutboxState::CancelPending
            && record.source == intent.source
            && record.chunk_seq <= intent.through_chunk_seq
    });
    if unresolved {
        return false;
    }
    manifest.cancel_intent = None;
    true
}

/// 淘汰保留窗口:终态记录(WAV 已删)只用于去重/审计,超过该数量即淘汰最旧的。
pub const EVICT_KEEP_TERMINAL: usize = 200;

/// 淘汰「本地已终态」的旧记录,只保留最近 EVICT_KEEP_TERMINAL 条。
///
/// 背景:Done/TerminalError 只删 WAV 不删记录,manifest 每次变更又全量重写,
/// 3 小时录音 ≈ 3600 条 ≈ 1.2MB 且每 3 秒写一次。淘汰约束:
/// - 只淘汰 Done/TerminalError/Released(WAV 均已删除,无未决分片);
/// - Captured/Sending/Queued/CancelPending/RetryableFailed 永不淘汰;
/// - 从 records 头部(最旧)开始淘汰,保留的是最近 N 条终态 + 全部未决;
/// - next_chunk_seq 与 cancel_intent 不动:水位只增不减,序号分配不受影响,
///   崩溃安全等价于"旧记录从未存在"(其 WAV 已删,重发路径早已不可达)。
pub fn evict_terminal_records(manifest: &mut OutboxManifest) -> bool {
    let terminal_count = manifest
        .records
        .iter()
        .filter(|record| is_terminal(record.state))
        .count();
    if terminal_count <= EVICT_KEEP_TERMINAL {
        return false;
    }
    let excess = terminal_count - EVICT_KEEP_TERMINAL;
    let mut removed = 0usize;
    manifest.records.retain(|record| {
        if removed < excess && is_terminal(record.state) {
            removed += 1;
            false
        } else {
            true
        }
    });
    removed > 0
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct OutboxStats {
    pub captured: u32,
    pub sending: u32,
    pub queued: u32,
    pub retryable: u32,
    pub terminal: u32,
    pub released: u32,
    pub next_chunk_seq: i64,
}

pub fn stats_of(manifest: &OutboxManifest) -> OutboxStats {
    let mut stats = OutboxStats {
        next_chunk_seq: manifest.next_chunk_seq,
        ..Default::default()
    };
    for r in &manifest.records {
        match r.state {
            OutboxState::Captured => stats.captured += 1,
            OutboxState::Sending | OutboxState::Queued | OutboxState::CancelPending => {
                stats.queued += 1
            }
            OutboxState::RetryableFailed => stats.retryable += 1,
            OutboxState::Done | OutboxState::TerminalError => stats.terminal += 1,
            OutboxState::Released => stats.released += 1,
        }
    }
    stats
}

/// 新分片 → 记录。
#[allow(clippy::too_many_arguments)]
pub fn make_record(
    chunk_id: &str,
    session_id: &str,
    source: &str,
    codec: &str,
    chunk_seq: i64,
    captured_at: &str,
    duration_ms: i64,
    sha256: &str,
    byte_size: u64,
    now: u64,
) -> OutboxRecord {
    OutboxRecord {
        chunk_id: chunk_id.to_string(),
        session_id: session_id.to_string(),
        source: source.to_string(),
        codec: codec.to_string(),
        chunk_seq,
        captured_at: captured_at.to_string(),
        duration_ms,
        file: format!("{chunk_id}.wav"),
        sha256: sha256.to_string(),
        byte_size,
        state: OutboxState::Captured,
        attempts: 0,
        next_attempt_at: now,
        server_status: None,
        server_error: None,
        last_event_id: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(state: OutboxState, chunk_seq: i64) -> OutboxRecord {
        let mut record = make_record(
            &format!("chunk-{chunk_seq}"),
            "session-1",
            "pc",
            "wav_pcm_s16le",
            chunk_seq,
            "2026-08-20T00:00:00Z",
            1_000,
            "sha",
            4,
            0,
        );
        record.state = state;
        record
    }

    #[test]
    fn processing_failed_attempts_survive_queued_and_stop_after_three_failures() {
        let mut current = record(OutboxState::Sending, 0);

        for expected_attempts in 1..crate::protocol::MAX_PROCESSING_FAILED_ATTEMPTS {
            current = transition(
                &current,
                &Transition::AckStatus {
                    status: "failed".into(),
                    error_code: Some("processing_failed".into()),
                    event_id: None,
                    now: 1_000,
                },
            )
            .expect("processing failure should move to retryable");
            assert_eq!(current.state, OutboxState::RetryableFailed);
            assert_eq!(current.attempts, expected_attempts);

            current = transition(&current, &Transition::SendStarted { now: 2_000 })
                .expect("retryable record should send again");
            current = transition(
                &current,
                &Transition::AckStatus {
                    status: "queued".into(),
                    error_code: None,
                    event_id: None,
                    now: 2_000,
                },
            )
            .expect("queued ack should be accepted");
            assert_eq!(current.attempts, expected_attempts);
        }

        current = transition(
            &current,
            &Transition::AckStatus {
                status: "failed".into(),
                error_code: Some("processing_failed".into()),
                event_id: None,
                now: 3_000,
            },
        )
        .expect("third processing failure should become terminal");
        assert_eq!(current.state, OutboxState::TerminalError);
        assert_eq!(
            current.attempts,
            crate::protocol::MAX_PROCESSING_FAILED_ATTEMPTS
        );
    }

    #[test]
    fn paid_usage_limited_retries_are_capped_and_stop_after_three_hits() {
        let mut current = record(OutboxState::Sending, 0);

        for expected_attempts in 1..MAX_PAID_USAGE_LIMITED_ATTEMPTS {
            current = transition(
                &current,
                &Transition::ErrorCode {
                    code: "paid_usage_limited".into(),
                    now: 1_000,
                },
            )
            .expect("usage-limited hit should move to retryable");
            assert_eq!(current.state, OutboxState::RetryableFailed);
            assert_eq!(current.attempts, expected_attempts);
            assert!(current.next_attempt_at > 1_000);

            current = transition(&current, &Transition::SendStarted { now: 2_000 })
                .expect("retryable record should send again");
        }

        current = transition(
            &current,
            &Transition::ErrorCode {
                code: "paid_usage_limited".into(),
                now: 3_000,
            },
        )
        .expect("third usage-limited hit should become terminal");
        assert_eq!(current.state, OutboxState::TerminalError);
        assert_eq!(current.attempts, MAX_PAID_USAGE_LIMITED_ATTEMPTS);
        assert_eq!(current.server_error.as_deref(), Some("paid_usage_limited"));
        // 终态后再收到同一错误码不再迁移,退避循环真正终止
        assert!(transition(
            &current,
            &Transition::ErrorCode {
                code: "paid_usage_limited".into(),
                now: 4_000,
            },
        )
        .is_none());
    }

    #[test]
    fn capture_stop_terminalizes_unsent_and_persists_server_cancel_intent() {
        let mut manifest = OutboxManifest::empty("session-1");
        manifest.next_chunk_seq = 4;
        manifest.records = vec![
            record(OutboxState::Captured, 0),
            record(OutboxState::RetryableFailed, 1),
            record(OutboxState::Sending, 2),
            record(OutboxState::Queued, 3),
        ];

        let result = cancel_for_capture_stop(&mut manifest, "capture_stopped");

        assert!(result.changed);
        assert_eq!(result.files_to_delete, vec!["chunk-0.wav", "chunk-1.wav"]);
        assert_eq!(manifest.records[0].state, OutboxState::TerminalError);
        assert_eq!(manifest.records[1].state, OutboxState::TerminalError);
        assert_eq!(manifest.records[2].state, OutboxState::CancelPending);
        assert_eq!(manifest.records[3].state, OutboxState::CancelPending);
        assert_eq!(
            manifest.cancel_intent,
            Some(CancelIntent {
                source: "pc".into(),
                through_chunk_seq: 3,
                reason: "capture_stopped".into(),
            })
        );
    }

    #[test]
    fn interrupted_capture_keeps_local_reason_but_normalizes_server_cancel_reason() {
        let mut manifest = OutboxManifest::empty("session-1");
        manifest.next_chunk_seq = 1;
        manifest.records = vec![record(OutboxState::Sending, 0)];

        cancel_for_capture_stop(&mut manifest, crate::protocol::CAPTURE_INTERRUPTED_REASON);

        assert_eq!(
            manifest.records[0].server_error.as_deref(),
            Some(crate::protocol::CAPTURE_INTERRUPTED_REASON)
        );
        assert_eq!(
            manifest
                .cancel_intent
                .as_ref()
                .map(|intent| intent.reason.as_str()),
            Some(crate::protocol::CAPTURE_STOPPED_REASON)
        );
    }

    #[test]
    fn cancel_pending_ignores_queued_but_converges_on_done_or_cancelled() {
        let pending = record(OutboxState::CancelPending, 0);
        assert!(transition(
            &pending,
            &Transition::AckStatus {
                status: "queued".into(),
                error_code: None,
                event_id: None,
                now: 0,
            }
        )
        .is_none());

        let done = transition(
            &pending,
            &Transition::AckStatus {
                status: "done".into(),
                error_code: None,
                event_id: Some(10),
                now: 0,
            },
        )
        .expect("done should win over cancellation");
        assert_eq!(done.state, OutboxState::Done);

        let cancelled = transition(
            &pending,
            &Transition::AckStatus {
                status: "cancelled".into(),
                error_code: Some("capture_stopped".into()),
                event_id: Some(11),
                now: 0,
            },
        )
        .expect("cancelled ack should converge");
        assert_eq!(cancelled.state, OutboxState::TerminalError);
        assert_eq!(cancelled.server_status.as_deref(), Some("cancelled"));
    }

    #[test]
    fn cancel_intent_clears_only_after_all_pending_records_converge() {
        let mut manifest = OutboxManifest::empty("session-1");
        manifest.cancel_intent = Some(CancelIntent {
            source: "pc".into(),
            through_chunk_seq: 2,
            reason: "capture_stopped".into(),
        });
        manifest.records = vec![record(OutboxState::CancelPending, 1)];

        assert!(!clear_cancel_intent_if_resolved(&mut manifest));
        manifest.records[0].state = OutboxState::TerminalError;
        assert!(clear_cancel_intent_if_resolved(&mut manifest));
        assert!(manifest.cancel_intent.is_none());
    }

    #[test]
    fn terminal_eviction_keeps_recent_window_and_never_touches_pending() {
        let mut manifest = OutboxManifest::empty("session-1");
        let total = EVICT_KEEP_TERMINAL + 150;
        manifest.records = (0..total)
            .map(|seq| {
                // 最旧的一条留给未决分片:证明淘汰绝不越过未决记录
                if seq == 3 {
                    record(OutboxState::Queued, seq as i64)
                } else {
                    record(OutboxState::Done, seq as i64)
                }
            })
            .collect();
        manifest.next_chunk_seq = total as i64;
        manifest.cancel_intent = Some(CancelIntent {
            source: "pc".into(),
            through_chunk_seq: total as i64 - 1,
            reason: "capture_stopped".into(),
        });

        assert!(evict_terminal_records(&mut manifest));

        // 终态只保留最近 EVICT_KEEP_TERMINAL 条;未决的 seq=3 原样保留
        assert_eq!(
            manifest
                .records
                .iter()
                .filter(|r| is_terminal(r.state))
                .count(),
            EVICT_KEEP_TERMINAL
        );
        assert!(manifest
            .records
            .iter()
            .any(|r| r.chunk_seq == 3 && r.state == OutboxState::Queued));
        // 保留的是最近的:349 条终态淘汰最旧 149 条(跨过 seq=3 的未决记录),
        // 剩余最小终态 seq 应为 150,而不是 0。
        let first_terminal_seq = manifest
            .records
            .iter()
            .find(|r| is_terminal(r.state))
            .map(|r| r.chunk_seq)
            .expect("should keep terminal records");
        assert_eq!(first_terminal_seq, 150);
        assert_eq!(manifest.records.len(), 350 - 149);
        // 水位与取消意图不受淘汰影响
        assert_eq!(manifest.next_chunk_seq, total as i64);
        assert!(manifest.cancel_intent.is_some());
    }

    #[test]
    fn terminal_eviction_is_noop_below_threshold() {
        let mut manifest = OutboxManifest::empty("session-1");
        manifest.records = (0..EVICT_KEEP_TERMINAL)
            .map(|seq| record(OutboxState::Done, seq as i64))
            .collect();
        assert!(!evict_terminal_records(&mut manifest));
        assert_eq!(manifest.records.len(), EVICT_KEEP_TERMINAL);
    }

    #[test]
    fn terminal_eviction_survives_manifest_roundtrip_with_watermark() {
        // 大量 add + ack 后的收敛:重载 manifest 水位不回退
        let mut manifest = OutboxManifest::empty("session-1");
        let total = EVICT_KEEP_TERMINAL * 3;
        manifest.records = (0..total as i64)
            .map(|seq| record(OutboxState::Done, seq as i64))
            .collect();
        manifest.next_chunk_seq = total as i64;
        evict_terminal_records(&mut manifest);

        let body = serde_json::to_string(&manifest).expect("serialize");
        let reloaded: OutboxManifest = serde_json::from_str(&body).expect("deserialize");
        assert_eq!(reloaded.next_chunk_seq, total as i64);
        assert!(reloaded.records.len() <= EVICT_KEEP_TERMINAL);
    }
}
