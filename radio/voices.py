"""语音资源管理：本地克隆引擎 + 音色（音频样本）。

这里管两件事：

  1. **引擎资源** —— sherpa-onnx 运行时 + ZipVoice 零样本克隆模型 + 声码器
     全部是纯 ONNX，跑在 CPU 上，不需要 PyTorch、不需要显卡、不需要第二个 Python。

  2. **音色** —— 一个音色就是一个文件夹，里面是：

         voices/小夜/
           ├── sample.wav     一段干净的干声（5~15 秒）
           ├── sample.txt     这段音频里说的内容（要一字不差）
           └── meta.json      显示名、备注、创建时间

     合成的时候把这段样本喂给模型，它就会用这个人的音色说话。
     这就是「零样本克隆」：不用训练，给一段音频就能用。

国内直连 github.com 经常握手失败，所以下载全部走「多镜像 + 断点续传 + 重试」。
"""

from __future__ import annotations

import array
import io
import json
import logging
import shutil
import tarfile
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .dsp import apply_tuning, resample

log = logging.getLogger("radio.voices")

ProgressFn = Callable[[str, int, int], None]
StatusFn = Callable[[str], None]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ai-radio/3.0"

# ---------------------------------------------------------------------------
# 要下载的引擎资源
# ---------------------------------------------------------------------------

_GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
_MIRRORS = ("https://ghfast.top/", "https://ghproxy.net/", "")

SHERPA_RUNTIME = {
    "key": "sherpa-onnx 运行时",
    "archive": "sherpa-onnx-v1.13.8-win-x64-shared-MD-Release.tar.bz2",
    "url": f"{_GH}/v1.13.8/sherpa-onnx-v1.13.8-win-x64-shared-MD-Release.tar.bz2",
    "size_mb": 19.5,
    "extract_top": "sherpa-onnx-v1.13.8-win-x64-shared-MD-Release",
}

ZIPVOICE_MODELS: dict[str, dict] = {
    # 全精度：解码器 455MB。音质明显更干净，代价是加载慢、占内存多。
    "fp32": {
        "key": "ZipVoice 克隆模型（中英 · 全精度）",
        "archive": "sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia.tar.bz2",
        "url": f"{_GH}/tts-models/sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia.tar.bz2",
        "size_mb": 455.7,
        "extract_top": "sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia",
    },
    # 8 位量化：解码器 119MB。快、省内存，但会有沙沙的量化噪声。
    "int8": {
        "key": "ZipVoice 克隆模型（中英 · 量化）",
        "archive": "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2",
        "url": f"{_GH}/tts-models/sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2",
        "size_mb": 104.1,
        "extract_top": "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia",
    },
}

# 兼容旧引用
ZIPVOICE_MODEL = ZIPVOICE_MODELS["fp32"]

DEFAULT_QUALITY = "fp32"

VOCOS = {
    "key": "声码器 vocos",
    "archive": "vocos_24khz.onnx",
    "url": f"{_GH}/vocoder-models/vocos_24khz.onnx",
    "size_mb": 50.0,
    "extract_top": None,          # 不是压缩包
}

DEFAULT_PROFILE = "默认音色"

# Kokoro：模型自带 103 个说话人。开箱即用，不用录样本，
# 而且因为不是克隆，完全没有克隆带来的杂音/电流声/回音。
KOKORO_MODEL = {
    "key": "Kokoro 内置音色（中英 · 103 个说话人）",
    "archive": "kokoro-int8-multi-lang-v1_1.tar.bz2",
    "url": f"{_GH}/tts-models/kokoro-int8-multi-lang-v1_1.tar.bz2",
    "size_mb": 140.2,
    "extract_top": "kokoro-int8-multi-lang-v1_1",
}


# ---------------------------------------------------------------------------
# 音色（音频样本）
# ---------------------------------------------------------------------------


@dataclass
class VoiceProfile:
    """一个克隆音色。"""

    key: str                      # 文件夹名
    directory: Path
    sample: Path | None = None    # 实际喂给模型的样本（已应用微调）
    original: Path | None = None  # 导入/录制时的原声，微调是非破坏性的
    transcript: str = ""
    name: str = ""
    note: str = ""
    created: float = 0.0
    duration: float = 0.0
    sample_rate: int = 0
    # 音色微调：负数半音 = 更低沉，负数明暗 = 更浑厚
    semitones: float = 0.0
    tone: float = 0.0
    denoise: float = 0.0        # 噪声门强度，压掉词间的混响尾巴（消除"回音"）

    @property
    def display(self) -> str:
        return self.name or self.key

    @property
    def ready(self) -> bool:
        """样本和文本都有才算能用。"""
        return bool(self.sample and self.sample.exists() and self.transcript.strip())

    @property
    def problem(self) -> str:
        if self.sample is None or not self.sample.exists():
            return "缺少 sample.wav"
        if not self.transcript.strip():
            return "缺少 sample.txt（样本对应的文字）"
        return ""

    @property
    def tuned(self) -> bool:
        return (abs(self.semitones) >= 0.01 or abs(self.tone) >= 0.02
                or self.denoise >= 0.02)

    def describe(self) -> str:
        if not self.ready:
            return f"⚠ {self.problem}"
        bits = [f"{self.duration:.1f} 秒样本", f"{len(self.transcript)} 字文本"]
        if self.tuned:
            from .dsp import describe as describe_tune

            bits.append(describe_tune(self.semitones, self.tone))
        return " · ".join(bits)


def read_wav_info(path: Path) -> tuple[int, int, float]:
    """读 WAV 的 (声道数, 采样率, 时长)。"""
    with wave.open(str(path)) as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        channels = handle.getnchannels()
        return channels, rate, frames / rate if rate else 0.0


def normalize_wav(data: bytes) -> bytes:
    """把任意 PCM WAV 统一成「16bit 单声道」——克隆引擎只吃这种。

    立体声会混成单声道；采样率保持不动（引擎内部会重采样）。
    """
    with wave.open(io.BytesIO(data)) as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())

    if width != 2:
        raise ValueError(
            f"只支持 16 位 PCM 的 WAV（这个是 {width * 8} 位）。\n"
            "  用音频软件导出成「16-bit PCM WAV」再试。"
        )

    if channels > 1:
        samples = array.array("h")
        samples.frombytes(frames[: len(frames) - (len(frames) % (2 * channels))])
        mono = array.array("h")
        for index in range(0, len(samples), channels):
            mono.append(int(sum(samples[index:index + channels]) / channels))
        frames = mono.tobytes()
        channels = 1

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 参考音频的准备与渲染
# ---------------------------------------------------------------------------

# 统一成模型的原生采样率，这样引擎内部不用再重采样一次
TARGET_RATE = 24000


def bytes_to_samples(wav_bytes: bytes) -> tuple[array.array, int]:
    with wave.open(io.BytesIO(wav_bytes)) as handle:
        rate = handle.getframerate()
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    return samples, rate


def samples_to_wav_bytes(samples: array.array, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())
    return buffer.getvalue()


def prepare_sample(wav_bytes: bytes) -> array.array:
    """用户给的 WAV -> 24kHz 单声道采样数组（这就是「原声」）。

    注意：这里换采样率用的是重采样 + 改标记采样率，
    所以音调和时长都不变，只是数据格式统一了。
    """
    normalized = normalize_wav(wav_bytes)
    samples, rate = bytes_to_samples(normalized)
    if rate != TARGET_RATE and rate > 0:
        samples = resample(samples, TARGET_RATE / rate)
    return samples


def render_sample(directory: Path, semitones: float = 0.0,
                  tone: float = 0.0, denoise: float = 0.0) -> array.array:
    """从 original.wav 按微调参数生成 sample.wav（真正喂给模型的）。

    安全约定：**original.wav 必须先存在**。
    如果这是一个老音色（只有 sample.wav），先把当前的 sample.wav 固化成
    original.wav，再基于它生成新的 sample.wav。

    绝对不能直接拿 sample.wav 调完写回 sample.wav ——
    那样每调一次都会在上一次的结果上叠加，而且会永久毁掉唯一的原声。
    """
    original = directory / "original.wav"
    if not original.exists():
        current = directory / "sample.wav"
        if not current.exists():
            raise FileNotFoundError(f"{directory.name} 里既没有 original.wav 也没有 sample.wav")
        original.write_bytes(current.read_bytes())
        log.info("为老音色「%s」补建了 original.wav（原声备份）", directory.name)

    samples, rate = bytes_to_samples(original.read_bytes())
    tuned = apply_tuning(samples, semitones=semitones, tone=tone,
                         denoise=denoise, sample_rate=rate or TARGET_RATE)
    (directory / "sample.wav").write_bytes(samples_to_wav_bytes(tuned, rate or TARGET_RATE))
    return tuned


# ---------------------------------------------------------------------------


class VoiceKit:
    """引擎资源的安装 + 音色的增删查。"""

    def __init__(self, runtime_dir: Path, model_dir: Path, profiles_dir: Path,
                 quality: str = DEFAULT_QUALITY) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.model_dir = Path(model_dir)
        self.profiles_dir = Path(profiles_dir)
        self.quality = quality if quality in ZIPVOICE_MODELS else DEFAULT_QUALITY

    @property
    def model_spec(self) -> dict:
        return ZIPVOICE_MODELS[self.quality]

    @property
    def other_quality(self) -> str:
        return "int8" if self.quality == "fp32" else "fp32"

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    @property
    def tts_exe(self) -> Path:
        for candidate in (
            self.runtime_dir / "bin" / "sherpa-onnx-offline-tts.exe",
            self.runtime_dir / "sherpa-onnx-offline-tts.exe",
        ):
            if candidate.exists():
                return candidate
        # 解压时可能多套了一层目录
        for found in self.runtime_dir.rglob("sherpa-onnx-offline-tts.exe"):
            return found
        return self.runtime_dir / "bin" / "sherpa-onnx-offline-tts.exe"

    @property
    def zipvoice_dir(self) -> Path:
        candidate = self.model_dir / self.model_spec["extract_top"]
        if candidate.is_dir():
            return candidate
        # 指定的精度没装，但有别的版本 —— 先用着，别让整个软件卡死
        for name, spec in ZIPVOICE_MODELS.items():
            fallback = self.model_dir / spec["extract_top"]
            if fallback.is_dir():
                log.warning("没找到 %s 版模型，退而使用 %s 版", self.quality, name)
                return fallback
        for found in self.model_dir.glob("*zipvoice*"):
            if found.is_dir():
                return found
        return candidate

    @property
    def vocoder(self) -> Path:
        return self.model_dir / VOCOS["archive"]

    # ---- Kokoro ------------------------------------------------------

    @property
    def kokoro_dir(self) -> Path:
        candidate = self.model_dir / KOKORO_MODEL["extract_top"]
        if candidate.is_dir():
            return candidate
        for found in self.model_dir.glob("kokoro*"):
            if found.is_dir():
                return found
        return candidate

    def kokoro_ready(self) -> bool:
        root = self.kokoro_dir
        if not root.is_dir():
            return False
        model = any((root / name).exists()
                    for name in ("model.int8.onnx", "model.onnx"))
        return model and (root / "voices.bin").exists() \
            and (root / "tokens.txt").exists()

    # ---- 角色音色（VITS 中文多说话人）------------------------------

    def character_dir(self, key: str) -> Path:
        """角色音色的目录，例如 models/vits-zh-hf-keqing。"""
        return self.model_dir / f"vits-zh-hf-{key}"

    def character_ready(self, key: str) -> bool:
        directory = self.character_dir(key)
        if not directory.is_dir():
            return False
        has_model = any(directory.glob("*.onnx"))
        return bool(has_model
                    and (directory / "lexicon.txt").exists()
                    and (directory / "tokens.txt").exists())

    def installed_characters(self) -> list[str]:
        from .tts.characters import CHARACTER_VOICES

        return [v.key for v in CHARACTER_VOICES if self.character_ready(v.key)]

    def install_character(self, key: str, progress: ProgressFn | None = None,
                          status: StatusFn | None = None) -> None:
        """下载一个角色音色（约 115MB）。"""
        from .tts.characters import BY_KEY

        voice = BY_KEY.get(key)
        if voice is None:
            raise ValueError(f"没有这个角色音色：{key}")
        self.model_dir.mkdir(parents=True, exist_ok=True)
        if not self.runtime_ready():
            self._install_archive(SHERPA_RUNTIME, self.runtime_dir, progress, status)
        spec = {
            "key": f"角色音色 {voice.name}（{voice.origin}）",
            "archive": f"{voice.archive}.tar.bz2",
            "url": voice.url,
            "size_mb": voice.size_mb,
            "extract_top": voice.archive,
        }
        if not self.character_ready(key):
            self._install_archive(spec, self.model_dir, progress, status)
        elif status:
            status(f"角色音色 {voice.name} 已就绪")

    def install_kokoro(self, progress: ProgressFn | None = None,
                       status: StatusFn | None = None) -> None:
        """只装 Kokoro（内置音色）那部分，不碰克隆模型。"""
        self.model_dir.mkdir(parents=True, exist_ok=True)
        if not self.runtime_ready():
            self._install_archive(SHERPA_RUNTIME, self.runtime_dir, progress, status)
        elif status:
            status(f"{SHERPA_RUNTIME['key']} 已就绪")
        if not self.kokoro_ready():
            self._install_archive(KOKORO_MODEL, self.model_dir, progress, status)
        elif status:
            status("Kokoro 内置音色已就绪")

    def _model_file(self, *names: str) -> Path | None:
        for name in names:
            candidate = self.zipvoice_dir / name
            if candidate.exists():
                return candidate
        return None

    @property
    def encoder(self) -> Path | None:
        if self.quality == "int8":
            return self._model_file("encoder.int8.onnx", "encoder.onnx")
        return self._model_file("encoder.onnx", "encoder.int8.onnx")

    @property
    def decoder(self) -> Path | None:
        if self.quality == "int8":
            return self._model_file("decoder.int8.onnx", "decoder.onnx")
        return self._model_file("decoder.onnx", "decoder.int8.onnx")

    @property
    def tokens(self) -> Path | None:
        return self._model_file("tokens.txt")

    @property
    def lexicon(self) -> Path | None:
        return self._model_file("lexicon.txt")

    @property
    def espeak_data(self) -> Path | None:
        candidate = self.zipvoice_dir / "espeak-ng-data"
        return candidate if candidate.is_dir() else None

    # ------------------------------------------------------------------
    # 安装状态
    # ------------------------------------------------------------------

    def runtime_ready(self) -> bool:
        return self.tts_exe.exists()

    def model_ready(self) -> bool:
        return all((self.encoder, self.decoder, self.tokens, self.espeak_data)) \
            and self.vocoder.exists()

    def ready(self) -> bool:
        return self.runtime_ready() and self.model_ready()

    def missing_parts(self) -> list[str]:
        out = []
        if not self.runtime_ready():
            out.append(SHERPA_RUNTIME["key"])
        if not self.model_ready():
            out.append(self.model_spec["key"])
            if not self.vocoder.exists():
                out.append(VOCOS["key"])
        return out

    def describe(self) -> str:
        if self.ready():
            profiles = self.profiles()
            return f"引擎已就绪 · {len(profiles)} 个音色"
        return "缺少：" + "、".join(self.missing_parts())

    # ------------------------------------------------------------------
    # 下载安装
    # ------------------------------------------------------------------

    def install(self, progress: ProgressFn | None = None,
                status: StatusFn | None = None) -> None:
        """把运行时、模型、声码器都装好（已有的会跳过）。"""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.model_dir.mkdir(parents=True, exist_ok=True)

        if not self.runtime_ready():
            self._install_archive(SHERPA_RUNTIME, self.runtime_dir, progress, status)
        elif status:
            status(f"{SHERPA_RUNTIME['key']} 已就绪")

        if not self.model_ready():
            if self.encoder is None or self.decoder is None:
                self._install_archive(self.model_spec, self.model_dir, progress, status)
            if not self.vocoder.exists():
                self._install_file(VOCOS, self.vocoder, progress, status)
        elif status:
            status("克隆模型已就绪")

    def _install_archive(self, spec: dict, target_dir: Path,
                         progress: ProgressFn | None, status: StatusFn | None) -> None:
        archive = target_dir / spec["archive"]
        if not archive.exists():
            self._fetch(spec, archive, progress, status)
        # 下载完立刻校验，损坏就删掉重来
        if not _valid_bz2(archive):
            archive.unlink(missing_ok=True)
            self._fetch(spec, archive, progress, status)

        if status:
            status(f"正在解压 {spec['key']}…")
        with tarfile.open(archive, "r:bz2") as tf:
            top = spec.get("extract_top")
            if top:
                members = []
                for member in tf.getmembers():
                    parts = Path(member.name).parts
                    if len(parts) < 2:
                        continue
                    member.name = str(Path(*parts[1:]))
                    members.append(member)
                tf.extractall(target_dir, members=members)
            else:
                tf.extractall(target_dir)
        archive.unlink(missing_ok=True)

        # 关键一步：从 GitHub 下下来的 exe/dll 会被 Windows 打上
        # 「来自互联网」标记，一运行就弹安全提示。装完必须清掉，
        # 否则用户第一次开麦就会撞上「Windows 保护了你的电脑」。
        from .winfs import unblock_tree

        cleared = unblock_tree(target_dir)
        if cleared and status:
            status(f"{spec['key']} 已解压，并清除了 {cleared} 个文件的联网标记")

    def _install_file(self, spec: dict, dest: Path,
                      progress: ProgressFn | None, status: StatusFn | None) -> None:
        if not dest.exists():
            self._fetch(spec, dest, progress, status)

    def _fetch(self, spec: dict, dest: Path,
               progress: ProgressFn | None, status: StatusFn | None) -> None:
        errors = []
        for mirror in _MIRRORS:
            url = mirror + spec["url"]
            host = url.split("/")[2]
            try:
                if status:
                    status(f"正在从 {host} 下载 {spec['key']}"
                           f"（约 {spec['size_mb']:.0f}MB）…")
                _download_resumable(url, dest, progress)
                return
            except Exception as exc:
                errors.append(f"{host}: {exc}")
                log.warning("从 %s 下载 %s 失败: %s", host, spec["key"], exc)
                if status:
                    status(f"{host} 不行，换下一个源…")
        dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
        raise RuntimeError(
            f"{spec['key']} 下载失败。\n  尝试过的源：\n    " + "\n    ".join(errors) +
            f"\n  也可以手动下载后放到 {dest}"
        )

    # ------------------------------------------------------------------
    # 音色管理
    # ------------------------------------------------------------------

    def profiles(self) -> list[VoiceProfile]:
        if not self.profiles_dir.is_dir():
            return []
        out: list[VoiceProfile] = []
        for directory in sorted(self.profiles_dir.iterdir()):
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            out.append(self._read_profile(directory))
        return out

    def _read_profile(self, directory: Path) -> VoiceProfile:
        meta: dict = {}
        meta_file = directory / "meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}

        sample = None
        for name in ("sample.wav", "sample.WAV", "ref.wav"):
            candidate = directory / name
            if candidate.exists():
                sample = candidate
                break

        original = directory / "original.wav"
        if not original.exists():
            original = sample            # 老版本没有保留原声，就拿现样本当原声

        transcript = ""
        text_file = directory / "sample.txt"
        if text_file.exists():
            transcript = text_file.read_text(encoding="utf-8").strip()

        duration = sample_rate = 0
        if sample is not None:
            try:
                _, sample_rate, duration = read_wav_info(sample)
            except Exception:
                pass

        return VoiceProfile(
            key=directory.name,
            directory=directory,
            sample=sample,
            original=original,
            transcript=transcript,
            name=str(meta.get("name") or directory.name),
            note=str(meta.get("note") or ""),
            created=float(meta.get("created") or 0),
            duration=duration,
            sample_rate=sample_rate,
            semitones=float(meta.get("semitones") or 0),
            tone=float(meta.get("tone") or 0),
            denoise=float(meta.get("denoise") or 0),
        )

    def get(self, key: str) -> VoiceProfile | None:
        for profile in self.profiles():
            if profile.key == key or profile.name == key:
                return profile
        return None

    def create(self, key: str, wav_bytes: bytes, transcript: str,
               name: str = "", note: str = "", semitones: float = 0.0,
               tone: float = 0.0, denoise: float = 0.0) -> VoiceProfile:
        """用一段音频样本建一个音色。

        wav_bytes 会先统一成 24kHz 单声道存成 original.wav，
        然后按 semitones / tone 生成真正喂给模型的 sample.wav。
        这样微调是可逆的 —— 随时能从原声重新生成。
        """
        key = (key or "").strip()
        if not key:
            raise ValueError("音色名字不能为空")
        if any(ch in key for ch in '\\/:*?"<>|'):
            raise ValueError('音色名字不能包含 \\ / : * ? " < > | 这些字符')
        transcript = (transcript or "").strip()
        if not transcript:
            raise ValueError(
                "必须填写「样本里说了什么」。\n"
                "  克隆需要知道这段音频对应的文字，才能学会这个声音。"
            )

        directory = self.profiles_dir / key
        directory.mkdir(parents=True, exist_ok=True)
        base = prepare_sample(wav_bytes)
        (directory / "original.wav").write_bytes(
            samples_to_wav_bytes(base, TARGET_RATE))
        render_sample(directory, semitones=semitones, tone=tone, denoise=denoise)
        (directory / "sample.txt").write_text(transcript, encoding="utf-8")

        self._write_meta(directory, name=name or key, note=note,
                         created=time.time(), semitones=semitones, tone=tone,
                         denoise=denoise)
        return self.get(directory.name) or self._read_profile(directory)

    def _write_meta(self, directory: Path, **fields) -> None:
        meta_file = directory / "meta.json"
        meta: dict = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta.update(fields)
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def retune(self, key: str, semitones: float = 0.0, tone: float = 0.0,
               denoise: float = 0.0) -> VoiceProfile | None:
        """调整音色（从原声重新生成，不会累积失真）。"""
        profile = self.get(key)
        if profile is None:
            return None
        render_sample(profile.directory, semitones=semitones, tone=tone,
                      denoise=denoise)
        self._write_meta(profile.directory, semitones=float(semitones),
                         tone=float(tone), denoise=float(denoise))
        return self.get(key)

    def update(self, key: str, *, name: str = "", note: str = "",
               transcript: str = "") -> VoiceProfile | None:
        profile = self.get(key)
        if profile is None:
            return None
        if transcript.strip():
            (profile.directory / "sample.txt").write_text(
                transcript.strip(), encoding="utf-8")
        meta_file = profile.directory / "meta.json"
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        if name:
            meta["name"] = name
        if note or note == "":
            meta["note"] = note
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        return self.get(key)

    def delete(self, key: str) -> bool:
        profile = self.get(key)
        if profile is None:
            return False
        shutil.rmtree(profile.directory, ignore_errors=True)
        return True

    def rename(self, key: str, new_key: str) -> VoiceProfile | None:
        profile = self.get(key)
        if profile is None:
            return None
        new_key = (new_key or "").strip()
        if not new_key or any(ch in new_key for ch in '\\/:*?"<>|'):
            raise ValueError("新名字不合法")
        target = self.profiles_dir / new_key
        if target.exists():
            raise FileExistsError(f"「{new_key}」已经存在")
        profile.directory.rename(target)
        return self.get(new_key)


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------


def _download_resumable(url: str, dest: Path, progress: ProgressFn | None,
                        attempts: int = 4, timeout: float = 60.0) -> Path:
    """支持断点续传的下载。

    github release 的直链在国内经常中途断掉，续传能把已经下好的部分留住，
    不然 100MB 断一次就得从头再来。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_error: Exception | None = None

    for attempt in range(attempts):
        pos = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if pos:
            headers["Range"] = f"bytes={pos}-"
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                # 服务器不支持续传 -> 从头来
                if pos and response.status != 206:
                    log.info("服务器不支持断点续传，重新下载 %s", dest.name)
                    pos = 0
                    tmp.unlink(missing_ok=True)

                total = pos + int(response.headers.get("Content-Length") or 0)
                done = pos
                started = time.time()
                mode = "ab" if pos else "wb"
                with open(tmp, mode) as fp:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        fp.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(dest.name, done, total)
                        # 卡死检测：超过 25 秒且速度低得离谱就断开重来
                        elapsed = time.time() - started
                        if elapsed > 25 and (done - pos) / elapsed < 15 * 1024:
                            raise TimeoutError("下载速度过慢，重新连接")

            if total and tmp.stat().st_size < total:
                raise IOError(f"没下完 {tmp.stat().st_size}/{total}")
            tmp.replace(dest)
            return dest
        except Exception as exc:
            last_error = exc
            log.warning("下载 %s 第 %s 次失败: %s", dest.name, attempt + 1, exc)
            if attempt < attempts - 1:
                time.sleep(2.0 * (attempt + 1))

    raise RuntimeError(f"{last_error}（已重试 {attempts} 次）")


def _valid_bz2(path: Path) -> bool:
    """粗查一下 bz2 文件是不是完整的。"""
    if not path.exists() or path.stat().st_size < 1024:
        return False
    try:
        with tarfile.open(path, "r:bz2") as tf:
            tf.getmembers()
        return True
    except Exception:
        return False


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"
