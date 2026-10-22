# -*- coding: utf-8 -*-
"""ops/watchdog.py — 看板服务守护：端口没在应答就拉起，起来后验证 HTTP 200。

用法：
    python ops/watchdog.py              # 检查一次（供计划任务每 10 分钟跑）
    python ops/watchdog.py --foreground # 常驻循环，每 60 秒检查一次
    python ops/watchdog.py --force      # 服务在跑也重启一次（用于升级代码后）

服务拉起方式：复用 manual_server.py --daemon（日志写 Database/manual.log）。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "manual_server.py")
PORT = 8795
LOG = os.path.join(HERE, "..", "manual.log")
LISTENING_STATE = "LISTENING"
NETSTAT_COMMAND = ["netstat", "-ano", "-p", "TCP"]
SERVICE_STOP_TIMEOUT_SECONDS = 10
STARTUP_RETRY_COUNT = 15
RETRY_INTERVAL_SECONDS = 1
RUNTIME_CONFIG = os.path.join(HERE, "runtime.json")


def runtime_config():
    with open(RUNTIME_CONFIG, encoding="utf-8") as handle:
        settings = json.load(handle)
    cert = settings.get("tls_cert_file") or os.environ.get("FREIGHT_TLS_CERT_FILE", "")
    key = settings.get("tls_key_file") or os.environ.get("FREIGHT_TLS_KEY_FILE", "")
    if bool(cert) != bool(key):
        raise ValueError("HTTPS 证书和私钥必须同时配置")
    url = settings.get("health_base_url") or (("https://localhost" if cert else "http://127.0.0.1") + ":" + str(PORT))
    parsed = urlparse(url)
    if (parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.port != PORT
            or parsed.scheme != ("https" if cert else "http") or parsed.path not in ("", "/")):
        raise ValueError("健康探测必须使用当前服务的本机地址和正确协议")
    center_auth_url = str(settings.get("center_auth_url") or os.environ.get("NAVIGATOR_CENTER_AUTH_URL", "")).rstrip("/")
    if center_auth_url and not center_auth_url.startswith(("http://", "https://")):
        raise ValueError("中心身份地址必须是 HTTP 或 HTTPS URL")
    trusted_proxy_https = settings.get("trusted_proxy_https") is True
    return {**settings, "tls_cert_file": cert, "tls_key_file": key, "health_base_url": url.rstrip("/"),
            "center_auth_url": center_auth_url, "trusted_proxy_https": trusted_proxy_https}


def runtime_backend() -> str:
    backend = runtime_config()["backend"]
    if backend not in ("sqlite", "postgres"):
        raise ValueError("未知数据库后端配置")
    return backend


def parse_listener_pids(netstat_output: str, port: int) -> list[int]:
    """Return PIDs listening on exactly ``port`` from Windows netstat output."""
    port_pattern = re.compile(
        rf"^\s*TCP\s+\S+:{port}\s+\S+\s+{LISTENING_STATE}\s+(\d+)\s*$",
        re.IGNORECASE,
    )
    return [int(match.group(1)) for line in netstat_output.splitlines()
            if (match := port_pattern.match(line))]


def listener_pids(port: int) -> list[int]:
    result = subprocess.run(NETSTAT_COMMAND, capture_output=True, text=True, check=True)
    return parse_listener_pids(result.stdout, port)


def port_up(port: int) -> bool:
    return bool(listener_pids(port))


def http_ok(expected_backend: str | None = None) -> bool:
    try:
        try:
            response = urllib.request.urlopen(runtime_config()["health_base_url"] + "/api/health/live", timeout=5)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                return False
            # One-time compatibility with a pre-auth service during its upgrade.
            response = urllib.request.urlopen(runtime_config()["health_base_url"] + "/api/stats", timeout=5)
        with response as r:
            if r.status != 200:
                return False
            if expected_backend is None:
                return True
            result = json.load(r)
            return result.get("data", result).get("backend") == expected_backend
    except Exception:
        return False


def start() -> None:
    print("服务未在应答，拉起 daemon ...")
    backend = runtime_backend()
    settings = runtime_config()
    command = [sys.executable, SERVER, "--daemon", "--backend", backend]
    if settings["center_auth_url"]:
        command.extend(["--center-auth-url", settings["center_auth_url"]])
    if settings["tls_cert_file"]:
        command.extend(["--tls-cert-file", settings["tls_cert_file"], "--tls-key-file", settings["tls_key_file"]])
    child_env = os.environ.copy()
    if settings["trusted_proxy_https"]:
        child_env["NAVIGATOR_TRUSTED_PROXY_HTTPS"] = "1"
    subprocess.run(command, cwd=os.path.dirname(SERVER), env=child_env, check=True)
    for _ in range(STARTUP_RETRY_COUNT):
        time.sleep(RETRY_INTERVAL_SECONDS)
        if http_ok(backend):
            print("已就绪：HTTP 200")
            return
    print("拉起后 15 秒仍未就绪，请查日志 %s" % LOG)
    sys.exit(1)


def stop_healthy_listener() -> None:
    """Stop only the verified local freight service currently owning ``PORT``."""
    if not http_ok():
        raise RuntimeError("端口被占用但运价服务健康检查失败，拒绝终止未知进程")

    pids = listener_pids(PORT)
    if not pids:
        return
    print("--force：停止已通过健康检查的旧实例 PID=%s" % ",".join(map(str, pids)))
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=True)

    deadline = time.monotonic() + SERVICE_STOP_TIMEOUT_SECONDS
    while port_up(PORT) and time.monotonic() < deadline:
        time.sleep(RETRY_INTERVAL_SECONDS)
    if port_up(PORT):
        raise RuntimeError("旧实例在 %d 秒内未停止" % SERVICE_STOP_TIMEOUT_SECONDS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--foreground", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.force and port_up(PORT):
        stop_healthy_listener()
        start()
        return
    if args.foreground:
        while True:
            if not http_ok(runtime_backend()):
                start()
            time.sleep(60)
    else:
        if http_ok(runtime_backend()):
            print("服务正常（HTTP 200），无需动作")
        else:
            start()


if __name__ == "__main__":
    main()
