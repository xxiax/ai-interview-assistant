//! REST 客户端:Bearer 认证、超时、429 Retry-After、错误翻译。
//! 对应 desktop/src/main/net/rest.ts。

use serde::{de::DeserializeOwned, Serialize};
use std::sync::OnceLock;
use std::time::Duration;

#[derive(Debug, thiserror::Error)]
pub enum RestError {
    #[error("网络错误:{0}")]
    Network(String),
    #[error("请求超时:{0}")]
    Timeout(String),
    #[error("认证失败:Token 无效或已过期")]
    Unauthorized,
    #[error("请求过于频繁,请 {0} 秒后重试")]
    RateLimited(u64),
    #[error("HTTP {0}: {1}")]
    Status(u16, String),
}

#[derive(Clone, Debug)]
pub struct RestContext {
    pub server_url: String,
    pub token: String,
}

pub type RestResult<T> = Result<T, RestError>;

fn normalize_base(server_url: &str) -> String {
    server_url.trim().trim_end_matches('/').to_string()
}

/// 进程级共享 HTTP 客户端:复用连接池,超时改为逐请求设置。
/// 构造失败时缓存错误,各请求沿用原先「按请求构造失败」的网络错误语义。
fn shared_client() -> Result<&'static reqwest::Client, RestError> {
    static CLIENT: OnceLock<Result<reqwest::Client, String>> = OnceLock::new();
    CLIENT
        .get_or_init(|| {
            reqwest::Client::builder()
                .build()
                .map_err(|e| e.to_string())
        })
        .as_ref()
        .map_err(|message| RestError::Network(message.clone()))
}

async fn request<T: DeserializeOwned>(
    ctx: &RestContext,
    method: reqwest::Method,
    path: &str,
    body: Option<&(impl Serialize + ?Sized)>,
    timeout: Duration,
) -> RestResult<T> {
    let url = format!("{}{}", normalize_base(&ctx.server_url), path);
    let client = shared_client()?;

    let mut req = client
        .request(method, &url)
        .timeout(timeout)
        .bearer_auth(&ctx.token);
    if let Some(b) = body {
        req = req.json(b);
    }
    let resp = req.send().await.map_err(|e| {
        if e.is_timeout() {
            RestError::Timeout(path.to_string())
        } else {
            RestError::Network(e.to_string())
        }
    })?;

    let status = resp.status();
    let retry_after = resp
        .headers()
        .get("Retry-After")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok());

    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(match status.as_u16() {
            401 => RestError::Unauthorized,
            429 => RestError::RateLimited(retry_after.unwrap_or(60)),
            code => {
                let detail = extract_detail(&text);
                RestError::Status(code, detail)
            }
        });
    }
    serde_json::from_str(&text)
        .map_err(|e| RestError::Status(status.as_u16(), format!("响应解析失败: {e}")))
}

/// 从 FastAPI 错误体 {"detail": "..."} 提取用户可读信息。
fn extract_detail(text: &str) -> String {
    #[derive(serde::Deserialize)]
    struct ErrBody {
        detail: serde_json::Value,
    }
    if let Ok(body) = serde_json::from_str::<ErrBody>(text) {
        match body.detail {
            serde_json::Value::String(s) => return s,
            serde_json::Value::Object(map) => {
                if let Some(serde_json::Value::String(msg)) = map.get("message") {
                    return msg.clone();
                }
            }
            serde_json::Value::Array(items) => {
                if let Some(joined) = join_validation_errors(&items) {
                    return joined;
                }
            }
            _ => {}
        }
    }
    // 按字符截断:第 200 字节落在多字节字符中间时,字节切片会 panic
    // (release 下 panic=abort 直接闪退)。中文 422 响应/网关错误页可达。
    if text.chars().count() > 200 {
        let prefix: String = text.chars().take(200).collect();
        format!("{prefix}...")
    } else {
        text.to_string()
    }
}

/// pydantic v2 的 422 响应 detail 是数组,每项形如
/// {"type":"value_error","loc":["body","data","base_url"],"msg":"...","input":...}。
/// 取每项的 msg;若 loc 存在,用最后一段做「字段: 消息」前缀提升可读性。
/// 多条以「；」拼接。数组为空或全部取不到 msg 时返回 None(走截断兜底)。
fn join_validation_errors(items: &[serde_json::Value]) -> Option<String> {
    let mut parts: Vec<String> = Vec::new();
    for item in items {
        let serde_json::Value::Object(map) = item else {
            continue;
        };
        let Some(serde_json::Value::String(msg)) = map.get("msg") else {
            continue;
        };
        let field = map
            .get("loc")
            .and_then(|loc| loc.as_array())
            .and_then(|segments| segments.last())
            .and_then(|segment| segment.as_str())
            .filter(|field| !field.is_empty());
        match field {
            // 同一字段出现多条时保留原文,不去重(后端极少这样返回)。
            Some(field) if field != msg.as_str() => parts.push(format!("{field}: {msg}")),
            _ => parts.push(msg.clone()),
        }
    }
    if parts.is_empty() {
        None
    } else {
        Some(parts.join("；"))
    }
}

/// 匿名健康检查(不带 Token)。
pub async fn check_health(server_url: &str) -> RestResult<()> {
    let client = shared_client()?;
    let url = format!("{}/health", normalize_base(server_url));
    let resp = client
        .get(&url)
        .timeout(Duration::from_secs(5))
        .send()
        .await
        .map_err(|e| {
            if e.is_timeout() {
                RestError::Timeout("health".into())
            } else {
                RestError::Network(e.to_string())
            }
        })?;
    if !resp.status().is_success() {
        return Err(RestError::Status(
            resp.status().as_u16(),
            "健康检查失败".into(),
        ));
    }
    Ok(())
}

/// 带 Token 探针:GET /api/sessions?limit=1 最低开销验证。
pub async fn probe_auth(ctx: &RestContext) -> RestResult<Vec<crate::protocol::Session>> {
    request(
        ctx,
        reqwest::Method::GET,
        "/api/sessions?limit=1&offset=0",
        None::<&serde_json::Value>,
        Duration::from_secs(10),
    )
    .await
}

// ---------- 会话 ----------

pub async fn sessions_create(
    ctx: &RestContext,
    title: &str,
) -> RestResult<crate::protocol::Session> {
    #[derive(Serialize)]
    struct Body<'a> {
        title: &'a str,
    }
    request(
        ctx,
        reqwest::Method::POST,
        "/api/sessions",
        Some(&Body { title }),
        Duration::from_secs(15),
    )
    .await
}

pub async fn sessions_list(
    ctx: &RestContext,
    limit: u32,
    offset: u32,
) -> RestResult<Vec<crate::protocol::Session>> {
    request(
        ctx,
        reqwest::Method::GET,
        &format!("/api/sessions?limit={limit}&offset={offset}"),
        None::<&serde_json::Value>,
        Duration::from_secs(15),
    )
    .await
}

pub async fn sessions_get(ctx: &RestContext, id: &str) -> RestResult<crate::protocol::Session> {
    request(
        ctx,
        reqwest::Method::GET,
        &format!("/api/sessions/{id}"),
        None::<&serde_json::Value>,
        Duration::from_secs(15),
    )
    .await
}

#[allow(dead_code)]
pub async fn sessions_start(
    ctx: &RestContext,
    id: &str,
    radio_mode: &str,
) -> RestResult<crate::protocol::Session> {
    #[derive(Serialize)]
    #[serde(rename_all = "snake_case")]
    struct Body<'a> {
        radio_mode: &'a str,
    }
    request(
        ctx,
        reqwest::Method::POST,
        &format!("/api/sessions/{id}/start"),
        Some(&Body { radio_mode }),
        Duration::from_secs(15),
    )
    .await
}

pub async fn sessions_end(ctx: &RestContext, id: &str) -> RestResult<crate::protocol::Session> {
    request(
        ctx,
        reqwest::Method::POST,
        &format!("/api/sessions/{id}/end"),
        None::<&serde_json::Value>,
        Duration::from_secs(15),
    )
    .await
}

/// 删除会话(204 无响应体)。
pub async fn sessions_delete(ctx: &RestContext, id: &str) -> RestResult<()> {
    let url = format!("{}/api/sessions/{id}", normalize_base(&ctx.server_url));
    let client = shared_client()?;
    let resp = client
        .request(reqwest::Method::DELETE, &url)
        .timeout(Duration::from_secs(15))
        .bearer_auth(&ctx.token)
        .send()
        .await
        .map_err(|e| {
            if e.is_timeout() {
                RestError::Timeout(format!("/api/sessions/{id}"))
            } else {
                RestError::Network(e.to_string())
            }
        })?;
    let status = resp.status();
    let retry_after = resp
        .headers()
        .get("Retry-After")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok());
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(match status.as_u16() {
            401 => RestError::Unauthorized,
            429 => RestError::RateLimited(retry_after.unwrap_or(60)),
            code => RestError::Status(code, extract_detail(&text)),
        });
    }
    Ok(())
}

// ---------- 历史 ----------

pub async fn transcripts(
    ctx: &RestContext,
    session_id: &str,
) -> RestResult<Vec<crate::protocol::Transcript>> {
    request(
        ctx,
        reqwest::Method::GET,
        &format!("/api/sessions/{session_id}/transcripts"),
        None::<&serde_json::Value>,
        Duration::from_secs(15),
    )
    .await
}

pub async fn answers(
    ctx: &RestContext,
    session_id: &str,
) -> RestResult<Vec<crate::protocol::Answer>> {
    request(
        ctx,
        reqwest::Method::GET,
        &format!("/api/sessions/{session_id}/answers"),
        None::<&serde_json::Value>,
        Duration::from_secs(15),
    )
    .await
}

pub async fn reviews(
    ctx: &RestContext,
    session_id: &str,
) -> RestResult<Vec<crate::protocol::Review>> {
    request(
        ctx,
        reqwest::Method::GET,
        &format!("/api/sessions/{session_id}/reviews"),
        None::<&serde_json::Value>,
        Duration::from_secs(15),
    )
    .await
}

pub async fn generate_review(
    ctx: &RestContext,
    session_id: &str,
    use_search: bool,
) -> RestResult<crate::protocol::Review> {
    #[derive(Serialize)]
    #[serde(rename_all = "snake_case")]
    struct Body {
        use_search: bool,
    }
    request(
        ctx,
        reqwest::Method::POST,
        &format!("/api/sessions/{session_id}/review"),
        Some(&Body { use_search }),
        Duration::from_secs(120),
    )
    .await
}

pub async fn audio_chunks(
    ctx: &RestContext,
    session_id: &str,
    source: &str,
    after_chunk_seq: i64,
) -> RestResult<Vec<crate::protocol::AudioChunk>> {
    request(
        ctx,
        reqwest::Method::GET,
        &format!(
            "/api/sessions/{session_id}/audio-chunks?source={source}&after_chunk_seq={after_chunk_seq}&limit=200"
        ),
        None::<&serde_json::Value>,
        Duration::from_secs(15),
    )
    .await
}

// ---------- 配置 ----------

pub async fn configs_list(
    ctx: &RestContext,
    config_type: &str,
) -> RestResult<Vec<crate::protocol::ConfigItem>> {
    request(
        ctx,
        reqwest::Method::GET,
        &format!("/api/configs/{config_type}"),
        None::<&serde_json::Value>,
        Duration::from_secs(15),
    )
    .await
}

pub async fn configs_save(
    ctx: &RestContext,
    config_type: &str,
    body: &serde_json::Value,
) -> RestResult<crate::protocol::ConfigItem> {
    request(
        ctx,
        reqwest::Method::POST,
        &format!("/api/configs/{config_type}"),
        Some(body),
        Duration::from_secs(15),
    )
    .await
}

pub async fn configs_activate(
    ctx: &RestContext,
    config_type: &str,
    id: i64,
) -> RestResult<crate::protocol::ConfigItem> {
    request(
        ctx,
        reqwest::Method::POST,
        &format!("/api/configs/{config_type}/activate/{id}"),
        None::<&serde_json::Value>,
        Duration::from_secs(15),
    )
    .await
}

/// 用给定 base_url + api_key + auth_field 拉取可用模型列表。
/// api_key 允许为空串(编辑模式不修改 key):由后端验证 URL 后决定是否复用。
pub async fn llm_fetch_models(
    ctx: &RestContext,
    base_url: &str,
    api_key: &str,
    auth_field: &str,
) -> RestResult<crate::protocol::LlmModelsResponse> {
    #[derive(Serialize)]
    struct Body<'a> {
        base_url: &'a str,
        api_key: &'a str,
        auth_field: &'a str,
    }
    request(
        ctx,
        reqwest::Method::POST,
        "/api/configs/llm/models",
        Some(&Body {
            base_url,
            api_key,
            auth_field,
        }),
        Duration::from_secs(15),
    )
    .await
}

pub async fn settings_prompt_get(ctx: &RestContext) -> RestResult<crate::protocol::PromptResponse> {
    request(
        ctx,
        reqwest::Method::GET,
        "/api/settings/prompt",
        None::<&serde_json::Value>,
        Duration::from_secs(15),
    )
    .await
}

pub async fn settings_prompt_set(
    ctx: &RestContext,
    prompt: &str,
) -> RestResult<crate::protocol::PromptResponse> {
    #[derive(Serialize)]
    struct Body<'a> {
        prompt: &'a str,
    }
    request(
        ctx,
        reqwest::Method::PUT,
        "/api/settings/prompt",
        Some(&Body { prompt }),
        Duration::from_secs(15),
    )
    .await
}

/// 删除配置(204 无响应体)。
pub async fn configs_delete(ctx: &RestContext, config_type: &str, id: i64) -> RestResult<()> {
    let url = format!(
        "{}/api/configs/{config_type}/{id}",
        normalize_base(&ctx.server_url)
    );
    let client = shared_client()?;
    let resp = client
        .request(reqwest::Method::DELETE, &url)
        .timeout(Duration::from_secs(15))
        .bearer_auth(&ctx.token)
        .send()
        .await
        .map_err(|e| RestError::Network(e.to_string()))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(match status.as_u16() {
            401 => RestError::Unauthorized,
            code => RestError::Status(code, extract_detail(&text)),
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_client_is_constructed_once() {
        let first = shared_client().expect("client should build");
        let second = shared_client().expect("client should build");
        assert!(std::ptr::eq(first, second));
    }

    #[test]
    fn detail_extraction_truncates_on_char_boundary() {
        // 300 个三字节汉字,第 200 字节落在某个字符中间:旧的字节切片会 panic。
        let cjk = "面".repeat(300);
        let detail = extract_detail(&cjk);
        assert_eq!(detail.chars().count(), 203); // 200 个字符 + "..." 省略标记
        assert!(detail.starts_with(&"面".repeat(200)));
        assert!(detail.ends_with("..."));

        // 200 字符 ASCII 恰好不超限:原样返回,无省略号。
        let ascii = "x".repeat(200);
        assert_eq!(extract_detail(&ascii), ascii);
    }

    #[test]
    fn detail_extraction_prefers_fastapi_detail_fields() {
        let body = r#"{"detail":{"message":"采样率不支持"}}"#;
        assert_eq!(extract_detail(body), "采样率不支持");
        assert_eq!(
            extract_detail(r#"{"detail":"Token 已过期"}"#),
            "Token 已过期"
        );
        // 非 JSON 网关错误页:原样返回。
        assert_eq!(extract_detail("Bad Gateway"), "Bad Gateway");
    }

    #[test]
    fn detail_extraction_joins_single_validation_error_with_field() {
        // pydantic v2 422:单条 loc + msg → 「字段: 消息」。
        let body = r#"{"detail":[{"type":"value_error","loc":["body","data","LLMConfigData","base_url"],"msg":"Value error, base_url 必须是无内嵌凭据的 HTTPS URL","input":"http://x"}]}"#;
        assert_eq!(
            extract_detail(body),
            "base_url: Value error, base_url 必须是无内嵌凭据的 HTTPS URL"
        );
    }

    #[test]
    fn detail_extraction_joins_multiple_validation_errors() {
        // 多条:以「；」拼接;缺 loc 的项只保留 msg;缺 msg 的项整体跳过。
        let body = r#"{"detail":[
            {"type":"value_error","loc":["body","data","LLMConfigData","base_url"],"msg":"Value error, base_url 必须是无内嵌凭据的 HTTPS URL"},
            {"type":"missing","loc":["body","data","LLMConfigData","api_key"],"msg":"Field required"},
            {"type":"string_type","loc":["body"],"msg":"Input should be a valid string"},
            {"type":"int_parsing","loc":["body","n"]}
        ]}"#;
        assert_eq!(
            extract_detail(body),
            "base_url: Value error, base_url 必须是无内嵌凭据的 HTTPS URL；api_key: Field required；body: Input should be a valid string"
        );
    }

    #[test]
    fn detail_extraction_falls_back_when_array_has_no_msgs() {
        // 空数组:取不到任何 msg → 退回原文本截断路径。
        assert_eq!(extract_detail(r#"{"detail":[]}"#), r#"{"detail":[]}"#);
        // 全部缺 msg 的数组同样退回原文。
        let no_msg = r#"{"detail":[{"type":"int_parsing","loc":["body","n"]},"not-an-object"]}"#;
        assert_eq!(extract_detail(no_msg), no_msg);
    }
}
