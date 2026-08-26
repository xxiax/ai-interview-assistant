"""容器内就绪探针，模拟来自受信任 HTTPS 反向代理的请求。"""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    allowed_hosts = [
        item.strip()
        for item in os.environ.get("AI_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    ]
    host = allowed_hosts[0] if allowed_hosts else "127.0.0.1"
    request = urllib.request.Request(
        "http://127.0.0.1:8000/health/ready",
        headers={"Host": host, "X-Forwarded-Proto": "https"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError(f"健康检查返回 HTTP {response.status}")
            response.read()
    except Exception as exc:  # noqa: BLE001 - 探针必须把所有失败转换为非零退出码
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
