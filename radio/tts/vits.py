"""VITS 中文多说话人合成（角色音色）。

和另外两个引擎的关系：
    ZipVoice  零样本克隆，要你提供音频样本，慢，容易带上样本的杂音
    Kokoro    自带 103 个通用音色，速度一般（RTF ~2）
    VITS      自带 804 个说话人编号，**实测 RTF 0.33**，念稿追得上播放

为什么这个适合念稿：
    1. 快。合成比播放快 3 倍，边合成边播永远不会断。
    2. 多音字有救。模型自带 lexicon.txt（66395 条，其中 45494 条是多字词组）
       和 new_heteronym.fst，上下文消歧。
       实测「行长 háng zhǎng」「重要 zhòng yào」「音乐 yīn yuè」「长城 cháng chéng」
       「归还 guī huán」「便宜 pián yi」「大夫 dài fu」都读对了。
    3. 804 个说话人编号，总有一个适合当主播。
"""

from __future__ import annotations

import asyncio
import array
import ctypes
import logging
import os
import struct
import sys
import threading
from pathlib import Path

from .base import TTSError, TTSProvider, VoiceParams

log = logging.getLogger("radio.tts.vits")

from .zipvoice import (  # noqa: E402
    _c_float_p,
    _GenerationConfig,
    _GeneratedAudio,
    _ModelConfig,
    _TtsConfig,
    _VitsConfig,
    float_samples_to_pcm16,
)

# 这些 FST 负责文本规整：数字、日期、电话、多音字
RULE_FSTS = ("phone.fst", "number.fst", "date.fst", "new_heteronym.fst")

DEFAULT_SID = 0


class VitsTTS(TTSProvider):
    """VITS 多说话人合成。用 sid 选声音。"""

    name = "vits"
    needs_reference = False

    def __init__(self, kit, model_key: str = "zenyatta", sid: int = DEFAULT_SID,
                 num_threads: int = 0, provider: str = "cpu",
                 lexicon_override: Path | None = None,
                 noise_scale: float = 0.667, noise_scale_w: float = 0.8,
                 silence_scale: float = 0.2) -> None:
        self.kit = kit
        self.model_key = model_key
        self.sid = int(sid)
        self.num_threads = num_threads or max(2, min(8, (os.cpu_count() or 4) - 2))
        self.provider = provider
        self.lexicon_override = lexicon_override
        # 韵律三件套。这是 VITS 上最接近「情感」的三个旋钮：
        #   noise_scale   越大，语调起伏越丰富（太大会飘、会破音）
        #   noise_scale_w 越大，各音节长短变化越多（念稿会显得更"随意"）
        #   silence_scale 句子之间的静音长度，越大停顿越明显
        self.noise_scale = float(noise_scale)
        self.noise_scale_w = float(noise_scale_w)
        self.silence_scale = float(silence_scale)

        self._lib = None
        self._tts = None
        self._sample_rate = 22050
        self._num_speakers = 0
        self._lock = threading.RLock()
        self._config_refs: list = []

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self.kit.character_dir(self.model_key)

    @property
    def model_path(self) -> Path | None:
        candidate = self.directory / f"{self.model_key}.onnx"
        if candidate.exists():
            return candidate
        for found in self.directory.glob("*.onnx"):
            return found
        return None

    @property
    def lexicon_path(self) -> Path | None:
        if self.lexicon_override is not None and self.lexicon_override.exists():
            return self.lexicon_override
        candidate = self.directory / "lexicon.txt"
        return candidate if candidate.exists() else None

    @property
    def tokens_path(self) -> Path | None:
        candidate = self.directory / "tokens.txt"
        return candidate if candidate.exists() else None

    def available(self) -> bool:
        return bool(self.model_path and self.lexicon_path and self.tokens_path)

    @property
    def num_speakers(self) -> int:
        return self._num_speakers or 804

    def _rule_fsts(self) -> str:
        parts = []
        for name in RULE_FSTS:
            candidate = self.directory / name
            if candidate.exists():
                parts.append(str(candidate))
        return ",".join(parts)

    # ------------------------------------------------------------------

    def _load_library(self):
        if self._lib is not None:
            return self._lib
        from .zipvoice import ZipVoiceTTS

        self._lib = ZipVoiceTTS._load_library(self)      # type: ignore[arg-type]
        lib = self._lib
        try:
            lib.SherpaOnnxOfflineTtsNumSpeakers.restype = ctypes.c_int32
            lib.SherpaOnnxOfflineTtsNumSpeakers.argtypes = [ctypes.c_void_p]
        except AttributeError:
            pass
        return lib

    def _ensure_ready(self) -> None:
        if self._tts is not None:
            return
        if not self.available():
            raise TTSError(
                f"角色音色「{self.model_key}」还没下载。\n"
                "  请在软件「音色」页点「一键安装」，或运行 python run.py setup"
            )

        lib = self._load_library()

        def enc(path) -> bytes:
            return str(path).encode("utf-8")

        vits = _VitsConfig(
            model=enc(self.model_path),
            lexicon=enc(self.lexicon_path),
            tokens=enc(self.tokens_path),
            data_dir=None,
            noise_scale=self.noise_scale,
            noise_scale_w=self.noise_scale_w,
            length_scale=1.0,
            dict_dir=None,
        )
        model = _ModelConfig()
        model.vits = vits
        model.num_threads = self.num_threads
        model.debug = 0
        model.provider = self.provider.encode("utf-8")

        config = _TtsConfig()
        config.model = model
        config.rule_fsts = enc(self._rule_fsts()) if self._rule_fsts() else None
        config.max_num_sentences = 1
        config.silence_scale = 0.2

        self._config_refs = [vits, model, config]

        log.info("正在加载角色音色「%s」（线程 %s）…", self.model_key, self.num_threads)
        handle = lib.SherpaOnnxCreateOfflineTts(ctypes.byref(config))
        if not handle:
            raise TTSError(
                f"角色音色「{self.model_key}」加载失败。\n"
                f"  目录：{self.directory}\n"
                "  常见原因：模型文件不完整，或缺 onnxruntime.dll。"
            )
        self._tts = handle
        try:
            self._sample_rate = lib.SherpaOnnxOfflineTtsSampleRate(handle) or 22050
        except Exception:
            self._sample_rate = 22050
        try:
            self._num_speakers = lib.SherpaOnnxOfflineTtsNumSpeakers(handle)
        except Exception:
            self._num_speakers = 804
        log.info("角色音色就绪：%s，%s Hz，%s 个说话人，韵律 %.2f/%.2f/%.2f",
                 self.model_key, self._sample_rate, self._num_speakers,
                 self.noise_scale, self.noise_scale_w, self.silence_scale)

    # ------------------------------------------------------------------

    def _sid_of(self, params: VoiceParams) -> int:
        """人设/音色里可能写着别的引擎的名字，解析不出来就用对象自己的 sid。"""
        raw = (params.voice or "").strip()
        if not raw or raw in ("默认音色", "自动", "default"):
            return self.sid
        if raw.isdigit():
            return max(0, min(self.num_speakers - 1, int(raw)))
        # 形如 "zenyatta:12" 或 "禅雅塔#12"
        for sep in (":", "#", "/"):
            if sep in raw:
                tail = raw.rsplit(sep, 1)[-1].strip()
                if tail.isdigit():
                    return max(0, min(self.num_speakers - 1, int(tail)))
        return self.sid

    def _generation_config(self, sid: int, speed: float):
        config = _GenerationConfig()
        config.silence_scale = self.silence_scale
        config.speed = speed
        config.sid = sid
        config.reference_audio = None
        config.reference_audio_len = 0
        config.reference_sample_rate = 0
        config.reference_text = None
        config.num_steps = 0
        config.extra = None
        return config

    # ------------------------------------------------------------------

    def synthesize_stream(self, text: str, params: VoiceParams, on_pcm,
                          should_stop=None, on_open=None) -> int:
        text = (text or "").strip()
        if not text:
            return 0

        from .zipvoice import ZipVoiceTTS, _rate_to_speed

        lib = self._load_library()
        state = {"total": 0, "abort": False}

        def _on_audio(samples, count, progress, _arg):
            try:
                if count <= 0:
                    return 1
                if should_stop is not None and should_stop():
                    state["abort"] = True
                    return 0
                pcm = float_samples_to_pcm16(samples, count)
                state["total"] += count
                if not on_pcm(pcm):
                    state["abort"] = True
                    return 0
                return 1
            except Exception as exc:
                log.error("VITS 流式回调出错：%s", exc)
                state["abort"] = True
                return 0

        callback = ZipVoiceTTS._CALLBACK_TYPE(_on_audio)

        with self._lock:
            self._ensure_ready()
            sid = self._sid_of(params)
            speed = _rate_to_speed(params.rate) if params.rate not in ("", "+0%") else 1.0
            config = self._generation_config(sid, speed)
            self._config_refs.append(config)
            if on_open is not None:
                on_open(self._sample_rate)

            audio = lib.SherpaOnnxOfflineTtsGenerateWithConfig(
                self._tts, text.encode("utf-8"), ctypes.byref(config),
                callback, None,
            )
            if not audio:
                raise TTSError("VITS 合成失败（返回空指针）")
            try:
                total = audio.contents.n or state["total"]
            finally:
                lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio)

        return total

    async def synthesize(self, text: str, params: VoiceParams) -> bytes:
        chunks: list[bytes] = []

        def collect(pcm: bytes) -> bool:
            chunks.append(pcm)
            return True

        await asyncio.to_thread(self.synthesize_stream, text, params, collect)
        pcm = b"".join(chunks)
        if not pcm:
            return b""
        samples = array.array("h")
        samples.frombytes(pcm)
        return _pack_wav(samples, self._sample_rate)

    async def check(self) -> str:
        await asyncio.to_thread(self._ensure_ready)
        return (f"角色音色「{self.model_key}」就绪："
                f"{self.num_speakers} 个说话人，采样率 {self._sample_rate} Hz")

    def close(self) -> None:
        with self._lock:
            if self._tts is not None and self._lib is not None:
                try:
                    self._lib.SherpaOnnxDestroyOfflineTts(self._tts)
                except Exception:
                    pass
                self._tts = None
            self._config_refs = []


def _pack_wav(samples: array.array, rate: int) -> bytes:
    data = samples.tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data
