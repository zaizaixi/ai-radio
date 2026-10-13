"""Kokoro 多说话人语音合成（纯 ctypes，不用克隆）。

和 ZipVoice 的区别：
    ZipVoice 是「零样本克隆」——你给一段声音，它学那个音色。
    Kokoro 是「自带音色」——模型里内置了 103 个说话人，选编号就行。

    Kokoro 不需要参考音频，所以：
      * 没有「每句话重新处理参考音频」那笔固定开销，快得多
      * 没有克隆带来的杂音、电流声、回音
      * 开箱就能用，不需要先录一段声音

    kokoro-int8-multi-lang-v1_1 里中文音色有 100 个：
      sid   3 ~  57   55 个中文女声（zf_*）
      sid  58 ~ 102   45 个中文男声（zm_*）
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .base import TTSError, TTSProvider, VoiceParams

log = logging.getLogger("radio.tts.kokoro")

# 复用 ZipVoice 那套已经验证过的 ctypes 结构体，避免两份定义走样
from .zipvoice import (  # noqa: E402
    _c_float_p,
    _GeneratedAudio,
    _GenerationConfig,
    _KokoroConfig,
    _ModelConfig,
    _TtsConfig,
    float_samples_to_pcm16,
    float_samples_to_wav,
)


# ---------------------------------------------------------------------------
# 音色表
# ---------------------------------------------------------------------------

# `sid 名字` —— 来自 sherpa-onnx 官方文档 kokoro-multi-lang-v1_1
_SPEAKER_NAMES = """
0 af_maple 1 af_sol 2 bf_vale
3 zf_001 4 zf_002 5 zf_003 6 zf_004 7 zf_005 8 zf_006 9 zf_007 10 zf_008
11 zf_017 12 zf_018 13 zf_019 14 zf_021 15 zf_022 16 zf_023 17 zf_024
18 zf_026 19 zf_027 20 zf_028 21 zf_032 22 zf_036 23 zf_038 24 zf_039
25 zf_040 26 zf_042 27 zf_043 28 zf_044 29 zf_046 30 zf_047 31 zf_048
32 zf_049 33 zf_051 34 zf_059 35 zf_060 36 zf_067 37 zf_070 38 zf_071
39 zf_072 40 zf_073 41 zf_074 42 zf_075 43 zf_076 44 zf_077 45 zf_078
46 zf_079 47 zf_083 48 zf_084 49 zf_085 50 zf_086 51 zf_087 52 zf_088
53 zf_090 54 zf_092 55 zf_093 56 zf_094 57 zf_099
58 zm_009 59 zm_010 60 zm_011 61 zm_012 62 zm_013 63 zm_014 64 zm_015
65 zm_016 66 zm_020 67 zm_025 68 zm_029 69 zm_030 70 zm_031 71 zm_033
72 zm_034 73 zm_035 74 zm_037 75 zm_041 76 zm_045 77 zm_050 78 zm_052
79 zm_053 80 zm_054 81 zm_055 82 zm_056 83 zm_057 84 zm_058 85 zm_061
86 zm_062 87 zm_063 88 zm_064 89 zm_065 90 zm_066 91 zm_068 92 zm_069
93 zm_080 94 zm_081 95 zm_082 96 zm_089 97 zm_091 98 zm_095 99 zm_096
100 zm_097 101 zm_098 102 zm_100
"""


@dataclass
class KokoroVoice:
    sid: int
    name: str          # af_maple / zf_001 / zm_009
    gender: str        # 男 / 女
    region: str        # 中文 / 英语
    # 下面这些由 profile_voices.py 实测填进来（空 = 还没测）
    pitch: float = 0.0        # 基频 Hz，越低越低沉
    brightness: float = 0.0   # 频谱重心 Hz，越高越清亮
    speed: float = 0.0        # 字/秒

    @property
    def label(self) -> str:
        return f"{self.name}（{self.gender}·{self.region}）"

    def keywords(self) -> list[str]:
        """把这个音色能匹配的关键词列出来，给 AI 挑音色时当索引。"""
        out = [self.name, self.gender, self.region]
        if self.pitch:
            if self.pitch < 105:
                out += ["低沉", "厚重", "成熟"]
            elif self.pitch < 135:
                out += ["中低", "沉稳"]
            elif self.pitch < 175:
                out += ["中等", "自然"]
            elif self.pitch < 210:
                out.append("偏高")
            else:
                out += ["高", "年轻", "清脆"]
        if self.brightness:
            if self.brightness < 1200:
                out += ["浑厚", "磁性", "闷"]
            elif self.brightness < 1800:
                out += ["温和", "柔和"]
            elif self.brightness < 2500:
                out.append("清晰")
            else:
                out += ["明亮", "清亮", "清脆"]
        return out


def _build_catalog() -> list[KokoroVoice]:
    voices: list[KokoroVoice] = []
    tokens = _SPEAKER_NAMES.split()
    for index in range(0, len(tokens), 2):
        sid = int(tokens[index])
        name = tokens[index + 1]
        if name.startswith("zf") or name.startswith("zm"):
            gender = "女" if name[1] == "f" else "男"
            region = "中文"
        else:
            gender = "女" if name[1] == "f" else "男"
            region = "英语"
        voices.append(KokoroVoice(sid=sid, name=name, gender=gender, region=region))
    return voices


KOKORO_VOICES: list[KokoroVoice] = _build_catalog()
VOICE_BY_NAME = {v.name: v for v in KOKORO_VOICES}
VOICE_BY_SID = {v.sid: v for v in KOKORO_VOICES}
CHINESE_VOICES = [v for v in KOKORO_VOICES if v.region == "中文"]

# 默认挑一个中文男声当「电台主播」，一个中文女声当「深夜主播」
DEFAULT_SID = 58          # zm_009

PROFILE_FILE = Path(__file__).with_name("kokoro_voices.json")


def load_profiles() -> int:
    """把实测出来的音色特征读进来，返回读到了几条。"""
    if not PROFILE_FILE.exists():
        return 0
    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("音色特征文件读不了：%s", exc)
        return 0
    count = 0
    for name, item in (data.get("voices") or {}).items():
        voice = VOICE_BY_NAME.get(name)
        if voice is None:
            continue
        voice.pitch = float(item.get("pitch") or 0)
        voice.brightness = float(item.get("brightness") or 0)
        voice.speed = float(item.get("speed") or 0)
        count += 1
    return count


def parse_voice(value: str) -> KokoroVoice | None:
    """把用户填的东西解析成音色：可以是名字、编号，或者中文描述。"""
    text = (value or "").strip()
    if not text:
        return VOICE_BY_SID.get(DEFAULT_SID)
    if text.isdigit():
        return VOICE_BY_SID.get(int(text))
    if text in VOICE_BY_NAME:
        return VOICE_BY_NAME[text]
    low = text.lower()
    for voice in KOKORO_VOICES:
        if voice.name == low:
            return voice
    return None


# ---------------------------------------------------------------------------
# 按中文描述找音色
# ---------------------------------------------------------------------------

# 每个词把「想要的方向」推多少。pitch: -1 = 越低越好，+1 = 越高越好
_WORDS: list[tuple[tuple[str, ...], str, float]] = [
    (("低沉", "浑厚", "厚重", "磁性", "低音", "烟嗓", "沙哑", "成熟", "稳重", "沉稳",
      "大叔", "沧桑", "老练"),
     "pitch", -1.0),
    (("高亢", "尖细", "清脆", "清亮", "明亮", "年轻", "活泼", "元气", "可爱",
      "少女感", "娃娃音"),
     "pitch", +1.0),
    (("温柔", "柔和", "软", "治愈", "亲切", "轻声", "低语", "耳语", "舒缓", "甜美"),
     "brightness", -0.6),
    (("清晰", "播音", "字正腔圆", "标准", "专业", "新闻", "主持", "主播", "干练",
      "有力", "洪亮"),
     "brightness", +0.5),
    (("男", "男生", "男声", "男性", "小哥哥", "大哥", "大叔", "大爷", "老哥",
      "汉子", "少年", "公子"),
     "gender", 1.0),
    (("女", "女生", "女声", "女性", "小姐姐", "妹子", "少女", "萝莉", "姐姐",
      "妹妹", "阿姨", "大妈", "御姐"),
     "gender", -1.0),
    (("英文", "英语", "外语"),
     "lang", 1.0),
    (("中文", "汉语", "普通话"),
     "lang", -1.0),
]


def parse_want(text: str) -> dict:
    """把一句中文描述解析成「想要什么声音」。"""
    want = {"gender": 0.0, "pitch": 0.0, "brightness": 0.0, "lang": 0.0,
            "hits": []}
    for words, key, weight in _WORDS:
        for word in words:
            if word in text:
                want[key] += weight
                want["hits"].append(word)
                break
    return want


def _normalize(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 1.0
    low, high = min(values), max(values)
    if high - low < 1e-6:
        return low, low + 1.0
    return low, high


def match_by_keywords(text: str, limit: int = 6,
                      only_chinese: bool = True) -> list[tuple[KokoroVoice, float]]:
    """按中文描述给音色打分排序。

    打分依据是实测出来的基频和频谱重心（见 tools/profile_kokoro_voices.py）。
    没有实测数据时，只能按性别和语言筛，会在结果里标注「未测量」。
    """
    want = parse_want(text)
    pool = [v for v in KOKORO_VOICES if (not only_chinese or v.region == "中文")]
    if not pool:
        pool = list(KOKORO_VOICES)

    # 性别明确就筛掉另一半
    if want["gender"] > 0.1:
        pool = [v for v in pool if v.gender == "男"] or pool
    elif want["gender"] < -0.1:
        pool = [v for v in pool if v.gender == "女"] or pool

    measured = [v for v in pool if v.pitch > 0 and v.brightness > 0]
    if not measured:
        # 还没有实测数据：只能给个平均分，让用户自己试听
        return [(v, 0.5) for v in pool[:limit]]

    pitches = [v.pitch for v in measured]
    brights = [v.brightness for v in measured]
    speeds = [v.speed for v in measured if v.speed > 0]
    p_low, p_high = _normalize(pitches)
    b_low, b_high = _normalize(brights)
    s_low, s_high = _normalize(speeds or [1.0])

    def norm(value: float, low: float, high: float) -> float:
        return (value - low) / (high - low) if high > low else 0.5

    # 归一化后的值域是 0~1：0 = 最低/最闷，1 = 最高/最亮。
    # want["pitch"] 为负表示「要更低」，所以目标值要跟着往下走 ——
    # 符号写反的话，「低沉」会去找最高亢的音色。
    target_pitch = 0.5 + want["pitch"] * 0.35
    target_bright = 0.5 + want["brightness"] * 0.35

    scored = []
    for voice in measured:
        actual_pitch = norm(voice.pitch, p_low, p_high)
        actual_bright = norm(voice.brightness, b_low, b_high)

        distance = abs(actual_pitch - target_pitch) * 1.0
        distance += abs(actual_bright - target_bright) * 0.7

        # 完全没给方向的词，就按「中等最保险」轻微惩罚极端值
        if abs(want["pitch"]) < 0.1:
            distance += abs(actual_pitch - 0.5) * 0.25
        if abs(want["brightness"]) < 0.1:
            distance += abs(actual_bright - 0.5) * 0.15

        score = max(0.0, 1.0 - distance / 1.6)
        scored.append((voice, score))

    scored.sort(key=lambda item: (-item[1], item[0].name))
    return scored[:limit]


# ---------------------------------------------------------------------------


class KokoroTTS(TTSProvider):
    """Kokoro 多说话人合成。不用克隆，选编号即可。"""

    name = "kokoro"
    needs_reference = False

    def __init__(self, kit, sid: int = DEFAULT_SID, num_threads: int = 0,
                 provider: str = "cpu", speed: float = 1.0) -> None:
        self.kit = kit
        self.sid = int(sid)
        self.num_threads = num_threads or max(2, min(8, (os.cpu_count() or 4) - 2))
        self.provider = provider
        self.speed = float(speed)

        self._lib = None
        self._tts = None
        self._sample_rate = 24000
        self._num_speakers = 0
        self._lock = threading.RLock()
        self._config_refs: list = []

    # ------------------------------------------------------------------

    @property
    def model_dir(self) -> Path:
        return self.kit.model_dir

    @property
    def model_path(self) -> Path | None:
        root = self.kit.kokoro_dir
        for name in ("model.int8.onnx", "model.onnx"):
            candidate = root / name
            if candidate.exists():
                return candidate
        return None

    @property
    def voices_path(self) -> Path | None:
        candidate = self.kit.kokoro_dir / "voices.bin"
        return candidate if candidate.exists() else None

    @property
    def tokens_path(self) -> Path | None:
        candidate = self.kit.kokoro_dir / "tokens.txt"
        return candidate if candidate.exists() else None

    def available(self) -> bool:
        return bool(self.model_path and self.voices_path and self.tokens_path)

    @property
    def num_speakers(self) -> int:
        return self._num_speakers or len(KOKORO_VOICES)

    # ------------------------------------------------------------------

    def _load_library(self):
        if self._lib is not None:
            return self._lib
        from .zipvoice import ZipVoiceTTS

        # 借用 ZipVoice 里那套已经验证过的 DLL 加载逻辑
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
                "Kokoro 模型还没装好。\n"
                "  请在软件「音色」页点「一键安装」，或运行 python run.py setup"
            )

        lib = self._load_library()
        root = self.kit.kokoro_dir

        def enc(path) -> bytes:
            return str(path).encode("utf-8")

        # 中英双语的词典要一起给
        lexicons = []
        for name in ("lexicon-us-en.txt", "lexicon-gb-en.txt", "lexicon-zh.txt"):
            candidate = root / name
            if candidate.exists():
                lexicons.append(str(candidate))
        lexicon = ",".join(lexicons)

        keep: list = []
        kokoro = _KokoroConfig(
            model=enc(self.model_path),
            voices=enc(self.voices_path),
            tokens=enc(self.tokens_path),
            data_dir=enc(root / "espeak-ng-data") if (root / "espeak-ng-data").is_dir() else None,
            length_scale=1.0,
            dict_dir=enc(root / "dict") if (root / "dict").is_dir() else None,
            lexicon=enc(lexicon) if lexicon else None,
            lang=None,
        )
        model = _ModelConfig()
        model.kokoro = kokoro
        model.num_threads = self.num_threads
        model.debug = 0
        model.provider = self.provider.encode("utf-8")

        config = _TtsConfig()
        config.model = model
        config.rule_fsts = None
        config.max_num_sentences = 1
        config.silence_scale = 0.2

        keep.extend([kokoro, model, config])
        self._config_refs = keep

        log.info("正在加载 Kokoro 模型（线程 %s，%s 个说话人）…",
                 self.num_threads, len(KOKORO_VOICES))
        handle = lib.SherpaOnnxCreateOfflineTts(ctypes.byref(config))
        if not handle:
            raise TTSError(
                "Kokoro 模型加载失败。\n"
                f"  模型目录：{root}\n"
                "  常见原因：模型文件不完整，或缺 onnxruntime.dll。"
            )
        self._tts = handle
        try:
            self._sample_rate = lib.SherpaOnnxOfflineTtsSampleRate(handle) or 24000
        except Exception:
            self._sample_rate = 24000
        try:
            self._num_speakers = lib.SherpaOnnxOfflineTtsNumSpeakers(handle)
        except Exception:
            self._num_speakers = len(KOKORO_VOICES)
        log.info("Kokoro 就绪，%s Hz，%s 个说话人",
                 self._sample_rate, self._num_speakers)

    # ------------------------------------------------------------------

    def _voice_of(self, params: VoiceParams) -> KokoroVoice:
        raw = (params.voice or "").strip()
        # 人设/音色里可能填的是「自动」或空 —— 那就用默认
        if not raw or raw in ("默认音色", "自动", "default"):
            return VOICE_BY_SID.get(self.sid) or VOICE_BY_SID[DEFAULT_SID]
        voice = parse_voice(raw)
        if voice is None:
            return VOICE_BY_SID.get(self.sid) or VOICE_BY_SID[DEFAULT_SID]
        return voice

    def _generation_config(self, voice: KokoroVoice, speed: float):
        config = _GenerationConfig()
        config.silence_scale = 0.2
        config.speed = speed
        config.sid = voice.sid
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

        from .zipvoice import _rate_to_speed

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
                log.error("Kokoro 流式回调出错：%s", exc)
                state["abort"] = True
                return 0

        from .zipvoice import ZipVoiceTTS

        callback = ZipVoiceTTS._CALLBACK_TYPE(_on_audio)

        with self._lock:
            self._ensure_ready()
            voice = self._voice_of(params)
            speed = self.speed if params.rate in ("", "+0%") else _rate_to_speed(params.rate)
            config = self._generation_config(voice, speed)
            self._config_refs.append(config)
            if on_open is not None:
                on_open(self._sample_rate)

            audio = lib.SherpaOnnxOfflineTtsGenerateWithConfig(
                self._tts, text.encode("utf-8"), ctypes.byref(config),
                callback, None,
            )
            if not audio:
                raise TTSError("Kokoro 合成失败（返回空指针）")
            try:
                total = audio.contents.n or state["total"]
            finally:
                lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio)

        return total

    async def synthesize(self, text: str, params: VoiceParams) -> bytes:
        import array

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
        # 直接用 provider 的采样率打包成 WAV
        return _pack_wav(samples, self._sample_rate)

    async def check(self) -> str:
        await asyncio.to_thread(self._ensure_ready)
        voice = VOICE_BY_SID.get(self.sid) or VOICE_BY_SID[DEFAULT_SID]
        return (f"Kokoro 就绪：{self.num_speakers} 个说话人，"
                f"采样率 {self._sample_rate} Hz，当前音色 {voice.label}")

    def close(self) -> None:
        with self._lock:
            if self._tts is not None and self._lib is not None:
                try:
                    self._lib.SherpaOnnxDestroyOfflineTts(self._tts)
                except Exception:
                    pass
                self._tts = None
            self._config_refs = []


def _pack_wav(samples, rate: int) -> bytes:
    import io
    import struct
    import wave

    data = samples.tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data
