"""TTS provider 接口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class VoiceParams:
    """一次合成用到的声音参数。"""

    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: str = "+0%"      # 语速，如 "+10%" / "-5%"
    pitch: str = "+0Hz"    # 音调，如 "+2Hz" / "-2Hz"
    volume: str = "+0%"    # 音量（TTS 侧），如 "+50%"

    def describe(self) -> str:
        return f"{self.voice} rate={self.rate} pitch={self.pitch}"


class TTSError(Exception):
    """语音合成失败。"""


class TTSProvider(Protocol):
    """语音合成器。合成结果统一是 WAV 字节。"""

    name: str

    async def synthesize(self, text: str, params: VoiceParams) -> bytes:
        """把文字合成为 WAV 字节。"""
        ...

    async def close(self) -> None:
        ...

    async def check(self) -> str:
        """自检，返回一句人类可读的结果。"""
        ...
