"""控制台聊天源：本地手动敲字模拟弹幕，用来调试 AI 反应。

在终端里直接输入文字回车，就会被当成一条弹幕送进电台。
适合在不直播的时候先调人设、调语气、听效果。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import AsyncIterator

from .base import KIND_DANMAKU, KIND_SYSTEM, ChatMessage

log = logging.getLogger("radio.console")


class ConsoleChatSource:
    """从标准输入读取"弹幕"。"""

    name = "console"

    def __init__(self, prompt: str = "弹幕> ", username: str = "本地测试用户") -> None:
        self.prompt = prompt
        self.username = username
        self._queue: asyncio.Queue[ChatMessage | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._closing = False

    # ------------------------------------------------------------------

    async def _read_stdin(self) -> None:
        loop = asyncio.get_running_loop()
        # 在终端里用 run_in_executor 读 stdin —— 简单可靠，
        # 不用去折腾 Windows 上 asyncio 的 console 支持。
        while not self._closing:
            try:
                line = await loop.run_in_executor(None, self._blocking_read)
            except Exception:
                break
            if line is None or self._closing:
                break
            text = line.strip()
            if not text:
                continue
            if text in ("/quit", "/exit", "quit", "exit"):
                await self._queue.put(None)
                break
            await self._queue.put(
                ChatMessage(kind=KIND_DANMAKU, text=text, username=self.username)
            )
        await self._queue.put(None)

    def _blocking_read(self) -> str | None:
        try:
            sys.stdout.write(self.prompt)
            sys.stdout.flush()
            line = sys.stdin.readline()
        except Exception:
            return None
        if line == "":
            return None  # EOF
        return line

    # ------------------------------------------------------------------

    async def messages(self) -> AsyncIterator[ChatMessage]:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_stdin())

        # 先给一条系统消息，提示怎么用
        yield ChatMessage(
            kind=KIND_SYSTEM,
            text="控制台模式已就绪：直接敲字回车即可模拟弹幕；输入 /quit 退出。",
        )

        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self._closing = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None


class ScriptedChatSource:
    """按时间表投放预设弹幕，用于自动化测试。

    script 里每一项可以是 (秒, 文本)，也可以是 (秒, 文本, 昵称)。
    """

    name = "scripted"

    def __init__(self, script: list[tuple], username: str = "测试观众") -> None:
        self.script = script
        self.username = username

    async def messages(self) -> AsyncIterator[ChatMessage]:
        elapsed = 0.0
        for item in self.script:
            delay, text = item[0], item[1]
            who = item[2] if len(item) > 2 else self.username
            # 给不同昵称一个稳定的假 uid，方便验证按用户冷却
            uid = abs(hash(who)) % 100000
            await asyncio.sleep(max(0.0, delay - elapsed))
            elapsed = delay
            yield ChatMessage(kind=KIND_DANMAKU, text=text, username=who, uid=uid)

    async def close(self) -> None:
        return None
