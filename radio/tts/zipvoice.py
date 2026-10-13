"""ZipVoice 零样本语音克隆引擎（ctypes 直连 sherpa-onnx 的 C API）。

为什么不用命令行？
    官方 CLI 每合成一句话都要重新加载一次模型（约 2~3 秒）。
    直播时每句话白等 3 秒是不能接受的。
    所以这里直接用 ctypes 调 sherpa-onnx-c-api.dll，把模型常驻在内存里，
    以后每次合成就只剩推理时间。

一句话概括原理：
    给模型一段参考音频 + 这段音频对应的文字，它就能用那个人的音色
    念你给的新文本。不需要训练，不需要显卡，纯 CPU。
"""

from __future__ import annotations

import array
import ctypes
import logging
import os
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from ..voices import VoiceKit, VoiceProfile
from .base import TTSError, VoiceParams

log = logging.getLogger("radio.tts.zipvoice")

_c_float_p = ctypes.POINTER(ctypes.c_float)


# ---------------------------------------------------------------------------
# C 结构体（必须和 c-api.h 里的字段顺序、类型完全一致）
# ---------------------------------------------------------------------------


class _VitsConfig(ctypes.Structure):
    _fields_ = [
        ("model", ctypes.c_char_p),
        ("lexicon", ctypes.c_char_p),
        ("tokens", ctypes.c_char_p),
        ("data_dir", ctypes.c_char_p),
        ("noise_scale", ctypes.c_float),
        ("noise_scale_w", ctypes.c_float),
        ("length_scale", ctypes.c_float),
        ("dict_dir", ctypes.c_char_p),
    ]


class _MatchaConfig(ctypes.Structure):
    _fields_ = [
        ("acoustic_model", ctypes.c_char_p),
        ("vocoder", ctypes.c_char_p),
        ("lexicon", ctypes.c_char_p),
        ("tokens", ctypes.c_char_p),
        ("data_dir", ctypes.c_char_p),
        ("noise_scale", ctypes.c_float),
        ("length_scale", ctypes.c_float),
        ("dict_dir", ctypes.c_char_p),
    ]


class _KokoroConfig(ctypes.Structure):
    _fields_ = [
        ("model", ctypes.c_char_p),
        ("voices", ctypes.c_char_p),
        ("tokens", ctypes.c_char_p),
        ("data_dir", ctypes.c_char_p),
        ("length_scale", ctypes.c_float),
        ("dict_dir", ctypes.c_char_p),
        ("lexicon", ctypes.c_char_p),
        ("lang", ctypes.c_char_p),
    ]


class _KittenConfig(ctypes.Structure):
    _fields_ = [
        ("model", ctypes.c_char_p),
        ("voices", ctypes.c_char_p),
        ("tokens", ctypes.c_char_p),
        ("data_dir", ctypes.c_char_p),
        ("length_scale", ctypes.c_float),
    ]


class _ZipvoiceConfig(ctypes.Structure):
    _fields_ = [
        ("tokens", ctypes.c_char_p),
        ("encoder", ctypes.c_char_p),
        ("decoder", ctypes.c_char_p),
        ("vocoder", ctypes.c_char_p),
        ("data_dir", ctypes.c_char_p),
        ("lexicon", ctypes.c_char_p),
        ("feat_scale", ctypes.c_float),
        ("t_shift", ctypes.c_float),
        ("target_rms", ctypes.c_float),
        ("guidance_scale", ctypes.c_float),
    ]


class _PocketConfig(ctypes.Structure):
    _fields_ = [
        ("lm_flow", ctypes.c_char_p),
        ("lm_main", ctypes.c_char_p),
        ("encoder", ctypes.c_char_p),
        ("decoder", ctypes.c_char_p),
        ("text_conditioner", ctypes.c_char_p),
        ("vocab_json", ctypes.c_char_p),
        ("token_scores_json", ctypes.c_char_p),
        ("voice_embedding_cache_capacity", ctypes.c_int32),
    ]


class _SupertonicConfig(ctypes.Structure):
    _fields_ = [
        ("duration_predictor", ctypes.c_char_p),
        ("text_encoder", ctypes.c_char_p),
        ("vector_estimator", ctypes.c_char_p),
        ("vocoder", ctypes.c_char_p),
        ("tts_json", ctypes.c_char_p),
        ("unicode_indexer", ctypes.c_char_p),
        ("voice_style", ctypes.c_char_p),
    ]


class _ModelConfig(ctypes.Structure):
    _fields_ = [
        ("vits", _VitsConfig),
        ("num_threads", ctypes.c_int32),
        ("debug", ctypes.c_int32),
        ("provider", ctypes.c_char_p),
        ("matcha", _MatchaConfig),
        ("kokoro", _KokoroConfig),
        ("kitten", _KittenConfig),
        ("zipvoice", _ZipvoiceConfig),
        ("pocket", _PocketConfig),
        ("supertonic", _SupertonicConfig),
    ]


class _TtsConfig(ctypes.Structure):
    _fields_ = [
        ("model", _ModelConfig),
        ("rule_fsts", ctypes.c_char_p),
        ("max_num_sentences", ctypes.c_int32),
        ("rule_fars", ctypes.c_char_p),
        ("silence_scale", ctypes.c_float),
    ]


class _GenerationConfig(ctypes.Structure):
    _fields_ = [
        ("silence_scale", ctypes.c_float),
        ("speed", ctypes.c_float),
        ("sid", ctypes.c_int32),
        ("reference_audio", _c_float_p),
        ("reference_audio_len", ctypes.c_int32),
        ("reference_sample_rate", ctypes.c_int32),
        ("reference_text", ctypes.c_char_p),
        ("num_steps", ctypes.c_int32),
        ("extra", ctypes.c_char_p),
    ]


class _GeneratedAudio(ctypes.Structure):
    _fields_ = [
        ("samples", _c_float_p),
        ("n", ctypes.c_int32),
        ("sample_rate", ctypes.c_int32),
    ]


# ---------------------------------------------------------------------------
# 参考音频缓存
# ---------------------------------------------------------------------------


@dataclass
class _Reference:
    samples: array.array       # float32 采样
    length: int
    sample_rate: int
    text: str
    mtime: float


def load_reference(path: Path, text: str) -> _Reference:
    """把参考 WAV 读成 float32 采样。"""
    import wave

    with wave.open(str(path)) as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())

    if width != 2:
        raise TTSError(
            f"参考音频必须是 16 位 PCM WAV（当前是 {width * 8} 位）：{path.name}"
        )

    raw = array.array("h")
    raw.frombytes(frames[: len(frames) - (len(frames) % 2)])

    if channels > 1:
        mono = array.array("h")
        for index in range(0, len(raw), channels):
            mono.append(int(sum(raw[index:index + channels]) / channels))
        raw = mono

    floats = array.array("f", (value / 32768.0 for value in raw))
    return _Reference(samples=floats, length=len(floats), sample_rate=rate,
                      text=text, mtime=path.stat().st_mtime)


def float_samples_to_pcm16(samples: _c_float_p, count: int) -> bytes:
    """把 C 返回的 float 采样转成 16bit PCM 原始字节（不带 WAV 头）。"""
    pcm = array.array("h", bytes(count * 2))
    for index in range(count):
        value = samples[index]
        if value > 1.0:
            value = 1.0
        elif value < -1.0:
            value = -1.0
        pcm[index] = int(value * 32767.0)
    return pcm.tobytes()


def float_samples_to_wav(samples: _c_float_p, count: int, sample_rate: int) -> bytes:
    """把 C 返回的 float 采样转成 16bit PCM WAV 字节。"""
    data = float_samples_to_pcm16(samples, count)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                    sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class ZipVoiceTTS:
    """常驻内存的克隆语音引擎。"""

    name = "zipvoice"

    def __init__(self, kit: VoiceKit, voice_key: str = "",
                 num_threads: int = 0, num_steps: int = 8,
                 guidance_scale: float = 1.0, feat_scale: float = 0.1,
                 target_rms: float = 0.1, t_shift: float = 0.5,
                 provider: str = "cpu", cache_size: int = 64) -> None:
        self.kit = kit
        self.voice_key = voice_key
        self.num_threads = num_threads or max(2, min(8, (os.cpu_count() or 4) - 2))
        self.num_steps = num_steps
        # 分类器无关引导强度：1.0 = 关闭引导（快）；3.0 左右通常音色更像、更干净
        self.guidance_scale = guidance_scale
        self.feat_scale = feat_scale
        self.target_rms = target_rms
        self.t_shift = t_shift
        self.provider = provider

        self._tts = None
        self._lock = threading.RLock()
        self._references: dict[str, _Reference] = {}
        self._cache: dict[tuple, bytes] = {}
        self._cache_order: list[tuple] = []
        self._cache_size = max(0, cache_size)
        self._lib = None
        self._config_refs: list = []      # 防止配置结构体被 GC
        self._sample_rate = 24000

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _load_library(self):
        if self._lib is not None:
            return self._lib

        exe_dir = self.kit.tts_exe.parent
        lib_dir = None
        for candidate in (self.kit.runtime_dir / "lib", exe_dir):
            if (candidate / "sherpa-onnx-c-api.dll").exists():
                lib_dir = candidate
                break
        if lib_dir is None:
            for found in self.kit.runtime_dir.rglob("sherpa-onnx-c-api.dll"):
                lib_dir = found.parent
                break
        if lib_dir is None:
            raise TTSError(
                "找不到 sherpa-onnx-c-api.dll。\n"
                "  请在软件「音色」页点「一键安装本地语音」重新下载运行时。"
            )

        # 让 c-api.dll 能找到同目录的 onnxruntime.dll
        if sys.platform == "win32":
            try:
                os.add_dll_directory(str(lib_dir))
            except Exception:
                pass
            try:
                os.add_dll_directory(str(exe_dir))
            except Exception:
                pass
        os.environ["PATH"] = f"{lib_dir}{os.pathsep}{exe_dir}{os.pathsep}" + os.environ.get("PATH", "")

        try:
            lib = ctypes.CDLL(str(lib_dir / "sherpa-onnx-c-api.dll"))
        except OSError as exc:
            raise TTSError(
                f"加载 sherpa-onnx-c-api.dll 失败：{exc}\n"
                f"  目录：{lib_dir}"
            ) from exc

        lib.SherpaOnnxCreateOfflineTts.restype = ctypes.c_void_p
        lib.SherpaOnnxCreateOfflineTts.argtypes = [ctypes.POINTER(_TtsConfig)]
        lib.SherpaOnnxDestroyOfflineTts.argtypes = [ctypes.c_void_p]
        lib.SherpaOnnxOfflineTtsSampleRate.restype = ctypes.c_int32
        lib.SherpaOnnxOfflineTtsSampleRate.argtypes = [ctypes.c_void_p]
        lib.SherpaOnnxOfflineTtsGenerateWithConfig.restype = ctypes.POINTER(_GeneratedAudio)
        lib.SherpaOnnxOfflineTtsGenerateWithConfig.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(_GenerationConfig),
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio.argtypes = [
            ctypes.POINTER(_GeneratedAudio)
        ]
        self._lib = lib
        return lib

    def _ensure_ready(self) -> None:
        if self._tts:
            return
        if not self.kit.ready():
            missing = "、".join(self.kit.missing_parts())
            raise TTSError(
                f"本地语音还没装好，缺少：{missing}\n"
                "  请在软件「音色」页点「一键安装本地语音」。"
            )

        lib = self._load_library()

        # ctypes 的 c_char_p 指向的 bytes 必须一直活着，否则会被 GC 回收导致野指针
        keep: list = []

        def enc(value) -> bytes | None:
            if not value:
                return None
            data = str(value).encode("utf-8")
            keep.append(data)
            return data

        zipvoice_config = _ZipvoiceConfig(
            tokens=enc(self.kit.tokens),
            encoder=enc(self.kit.encoder),
            decoder=enc(self.kit.decoder),
            vocoder=enc(self.kit.vocoder),
            data_dir=enc(self.kit.espeak_data),
            lexicon=enc(self.kit.lexicon),
            feat_scale=self.feat_scale,
            t_shift=self.t_shift,
            target_rms=self.target_rms,
            guidance_scale=self.guidance_scale,
        )

        model = _ModelConfig()
        model.num_threads = self.num_threads
        model.debug = 0
        model.provider = enc(self.provider)
        model.zipvoice = zipvoice_config

        config = _TtsConfig()
        config.model = model
        config.max_num_sentences = 1
        config.silence_scale = 0.2

        # 全部保活：结构体 + 字符串
        self._config_refs = [config, model, zipvoice_config, keep]

        log.info("正在加载 ZipVoice 模型（线程 %s）…", self.num_threads)
        handle = lib.SherpaOnnxCreateOfflineTts(ctypes.byref(config))
        if not handle:
            raise TTSError(
                "ZipVoice 模型加载失败。\n"
                "  常见原因：模型文件不完整，或缺少 onnxruntime.dll。\n"
                f"  模型目录：{self.kit.zipvoice_dir}"
            )
        self._tts = handle
        try:
            self._sample_rate = lib.SherpaOnnxOfflineTtsSampleRate(handle) or 24000
        except Exception:
            self._sample_rate = 24000
        log.info("ZipVoice 就绪，输出采样率 %s Hz", self._sample_rate)

    def close(self) -> None:
        with self._lock:
            if self._tts and self._lib is not None:
                try:
                    self._lib.SherpaOnnxDestroyOfflineTts(self._tts)
                except Exception:
                    pass
            self._tts = None
            self._references.clear()
            self._cache.clear()
            self._cache_order.clear()

    def reload(self) -> None:
        """模型或音色换过之后重新加载。"""
        self.close()

    # ------------------------------------------------------------------
    # 音色
    # ------------------------------------------------------------------

    def _profile(self, params: VoiceParams) -> VoiceProfile:
        key = (params.voice or self.voice_key or "").strip()
        profile = self.kit.get(key) if key else None
        if profile is None:
            profiles = [p for p in self.kit.profiles() if p.ready]
            if not profiles:
                raise TTSError(
                    "还没有可用的克隆音色。\n"
                    "  去软件「音色」页加一个：录一段 5~15 秒的干声（WAV），\n"
                    "  再填上这段音频里说的内容，就能用它说话了。"
                )
            profile = profiles[0]
        if not profile.ready:
            raise TTSError(f"音色「{profile.display}」还不能用：{profile.problem}")
        return profile

    def _reference(self, profile: VoiceProfile) -> _Reference:
        cache_key = str(profile.directory)
        mtime = profile.sample.stat().st_mtime
        cached = self._references.get(cache_key)
        if cached and cached.mtime == mtime and cached.text == profile.transcript:
            return cached
        reference = load_reference(profile.sample, profile.transcript)
        self._references[cache_key] = reference
        return reference

    # ------------------------------------------------------------------
    # 合成
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 流式合成：整段一次算，音频边生成边交出去
    # ------------------------------------------------------------------

    # 进度回调：int32(const float *samples, int32_t n, float progress, void *arg)
    _CALLBACK_TYPE = ctypes.CFUNCTYPE(ctypes.c_int32, _c_float_p,
                                      ctypes.c_int32, ctypes.c_float,
                                      ctypes.c_void_p)

    def _build_gen_config(self, profile: VoiceProfile, speed: float,
                          reference: _Reference):
        config = _GenerationConfig()
        config.silence_scale = 0.2
        config.speed = speed
        config.sid = 0
        config.reference_audio = ctypes.cast(reference.samples.buffer_info()[0],
                                             _c_float_p)
        config.reference_audio_len = reference.length
        config.reference_sample_rate = reference.sample_rate
        config.reference_text = reference.text.encode("utf-8")
        config.num_steps = self.num_steps
        return config

    def synthesize_stream(self, text: str, params: VoiceParams, on_pcm,
                          should_stop=None, on_open=None) -> int:
        """把整段文字一次合成完，音频一边生成一边交给 on_pcm。

        为什么整段一次算：
            ZipVoice 每次调用都要重新处理一遍参考音频（约 0.67 秒 × 样本秒数）。
            切成很多小段分别合成的话，这个固定开销会被重复付很多遍，
            总合成时间远远超过播放时间，段与段之间就会出现明显的空白。

        on_pcm(chunk: bytes) 返回 False，或者 should_stop() 为真时，会提前中止。
        on_open(sample_rate) 在真正开始生成前调用一次，方便按采样率打开播放器。
        返回实际生成的采样点数量。
        """
        text = (text or "").strip()
        if not text:
            return 0

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
            except Exception as exc:      # 回调里绝不能抛出去
                log.error("流式回调出错：%s", exc)
                state["abort"] = True
                return 0

        callback = self._CALLBACK_TYPE(_on_audio)

        with self._lock:
            self._ensure_ready()
            profile = self._profile(params)
            reference = self._reference(profile)
            speed = _rate_to_speed(params.rate)
            config = self._build_gen_config(profile, speed, reference)
            if on_open is not None:
                on_open(self._sample_rate)

            audio = lib.SherpaOnnxOfflineTtsGenerateWithConfig(
                self._tts, text.encode("utf-8"), ctypes.byref(config),
                callback, None,
            )
            if not audio:
                raise TTSError("ZipVoice 合成失败（返回空指针）")
            try:
                total = audio.contents.n or state["total"]
            finally:
                lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio)

        if state["abort"]:
            log.debug("流式合成被中断")
        return total

    # ------------------------------------------------------------------

    async def synthesize(self, text: str, params: VoiceParams) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        import asyncio

        return await asyncio.to_thread(self._synthesize_sync, text, params)

    def _synthesize_sync(self, text: str, params: VoiceParams) -> bytes:
        lib = self._load_library()
        with self._lock:
            self._ensure_ready()
            profile = self._profile(params)
            reference = self._reference(profile)

            speed = _rate_to_speed(params.rate)
            cache_key = (profile.key, text, speed, self.num_steps,
                         self.guidance_scale, self.feat_scale, self.target_rms)
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit

            config = _GenerationConfig()
            config.silence_scale = 0.2
            config.speed = speed
            config.sid = 0
            config.reference_audio = ctypes.cast(reference.samples.buffer_info()[0],
                                                 _c_float_p)
            config.reference_audio_len = reference.length
            config.reference_sample_rate = reference.sample_rate
            config.reference_text = reference.text.encode("utf-8")
            config.num_steps = self.num_steps

            audio = lib.SherpaOnnxOfflineTtsGenerateWithConfig(
                self._tts, text.encode("utf-8"), ctypes.byref(config), None, None
            )
            if not audio:
                raise TTSError("ZipVoice 合成失败（返回空指针）")

            try:
                count = audio.contents.n
                rate = audio.contents.sample_rate or self._sample_rate
                if count <= 0:
                    raise TTSError("ZipVoice 生成了 0 个采样点")
                wav = float_samples_to_wav(audio.contents.samples, count, rate)
            finally:
                lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio)

            if self._cache_size:
                self._cache[cache_key] = wav
                self._cache_order.append(cache_key)
                while len(self._cache_order) > self._cache_size:
                    self._cache.pop(self._cache_order.pop(0), None)
            return wav

    # ------------------------------------------------------------------

    async def close_async(self) -> None:
        self.close()

    async def check(self) -> str:
        profiles = [p for p in self.kit.profiles() if p.ready]
        if not profiles:
            raise TTSError("还没有可用的克隆音色")
        import asyncio

        data = await asyncio.to_thread(
            self._synthesize_sync, "自检测试。", VoiceParams(voice=profiles[0].key))
        from ..audio import parse_wav

        wav = parse_wav(data)
        return (f"ZipVoice 克隆语音正常（音色「{profiles[0].display}」，"
                f"示例 {wav.duration:.2f} 秒）")

    def describe(self, params: VoiceParams | None = None) -> str:
        try:
            profile = self._profile(params or VoiceParams(voice=self.voice_key))
            return profile.display
        except Exception:
            return "（没有可用音色）"


def _rate_to_speed(rate: str) -> float:
    """把 "+20%" 换算成 speed 倍数（1.2 = 快 20%）。"""
    import re

    if not rate:
        return 1.0
    match = re.match(r"\s*([+-]?)\s*(\d+(?:\.\d+)?)\s*%?", str(rate))
    if not match:
        return 1.0
    percent = float(match.group(2))
    if match.group(1) == "-":
        percent = -percent
    percent = max(-50.0, min(150.0, percent))
    return round(1.0 + percent / 100.0, 3)
