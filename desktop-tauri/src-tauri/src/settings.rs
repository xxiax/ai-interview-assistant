//! 应用设置持久化:serverUrl 明文 JSON;Token 走 OS 凭据管理器(Windows Credential Manager)。
//! 对应 desktop/src/main/settings/store.ts(Electron safeStorage → keyring)。

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

const KEYRING_SERVICE: &str = "com.aiinterview.desktop";
const KEYRING_USER: &str = "auth-token";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct StoredSettings {
    #[serde(rename = "serverUrl", default)]
    pub server_url: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AppSettings {
    pub server_url: String,
    pub has_token: bool,
}

fn settings_path(app_data_dir: &Path) -> PathBuf {
    app_data_dir.join("settings.json")
}

fn atomic_write(path: &Path, data: &str) {
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
    drop(f);
    match std::fs::rename(&tmp, path) {
        Ok(()) => {}
        Err(_) => {
            let _ = std::fs::remove_file(path);
            if std::fs::rename(&tmp, path).is_err() {
                let _ = std::fs::copy(&tmp, path).map(|_| ());
                let _ = std::fs::remove_file(&tmp);
            }
        }
    }
}

pub fn load_settings(app_data_dir: &Path) -> StoredSettings {
    let Ok(raw) = std::fs::read_to_string(settings_path(app_data_dir)) else {
        return Default::default();
    };
    serde_json::from_str(&raw).unwrap_or_default()
}

pub fn save_settings(app_data_dir: &Path, settings: &StoredSettings) {
    if let Ok(body) = serde_json::to_string_pretty(settings) {
        atomic_write(&settings_path(app_data_dir), &body);
    }
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
