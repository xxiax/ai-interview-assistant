from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_container_healthcheck_uses_trusted_https_proxy_headers():
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    compose = (BACKEND_DIR / "compose.prod.yml").read_text(encoding="utf-8")

    assert "scripts/healthcheck.py" in dockerfile
    assert "scripts/healthcheck.py" in compose
    assert '"127.0.0.1,172.16.0.0/12"' in dockerfile


def test_compose_passes_through_runtime_read_env_vars():
    """代码实际读取但 compose 白名单缺失的变量必须透传（B7）。

    AI_OUTBOUND_PROXY（出网代理覆盖）与 AI_FFPROBE_PATH（自定义 ffprobe）
    在 app/asr.py 读取；AI_DOCS_ENABLED 在 app/main.py 读取。
    """
    compose = (BACKEND_DIR / "compose.prod.yml").read_text(encoding="utf-8")
    for name in ("AI_OUTBOUND_PROXY", "AI_FFPROBE_PATH", "AI_DOCS_ENABLED"):
        assert f"{name}:" in compose, f"{name} 必须在 compose environment 中透传"


def test_windows_requirements_resolve():
    """requirements.lock 必须能在 Windows 解析（B5）。

    旧 lock 以 --python-platform x86_64-manylinux 生成：0 个 win_amd64
    哈希且钉死 linux-only 的 uvloop，Windows pip install 必然失败。
    现改为 --universal：同时携带 Windows/Linux 哈希，uvloop 带平台标记。
    """
    lock = (BACKEND_DIR / "requirements.lock").read_text(encoding="utf-8")
    req_in = (BACKEND_DIR / "requirements.in").read_text(encoding="utf-8")
    assert "python-socks" in lock, "SOCKS 代理依赖（app/asr.py）必须进 lock"
    assert "python-socks" in req_in
    # uvloop 必须带非 Windows 平台标记，否则 Windows 安装失败
    uvloop_lines = [line for line in lock.splitlines() if line.startswith("uvloop==")]
    assert uvloop_lines and "sys_platform != 'win32'" in uvloop_lines[0], (
        "uvloop 必须带 sys_platform != 'win32' 标记"
    )
    # universal 编译的哈希应包含 Windows 轮子（以 colorama win32 标记为代表）
    assert "colorama==0.4.6 ; sys_platform == 'win32'" in lock
