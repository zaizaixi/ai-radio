"""语音合成 + 播放。

对上层只暴露 SpeechEngine：
    engine.speak("一段话", VoiceParams(...))   # 合成并播完
    engine.interrupt()                          # 立刻打断

支持三种引擎（见 build_provider）：
    vits      角色音色，804 个说话人，最快（实测 RTF 0.25）
    kokoro    通用内置音色 103 个
    zipvoice  零样本克隆，用你自己的音频样本

合成前会做两件文本处理：
    pronunciation  多音字换同音替身（见 radio/pronunciation.py）
    delivery       按标点决定语速/音量/停顿（见 radio/delivery.py）
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path

from ..audio import (AudioDevice, StreamPlayer, apply_volume, list_devices,
                     make_player)
from ..config import AudioConfig, TTSConfig
from ..persona import split_for_tts
from ..voices import VoiceKit
from .base import TTSError, TTSProvider, VoiceParams

log = logging.getLogger("radio.tts")

_RATE_RE = re.compile(r"^\s*([+-]?)\s*(\d+(?:\.\d+)?)\s*%\s*$")


def _scale_rate(rate: str, factor: float) -> str:
    """把语速字符串乘一个倍数。'+0%' 乘 1.07 → '+7%'。"""
    if abs(factor - 1.0) < 0.005:
        return rate or "+0%"
    base = 0.0
    match = _RATE_RE.match(rate or "")
    if match:
        sign = -1.0 if match.group(1) == "-" else 1.0
        base = sign * float(match.group(2))
    value = (1.0 + base / 100.0) * factor
    value = max(0.3, min(3.0, value))
    percent = (value - 1.0) * 100.0
    return f"{percent:+.0f}%"

__all__ = [
    "SpeechEngine",
    "VoiceParams",
    "TTSError",
    "TTSProvider",
    "VoiceKit",
    "build_provider",
    "list_devices",
    "AudioDevice",
]


class NullTTS:
    """不出声，只把要说的话打印出来（排错、写稿时用）。"""

    name = "none"

    async def synthesize(self, text: str, params: VoiceParams) -> bytes:
        return b""

    async def close(self) -> None:
        return None

    async def check(self) -> str:
        return "当前是无声模式（provider = none）"

    def reload(self) -> None:
        return None


def build_provider(cfg: TTSConfig, kit: VoiceKit) -> TTSProvider:
    kind = (cfg.provider or "zipvoice").strip().lower()
    if kind in ("vits", "角色", "角色音色", "anime"):
        from .characters import RECOMMENDED, get as get_character
        from .vits import VitsTTS

        # cfg.voice 可能是「keqing:12」这种写法
        raw = (cfg.voice or "").strip()
        model_key, sid = "", 0
        for sep in (":", "#", "/"):
            if sep in raw:
                head, tail = raw.rsplit(sep, 1)
                if tail.strip().isdigit():
                    model_key, sid = head.strip(), int(tail.strip())
                    break
        if not model_key:
            model_key = raw
        voice = get_character(model_key)
        if voice is None:
            voice = get_character(RECOMMENDED[0])
        return VitsTTS(
            kit,
            model_key=voice.key,
            sid=sid,
            num_threads=int(cfg.num_threads or 0),
            noise_scale=float(getattr(cfg, "noise_scale", 0.667) or 0.667),
            noise_scale_w=float(getattr(cfg, "noise_scale_w", 0.8) or 0.8),
            silence_scale=float(getattr(cfg, "silence_scale", 0.2) or 0.2),
        )
    if kind in ("kokoro", "内置", "虚拟音"):
        from .kokoro import DEFAULT_SID, KokoroTTS, parse_voice

        voice = parse_voice(cfg.voice)
        return KokoroTTS(
            kit,
            sid=voice.sid if voice else DEFAULT_SID,
            num_threads=int(cfg.num_threads or 0),
        )
    if kind in ("zipvoice", "clone", "克隆", "本地"):
        from .zipvoice import ZipVoiceTTS

        return ZipVoiceTTS(
            kit,
            voice_key=cfg.voice,
            num_threads=int(cfg.num_threads or 0),
            num_steps=int(cfg.num_steps or 8),
            guidance_scale=float(getattr(cfg, "guidance_scale", 1.0) or 1.0),
        )
    if kind in ("none", "off", "无声"):
        return NullTTS()
    raise ValueError(
        f"未知的 TTS provider: {cfg.provider!r}"
        f"（可选 kokoro / zipvoice / none）"
    )


class SpeechEngine:
    """把一段文字变成声音放出来。"""

    def __init__(self, tts_cfg: TTSConfig, audio_cfg: AudioConfig,
                 work_dir: Path, kit: VoiceKit) -> None:
        self.tts_cfg = tts_cfg
        self.audio_cfg = audio_cfg
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.kit = kit

        self.provider = build_provider(tts_cfg, kit)
        self.player = make_player(
            volume=audio_cfg.volume,
            device=audio_cfg.device,
            cache_dir=self.work_dir / "cache",
        )
        self._interrupt = threading.Event()
        self.max_chars = int(tts_cfg.max_chars or 220)
        self.last_text = ""

        # 读音纠正：把多音字换成同音替身，送去合成之前做
        # （见 radio/pronunciation.py，用户可在 voices/pronunciation.txt 里加规则）
        try:
            from ..pronunciation import Pronunciation

            self.pronunciation = Pronunciation(
                self.kit.profiles_dir,
                use_builtin=bool(getattr(tts_cfg, "fix_polyphones", True)))
        except Exception as exc:
            log.warning("读音纠正表加载失败，跳过：%s", exc)
            self.pronunciation = None

        # 标点感知：按 。！？…… 决定每句的语速/音量/停顿
        self.delivery_on = bool(getattr(tts_cfg, "punctuation_delivery", True))
        # 语气强度：0 = 全拉平，1 = 默认，2 = 夸张
        self.delivery_strength = float(
            getattr(tts_cfg, "punctuation_strength", 1.0) or 0.0)

    # ------------------------------------------------------------------

    def rebuild(self, kit: VoiceKit | None = None) -> None:
        """换掉底层引擎（比如切了模型精度），下次说话时按新配置重建。"""
        old = self.provider
        if kit is not None:
            self.kit = kit
        self.provider = build_provider(self.tts_cfg, self.kit)
        self._interrupt.set()
        close = getattr(old, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
        self._interrupt.clear()
        log.info("已切换到 %s 引擎（精度 %s）", self.provider_name,
                 getattr(self.kit, "quality", "?"))

    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return getattr(self.provider, "name", "?")

    @property
    def playing(self) -> bool:
        return self.player.is_playing

    def interrupt(self) -> None:
        """立刻打断当前播报。"""
        self._interrupt.set()
        self.player.stop()

    def clear_interrupt(self) -> None:
        self._interrupt.clear()

    def reload_pronunciation(self) -> int:
        """用户改了 voices/pronunciation.txt 之后调用。"""
        if self.pronunciation is None:
            return 0
        return self.pronunciation.reload()

    def reload_voice(self) -> None:
        """换了音色或模型之后让引擎重新加载。"""
        reload_fn = getattr(self.provider, "reload", None)
        if reload_fn:
            reload_fn()

    # ------------------------------------------------------------------

    def resolve_params(self, voice: str = "", rate: str = "",
                       pitch: str = "", volume: str = "") -> VoiceParams:
        """人设里写了就用人的，没写就用 config.toml 的。

        注意：ZipVoice 只认 voice 和 rate（语速）。
        pitch / volume 是早期引擎留下的字段，这里原样带上但不生效。
        """
        cfg = self.tts_cfg
        return VoiceParams(
            voice=voice or cfg.voice,
            rate=rate or cfg.rate,
            pitch=pitch,
            volume=volume,
        )

    # ------------------------------------------------------------------

    async def speak(self, text: str, params: VoiceParams | None = None,
                    on_chunk=None) -> bool:
        """合成并播放一段话。返回 True 表示完整播完，False 表示被打断。"""
        params = params or self.resolve_params()
        text = (text or "").strip()
        if not text:
            return True
        self.last_text = text

        if isinstance(self.provider, NullTTS):
            log.info("[无声模式] %s", text)
            if on_chunk is not None:
                result = on_chunk(text)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(min(6.0, 0.12 * len(text)))
            return True

        if self.pronunciation is not None:
            text = self.pronunciation.fix(text)

        # 断句交给谁，取决于引擎：
        #   VITS      内部会按中文标点自己断句，还能用 silence_scale 控制停顿。
        #             而且实测 RTF 0.25、没有每段固定开销 ——
        #             那就别在外面抢着切，整段给它，让它自己安排节奏，
        #             听起来才像真人一口气念完，而不是被硬切成一截一截。
        #   其它引擎  有每段固定开销，切太碎会断断续续，要控制单段长度。
        limit = self.max_chars
        if self.provider_name in ("vits",):
            limit = max(self.max_chars, 400)

        # 标点感知：按 。！？…… 切成「语气单元」，每句用不同的语速/音量/停顿念。
        # 关掉的话就退回「只按长度切」的老路子。
        if getattr(self, "delivery_on", False):
            from ..delivery import scale, split_units

            units = split_units(text, limit)
            if units and abs(self.delivery_strength - 1.0) > 0.01:
                units = scale(units, self.delivery_strength)
            if units:
                if hasattr(self.provider, "synthesize_stream"):
                    return await self._speak_units(units, params, on_chunk)
                chunks = [str(u) for u in units]
                return await self._speak_chunked(chunks, params, on_chunk)

        chunks = split_for_tts(text, limit)
        if not chunks:
            return True

        if hasattr(self.provider, "synthesize_stream"):
            return await self._speak_streaming(chunks, params, on_chunk)
        return await self._speak_chunked(chunks, params, on_chunk)

    # ------------------------------------------------------------------
    # 标点感知：逐句用不同的念法
    # ------------------------------------------------------------------

    async def _speak_units(self, units, params: VoiceParams, on_chunk) -> bool:
        """每个语气单元单独合成 —— 语速和音量按收尾标点变，念完补一段静音。"""
        self._interrupt.clear()
        loop = asyncio.get_running_loop()
        for unit in units:
            if self._interrupt.is_set():
                return False
            # 单元自己的语速乘在基准语速上
            rate = _scale_rate(params.rate, unit.delivery.speed)
            unit_params = VoiceParams(voice=params.voice, rate=rate,
                                      pitch=params.pitch, volume=params.volume)

            started = asyncio.Event()

            def mark_started(_loop=loop, _event=started) -> None:
                _loop.call_soon_threadsafe(_event.set)

            play_task = asyncio.create_task(asyncio.to_thread(
                self._play_stream, unit.text, unit_params, mark_started,
                unit.delivery.gain_db, unit.delivery.pause))
            started_task = asyncio.create_task(started.wait())
            try:
                done, _ = await asyncio.wait({play_task, started_task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if on_chunk is not None and started_task in done:
                    result = on_chunk(str(unit))
                    if asyncio.iscoroutine(result):
                        await result
            finally:
                started_task.cancel()

            if not await play_task:
                return False
        return True

    # ------------------------------------------------------------------
    # 流式：整段一次合成，音频边生成边播（推荐）
    # ------------------------------------------------------------------

    async def _speak_streaming(self, chunks: list[str], params: VoiceParams,
                               on_chunk) -> bool:
        self._interrupt.clear()
        loop = asyncio.get_running_loop()
        finished = True
        for chunk in chunks:
            if self._interrupt.is_set():
                finished = False
                break

            started = asyncio.Event()

            def mark_started(_loop=loop, _event=started) -> None:
                _loop.call_soon_threadsafe(_event.set)

            play_task = asyncio.create_task(
                asyncio.to_thread(self._play_stream, chunk, params, mark_started))
            started_task = asyncio.create_task(started.wait())
            try:
                done, _pending = await asyncio.wait(
                    {play_task, started_task},
                    return_when=asyncio.FIRST_COMPLETED)
                # 关键：等「真的出声」而不是「开始合成」。
                # 两者之间可能隔着十几秒，界面提示必须卡在正确的位置。
                if on_chunk is not None and started_task in done:
                    result = on_chunk(chunk)
                    if asyncio.iscoroutine(result):
                        await result
            finally:
                started_task.cancel()

            if not await play_task:
                finished = False
                break
        return finished

    def _play_stream(self, text: str, params: VoiceParams,
                     on_started=None, gain_db: float = 0.0,
                     pause: float = 0.0) -> bool:
        """在后台线程里跑：整段合成，音频一到就写进声卡。

        合成比播放慢，所以 write() 的阻塞会形成背压，
        合成和播放自动对上节奏，中间不会出现空白。

        gain_db / pause 由标点决定：感叹句响一点，省略号后面停久一点。
        """
        player = StreamPlayer(device=self.audio_cfg.device,
                              volume=self.audio_cfg.volume)
        opened = {"done": False}
        first = {"done": False}

        gain_pct = 100
        if abs(gain_db) > 0.1:
            gain_pct = max(5, min(400, int(round(100 * (10 ** (gain_db / 20.0))))))

        def on_open(sample_rate: int) -> None:
            player.open(sample_rate)
            opened["done"] = True

        def on_pcm(pcm: bytes) -> bool:
            if self._interrupt.is_set():
                return False
            if not first["done"]:
                first["done"] = True
                if on_started is not None:
                    try:
                        on_started()      # 第一块音频到了 —— 这才叫「出声」
                    except Exception:
                        pass
            if gain_pct != 100:
                pcm = apply_volume(pcm, gain_pct)
            return player.write(pcm)

        try:
            self.provider.synthesize_stream(
                text, params, on_pcm,
                should_stop=self._interrupt.is_set,
                on_open=on_open,
            )
            if opened["done"]:
                # 句子之间补静音：这是标点带来的停顿，不是引擎给的
                if pause > 0.01 and not self._interrupt.is_set():
                    frames = int(player.sample_rate * pause)
                    if frames > 0:
                        player.write(b"\x00" * (frames * 2))
                player.drain()
            return not self._interrupt.is_set()
        except TTSError as exc:
            log.error("流式合成失败：%s", exc)
            return False
        except Exception as exc:      # 不能让播放线程把整个电台带崩
            log.exception("流式播放出错：%s", exc)
            return False
        finally:
            player.close()

    # ------------------------------------------------------------------
    # 分段：老的「合成一段播一段」路径（给不支持流式的引擎兜底）
    # ------------------------------------------------------------------

    async def _speak_chunked(self, chunks: list[str], params: VoiceParams,
                             on_chunk) -> bool:
        self._interrupt.clear()
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)

        async def producer() -> None:
            try:
                for chunk in chunks:
                    if self._interrupt.is_set():
                        break
                    try:
                        data = await self._synthesize(chunk, params)
                    except TTSError as exc:
                        log.error("合成失败：%s", exc)
                        data = b""
                    await queue.put((chunk, data))
            finally:
                await queue.put(None)

        task = asyncio.create_task(producer())
        finished = True
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                chunk, data = item
                if self._interrupt.is_set():
                    finished = False
                    break
                if not data:
                    continue
                if on_chunk is not None:
                    result = on_chunk(chunk)
                    if asyncio.iscoroutine(result):
                        await result
                ok = await asyncio.to_thread(
                    self.player.play, data, None, self._interrupt
                )
                if not ok:
                    finished = False
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return finished

    # ------------------------------------------------------------------

    async def _synthesize(self, text: str, params: VoiceParams) -> bytes:
        attempts = max(1, int(self.tts_cfg.retries) + 1)
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self.provider.synthesize(text, params)
            except TTSError as exc:
                last = exc
                log.warning("合成失败（第 %s/%s 次）：%s", attempt + 1, attempts, exc)
                if attempt < attempts - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
        if last is not None:
            log.error("这段没合成出来：%s", last)
        return b""

    # ------------------------------------------------------------------

    async def check(self) -> str:
        return await self.provider.check()

    async def close(self) -> None:
        self.interrupt()
        close = getattr(self.provider, "close", None)
        if close:
            result = close()
            if asyncio.iscoroutine(result):
                await result
