//! 与服务端 audio-chunks 的对账 diff(纯函数,与 desktop/src/main/engine/reconcile.ts 一致)。
//!
//! 语义:
//! - 相同 chunk_id → 采纳服务端状态
//! - 本地非终态但服务端无记录 → 原样重发(断在 reserve 之前)
//! - 服务端有但本地无 → 释放墓碑(外来客户端或已释放)
//! - 我方 seq 上是不同 chunk_id → seq 烧毁(唯一合法跳 seq 途径)

use std::collections::HashMap;

use crate::outbox::{OutboxManifest, OutboxState};

pub enum ReconcileAction {
    AdoptStatus {
        chunk_id: String,
        status: String,
        error_code: Option<String>,
    },
    Resend {
        chunk_id: String,
    },
    BurnSeq {
        chunk_id: String,
        #[allow(dead_code)]
        chunk_seq: i64,
    },
}

pub struct ReconcileResult {
    pub actions: Vec<ReconcileAction>,
    /// 只进不退的下一序号
    pub next_chunk_seq: i64,
    pub warnings: Vec<String>,
}

fn is_terminal_local(state: OutboxState) -> bool {
    matches!(
        state,
        OutboxState::Done | OutboxState::TerminalError | OutboxState::Released
    )
}

pub fn diff_reconcile(
    manifest: &OutboxManifest,
    server_chunks: &[crate::protocol::AudioChunk],
) -> ReconcileResult {
    let mut actions = Vec::new();
    let mut warnings = Vec::new();
    let mut by_seq: HashMap<i64, &crate::protocol::AudioChunk> = HashMap::new();
    for chunk in server_chunks {
        by_seq.insert(chunk.chunk_seq, chunk);
    }

    let mut max_seq = manifest.next_chunk_seq - 1;

    for record in &manifest.records {
        let server = by_seq.get(&record.chunk_seq).copied();
        match server {
            None => {
                if !is_terminal_local(record.state) {
                    // 服务端无记录且本地未完成 → 重发(同 chunk_id 原样)
                    actions.push(ReconcileAction::Resend {
                        chunk_id: record.chunk_id.clone(),
                    });
                }
            }
            Some(server) if server.chunk_id == record.chunk_id => {
                // 同 id:采纳服务端状态
                if server.status != "done" && !is_terminal_local(record.state) {
                    actions.push(ReconcileAction::AdoptStatus {
                        chunk_id: record.chunk_id.clone(),
                        status: server.status.clone(),
                        error_code: server.error_code.clone(),
                    });
                } else if server.status == "done"
                    && record.state != OutboxState::Done
                    && record.state != OutboxState::Released
                {
                    // 服务端 done 但本地还在跑(错过的 ack)
                    actions.push(ReconcileAction::AdoptStatus {
                        chunk_id: record.chunk_id.clone(),
                        status: "done".into(),
                        error_code: None,
                    });
                }
            }
            Some(_server) if is_terminal_local(record.state) => {
                // 已烧毁/已终态的旧记录:不再重复警告(每次重连对账都会跑,否则刷屏)
            }
            Some(server) => {
                // 不同 chunk_id 占据我方 seq → seq 烧毁
                actions.push(ReconcileAction::BurnSeq {
                    chunk_id: record.chunk_id.clone(),
                    chunk_seq: record.chunk_seq,
                });
                warnings.push(format!(
                    "分片序号 {} 已被其他客户端 ({}) 占用,本地分片 {} 已放弃",
                    record.chunk_seq,
                    &server.chunk_id[..8.min(server.chunk_id.len())],
                    &record.chunk_id[..8.min(record.chunk_id.len())]
                ));
            }
        }
    }

    for chunk in server_chunks {
        max_seq = max_seq.max(chunk.chunk_seq);
    }
    for record in &manifest.records {
        max_seq = max_seq.max(record.chunk_seq);
    }

    ReconcileResult {
        actions,
        next_chunk_seq: max_seq + 1,
        warnings,
    }
}
