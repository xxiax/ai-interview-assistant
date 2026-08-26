from __future__ import annotations

import asyncio
import time

import pytest

from app import security


def test_secret_encryption_round_trip():
    ciphertext = security.encrypt_secret("top-secret")
    assert "top-secret" not in ciphertext
    assert security.decrypt_secret(ciphertext) == "top-secret"


def test_external_url_rejects_credentials_query_and_private_ip(monkeypatch):
    with pytest.raises(ValueError):
        security.validate_external_base_url("https://user:pass@example.com/v1")
    with pytest.raises(ValueError):
        security.validate_external_base_url("https://example.com/v1?token=x")

    monkeypatch.setenv("AI_ALLOW_PRIVATE_LLM_HOSTS", "false")
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="内网"):
        security.validate_external_base_url("https://internal.example/v1")

    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    assert security.validate_external_base_url(
        "https://not-allowed.example/v1"
    ) == "https://not-allowed.example/v1"
    assert security.validate_external_base_url("https://llm.example/v1") == "https://llm.example/v1"


def test_external_url_pins_the_validated_public_address(monkeypatch):
    monkeypatch.setenv("AI_ALLOW_PRIVATE_LLM_HOSTS", "false")
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args: [(None, None, None, None, ("93.184.216.34", 443))],
    )

    endpoint = security.resolve_external_base_url("https://llm.example:8443/v1")

    assert endpoint.original_url == "https://llm.example:8443/v1"
    assert endpoint.request_base_url == "https://93.184.216.34:8443/v1"
    assert endpoint.host_header == "llm.example:8443"
    assert endpoint.sni_hostname == "llm.example"


def test_external_url_keeps_hostname_for_clash_fake_ip(monkeypatch):
    """fake-IP 只对本机代理有意义，不能作为 HTTP 代理的 CONNECT 目标。"""
    monkeypatch.setenv("AI_ALLOW_PRIVATE_LLM_HOSTS", "false")
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args: [(None, None, None, None, ("198.18.38.172", 443))],
    )

    endpoint = security.resolve_external_base_url("https://llm.example/v1")

    assert endpoint.original_url == "https://llm.example/v1"
    assert endpoint.request_base_url == "https://llm.example/v1"
    assert endpoint.host_header == "llm.example"
    assert endpoint.sni_hostname == "llm.example"


def test_explicit_proxy_uses_original_hostname_after_dns_validation(monkeypatch):
    monkeypatch.setenv("AI_ALLOW_PRIVATE_LLM_HOSTS", "false")
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    endpoint = security.resolve_external_base_url("https://llm.example/v1")

    proxy_url, proxy_extensions = security.external_request_target(
        endpoint, {"proxy": "http://127.0.0.1:7897"}
    )
    direct_url, direct_extensions = security.external_request_target(endpoint, {})

    assert proxy_url == "https://llm.example/v1"
    assert proxy_extensions == {}
    assert direct_url == "https://93.184.216.34/v1"
    assert direct_extensions == {"sni_hostname": "llm.example"}


def test_openai_base_url_adds_v1_only_when_path_is_empty():
    assert security.normalize_openai_base_url("https://llm.example") == "https://llm.example/v1"
    assert security.normalize_openai_base_url("https://llm.example/") == "https://llm.example/v1"
    assert security.normalize_openai_base_url("https://llm.example/api") == "https://llm.example/api"
    assert security.normalize_openai_base_url("https://llm.example/v1") == "https://llm.example/v1"


@pytest.mark.asyncio
async def test_external_url_dns_validation_uses_worker_thread(monkeypatch):
    called = {}

    async def fake_to_thread(func, *args):
        called["func"] = func
        called["args"] = args
        return "https://llm.example/v1"

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    result = await security.validate_external_base_url_async("https://llm.example/v1")
    assert result == "https://llm.example/v1"
    assert called == {
        "func": security.validate_external_base_url,
        "args": ("https://llm.example/v1",),
    }


def test_runtime_security_config_rejects_invalid_limits(monkeypatch):
    monkeypatch.setenv("AI_AUDIO_QUEUE_SIZE", "0")
    with pytest.raises(RuntimeError, match="AI_AUDIO_QUEUE_SIZE"):
        security.validate_security_config()
    monkeypatch.setenv("AI_AUDIO_QUEUE_SIZE", "8")
    monkeypatch.setenv("AI_ALLOWED_ORIGINS", "*")
    with pytest.raises(RuntimeError, match="通配符"):
        security.validate_security_config()


def test_rest_rate_limit_is_applied_after_auth(client, auth_headers, monkeypatch):
    monkeypatch.setenv("AI_REST_RATE_LIMIT_PER_MINUTE", "1")
    assert client.get("/api/sessions", headers=auth_headers).status_code == 200
    assert client.get("/api/sessions", headers=auth_headers).status_code == 429


def test_rate_limiter_recycles_one_shot_keys():
    """一次性键（公网扫描的 ws-auth:{ip}）过期后必须被整体回收。

    B9 回归：此前空桶 key 永留 defaultdict，事件过期后若键不再被访问，
    内存仍随历史独立 IP 数单调增长。用短窗口模拟时间推进。
    """
    limiter = security.SlidingWindowRateLimiter()
    for index in range(200):
        limiter.allow(f"ws-auth:10.0.{index // 256}.{index % 256}", 30, 0.05)
    assert len(limiter._events) == 200, "窗口内事件应保留"

    time.sleep(0.1)  # 让全部事件过期
    limiter.allow("ws-auth:new-client", 30, 0.05)  # 触发摊销式全量清扫
    assert "ws-auth:10.0.0.1" not in limiter._events, "过期的一次性键必须被回收"
    assert len(limiter._events) == 1


def test_rate_limiter_still_enforces_limit_within_window():
    limiter = security.SlidingWindowRateLimiter()
    key = "ws-auth:1.2.3.4"
    assert limiter.allow(key, 2, 60) is True
    assert limiter.allow(key, 2, 60) is True
    assert limiter.allow(key, 2, 60) is False


def test_cgnat_range_is_blocked():
    """RFC6598 CGNAT（100.64/10，含云元数据 100.100.100.200）必须拦截。

    该段不属于 ip.is_private，需显式包含。
    """
    for address in ("100.64.0.1", "100.100.100.200", "100.127.255.255"):
        assert security._is_blocked_ip(address) is True, address
    # 段外边界不得误伤
    for address in ("100.63.255.255", "100.128.0.1", "8.8.8.8"):
        assert security._is_blocked_ip(address) is False, address


def test_fake_ip_range_from_tun_proxy_is_allowed():
    """Clash TUN fake-IP 段(198.18.0.0/15)不拦截,本机代理场景的正常流量。"""
    assert not security._is_blocked_ip("198.18.38.172")
    assert not security._is_blocked_ip("198.19.255.1")


def test_fake_ip_adjacent_ranges_still_blocked():
    """fake-IP 段之外的私网/保留地址仍然拦截(198.17/198.20 属公网未分配段,本就不拦)。"""
    assert security._is_blocked_ip("127.0.0.1")
    assert security._is_blocked_ip("10.0.0.1")
    assert security._is_blocked_ip("192.168.1.1")
    assert security._is_blocked_ip("169.254.1.1")
    assert security._is_blocked_ip("100.100.100.200")
