"""最小验证脚本：本机直连 ModelHub 的 OpenAI 兼容端点，依次核对 DNS、密钥、模型 ID。

用法（密钥只进环境变量，不写进脚本）：
    $env:LIBY_API_KEY = "<你的密钥>"
    .venv/Scripts/python.exe -X utf8 scripts/probe_modelhub.py
    # 想换地址验证时：.venv/Scripts/python.exe -X utf8 scripts/probe_modelhub.py https://modelhub.liby.com.cn/v1

默认地址就是截图里的 https://modelhubb.liby.com.cn/v1，用于复现"测试并添加"的失败。
"""

import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://modelhubb.liby.com.cn/v1"
MODEL_ID = "liby-backend-qwen3.8-max"


def check_dns(base_url: str) -> None:
    host = urllib.parse.urlsplit(base_url).hostname
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        print(f"[DNS ] {host} -> {', '.join(ips)}")
    except socket.gaierror as exc:
        print(f"[DNS ] {host} 解析失败：{exc}")
        print("[HINT] 本机 DNS 解析不到这个域名，后面的请求不可能发出去。")
        sys.exit(1)


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    check_dns(base)

    key = os.environ.get("LIBY_API_KEY", "")
    if not key:
        print("[KEY ] 未设置 LIBY_API_KEY 环境变量，DNS 检查已完成，POST 未发送。")
        sys.exit(2)

    url = f"{base}/chat/completions"
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    print(f"[POST] {url}  model={MODEL_ID}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[HTTP] {resp.status} OK —— 密钥与模型 ID 均可用")
            print(f"[BODY] {resp.read(2000).decode('utf-8', 'replace')}")
    except urllib.error.HTTPError as exc:
        print(f"[HTTP] {exc.code} {exc.reason}")
        print(f"[BODY] {exc.read(2000).decode('utf-8', 'replace')}")
        hints = {
            401: "密钥无效或已过期",
            403: "密钥无权访问该模型",
            404: "模型 ID 不存在，或 base_url 路径不对",
        }
        if exc.code in hints:
            print(f"[HINT] {hints[exc.code]}")
    except urllib.error.URLError as exc:
        print(f"[NET ] 请求发不出去：{exc.reason}")
        print("[HINT] 端口不通或 TLS 握手失败——本机到该端点的网络路径有问题。")


if __name__ == "__main__":
    main()
