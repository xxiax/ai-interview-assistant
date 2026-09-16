"""认证、秘密加密和外部地址安全校验。"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import re
import socket
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlparse, urlunparse

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

AUTH_SCHEME = HTTPBearer(auto_error=False)
MIN_AUTH_TOKEN_LENGTH = 32
ALLOWED_AUTH_FIELDS = {"authorization", "x-api-key", "api-key"}
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_auth_token() -> str:
    """读取服务端访问令牌；生产和开发环境都不允许隐式无鉴权。"""
    token = os.environ.get("AI_AUTH_TOKEN", "").strip()
    if len(token) < MIN_AUTH_TOKEN_LENGTH:
        raise RuntimeError(f"AI_AUTH_TOKEN 必须至少 {MIN_AUTH_TOKEN_LENGTH} 个字符")
    return token


def validate_security_config() -> None:
    """启动时校验认证令牌和配置加密密钥。"""
    get_auth_token()
    get_fernet()
    integer_limits = {
        "AI_REST_RATE_LIMIT_PER_MINUTE": (1, 100_000, 300),
        "AI_MAX_AUDIO_CHUNK_BYTES": (1, 8 * 1024 * 1024, 2 * 1024 * 1024),
        "AI_AUDIO_QUEUE_SIZE": (1, 10_000, 8),
        "AI_ANSWER_QUEUE_SIZE": (1, 10_000, 8),
        "AI_LLM_SESSION_MAX_CONCURRENCY": (1, 32, 3),
        "AI_LLM_MAX_CONCURRENCY": (1, 100, 4),
        "AI_SEARCH_MAX_CONCURRENCY": (1, 100, 2),
        "AI_ASR_MAX_CONCURRENCY": (1, 100, 2),
        "AI_LLM_TOKENS_PER_MINUTE": (1, 100_000_000, 60_000),
        "AI_LLM_TOKENS_PER_HOUR": (1, 1_000_000_000, 300_000),
        "AI_LLM_TOKENS_PER_DAY": (1, 10_000_000_000, 1_000_000),
        "AI_SEARCH_REQUESTS_PER_MINUTE": (1, 100_000, 30),
        "AI_SEARCH_REQUESTS_PER_HOUR": (1, 1_000_000, 300),
        "AI_SEARCH_REQUESTS_PER_DAY": (1, 10_000_000, 2_000),
        "AI_ASR_SECONDS_PER_MINUTE": (1, 86_400, 300),
        "AI_ASR_SECONDS_PER_HOUR": (1, 604_800, 3_600),
        "AI_ASR_SECONDS_PER_DAY": (1, 31_536_000, 14_400),
        "AI_MAX_AUDIO_DURATION_MS": (100, 60_000, 10_000),
        "AI_AUDIO_DURATION_TOLERANCE_MS": (0, 10_000, 750),
        "AI_LLM_ANSWER_MAX_COMPLETION_TOKENS": (1, 16_384, 512),
        "AI_LLM_REVIEW_MAX_COMPLETION_TOKENS": (1, 65_536, 2_048),
        "AI_LLM_TRANSCRIBE_MAX_TOKENS": (1, 65_536, 2_048),
        "AI_LLM_SOLVE_MAX_COMPLETION_TOKENS": (1, 65_536, 1_536),
    }
    for name, (minimum, maximum, default) in integer_limits.items():
        raw_value = os.environ.get(name, str(default))
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise RuntimeError(f"{name} 必须是整数") from exc
        if not minimum <= value <= maximum:
            raise RuntimeError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    try:
        reorder_wait = float(os.environ.get("AI_AUDIO_REORDER_WAIT_SECONDS", "5"))
    except ValueError as exc:
        raise RuntimeError("AI_AUDIO_REORDER_WAIT_SECONDS 必须是数字") from exc
    if not 0.1 <= reorder_wait <= 60:
        raise RuntimeError("AI_AUDIO_REORDER_WAIT_SECONDS 必须在 0.1 到 60 之间")
    try:
        question_grace = float(
            os.environ.get("AI_QUESTION_THREAD_GRACE_SECONDS", "6")
        )
    except ValueError as exc:
        raise RuntimeError("AI_QUESTION_THREAD_GRACE_SECONDS 必须是数字") from exc
    if not 0.5 <= question_grace <= 30:
        raise RuntimeError(
            "AI_QUESTION_THREAD_GRACE_SECONDS 必须在 0.5 到 30 之间"
        )
    try:
        paid_wait = float(os.environ.get("AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS", "1.0"))
    except ValueError as exc:
        raise RuntimeError("AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS 必须是数字") from exc
    if not 0.01 <= paid_wait <= 60:
        raise RuntimeError("AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS 必须在 0.01 到 60 之间")
    try:
        revision_ratio = float(
            os.environ.get("AI_QUESTION_REVISION_GROWTH_RATIO", "1.5")
        )
    except ValueError as exc:
        raise RuntimeError("AI_QUESTION_REVISION_GROWTH_RATIO 必须是数字") from exc
    if not 1.0 <= revision_ratio <= 5.0:
        raise RuntimeError("AI_QUESTION_REVISION_GROWTH_RATIO 必须在 1.0 到 5.0 之间")
    # FunASR 开放式 utterance:推流后收取累计 partial 的空闲窗口与总上限。
    for name, default, low, high in (
        ("AI_FUNASR_PARTIAL_IDLE_SECONDS", "0.6", 0.05, 5.0),
        ("AI_FUNASR_PARTIAL_MAX_WAIT_SECONDS", "3.0", 0.2, 15.0),
    ):
        try:
            value = float(os.environ.get(name, default))
        except ValueError as exc:
            raise RuntimeError(f"{name} 必须是数字") from exc
        if not low <= value <= high:
            raise RuntimeError(f"{name} 必须在 {low} 到 {high} 之间")
    if "*" in {
        item.strip() for item in os.environ.get("AI_ALLOWED_ORIGINS", "").split(",")
    }:
        raise RuntimeError("AI_ALLOWED_ORIGINS 不允许使用通配符 *")
    # LLM 主机白名单已移除:任意 HTTPS 主机均允许(私网仍默认拦截,见下)。


def verify_token(candidate: str) -> bool:
    """使用常量时间比较访问令牌。"""
    if not candidate:
        return False
    try:
        expected = get_auth_token()
    except RuntimeError:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def token_fingerprint(token: str) -> str:
    """为跨会话限流生成不可逆 Token 标识。"""
    return hmac.digest(
        b"ai-interview-token-fingerprint", token.encode("utf-8"), "sha256"
    ).hex()[:24]


def require_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(AUTH_SCHEME)],
) -> str:
    """FastAPI Bearer Token 认证依赖。"""
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not verify_token(credentials.credentials)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证失败",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    try:
        limit = max(1, int(os.environ.get("AI_REST_RATE_LIMIT_PER_MINUTE", "300")))
    except ValueError:
        limit = 300
    enforce_rate_limit(client_identity(request, token), limit=limit, window_seconds=60)
    return token


def get_fernet() -> Fernet:
    """读取用于加密配置秘密的 Fernet 密钥。"""
    raw_key = os.environ.get("AI_CONFIG_ENCRYPTION_KEY", "").strip()
    if not raw_key:
        raise RuntimeError("未设置 AI_CONFIG_ENCRYPTION_KEY")
    try:
        return Fernet(raw_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("AI_CONFIG_ENCRYPTION_KEY 不是有效的 Fernet 密钥") from exc


def encrypt_secret(value: str) -> str:
    """加密秘密后再写入 SQLite。"""
    return get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    """解密 SQLite 中的秘密。"""
    try:
        return get_fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise RuntimeError("配置秘密无法解密，请检查 AI_CONFIG_ENCRYPTION_KEY") from exc


def validate_auth_field(value: str) -> str:
    """限制可配置的认证 Header，避免覆盖 Host 等敏感头。"""
    normalized = value.strip()
    if (
        not HEADER_NAME_RE.fullmatch(normalized)
        or normalized.lower() not in ALLOWED_AUTH_FIELDS
    ):
        raise ValueError(f"auth_field 仅支持: {', '.join(sorted(ALLOWED_AUTH_FIELDS))}")
    return normalized


# RFC6598 运营商级 NAT（CGNAT）段。部分云厂商把元数据服务放在这里
# （如阿里云 100.100.100.200），且该段不属于 ip.is_private，必须显式拦截。
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
# Clash/mihomo TUN 模式的 fake-IP 段(RFC 2544 基准地址 198.18.0.0/15)。
# 本机开启 TUN 时所有域名都解析到这里,由代理隧道转发到真实目标;
# 对个人桌面部署这是正常流量,拦截会导致所有第三方 LLM 不可用。
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _is_blocked_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if ip in _FAKE_IP_NETWORK:
        return False
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
            ip in _CGNAT_NETWORK,
        )
    )


def validate_external_base_url_syntax(value: str) -> str:
    """同步校验 URL 结构；域名解析由异步边界显式执行。"""
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    allowed_schemes = {"https"}
    if _env_bool("AI_ALLOW_INSECURE_HTTP"):
        allowed_schemes.add("http")
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url 必须是无内嵌凭据的 HTTPS URL")

    if not _env_bool("AI_ALLOW_PRIVATE_LLM_HOSTS"):
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass  # 域名在异步校验阶段解析全部地址。
        else:
            if _is_blocked_ip(parsed.hostname):
                raise ValueError("base_url 不允许指向本机、内网或保留地址")
    return url


def normalize_openai_base_url(value: str) -> str:
    """无路径的 OpenAI-compatible 网关默认使用标准 /v1 API 前缀。"""
    url = validate_external_base_url_syntax(value)
    parsed = urlparse(url)
    if parsed.path in {"", "/"}:
        return urlunparse(parsed._replace(path="/v1"))
    return url


def validate_external_base_url(value: str) -> str:
    """完整校验 LLM Base URL，默认阻止明文 HTTP 和内网 SSRF。"""
    return resolve_external_base_url(value).original_url


@dataclass(frozen=True)
class ResolvedExternalBaseUrl:
    original_url: str
    request_base_url: str
    host_header: str
    sni_hostname: str


def external_request_target(
    endpoint: ResolvedExternalBaseUrl, client_kwargs: dict
) -> tuple[str, dict]:
    """显式代理必须接收原域名；直连才使用已校验并固定的公网 IP。"""
    if client_kwargs.get("proxy"):
        return endpoint.original_url, {}
    return endpoint.request_base_url, {"sni_hostname": endpoint.sni_hostname}


def resolve_external_base_url(value: str) -> ResolvedExternalBaseUrl:
    """校验并固定实际连接 IP，避免校验与请求之间发生 DNS 重绑定。"""
    url = validate_external_base_url_syntax(value)
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if _env_bool("AI_ALLOW_PRIVATE_LLM_HOSTS"):
        return ResolvedExternalBaseUrl(url, url, parsed.netloc, hostname)

    try:
        resolved = [
            item[4][0]
            for item in socket.getaddrinfo(hostname, parsed.port or 443)
        ]
    except socket.gaierror as exc:
        raise ValueError("base_url 域名无法解析") from exc
    addresses = list(dict.fromkeys(resolved))
    if not addresses:
        raise ValueError("base_url 域名无法解析")
    if any(_is_blocked_ip(address) for address in addresses):
        raise ValueError("base_url 不允许指向本机、内网或保留地址")

    # Clash/mihomo 的 fake-IP 是本机代理维护的域名占位符，不是真实上游地址。
    # 如果把它固定进 URL，显式 HTTP 代理会收到 CONNECT 198.18.x.x，无法再按
    # 原域名路由，最终在 TLS 握手阶段失败。全部解析结果都是 fake-IP 时保留
    # 原域名，让 TUN 或显式代理完成解析；真实公网地址仍继续固定以防 DNS 重绑定。
    pinnable_addresses = [
        address
        for address in addresses
        if ipaddress.ip_address(address) not in _FAKE_IP_NETWORK
    ]
    if not pinnable_addresses:
        return ResolvedExternalBaseUrl(url, url, parsed.netloc, hostname)

    address = pinnable_addresses[0]
    request_host = f"[{address}]" if ":" in address else address
    if parsed.port is not None:
        request_host = f"{request_host}:{parsed.port}"
    pinned_url = urlunparse(parsed._replace(netloc=request_host))
    return ResolvedExternalBaseUrl(url, pinned_url, parsed.netloc, hostname)


async def validate_external_base_url_async(value: str) -> str:
    """在线程中解析 DNS，避免在异步请求路径阻塞事件循环。"""
    return await asyncio.to_thread(validate_external_base_url, value)


async def resolve_external_base_url_async(value: str) -> ResolvedExternalBaseUrl:
    return await asyncio.to_thread(resolve_external_base_url, value)


def client_identity(request: Request, token: str) -> str:
    """生成限流键，不保存真实访问令牌。"""
    host = request.client.host if request.client else "unknown"
    return f"{host}:{hmac.digest(b'ai-interview-rate-limit', token.encode(), 'sha256').hex()[:16]}"


class SlidingWindowRateLimiter:
    """单进程滑动窗口限流器；与项目当前单 worker 部署模型一致。"""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._next_sweep = 0.0

    def _sweep_expired(self, cutoff: float) -> None:
        """删除全部事件都已过期的桶。

        未认证 WebSocket 的 ws-auth:{ip} 键在 Token 校验前就会创建；
        公网扫描会产生大量一次性 IP，若只在被再次访问时清理，
        defaultdict 会随独立键数量单调增长。
        """
        for expired_key in [
            expired_key
            for expired_key, bucket in self._events.items()
            if not bucket or bucket[-1] <= cutoff
        ]:
            self._events.pop(expired_key, None)

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            # 摊销式全量清扫：每个窗口至多一次，保证空桶最终被回收。
            if now >= self._next_sweep:
                self._sweep_expired(cutoff)
                self._next_sweep = now + window_seconds
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                self._events.pop(key, None)
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            self._events[key] = bucket
            return True

    def clear_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [
                candidate for candidate in self._events if candidate.startswith(prefix)
            ]:
                self._events.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


rate_limiter = SlidingWindowRateLimiter()


def enforce_rate_limit(key: str, *, limit: int, window_seconds: float) -> None:
    if not rate_limiter.allow(key, limit, window_seconds):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")
