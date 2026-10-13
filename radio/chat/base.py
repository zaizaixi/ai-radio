"""聊天源抽象：把不同直播平台的弹幕统一成 ChatMessage。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

# 消息类型
KIND_DANMAKU = "danmaku"          # 普通弹幕
KIND_SUPER_CHAT = "super_chat"    # 醒目留言（付费）
KIND_GIFT = "gift"                # 礼物
KIND_ENTER = "enter"              # 进场
KIND_FOLLOW = "follow"            # 关注
KIND_LIKE = "like"                # 点赞
KIND_SYSTEM = "system"            # 系统/主播提示（本程序内部使用）

__all__ = [
    "ChatMessage",
    "ChatSource",
    "KIND_DANMAKU",
    "KIND_SUPER_CHAT",
    "KIND_GIFT",
    "KIND_ENTER",
    "KIND_FOLLOW",
    "KIND_LIKE",
    "KIND_SYSTEM",
]


@dataclass(slots=True)
class ChatMessage:
    """一条归一化后的聊天消息。"""

    kind: str
    text: str
    username: str = ""
    uid: int = 0
    timestamp: float = field(default_factory=time.time)
    # 粉丝勋章名，例如「老粉」
    medal: str = ""
    medal_level: int = 0
    is_admin: bool = False
    is_vip: bool = False
    # 付费相关
    price: float = 0.0
    gift_name: str = ""
    gift_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------

    @property
    def is_paid(self) -> bool:
        """是否是需要优先处理的付费消息。"""
        return self.kind == KIND_SUPER_CHAT or (
            self.kind == KIND_GIFT and self.price > 0
        )

    def display(self) -> str:
        """给日志/终端看的单行摘要。"""
        tag = {
            KIND_DANMAKU: "弹幕",
            KIND_SUPER_CHAT: "SC",
            KIND_GIFT: "礼物",
            KIND_ENTER: "进场",
            KIND_FOLLOW: "关注",
            KIND_LIKE: "点赞",
            KIND_SYSTEM: "系统",
        }.get(self.kind, self.kind)
        medal = f"[{self.medal}{self.medal_level}]" if self.medal else ""
        return f"{tag} {medal}{self.username}: {self.text}"


class ChatSource(Protocol):
    """聊天源接口。"""

    name: str

    def messages(self) -> AsyncIterator[ChatMessage]:
        """异步产出聊天消息，直到被取消。"""
        ...

    async def start(self) -> None:
        """建立连接（可选）。"""
        ...

    async def close(self) -> None:
        """释放资源（可选）。"""
        ...
