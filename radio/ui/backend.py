"""图形界面和电台内核之间的桥。

Tkinter 必须在主线程跑，asyncio 只能在它自己的线程里跑，所以这里：
  * 后台线程持有一个 asyncio 事件循环
  * 界面调用的每个动作，都通过 run_coroutine_threadsafe 丢过去
  * 内核产生的事件放进线程安全队列，界面用 after() 轮询取走

界面永远不阻塞，合成、下载、联网这些慢活都在后台跑。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..chat import build_chat_source
from ..config import Config, load_config, save_config
from ..control import ControlServer
from ..director import Director
from ..llm import LLMClient
from ..memory import ConversationMemory, ScriptLibrary
from ..persona import PersonaRegistry
from ..tts import SpeechEngine, VoiceParams
from ..voices import VoiceKit

log = logging.getLogger("radio.ui.backend")

# 界面事件类型
EV_LOG = "log"          # 一行日志
EV_STATUS = "status"    # 状态变化（说话/待命/暂停）
EV_SAY = "say"          # AI 正在说（或说完了）什么
EV_CHAT = "chat"        # 收到一条弹幕
EV_DONE = "done"        # 某个后台任务结束
EV_PROGRESS = "progress"  # 下载进度


class RadioBackend:
    """后台电台。负责起停、试听、下载。"""

    def __init__(self, config_path: Path, events: queue.Queue) -> None:
        self.config_path = Path(config_path)
        self.events = events
        self.cfg: Config = load_config(self.config_path)

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="radio-loop",
                                        daemon=True)
        self._thread.start()

        self.kit = VoiceKit(self.cfg.sherpa_dir, self.cfg.model_dir,
                            self.cfg.voices_dir)

        self._director: Director | None = None
        self._control: ControlServer | None = None
        self._runner: asyncio.Task | None = None
        self._preview_engine: SpeechEngine | None = None
        self._lock = threading.Lock()
        self.running = False
        self._busy = threading.Event()

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, done: Callable[[Any], None] | None = None):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        if done is not None:
            def _callback(fut):
                try:
                    result = fut.result()
                except Exception as exc:
                    result = exc
                self.events.put((EV_DONE, {"result": result, "callback": done}))
            future.add_done_callback(_callback)
        return future

    def emit(self, kind: str, payload: dict) -> None:
        self.events.put((kind, payload))

    def _log(self, message: str) -> None:
        self.emit(EV_LOG, {"message": message, "at": time.time()})

    def reload_config(self) -> Config:
        self.cfg = load_config(self.config_path)
        self.kit = VoiceKit(self.cfg.sherpa_dir, self.cfg.model_dir,
                            self.cfg.voices_dir)
        return self.cfg

    def persist_config(self) -> Path:
        return save_config(self.cfg, self.config_path)

    # ------------------------------------------------------------------
    # 电台启停
    # ------------------------------------------------------------------

    def start_radio(self) -> bool:
        with self._lock:
            if self.running:
                self._log("电台已经在播了")
                return False
            self.running = True
        self.reload_config()
        self._submit(self._start_radio())
        return True

    async def _start_radio(self) -> None:
        try:
            cfg = self.cfg
            personas = PersonaRegistry(cfg.persona_dir, cfg.app.persona)
            scripts = ScriptLibrary(cfg.script_dir, cfg.idle.scripts,
                                    shuffle=cfg.idle.shuffle, loop=cfg.idle.loop)
            memory = ConversationMemory(cfg.llm.history_size)
            engine = SpeechEngine(cfg.tts, cfg.audio, cfg.log_dir / "tts_cache",
                                  self.kit)
            llm = LLMClient(cfg.llm)

            director = Director(cfg, personas, memory, scripts, engine, llm,
                                on_event=self._on_director_event)
            self._director = director

            self._control = ControlServer(cfg.control, director, self._loop)
            self._control.start()

            self._log(f"人设「{personas.current.display_name}」 · "
                      f"语音「{engine.provider_name}」 · "
                      f"弹幕 {cfg.chat.platform}")
            self.emit(EV_STATUS, director.state.as_dict())

            source = build_chat_source(cfg.chat)
            self._runner = asyncio.current_task()
            await director.run(source)
        except Exception as exc:
            log.exception("电台启动失败")
            self._log(f"⚠ 电台启动失败：{exc}")
            self.emit(EV_STATUS, {"speaking": False, "running": False})
        finally:
            self.running = False
            self._director = None
            if self._control is not None:
                self._control.stop()
                self._control = None

    def stop_radio(self) -> None:
        with self._lock:
            if not self.running:
                return
        self._log("正在停播…")
        self._submit(self._stop_radio())

    async def _stop_radio(self) -> None:
        director = self._director
        if director is not None:
            await director.stop()
        task = self._runner
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self.running = False
        self.emit(EV_STATUS, {"speaking": False, "running": False})
        self._log("已停播")

    # ------------------------------------------------------------------
    # 内核事件 -> 界面事件
    # ------------------------------------------------------------------

    def _on_director_event(self, event: str, payload: dict) -> None:
        if event == "speak":
            kind = {"reply": "回话", "idle": "念稿", "say": "插播"}.get(
                payload.get("kind", ""), "开口")
            self.emit(EV_SAY, {"kind": kind, "text": payload.get("text", "")})
        elif event == "trigger":
            self.emit(EV_CHAT, {"username": payload.get("username", ""),
                                "text": payload.get("text", ""),
                                "hit": True})
        elif event == "chat":
            self.emit(EV_CHAT, {"username": payload.get("username", ""),
                                "text": payload.get("text", ""),
                                "hit": False})
        elif event == "persona":
            self._log(f"人设切换 -> {payload.get('name')}")
        elif event == "preparing":
            seconds = float(payload.get("seconds") or 0)
            chars = int(payload.get("chars") or 0)
            self._log(f"正在合成语音（{chars} 字，约 {seconds:.0f} 秒后出声）…")
        elif event == "started":
            self._log("▶ 开始出声")
        elif event == "error":
            self._log(f"⚠ {payload.get('message')}")
        elif event == "interrupted":
            self._log("（被打断）")
        self._emit_state()

    def _emit_state(self) -> None:
        director = self._director
        state = director.state.as_dict() if director else {"speaking": False}
        state["running"] = self.running
        self.emit(EV_STATUS, state)

    # ------------------------------------------------------------------
    # 运行中的操作
    # ------------------------------------------------------------------

    def _with_director(self, action: str, *args) -> None:
        director = self._director
        if director is None:
            self._log("电台没在播，这个操作先跳过了")
            return

        def run() -> None:
            method = getattr(director, action)
            try:
                method(*args)
            except Exception as exc:
                self._log(f"⚠ {action} 失败：{exc}")
            self._emit_state()

        try:
            self._loop.call_soon_threadsafe(run)
        except RuntimeError:
            pass

    def skip(self) -> None:
        self._with_director("skip")

    def pause(self) -> None:
        self._with_director("pause")

    def resume(self) -> None:
        self._with_director("resume")

    def clear_queue(self) -> None:
        self._with_director("clear_queue")

    def say(self, text: str) -> None:
        if text.strip():
            self._with_director("say", text, "界面")

    def ask(self, text: str) -> None:
        if text.strip():
            self._with_director("ask", text, "界面")

    def switch_persona(self, key: str) -> None:
        self._with_director("switch_persona", key)

    # ------------------------------------------------------------------
    # 引擎 / 音色选择
    # ------------------------------------------------------------------

    def provider_kind(self) -> str:
        from ..config import provider_kind

        return provider_kind(self.cfg.tts.provider)

    def current_character_key(self) -> str:
        """当前选的角色音色 key（cfg.voice 形如 zenyatta:12）。"""
        raw = (self.cfg.tts.voice or "").strip()
        if not raw:
            return ""
        for sep in (":", "#", "/"):
            if sep in raw:
                head, tail = raw.rsplit(sep, 1)
                if tail.strip().isdigit():
                    return head.strip()
        return raw

    def current_character_sid(self) -> int:
        raw = (self.cfg.tts.voice or "").strip()
        for sep in (":", "#", "/"):
            if sep in raw:
                tail = raw.rsplit(sep, 1)[-1].strip()
                if tail.isdigit():
                    return int(tail)
        return 0

    def set_character(self, key: str, sid: int = 0) -> None:
        self.cfg.tts.provider = "vits"
        self.cfg.tts.voice = f"{key}:{int(sid)}"
        try:
            self.persist_config()
        except Exception:
            pass
        self.reload_engine()

    def install_character(self, key: str, on_done=None) -> None:
        self._submit(self._install_character(key), done=on_done)

    async def _install_character(self, key: str) -> str:
        from ..tts.characters import BY_KEY

        def status(msg: str) -> None:
            self.emit(EV_PROGRESS, {"message": msg, "done": 0, "total": 0})

        def progress(name: str, done: int, total: int) -> None:
            self.emit(EV_PROGRESS, {"message": f"下载 {name}", "done": done, "total": total})

        voice = BY_KEY.get(key)
        label = voice.name if voice else key
        try:
            await asyncio.to_thread(self.kit.install_character, key, progress, status)
            return f"角色音色「{label}」下载完成，804 个说话人编号可用"
        except Exception as exc:
            return f"下载失败：{exc}"

    def current_kokoro_name(self) -> str:
        from ..tts.kokoro import parse_voice

        voice = parse_voice(self.cfg.tts.voice)
        return voice.name if voice else ""

    def set_provider(self, kind: str) -> None:
        """切换语音引擎，同时把音色名换成该引擎认得的写法。"""
        if kind not in ("vits", "kokoro", "zipvoice"):
            kind = "vits"
        if kind == self.provider_kind():
            return
        self.cfg.tts.provider = kind
        if kind == "vits":
            from ..tts.characters import RECOMMENDED

            if not self.current_character_key():
                self.cfg.tts.voice = f"{RECOMMENDED[0]}:0"
        elif kind == "kokoro":
            from ..tts.kokoro import DEFAULT_SID, VOICE_BY_SID

            # 原来填的是克隆音色名，Kokoro 不认识，换成默认内置音色
            if not self.current_kokoro_name():
                self.cfg.tts.voice = VOICE_BY_SID[DEFAULT_SID].name
        else:
            profiles = self.kit.profiles()
            if profiles:
                self.cfg.tts.voice = profiles[0].key
        try:
            self.persist_config()
        except Exception:
            pass
        self.reload_engine()

    def set_voice(self, voice: str) -> None:
        self.cfg.tts.voice = voice
        try:
            self.persist_config()
        except Exception:
            pass
        self.reload_engine()

    # ------------------------------------------------------------------
    # 试听
    # ------------------------------------------------------------------

    def preview(self, text: str, voice_key: str, rate: str = "+0%",
                on_done: Callable[[Any], None] | None = None) -> None:
        """合成并播放一句话，用来试听音色。"""
        if self._busy.is_set():
            self._log("上一段试听还没放完")
            return
        self._busy.set()
        # 先让电台闭嘴，免得两个声音叠在一起
        director = self._director
        if director is not None:
            try:
                self._loop.call_soon_threadsafe(director.skip)
            except RuntimeError:
                pass
        self._submit(self._preview(text, voice_key, rate), done=on_done)

    async def _preview(self, text: str, voice_key: str, rate: str) -> str:
        """试听。

        优先复用正在跑的那个引擎 —— 新建一个会再加载一份模型
        （fp32 是 455MB），两个引擎还会抢 CPU，边说边试听就都卡了。
        """
        cfg = self.cfg
        voice = voice_key or cfg.tts.voice
        try:
            if self._director is not None:
                engine = self._director.engine
                params = engine.resolve_params(voice=voice, rate=rate or "+0%")
                started = time.monotonic()
                await engine.speak(text, params)
                return f"试听完成（{time.monotonic() - started:.2f} 秒）"
        except Exception as exc:
            log.warning("复用引擎试听失败，改用临时引擎：%s", exc)

        engine: SpeechEngine | None = None
        try:
            engine = SpeechEngine(cfg.tts, cfg.audio,
                                  cfg.log_dir / "tts_cache", self.kit)
            self._preview_engine = engine
            params = VoiceParams(voice=voice, rate=rate or "+0%")
            started = time.monotonic()
            await engine.speak(text, params)
            return f"试听完成（{time.monotonic() - started:.2f} 秒）"
        except Exception as exc:
            log.exception("试听失败")
            return f"试听失败：{exc}"
        finally:
            if engine is not None:
                await engine.close()
            self._preview_engine = None
            self._busy.clear()

    # ------------------------------------------------------------------
    # 引擎安装 + 音色管理
    # ------------------------------------------------------------------

    def install_engine(self, on_done=None, kind: str = "") -> None:
        """下载引擎资源。

        kind="kokoro"   只装 Kokoro 内置音色（约 140MB）
        kind="zipvoice" 装克隆引擎 + ZipVoice 模型 + 声码器（约 176MB）
        留空 = 按当前选的引擎来
        """
        self._submit(self._install_engine(kind or self.provider_kind()),
                     done=on_done)

    async def _install_engine(self, kind: str) -> str:
        def status(msg: str) -> None:
            self.emit(EV_PROGRESS, {"message": msg, "done": 0, "total": 0})

        def progress(name: str, done: int, total: int) -> None:
            self.emit(EV_PROGRESS, {"message": f"下载 {name}", "done": done, "total": total})

        try:
            if kind == "kokoro":
                await asyncio.to_thread(self.kit.install_kokoro, progress, status)
                return "内置音色（Kokoro）安装完成"
            await asyncio.to_thread(self.kit.install, progress, status)
            return "本地克隆引擎安装完成"
        except Exception as exc:
            return f"安装失败：{exc}"

    def create_profile(self, key: str, wav_bytes: bytes, transcript: str,
                       name: str = "", note: str = "", semitones: float = 0.0,
                       tone: float = 0.0, denoise: float = 0.0,
                       on_done=None) -> None:
        self._submit(self._create_profile(key, wav_bytes, transcript, name, note,
                                          semitones, tone, denoise),
                     done=on_done)

    async def _create_profile(self, key: str, wav_bytes: bytes, transcript: str,
                              name: str, note: str, semitones: float,
                              tone: float, denoise: float) -> str:
        try:
            profile = await asyncio.to_thread(
                self.kit.create, key, wav_bytes, transcript, name, note,
                semitones, tone, denoise)
            self._reload_engine_voice()
            return f"音色「{profile.display}」已创建（{profile.describe()}）"
        except Exception as exc:
            return f"创建失败：{exc}"

    def update_profile(self, key: str, *, name: str = "", note: str = "",
                       transcript: str = "", semitones: float = 0.0,
                       tone: float = 0.0, denoise: float = 0.0,
                       on_done=None) -> None:
        self._submit(self._update_profile(key, name, note, transcript,
                                          semitones, tone, denoise), done=on_done)

    async def _update_profile(self, key: str, name: str, note: str,
                              transcript: str, semitones: float,
                              tone: float, denoise: float) -> str:
        try:
            profile = await asyncio.to_thread(
                self.kit.update, key, name=name, note=note, transcript=transcript)
            if profile is None:
                return "找不到这个音色"
            # 音色微调要重新渲染样本
            profile = await asyncio.to_thread(
                self.kit.retune, key, semitones, tone, denoise) or profile
            self._reload_engine_voice()
            return f"音色「{profile.display}」已保存（{profile.describe()}）"
        except Exception as exc:
            return f"保存失败：{exc}"

    def delete_profile(self, key: str) -> str:
        try:
            if self.kit.delete(key):
                self._reload_engine_voice()
                return f"音色「{key}」已删除"
            return "找不到这个音色"
        except Exception as exc:
            return f"删除失败：{exc}"

    def _reload_engine_voice(self) -> None:
        """音色变了，让正在跑的引擎也换过来。"""
        engine = getattr(self, "_preview_engine", None)
        if engine is not None:
            try:
                engine.reload_voice()
            except Exception:
                pass
        if self._director is not None:
            try:
                self._loop.call_soon_threadsafe(self._director.engine.reload_voice)
            except Exception:
                pass

    def set_quality(self, quality: str) -> None:
        """切换模型精度（fp32 / int8），写进配置并重建 VoiceKit。"""
        if quality not in ("fp32", "int8"):
            return
        self.cfg.tts.model_quality = quality
        try:
            self.persist_config()
        except Exception:
            pass
        self.kit = VoiceKit(self.cfg.sherpa_dir, self.cfg.model_dir,
                            self.cfg.voices_dir, quality)

    def reload_engine(self) -> None:
        """丢掉正在跑的引擎，让它按新配置重建（下次说话时生效）。"""
        engine = getattr(self, "_preview_engine", None)
        if engine is not None:
            try:
                self._submit(engine.close())
            except Exception:
                pass
            self._preview_engine = None
        if self._director is not None:
            try:
                self._loop.call_soon_threadsafe(self._rebuild_engine)
            except Exception:
                pass

    def _rebuild_engine(self) -> None:
        try:
            self._director.engine.rebuild()
        except Exception:
            pass

    # ------------------------------------------------------------------

    def shutdown(self, timeout: float = 4.0) -> None:
        """退出前的收尾：停播、取消所有后台任务、关掉事件循环。"""
        try:
            if self.running:
                self._submit(self._stop_radio())
                deadline = time.monotonic() + 1.5
                while self.running and time.monotonic() < deadline:
                    time.sleep(0.05)
        except Exception:
            pass

        async def _cancel_pending() -> None:
            current = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not current]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            if self._loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_cancel_pending(), self._loop)
                future.result(timeout=timeout)
        except Exception:
            pass
        finally:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
