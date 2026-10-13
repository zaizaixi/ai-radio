"""B 站直播间弹幕接入（纯标准库实现）。

数据来源是 B 站直播间的公开弹幕长连接，和自己用浏览器看直播是同一条链路，
不需要开发者资质、不需要登录（登录后能拿到更多信息，见 cookies 配置）。

协议要点（B 站私有二进制协议，跑在 WebSocket 之上）：
    每个包 = 16 字节包头 + 包体
      4B  整包长度
      2B  包头长度（固定 16）
      2B  协议版本  0=纯 JSON  1=心跳/人气 2=zlib 压缩  3=brotli 压缩
      4B  操作码    2=心跳  3=人气值  5=业务消息  7=认证  8=认证回复
      4B  序列号（固定 1）
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import struct
import urllib.parse
import urllib.request
import zlib
from typing import Any, AsyncIterator

from ..wsclient import OP_BINARY, WebSocket, WebSocketError, connect
from .base import (
    KIND_DANMAKU,
    KIND_ENTER,
    KIND_FOLLOW,
    KIND_GIFT,
    KIND_LIKE,
    KIND_SUPER_CHAT,
    ChatMessage,
)

log = logging.getLogger("radio.bilibili")

# 协议操作码
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

# 协议版本
VER_PLAIN = 0
VER_HEARTBEAT = 1
VER_ZLIB = 2
VER_BROTLI = 3

HEADER_LEN = 16
HEARTBEAT_INTERVAL = 30.0
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

# 我们声明支持的协议版本。3 需要 brotli，装不上就退回 2（zlib）。
try:  # pragma: no cover - 取决于环境
    import brotli as _brotli  # type: ignore

    _HAS_BROTLI = True
except Exception:  # pragma: no cover
    _brotli = None  # type: ignore
    _HAS_BROTLI = False


class BilibiliError(Exception):
    """B 站接口调用失败。"""


# ---------------------------------------------------------------------------
# 打包 / 解包
# ---------------------------------------------------------------------------


def pack_packet(operation: int, body: bytes, protover: int = VER_HEARTBEAT) -> bytes:
    """按 B 站协议拼一个包。"""
    return struct.pack(">IHHII", HEADER_LEN + len(body), HEADER_LEN,
                       protover, operation, 1) + body


def iter_packets(data: bytes):
    """把一段字节流拆成若干 (protover, operation, body)。"""
    offset = 0
    total_len = len(data)
    while offset + HEADER_LEN <= total_len:
        length, header_len, protover, operation, _seq = struct.unpack_from(
            ">IHHII", data, offset
        )
        if length < HEADER_LEN or offset + length > total_len:
            break
        body = data[offset + header_len: offset + length]
        offset += length

        if protover == VER_ZLIB:
            try:
                yield from iter_packets(zlib.decompress(body))
            except zlib.error as exc:
                log.warning("zlib 解压失败: %s", exc)
        elif protover == VER_BROTLI:
            if _HAS_BROTLI:
                try:
                    yield from iter_packets(_brotli.decompress(body))
                except Exception as exc:  # pragma: no cover
                    log.warning("brotli 解压失败: %s", exc)
            else:
                log.warning(
                    "收到 brotli 压缩弹幕但未安装 brotli 模块，已丢弃。"
                    "执行  pip install brotli  可解决。"
                )
        else:
            yield protover, operation, body


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------


def _http_json(url: str, cookies: dict[str, str] | None = None,
               timeout: float = 10.0) -> dict[str, Any]:
    headers = {
        "User-Agent": DEFAULT_UA,
        "Referer": "https://live.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise BilibiliError(f"请求 {url} 失败: {exc}") from exc
    if payload.get("code") != 0:
        raise BilibiliError(
            f"{url} 返回错误 code={payload.get('code')} msg={payload.get('message') or payload.get('msg')}"
        )
    return payload.get("data") or {}


def resolve_room_id(room_id: int, cookies: dict[str, str] | None = None) -> int:
    """把短号（如 1）换成真实房间号（如 5440）。"""
    data = _http_json(
        f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}",
        cookies,
    )
    real = int(data.get("room_id") or room_id)
    if real != room_id:
        log.info("房间短号 %s -> 真实房间号 %s", room_id, real)
    return real


def _normalize_hosts(raw_hosts: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw_hosts:
        host = item.get("host") or item.get("host_server_list")
        if not host or host in seen:
            continue
        seen.add(host)
        out.append({
            "host": host,
            "wss_port": int(item.get("wss_port") or 443),
            "ws_port": int(item.get("ws_port") or 2244),
        })
    if not out:
        out = [{"host": "broadcastlv.chat.bilibili.com", "wss_port": 443, "ws_port": 2244}]
    return out


def fetch_danmu_info(real_room_id: int, cookies: dict[str, str] | None = None) -> tuple[str, list[dict]]:
    """取弹幕服务器地址列表和鉴权 token。

    优先用旧版 `room/v1/Danmu/getConf`：这个接口目前不需要登录、也不会触发
    新版接口那套风控（`xlive/.../getDanmuInfo` 在不少网络环境下会直接返回
    -352）。两个都失败才报错。
    """
    errors: list[str] = []

    try:
        data = _http_json(
            "https://api.live.bilibili.com/room/v1/Danmu/getConf"
            f"?room_id={real_room_id}&platform=pc&player=web",
            cookies,
        )
        token = data.get("token") or ""
        hosts = _normalize_hosts(data.get("host_server_list") or data.get("server_list") or [])
        if token:
            return token, hosts
        errors.append("getConf 没有返回 token")
    except BilibiliError as exc:
        errors.append(f"getConf: {exc}")

    try:
        data = _http_json(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
            f"?id={real_room_id}&type=0",
            cookies,
        )
        token = data.get("token") or ""
        hosts = _normalize_hosts(data.get("host_list") or [])
        if token:
            return token, hosts
        errors.append("getDanmuInfo 没有返回 token")
    except BilibiliError as exc:
        errors.append(f"getDanmuInfo: {exc}")

    raise BilibiliError("拿不到弹幕服务器 token；" + "；".join(errors))


def fetch_room_status(room_id: int, cookies: dict[str, str] | None = None) -> dict[str, Any]:
    """取直播间基本信息（标题 / 主播 / 是否在播）。"""
    return _http_json(
        f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}",
        cookies,
    )


# ---------------------------------------------------------------------------
# 弹幕源
# ---------------------------------------------------------------------------


class BilibiliChatSource:
    """B 站直播间弹幕源，断线自动重连。"""

    name = "bilibili"

    def __init__(
        self,
        room_id: int,
        cookies: dict[str, str] | None = None,
        reconnect_delay: float = 5.0,
        ignore_enter: bool = True,
    ) -> None:
        if not room_id:
            raise ValueError("必须配置 bilibili 房间号（room_id）")
        self.room_id = int(room_id)
        self.cookies = cookies or {}
        self.reconnect_delay = max(1.0, float(reconnect_delay))
        self.ignore_enter = ignore_enter
        self.real_room_id: int = self.room_id
        self._ws: WebSocket | None = None
        self._closing = False

    # ------------------------------------------------------------------

    async def _resolve(self) -> tuple[str, list[dict]]:
        self.real_room_id = await asyncio.to_thread(
            resolve_room_id, self.room_id, self.cookies
        )
        token, hosts = await asyncio.to_thread(
            fetch_danmu_info, self.real_room_id, self.cookies
        )
        random.shuffle(hosts)
        return token, hosts

    def _auth_body(self, token: str) -> bytes:
        return json.dumps(
            {
                "uid": 0,
                "roomid": self.real_room_id,
                "protover": VER_BROTLI if _HAS_BROTLI else VER_ZLIB,
                "platform": "web",
                "type": 2,
                "key": token,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    async def _heartbeat_loop(self, ws: WebSocket) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send_bytes(pack_packet(OP_HEARTBEAT, b"[object Object]"))
        except (asyncio.CancelledError, WebSocketError, Exception):
            return

    async def _connect_once(self) -> AsyncIterator[ChatMessage]:
        token, hosts = await self._resolve()
        last_error: Exception | None = None

        for host_info in hosts:
            host = host_info.get("host")
            port = host_info.get("wss_port") or 443
            if not host:
                continue
            url = f"wss://{host}:{port}/sub"
            log.info("连接弹幕服务器 %s（房间 %s）", url, self.real_room_id)
            try:
                ws = await connect(
                    url,
                    headers={
                        "User-Agent": DEFAULT_UA,
                        "Origin": "https://live.bilibili.com",
                    },
                    timeout=15.0,
                )
            except WebSocketError as exc:
                last_error = exc
                log.warning("连接 %s 失败: %s", host, exc)
                continue

            self._ws = ws
            heartbeat = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                await ws.send_bytes(pack_packet(OP_AUTH, self._auth_body(token)))
                async for opcode, payload in ws:
                    if opcode != OP_BINARY:
                        continue
                    for protover, operation, body in iter_packets(payload):
                        if operation == OP_AUTH_REPLY:
                            info = _safe_json(body) or {}
                            if info.get("code") == 0:
                                log.info("弹幕鉴权成功，开始接收消息")
                            else:
                                log.error("弹幕鉴权失败: %s", info)
                        elif operation == OP_HEARTBEAT_REPLY:
                            popularity = int.from_bytes(body[:4], "big") if len(body) >= 4 else 0
                            log.debug("当前人气值 %s", popularity)
                        elif operation == OP_MESSAGE:
                            for msg in self._parse_message(body):
                                yield msg
            finally:
                heartbeat.cancel()
                await ws.close()
                self._ws = None
            return

        raise BilibiliError(f"所有弹幕服务器都连不上：{last_error}")

    async def messages(self) -> AsyncIterator[ChatMessage]:
        """持续产出弹幕，断线后自动重连。"""
        while not self._closing:
            try:
                async for msg in self._connect_once():
                    yield msg
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    return
                log.warning("弹幕连接中断（%s），%.0f 秒后重连", exc, self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)
                continue
            if not self._closing:
                log.info("%.0f 秒后重连弹幕", self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            await self._ws.close()

    # ------------------------------------------------------------------

    def _parse_message(self, body: bytes) -> list[ChatMessage]:
        data = _safe_json(body)
        if data is None:
            # 少数情况下一个包体里塞了多段 JSON
            out: list[ChatMessage] = []
            for chunk in body.split(b"\x00"):
                chunk = chunk.strip()
                if chunk:
                    parsed = _safe_json(chunk)
                    if isinstance(parsed, dict):
                        out.extend(self._to_message(parsed))
            return out
        if isinstance(data, list):
            out = []
            for item in data:
                if isinstance(item, dict):
                    out.extend(self._to_message(item))
            return out
        return self._to_message(data)

    def _to_message(self, data: dict) -> list[ChatMessage]:
        cmd = data.get("cmd") or ""
        try:
            if cmd.startswith("DANMU_MSG"):
                return [self._danmaku(data)]
            if cmd == "SUPER_CHAT_MESSAGE":
                return [self._super_chat(data)]
            if cmd == "SEND_GIFT":
                return [self._gift(data)]
            if cmd == "INTERACT_WORD":
                msg = self._interact(data)
                return [msg] if msg else []
            if cmd == "LIKE_INFO_V3_CLICK":
                return [self._like(data)]
        except Exception as exc:  # 单条消息解析失败不该拖垮整条连接
            log.debug("解析 %s 失败: %s", cmd, exc)
        return []

    # ---------------------------------------------------------------- 各类消息

    @staticmethod
    def _danmaku(data: dict) -> ChatMessage:
        info = data.get("info") or []
        text = info[1] if len(info) > 1 else ""
        user = info[2] if len(info) > 2 else []
        uid = int(user[0]) if user and isinstance(user[0], int) else 0
        username = str(user[1]) if len(user) > 1 else ""

        medal_name = ""
        medal_level = 0
        medal = info[3] if len(info) > 3 else None
        if isinstance(medal, list) and len(medal) >= 2:
            medal_level = int(medal[0] or 0)
            medal_name = str(medal[1] or "")

        is_admin = bool(user[2]) if len(user) > 2 else False
        is_vip = bool(user[3]) if len(user) > 3 else False

        return ChatMessage(
            kind=KIND_DANMAKU,
            text=str(text),
            username=username,
            uid=uid,
            medal=medal_name,
            medal_level=medal_level,
            is_admin=is_admin,
            is_vip=is_vip,
            raw=data,
        )

    @staticmethod
    def _super_chat(data: dict) -> ChatMessage:
        payload = data.get("data") or {}
        user_info = payload.get("user_info") or {}
        return ChatMessage(
            kind=KIND_SUPER_CHAT,
            text=str(payload.get("message") or ""),
            username=str(user_info.get("uname") or ""),
            uid=int(payload.get("uid") or 0),
            medal=str((payload.get("medal_info") or {}).get("medal_name") or ""),
            medal_level=int((payload.get("medal_info") or {}).get("medal_level") or 0),
            price=float(payload.get("price") or 0),
            raw=data,
        )

    @staticmethod
    def _gift(data: dict) -> ChatMessage:
        payload = data.get("data") or {}
        price = float(payload.get("price") or 0) * int(payload.get("num") or 1)
        return ChatMessage(
            kind=KIND_GIFT,
            text=f"{payload.get('giftName') or '礼物'} x{payload.get('num') or 1}",
            username=str(payload.get("uname") or ""),
            uid=int(payload.get("uid") or 0),
            medal=str((payload.get("medal_info") or {}).get("medal_name") or ""),
            medal_level=int((payload.get("medal_info") or {}).get("medal_level") or 0),
            price=price,
            gift_name=str(payload.get("giftName") or ""),
            gift_count=int(payload.get("num") or 1),
            raw=data,
        )

    def _interact(self, data: dict) -> ChatMessage | None:
        payload = data.get("data") or {}
        msg_type = payload.get("msg_type")
        username = str(payload.get("uname") or "")
        if msg_type == 1:  # 进场
            if self.ignore_enter:
                return None
            return ChatMessage(kind=KIND_ENTER, text="进入直播间", username=username,
                               uid=int(payload.get("uid") or 0), raw=data)
        if msg_type == 2:  # 关注
            return ChatMessage(kind=KIND_FOLLOW, text="关注了主播", username=username,
                               uid=int(payload.get("uid") or 0), raw=data)
        return None

    @staticmethod
    def _like(data: dict) -> ChatMessage:
        payload = data.get("data") or {}
        return ChatMessage(
            kind=KIND_LIKE,
            text=f"点赞 x{payload.get('like_text') or 1}",
            username=str(payload.get("uname") or ""),
            uid=int(payload.get("uid") or 0),
            raw=data,
        )


def _safe_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def build_ws_url(host: str, port: int) -> str:
    """给外部调试用。"""
    return f"wss://{host}:{port}/sub"


def parse_room_input(raw: str) -> int:
    """支持直接填房间号，或粘贴直播间链接。

    https://live.bilibili.com/22637261  ->  22637261
    """
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw)
    parts = urllib.parse.urlsplit(raw)
    for segment in reversed([p for p in parts.path.split("/") if p]):
        if segment.isdigit():
            return int(segment)
    raise ValueError(f"无法从 {raw!r} 里解析出房间号")
