//! 后端 REST / WebSocket v1 协议类型(与 backend/app/models.py、protocol.py 严格对应)。
//! 序列化字段全部使用蛇形命名,和后端 JSON 一致。

use serde::{Deserialize, Serialize};

// ---------- 枚举 ----------

pub type RadioMode = String; // "pc" | "mobile" | "both"
pub type AudioSource = String; // "pc" | "mobile"
pub type SessionStatus = String; // "idle" | "recording" | "ended"

/// 可重试错误码:同 chunk_id + 相同内容重发会回到 queued。
pub const RETRYABLE_ERROR_CODES: [&str; 5] = [
    "missing_predecessor",
    "processing_failed",
    "service_restart",
    "service_shutdown",
    "usage_limited",
];

/// 同一音频分片连续处理失败达到该次数后终止，避免 failed ↔ queued 永动机。
pub const MAX_PROCESSING_FAILED_ATTEMPTS: u32 = 3;

/// 应用启动时发现遗留非终态分片，视为上次采集被中断。
pub const CAPTURE_INTERRUPTED_REASON: &str = "capture_interrupted";

/// 服务端 cancel_audio_source 允许的停止原因。
pub const CAPTURE_STOPPED_REASON: &str = "capture_stopped";
pub const SOURCE_DISABLED_REASON: &str = "source_disabled";

/// 本地可以记录更细的中断原因，但线上协议只接受两个稳定枚举值。
pub fn normalize_cancel_reason(reason: &str) -> &'static str {
    if reason == SOURCE_DISABLED_REASON {
        SOURCE_DISABLED_REASON
    } else {
        CAPTURE_STOPPED_REASON
    }
}

pub fn is_retryable_error(code: &str) -> bool {
    RETRYABLE_ERROR_CODES.contains(&code)
}

// ---------- REST 模型 ----------

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct Session {
    pub id: String,
    pub title: String,
    pub status: SessionStatus,
    pub radio_mode: RadioMode,
    pub created_at: String,
    pub ended_at: Option<String>,
    /// 岗位 JD：会话级答题背景，缺省时后端生成通用答案。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job_description: Option<String>,
    /// 简历：会话级答题背景。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resume: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct Transcript {
    pub id: i64,
    pub session_id: String,
    pub source: AudioSource,
    pub text: String,
    pub timestamp: String,
    pub seq: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub chunk_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub chunk_seq: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub captured_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct Answer {
    pub id: i64,
    pub session_id: String,
    pub question: String,
    pub answer: String,
    pub source: String, // "llm" | "search+llm"
    pub created_at: String,
    /// 线程身份：REST 历史答案据此挂回实时线程卡；手动提问为 None。
    /// 缺了这三个字段 serde 会静默丢弃，前端线程卡就拿不到"已入库"标记。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revision: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct AudioChunk {
    pub chunk_id: String,
    pub session_id: String,
    pub source: AudioSource,
    pub codec: String,
    pub chunk_seq: i64,
    pub captured_at: String,
    pub duration_ms: i64,
    pub status: String,
    pub transcript_id: Option<i64>,
    pub error_code: Option<String>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct Review {
    pub id: i64,
    pub session_id: String,
    pub content: String,
    pub source: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfigItem {
    pub id: i64,
    #[serde(rename = "type")]
    pub config_type: String,
    pub name: String,
    pub data: serde_json::Value,
    pub is_active: bool,
    pub secret_configured: bool,
}

/// POST /api/configs/llm/models 响应:指定 base_url + api_key 可拉取的模型列表。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmModelsResponse {
    pub models: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct PromptResponse {
    pub prompt: String,
}

// ---------- WebSocket ----------

pub const PROTOCOL_VERSION: i32 = 1;

pub const WS_CLOSE_AUTH_FAILED: u16 = 4401;
pub const WS_CLOSE_ORIGIN_DENIED: u16 = 4403;
#[allow(dead_code)]
pub const WS_CLOSE_SESSION_NOT_FOUND: u16 = 4404;
pub const WS_CLOSE_AUTH_RATE_LIMITED: u16 = 4429;
pub const WS_CLOSE_NORMAL: u16 = 1000;
pub const WS_CLOSE_GOING_AWAY: u16 = 1001;

pub fn is_fatal_close_code(code: u16) -> bool {
    matches!(code, 4401 | 4403 | 4404)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn llm_models_response_parses_model_list() {
        let resp: LlmModelsResponse =
            serde_json::from_str(r#"{"models":["llama-3.3-70b","qwen-plus"]}"#)
                .expect("valid response should parse");
        assert_eq!(resp.models, vec!["llama-3.3-70b", "qwen-plus"]);
        // 序列化字段名保持 snake_case,与后端契约一致。
        let json = serde_json::to_value(&resp).expect("serialize");
        assert_eq!(json["models"][0], "llama-3.3-70b");
    }

    #[test]
    fn llm_models_response_accepts_empty_list() {
        let resp: LlmModelsResponse =
            serde_json::from_str(r#"{"models":[]}"#).expect("empty list should parse");
        assert!(resp.models.is_empty());
    }

    #[test]
    fn llm_models_response_rejects_missing_field() {
        assert!(serde_json::from_str::<LlmModelsResponse>(r#"{"items":[]}"#).is_err());
    }
}
