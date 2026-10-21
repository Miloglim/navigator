"""Capture the KB login handoff from a dedicated local browser window."""

import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import urlparse
from urllib.request import urlopen

from core.company_credentials_contracts import (
    KB_BROWSER_LOGIN_URL, KB_BROWSER_LOGIN_TIMEOUT_SECONDS,
    KB_BROWSER_POLL_SECONDS, KB_BROWSER_START_TIMEOUT_SECONDS,
    KB_BROWSER_PROFILE_DIR,
)
from ops.company_credentials import default_credential_path

WEBSOCKET_TIMEOUT_SECONDS = 5
BROWSER_HTTP_TIMEOUT_SECONDS = 3
BROWSER_CLOSE_TIMEOUT_SECONDS = 3
WEBSOCKET_HANDSHAKE_MAX_BYTES = 8192
WEBSOCKET_EXTENDED_LENGTH = 126
WEBSOCKET_LONG_LENGTH = 127
WEBSOCKET_TEXT_FRAME = 0x81
WEBSOCKET_MASK_FLAG = 0x80
WEBSOCKET_EXTENDED_MASK = 0xFE
WEBSOCKET_CLOSE_OPCODE = 8
WEBSOCKET_TEXT_OPCODE = 1


def _browser_path():
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _read_exact(connection, length):
    chunks = []
    while length:
        chunk = connection.recv(length)
        if not chunk:
            raise OSError("浏览器连接已关闭")
        chunks.append(chunk)
        length -= len(chunk)
    return b"".join(chunks)


def _browser_command(websocket_url, method, params=None):
    endpoint = urlparse(websocket_url)
    if endpoint.scheme != "ws" or endpoint.hostname != "127.0.0.1":
        raise ValueError("浏览器调试地址无效")
    with socket.create_connection((endpoint.hostname, endpoint.port), timeout=WEBSOCKET_TIMEOUT_SECONDS) as connection:
        connection.settimeout(WEBSOCKET_TIMEOUT_SECONDS)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (f"GET {endpoint.path} HTTP/1.1\r\nHost: {endpoint.netloc}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n")
        connection.sendall(request.encode("ascii"))
        handshake = b""
        while b"\r\n\r\n" not in handshake:
            chunk = connection.recv(4096)
            if not chunk:
                raise OSError("浏览器连接已关闭")
            handshake += chunk
            if len(handshake) > WEBSOCKET_HANDSHAKE_MAX_BYTES:
                raise OSError("浏览器握手失败")
        if not handshake.startswith(b"HTTP/1.1 101"):
            raise OSError("浏览器拒绝本机连接")
        payload = json.dumps({"id": 1, "method": method, "params": params or {}}).encode("utf-8")
        mask = os.urandom(4)
        size = len(payload)
        header = (bytes((WEBSOCKET_TEXT_FRAME, WEBSOCKET_MASK_FLAG | size))
                  if size < WEBSOCKET_EXTENDED_LENGTH else
                  bytes((WEBSOCKET_TEXT_FRAME, WEBSOCKET_EXTENDED_MASK)) + size.to_bytes(2, "big"))
        connection.sendall(header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))
        while True:
            first, second = _read_exact(connection, 2)
            opcode, length = first & 0x0F, second & 0x7F
            if length == WEBSOCKET_EXTENDED_LENGTH:
                length = int.from_bytes(_read_exact(connection, 2), "big")
            elif length == WEBSOCKET_LONG_LENGTH:
                length = int.from_bytes(_read_exact(connection, 8), "big")
            frame = _read_exact(connection, length)
            if opcode == WEBSOCKET_CLOSE_OPCODE:
                raise OSError("浏览器连接已关闭")
            if opcode != WEBSOCKET_TEXT_OPCODE:
                continue
            response = json.loads(frame)
            if response.get("id") == 1:
                if "error" in response:
                    raise OSError("浏览器读取登录状态失败")
                return response.get("result", {})


def _targets(port):
    with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=BROWSER_HTTP_TIMEOUT_SECONDS) as response:
        return json.load(response)


def capture_refresh_token(cancelled=lambda: False, browser_path=None, profile_path=None,
                          login_url=KB_BROWSER_LOGIN_URL, browser_flags=()):
    browser = Path(browser_path) if browser_path else _browser_path()
    if not browser or not browser.is_file():
        raise RuntimeError("未找到 Edge 或 Chrome，无法打开知识库登录")
    profile = Path(profile_path) if profile_path else default_credential_path().parent / KB_BROWSER_PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)
    active_port = profile / "DevToolsActivePort"
    active_port.unlink(missing_ok=True)
    flags = [str(browser), "--new-window", "--no-first-run", "--remote-debugging-port=0",
             "--user-data-dir=" + str(profile), *browser_flags, login_url]
    process = subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = None
    deadline = time.monotonic() + KB_BROWSER_START_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline and not cancelled():
            if active_port.exists():
                try:
                    port = int(active_port.read_text(encoding="ascii").splitlines()[0])
                    _targets(port)
                    break
                except (OSError, ValueError, IndexError):
                    port = None
            time.sleep(KB_BROWSER_POLL_SECONDS)
        if port is None:
            raise RuntimeError("知识库登录窗口启动失败，请关闭旧登录窗口后重试")
        deadline = time.monotonic() + KB_BROWSER_LOGIN_TIMEOUT_SECONDS
        login_address = urlparse(login_url)
        while time.monotonic() < deadline and not cancelled():
            if process.poll() is not None:
                raise RuntimeError("知识库登录窗口已关闭，请重新连接")
            try:
                pages = _targets(port)
                for page in pages:
                    address = urlparse(page.get("url", ""))
                    if address.scheme != login_address.scheme or address.netloc != login_address.netloc:
                        continue
                    result = _browser_command(page["webSocketDebuggerUrl"], "Runtime.evaluate",
                                              {"expression": "localStorage.getItem('kb_refresh_token')",
                                               "returnByValue": True})
                    token = result.get("result", {}).get("value")
                    if isinstance(token, str) and token:
                        return token
            except (OSError, ValueError, KeyError):
                # Navigation can replace the inspected page between discovery and evaluation.
                pass
            time.sleep(KB_BROWSER_POLL_SECONDS)
        raise RuntimeError("知识库登录未完成，请重新连接")
    finally:
        if port is not None:
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=BROWSER_HTTP_TIMEOUT_SECONDS) as response:
                    endpoint = json.load(response)["webSocketDebuggerUrl"]
                _browser_command(endpoint, "Browser.close")
            except (OSError, ValueError, KeyError):
                # The user may have closed this dedicated login window already.
                pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=BROWSER_CLOSE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=BROWSER_CLOSE_TIMEOUT_SECONDS)
