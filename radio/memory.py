"""记忆与稿件库。

ConversationMemory —— 记住刚才聊了什么，让 AI 接得上话
ScriptLibrary      —— 管理"没人说话时念的稿子"，自动轮播不重复
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .llm import Message

log = logging.getLogger("radio.memory")

_SCRIPT_SUFFIX = (".md", ".markdown", ".txt")
_COMMENT = re.compile(r"^\s*(<!--.*?-->\s*$|//.*$)")


# ---------------------------------------------------------------------------
# 对话记忆
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    role: str
    content: str
    at: float = field(default_factory=time.time)
    username: str = ""


class ConversationMemory:
    """滚动窗口的对话记忆。"""

    def __init__(self, size: int = 12) -> None:
        self.size = max(2, int(size))
        self._turns: list[Turn] = []

    def add_user(self, text: str, username: str = "") -> None:
        self._turns.append(Turn("user", text, username=username))
        self._trim()

    def add_assistant(self, text: str) -> None:
        self._turns.append(Turn("assistant", text))
        self._trim()

    def _trim(self) -> None:
        if len(self._turns) > self.size:
            del self._turns[: len(self._turns) - self.size]

    def clear(self) -> None:
        self._turns.clear()

    def messages(self) -> list[Message]:
        return [Message(t.role, t.content) for t in self._turns]

    def recent_topics(self, count: int = 5) -> list[str]:
        return [t.content for t in self._turns if t.role == "user"][-count:]

    def __len__(self) -> int:
        return len(self._turns)


# ---------------------------------------------------------------------------
# 稿件库
# ---------------------------------------------------------------------------


def split_script(text: str) -> list[str]:
    """把一篇稿子切成一段一段可以单独念的内容。

    规则：
      * 空行分段
      * 单独一行的 --- 是硬分隔
      * # 开头的是标题，不会被念出来
      * // 或 <!-- --> 是注释
    """
    segments: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            piece = " ".join(line.strip() for line in buffer if line.strip())
            piece = piece.strip()
            if piece:
                segments.append(piece)
            buffer.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if _COMMENT.match(line):
            # 注释行本身不念，但会切断段落 —— 免得注释前后两段被粘成一句
            flush()
            continue
        if line.strip() in ("---", "***", "___"):
            flush()
            continue
        if line.lstrip().startswith("#"):
            flush()
            continue
        buffer.append(line)
    flush()
    return segments


class ScriptLibrary:
    """稿件轮播：尽量不重复，念完一轮再从头来。"""

    def __init__(self, directory: Path, files: list[str] | None = None,
                 shuffle: bool = True, loop: bool = True) -> None:
        self.directory = Path(directory)
        self.files = list(files or [])
        self.shuffle = shuffle
        self.loop = loop
        self._segments: list[tuple[str, str]] = []   # (来源文件名, 内容)
        self._order: list[int] = []
        self._cursor = 0
        self.reload()

    # ------------------------------------------------------------------

    def reload(self) -> None:
        """重新读取稿件目录（支持直播中改稿子）。"""
        self._segments.clear()
        for path in self._collect_files():
            try:
                text = path.read_text(encoding="utf-8")
            except Exception as exc:
                log.warning("稿件 %s 读取失败: %s", path.name, exc)
                continue
            for segment in split_script(text):
                self._segments.append((path.name, segment))
        self._reshuffle()
        log.info("载入稿件 %s 段（来自 %s 个文件）",
                 len(self._segments), len(self._collect_files()))

    def _collect_files(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        if self.files:
            picked: list[Path] = []
            for name in self.files:
                candidate = self.directory / name
                if candidate.is_file():
                    picked.append(candidate)
                elif Path(name).is_file():
                    picked.append(Path(name))
                else:
                    log.warning("配置里指定的稿件 %s 不存在，已跳过", name)
            return picked
        return [p for p in sorted(self.directory.iterdir())
                if p.is_file() and p.suffix.lower() in _SCRIPT_SUFFIX
                and not p.name.startswith("_")]

    def _reshuffle(self) -> None:
        self._order = list(range(len(self._segments)))
        if self.shuffle:
            random.shuffle(self._order)
        self._cursor = 0

    # ------------------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self._segments)

    def peek_segment(self) -> tuple[str, str] | None:
        """看下一条稿子是什么，但**不**推进游标。

        配合 commit_segment() 用：先看一眼，等真的念出来了再提交。
        否则中途 LLM 失败，这一段就被白白吃掉了。
        """
        if not self._segments:
            return None
        if self._cursor >= len(self._order):
            if not self.loop:
                return None
            self._reshuffle()
            if not self._order:
                return None
        return self._segments[self._order[self._cursor]]

    def commit_segment(self) -> None:
        """确认这一段已经念出去了，游标才前进。"""
        if self._segments and self._cursor < len(self._order):
            self._cursor += 1

    def next_segment(self) -> tuple[str, str] | None:
        """取下一条稿子，返回 (来源文件, 内容)，并立刻推进游标。"""
        segment = self.peek_segment()
        if segment is not None:
            self.commit_segment()
        return segment

    def peek_remaining(self) -> int:
        return max(0, len(self._order) - self._cursor)

    def reset(self) -> None:
        self._reshuffle()

    def describe(self) -> str:
        files = self._collect_files()
        if not self._segments:
            return "（稿件库是空的，空闲时 AI 会自己找话题聊）"
        names = "、".join(p.name for p in files[:5])
        return f"{len(self._segments)} 段，来自 {names}"
