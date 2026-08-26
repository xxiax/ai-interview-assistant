//! 重连退避计算(纯函数,与 desktop/src/main/net/backoff.ts 语义一致)。
//! 指数退避 + 全抖动;4429(认证限流)特殊处理为 60s 等待。

pub const BACKOFF_CAP_MS: u64 = 30_000;
pub const AUTH_RATE_LIMIT_WAIT_MS: u64 = 60_000;

/// 全抖动:uniform(0, min(cap, base·2^attempt))
pub fn backoff_delay_ms(attempt: u32, random: f64) -> u64 {
    let exp = BACKOFF_CAP_MS.min(1000u64 << attempt.min(5));
    (random * exp as f64) as u64
}

/// 4429 专用:60s ± 20% 抖动(后端限流器窗口 30 次/60s 每 IP)。
pub fn auth_rate_limit_delay_ms(random: f64) -> u64 {
    let jitter = AUTH_RATE_LIMIT_WAIT_MS as f64 * 0.2;
    (AUTH_RATE_LIMIT_WAIT_MS as f64 - jitter / 2.0 + random * jitter) as u64
}

/// 是否为「不可重试、需用户介入」的关闭码。
#[allow(dead_code)]
pub fn should_reconnect(code: u16) -> bool {
    !crate::protocol::is_fatal_close_code(code) && code != 1000
}
