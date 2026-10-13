"""聊天源集合。"""

from .base import (
    ChatMessage,
    ChatSource,
    KIND_DANMAKU,
    KIND_ENTER,
    KIND_FOLLOW,
    KIND_GIFT,
    KIND_LIKE,
    KIND_SUPER_CHAT,
    KIND_SYSTEM,
)

__all__ = [
    "ChatMessage",
    "ChatSource",
    "KIND_DANMAKU",
    "KIND_ENTER",
    "KIND_FOLLOW",
    "KIND_GIFT",
    "KIND_LIKE",
    "KIND_SUPER_CHAT",
    "KIND_SYSTEM",
    "build_chat_source",
]


def build_chat_source(cfg):
    """依据配置构造聊天源。

    cfg 是 radio.config.ChatConfig。
    """
    kind = (cfg.platform or "console").lower()
    if kind in ("bilibili", "b站", "bili"):
        from .bilibili import BilibiliChatSource

        return BilibiliChatSource(
            room_id=cfg.room_id,
            cookies=cfg.cookies,
            reconnect_delay=cfg.reconnect_delay,
            ignore_enter=cfg.ignore_enter,
        )
    if kind in ("console", "mock", "local", "本地"):
        from .console import ConsoleChatSource

        return ConsoleChatSource(prompt=cfg.console_prompt)
    raise ValueError(f"未知的聊天平台: {cfg.platform!r}（支持 bilibili / console）")
