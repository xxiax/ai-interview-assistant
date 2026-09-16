//! 应用设置:后端地址写死为本机 loopback;Token 走 OS 凭据管理器(Windows Credential Manager)。
//!
//! 后端与本客户端始终运行在同一台机器上(上线后后端与 ASR 同机),因此服务器地址
//! 不再是用户可配置项,也不再有 `settings.json`。唯一的本地设置是访问令牌。

use serde::Serialize;

const KEYRING_SERVICE: &str = "com.aiinterview.desktop";
const KEYRING_USER: &str = "auth-token";

/// 后端固定地址。改端口只能改这里并重新构建;运行时不可配置。
pub const SERVER_URL: &str = "http://127.0.0.1:8000";

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AppSettings {
    pub has_token: bool,
}

// ---------- Token(OS 凭据管理器) ----------

pub fn save_token(token: &str) -> Result<(), String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, KEYRING_USER)
        .map_err(|e| format!("凭据存储不可用:{e}"))?;
    entry
        .set_password(token)
        .map_err(|e| format!("令牌保存失败:{e}"))
}

pub fn load_token() -> Option<String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, KEYRING_USER).ok()?;
    entry.get_password().ok()
}
