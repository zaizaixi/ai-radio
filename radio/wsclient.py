"""极简 WebSocket 客户端（纯标准库实现，无第三方依赖）。

只实现本项目需要的部分：
  * RFC 6455 握手（含 wss/TLS）
  * 文本帧 / 二进制帧收发
  * 分片重组
  * ping / pong / close 控制帧
  * 客户端必须的掩码（mask）

不支持 permessage-deflate —— B 站弹幕和 Edge-TTS 都不需要它。
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import ssl
import struct
import urllib.parse
from typing import AsyncIterator

__all__ = ["WebSocket", "WebSocketError", "connect"]

# 帧类型
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_MAX_FRAME = 64 * 1024 * 1024  # 单帧上限，防止异常数据撑爆内存


class WebSocketError(Exception):
    """WebSocket 建连或传输失败。"""


def _mask_bytes(payload: bytes, mask: bytes) -> bytes:
    """用 4 字节掩码对 payload 做 XOR。"""
    if not payload:
        return b""
    key = mask * (len(payload) // 4 + 1)
    return bytes(a ^ b for a, b in zip(payload, key))


class WebSocket:
    """一个已建立连接的 WebSocket。

    典型用法::

        ws = await connect("wss://example.com/stream", headers={...})
        await ws.send_text("hello")
        async for kind, data in ws:
            ...
        await ws.close()
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 url: str) -> None:
        self._reader = reader
        self._writer = writer
        self.url = url
        self._closed = False
        self._close_sent = False

    # ---------------------------------------------------------------- 发送

    def _build_frame(self, opcode: int, payload: bytes) -> bytes:
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        header += _mask_bytes(payload, mask)
        return bytes(header)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed and opcode != OP_CLOSE:
            raise WebSocketError("连接已关闭，无法发送")
        self._writer.write(self._build_frame(opcode, payload))
        await self._writer.drain()

    async def send_text(self, text: str) -> None:
        await self._send_frame(OP_TEXT, text.encode("utf-8"))

    async def send_bytes(self, data: bytes) -> None:
        await self._send_frame(OP_BINARY, data)

    async def ping(self, data: bytes = b"") -> None:
        await self._send_frame(OP_PING, data)

    async def pong(self, data: bytes = b"") -> None:
        await self._send_frame(OP_PONG, data)

    # ---------------------------------------------------------------- 接收

    async def _read_raw_frame(self) -> tuple[bool, int, bytes]:
        try:
            head = await self._reader.readexactly(2)
        except (asyncio.IncompleteReadError, ConnectionResetError) as exc:
            raise WebSocketError("连接被对端关闭") from exc

        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", await self._reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", await self._reader.readexactly(8))[0]

        if length > _MAX_FRAME:
            raise WebSocketError(f"帧过大: {length} 字节")

        mask = await self._reader.readexactly(4) if masked else b""
        payload = await self._reader.readexactly(length) if length else b""
        if masked:
            payload = _mask_bytes(payload, mask)
        return fin, opcode, payload

    async def recv(self) -> tuple[int, bytes]:
        """收一条完整消息，返回 (opcode, payload)。

        控制帧在内部处理掉（自动回 pong），不会返回给调用方；
        分片消息会被重组成一条。
        """
        buffer = bytearray()
        buffer_opcode: int | None = None

        while True:
            fin, opcode, payload = await self._read_raw_frame()

            if opcode == OP_CLOSE:
                self._closed = True
                if not self._close_sent:
                    self._close_sent = True
                    try:
                        self._writer.write(self._build_frame(OP_CLOSE, b""))
                        await self._writer.drain()
                    except Exception:
                        pass
                raise WebSocketError("对端发送了关闭帧")

            if opcode == OP_PING:
                await self.pong(payload)
                continue
            if opcode == OP_PONG:
                continue

            if opcode == OP_CONT:
                if buffer_opcode is None:
                    continue  # 没有起始帧的续帧，忽略
                buffer += payload
            else:
                if buffer_opcode is not None:
                    # 上一条消息没收完就来了新消息，丢弃残缺数据
                    buffer = bytearray()
                buffer_opcode = opcode
                buffer = bytearray(payload)

            if fin and buffer_opcode is not None:
                result = (buffer_opcode, bytes(buffer))
                return result

    async def __aiter__(self) -> AsyncIterator[tuple[int, bytes]]:
        while not self._closed:
            try:
                yield await self.recv()
            except WebSocketError:
                return

    # ---------------------------------------------------------------- 关闭

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if not self._close_sent:
                self._close_sent = True
                self._writer.write(self._build_frame(OP_CLOSE, b""))
                await self._writer.drain()
        except Exception:
            pass
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


async def connect(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    ssl_context: ssl.SSLContext | None = None,
    timeout: float = 20.0,
    proxy: str | None = None,
) -> WebSocket:
    """建立 WebSocket 连接并完成握手。

    proxy 支持 HTTP 代理（如 http://127.0.0.1:7890），用于 Edge-TTS
    在部分地区被 403 时走代理。
    """
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme in ("wss", "https")
    if parts.scheme not in ("ws", "wss", "http", "https"):
        raise WebSocketError(f"不支持的 URL: {url}")

    host = parts.hostname
    if not host:
        raise WebSocketError(f"URL 缺少主机名: {url}")
    port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    if secure and ssl_context is None:
        ssl_context = ssl.create_default_context()

    try:
        if proxy:
            sock = await asyncio.wait_for(
                asyncio.to_thread(_open_proxy_tunnel, proxy, host, port, timeout),
                timeout=timeout + 5,
            )
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    sock=sock, ssl=ssl_context,
                    server_hostname=host if secure else None,
                ),
                timeout=timeout,
            )
        else:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host, port, ssl=ssl_context, server_hostname=host if secure else None
                ),
                timeout=timeout,
            )
    except Exception as exc:
        raise WebSocketError(f"无法连接 {host}:{port} —— {exc}") from exc

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    default_port = 443 if secure else 80
    host_header = host if port == default_port else f"{host}:{port}"

    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")

    try:
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        await writer.drain()
    except Exception as exc:
        writer.close()
        raise WebSocketError(f"发送握手请求失败: {exc}") from exc

    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    except asyncio.TimeoutError as exc:
        writer.close()
        raise WebSocketError("握手超时，对端没有响应") from exc
    except Exception as exc:
        writer.close()
        raise WebSocketError(f"握手失败: {exc}") from exc

    text = raw.decode("latin-1")
    status_line = text.split("\r\n", 1)[0]
    if " 101" not in status_line:
        writer.close()
        raise WebSocketError(f"握手被拒绝: {status_line.strip()}")

    return WebSocket(reader, writer, url)


def _open_proxy_tunnel(proxy: str, host: str, port: int, timeout: float) -> socket.socket:
    """通过 HTTP 代理建一条到 host:port 的隧道（阻塞，跑在线程里）。"""
    parts = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    proxy_host = parts.hostname
    if not proxy_host:
        raise WebSocketError(f"代理地址无法解析: {proxy!r}")
    proxy_port = parts.port or (443 if parts.scheme == "https" else 8080)

    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        lines = [
            f"CONNECT {host}:{port} HTTP/1.1",
            f"Host: {host}:{port}",
            "Proxy-Connection: Keep-Alive",
        ]
        if parts.username:
            raw = f"{parts.username}:{parts.password or ''}".encode("utf-8")
            lines.append("Proxy-Authorization: Basic " + base64.b64encode(raw).decode("ascii"))
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))

        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("代理在建立隧道时断开了连接")
            buffer += chunk
            if len(buffer) > 65536:
                raise WebSocketError("代理返回的响应头异常地大")

        status_line = buffer.split(b"\r\n", 1)[0].decode("latin-1")
        if " 200" not in status_line:
            raise WebSocketError(f"代理拒绝建立隧道: {status_line.strip()}")
    except Exception:
        sock.close()
        raise
    sock.settimeout(None)
    return sock
