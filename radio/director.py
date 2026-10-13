"""电台调度中枢。

整个程序的心跳就在这里，负责：
  * 判断哪条弹幕值得让 AI 开口（@ 触发 / 醒目留言 / 礼物）
  * 没人说话时，按节奏让 AI 自己念稿子
  * 控制冷却时间、排队、打断
  * 把提示词喂给大模型，再把结果交给语音引擎念出来
  * 响应控制指令（切人设、跳过、暂停、指定发言）
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .chat.base import (
    KIND_DANMAKU,
    KIND_GIFT,
    KIND_SUPER_CHAT,
    ChatMessage,
)
from .config import Config
from .llm import LLMClient, LLMError, Message
from .memory import ConversationMemory, ScriptLibrary
from .persona import PersonaRegistry, build_system_prompt, clean_speech
from .tts import SpeechEngine

log = logging.getLogger("radio.director")

EventHook = Callable[[str, dict[str, Any]], Awaitable[None] | None]


# ---------------------------------------------------------------------------
# 任务定义
# ---------------------------------------------------------------------------


@dataclass
class Job:
    """一次待处理的开麦任务。"""

    kind: str                      # reply | idle | say | ask
    prompt: str = ""               # 给模型看的
    say_text: str = ""             # 直接念的原文（kind=say）
    source: str = ""               # 日志用：谁触发的
    message: ChatMessage | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class DirectorState:
    persona: str = ""
    voice: str = ""
    speaking: bool = False
    paused: bool = False
    pending: int = 0
    spoken_count: int = 0
    last_text: str = ""
    game: str = ""
    tts_provider: str = ""
    audio_backend: str = ""
    idle_round: int = 0
    error_streak: int = 0
    last_error: str = ""
    # 「正在准备」——大模型想完了、语音还在合成的那一段。
    # 这段时间最长，界面上必须显示出来，否则会被当成卡死。
    preparing: bool = False
    preparing_note: str = ""
    estimate: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona,
            "voice": self.voice,
            "speaking": self.speaking,
            "paused": self.paused,
            "pending": self.pending,
            "spoken_count": self.spoken_count,
            "last_text": self.last_text,
            "game": self.game,
            "tts_provider": self.tts_provider,
            "audio_backend": self.audio_backend,
            "idle_round": self.idle_round,
            "error_streak": self.error_streak,
            "last_error": self.last_error,
            "preparing": self.preparing,
            "preparing_note": self.preparing_note,
            "estimate": self.estimate,
        }


# ---------------------------------------------------------------------------


class Director:
    """把弹幕、模型、语音串起来的那根线。"""

    def __init__(
        self,
        cfg: Config,
        personas: PersonaRegistry,
        memory: ConversationMemory,
        scripts: ScriptLibrary,
        engine: SpeechEngine,
        llm: LLMClient,
        on_event: EventHook | None = None,
    ) -> None:
        self.cfg = cfg
        self.personas = personas
        self.memory = memory
        self.scripts = scripts
        self.engine = engine
        self.llm = llm
        self.on_event = on_event

        self.state = DirectorState(
            persona=personas.current.display_name if personas.current else "",
            voice=cfg.tts.voice,
            game=cfg.app.game,
            tts_provider=engine.provider_name,
            audio_backend=getattr(engine.player, "backend", "?"),
        )

        self._pending: deque[Job] = deque()
        self._wake = asyncio.Event()
        self._stopping = False
        self._last_activity = time.monotonic()
        self._last_spoke_at = 0.0
        # 稿件两阶段提交：peek 之后先记着，真念出来了才推进游标
        self._pending_script_commit = False
        self._user_cooldown: dict[str, float] = {}
        self._current_job: Job | None = None
        self._speaking_task: asyncio.Task | None = None
        self._chat_task: asyncio.Task | None = None
        self._source = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def run(self, chat_source) -> None:
        """主循环：并发跑"收弹幕"和"开麦"两条流水线。"""
        self._source = chat_source
        self._chat_task = asyncio.create_task(self._chat_loop(chat_source))
        try:
            await self._speech_loop()
        finally:
            self._stopping = True
            for task in (self._chat_task, self._speaking_task):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(
                *[t for t in (self._chat_task, self._speaking_task) if t],
                return_exceptions=True,
            )
            close = getattr(chat_source, "close", None)
            if close:
                await close()

    async def stop(self) -> None:
        self._stopping = True
        self.engine.interrupt()
        self._wake.set()

    # ------------------------------------------------------------------
    # 收弹幕
    # ------------------------------------------------------------------

    async def _chat_loop(self, chat_source) -> None:
        try:
            async for message in chat_source.messages():
                if self._stopping:
                    break
                try:
                    await self._handle_message(message)
                except Exception as exc:  # 单条消息出问题不能拖垮电台
                    log.exception("处理弹幕出错: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("弹幕接收中断: %s", exc)
            await self._emit("error", {"message": f"弹幕接收中断：{exc}"})

    async def _handle_message(self, message: ChatMessage) -> None:
        self._emit_sync("chat", {
            "kind": message.kind,
            "username": message.username,
            "text": message.text,
        })

        # 指令优先
        if self._maybe_command(message):
            return

        # 只有"冲着 AI 来的"消息才重置空闲计时
        quiet = self.cfg.idle.only_mentions_reset
        targeted = self._is_targeted(message)
        if not quiet or targeted or message.is_paid:
            self._last_activity = time.monotonic()

        if self.state.paused:
            return
        if not targeted:
            return

        job = Job(kind="reply", message=message,
                  source=f"{message.username or '观众'}:{message.text[:20]}")
        # 冷却在入队时就判定：被冷却掉的弹幕不该进队列，
        # 也不该打"收到点名"的日志（否则日志会骗人）
        if not self._accept(job):
            return
        await self._emit("trigger", {
            "username": message.username,
            "text": message.text,
            "kind": message.kind,
        })

    # ------------------------------------------------------------------
    # 触发判定
    # ------------------------------------------------------------------

    def _keywords(self) -> list[str]:
        words = list(self.cfg.trigger.mention_keywords or [])
        if self.personas.current:
            words += self.personas.current.keywords()
        return [w for w in dict.fromkeys(w.strip() for w in words if w and w.strip())]

    def _keyword_hit(self, text: str) -> str | None:
        lowered = text.lower()
        for word in self._keywords():
            if word.lower() in lowered:
                return word
        return None

    def _is_targeted(self, message: ChatMessage) -> bool:
        if message.kind == KIND_SUPER_CHAT:
            return bool(self.cfg.trigger.super_chat_always)
        if message.kind == KIND_GIFT:
            threshold = float(self.cfg.trigger.gift_threshold or 0)
            if threshold <= 0:
                return False
            return message.price >= threshold
        if message.kind != KIND_DANMAKU:
            return False

        text = (message.text or "").strip()
        if len(text) < int(self.cfg.trigger.min_text_length or 1):
            return False
        if len(text) > int(self.cfg.trigger.max_text_length or 120):
            return False
        for word in self.cfg.trigger.block_words or []:
            if word and word in text:
                log.debug("弹幕命中屏蔽词 %r，已忽略", word)
                return False

        if not self.cfg.trigger.require_mention:
            return True
        return self._keyword_hit(text) is not None

    # ------------------------------------------------------------------
    # 队列
    # ------------------------------------------------------------------

    def _enqueue(self, job: Job) -> bool:
        """入队。返回 False 表示队列已满、这条被丢了。"""
        limit = max(1, int(self.cfg.trigger.queue_max or 6))
        dropped_any = False
        while len(self._pending) >= limit:
            dropped = self._pending.popleft()
            log.debug("队列已满，丢弃 %s", dropped.source)
            dropped_any = True
            if job.message is not None and not job.message.is_paid:
                # 宁可丢旧的，也要给新来的观众一个回应
                pass
        # 付费消息插队
        if job.message is not None and job.message.is_paid:
            self._pending.appendleft(job)
        else:
            self._pending.append(job)
        self.state.pending = len(self._pending)
        self._wake.set()
        return True

    def _accept(self, job: Job) -> bool:
        """收下任务：先过冷却，入队成功之后才占用该用户的冷却窗口。

        顺序很重要 —— 以前是先占冷却再入队，队列满时任务被丢掉，
        但用户的冷却已经消耗了：观众 @ 了 AI，要白等 45 秒，界面上还没提示。
        """
        if not self._passes_cooldown(job):
            return False
        message = job.message
        cooldown = float(self.cfg.trigger.same_user_cooldown or 0)
        accepted = self._enqueue(job)
        if accepted and message is not None and not message.is_paid and cooldown > 0:
            key = str(message.uid or message.username or "?")
            self._user_cooldown[key] = time.monotonic()
        return accepted

    async def _next_job(self, timeout: float) -> Job | None:
        """等一个任务，最多等 timeout 秒。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._pending:
                job = self._pending.popleft()
                self.state.pending = len(self._pending)
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    def _passes_cooldown(self, job: Job) -> bool:
        message = job.message
        if message is None or message.is_paid:
            return True
        key = str(message.uid or message.username or "?")
        cooldown = float(self.cfg.trigger.same_user_cooldown or 0)
        if cooldown <= 0:
            return True
        last = self._user_cooldown.get(key, 0.0)
        if time.monotonic() - last < cooldown:
            log.debug("用户 %s 还在冷却中，跳过", key)
            return False
        return True

    # ------------------------------------------------------------------
    # 开麦主循环
    # ------------------------------------------------------------------

    async def _speech_loop(self) -> None:
        while not self._stopping:
            # 暂停时什么都不说：清空积压，静静等着被 resume
            if self.state.paused:
                if self._pending:
                    self._pending.clear()
                    self.state.pending = 0
                self._last_activity = time.monotonic()
                await asyncio.sleep(0.5)
                continue

            gap = self._next_idle_gap()
            elapsed = time.monotonic() - self._last_activity
            wait = max(0.5, gap - elapsed)

            job = await self._next_job(wait)
            if self._stopping:
                return

            if job is None:
                if not self.cfg.idle.enabled:
                    self._last_activity = time.monotonic()
                    continue
                job = Job(kind="idle", source="空闲念稿")

            # 两次开口之间留一点呼吸
            minimum = float(self.cfg.trigger.min_interval or 0)
            since = time.monotonic() - self._last_spoke_at
            if since < minimum:
                await asyncio.sleep(minimum - since)

            spoke = await self._perform(job)

            now = time.monotonic()
            if spoke:
                self._last_spoke_at = now
                self._last_activity = now
                # 稿子真的念出去了，这时候才推进游标
                if self._pending_script_commit:
                    self.scripts.commit_segment()
            else:
                # 什么都没说出来（LLM 失败 / 空文本 / 被跳过）。
                # 旧代码在这里也无条件重置计时器，于是每次都白等一整个
                # 12~22 秒空闲间隔 —— 连续失败时就成了"莫名停顿"。
                # 现在只推后一点点，让重试来得快一些。
                self._last_activity = now - max(0.0, gap - 1.5)
            self._pending_script_commit = False

    def _next_idle_gap(self) -> float:
        low = max(1.0, float(self.cfg.idle.min_gap or 14))
        high = max(low, float(self.cfg.idle.max_gap or 30))
        return random.uniform(low, high)

    # ------------------------------------------------------------------

    async def _perform(self, job: Job) -> bool:
        """执行一次开麦。返回 True 表示确实说出去了话。"""
        self._current_job = job
        spoke = False
        try:
            text = await self._produce_text(job)
            if not text:
                return False
            self.state.error_streak = 0
            self.state.last_error = ""
            self.state.last_text = text
            await self._emit("speak", {"kind": job.kind, "text": text})

            self.state.speaking = True
            persona = self.personas.current
            params = self.engine.resolve_params(
                voice=persona.voice if persona else "",
                rate=persona.rate if persona else "",
                pitch=persona.pitch if persona else "",
                volume=persona.volume if persona else "",
            )
            self.state.voice = params.voice

            # 从「大模型想完」到「第一个字出声」之间有一大段静默：
            # 参考音频越长、文本越长，这段越久。必须告诉用户还要等多久，
            # 不然会被当成卡死（"莫名停顿相当久"就是这么来的）。
            estimate = self._estimate_speech_seconds(text, params.voice)
            self.state.preparing = True
            self.state.estimate = estimate
            if estimate >= 3:
                self.state.preparing_note = f"正在合成语音…大约还要 {estimate:.0f} 秒出声"
            else:
                self.state.preparing_note = "正在合成语音…"
            await self._emit("preparing", {"seconds": estimate,
                                           "chars": len(text)})

            started = {"done": False}

            async def on_chunk(_chunk: str) -> None:
                if not started["done"]:
                    started["done"] = True
                    self.state.preparing = False
                    self.state.preparing_note = ""
                    await self._emit("started", {"seconds": estimate})

            completed = await self.engine.speak(text, params, on_chunk=on_chunk)
            self.state.preparing = False
            self.state.preparing_note = ""
            spoke = True
            if not completed:
                await self._emit("interrupted", {})

            if job.kind != "say":
                self.memory.add_assistant(text)
            if job.message is not None:
                key = str(job.message.uid or job.message.username or "?")
                self._user_cooldown[key] = time.monotonic()
        except asyncio.CancelledError:
            raise
        except LLMError as exc:
            self._note_failure(exc)
        except Exception as exc:
            log.exception("开麦失败: %s", exc)
            await self._emit("error", {"message": f"开麦失败：{exc}"})
        finally:
            self.state.speaking = False
            self._current_job = None
        if spoke:
            self.state.spoken_count += 1
        return spoke

    def _estimate_speech_seconds(self, text: str, voice_key: str = "") -> float:
        """估算「从现在到听见第一个字」还要多久。

        参数是从实测数据拟合出来的，不是拍脑袋：

          总合成耗时 ≈ 0.67 × 参考音频秒数  +  RTF × 音频秒数
            · 0.67 是固定开销的斜率（每合成一次都要重新处理一遍参考音频）
            · RTF 取决于 flow-matching 步数：4 步 ≈ 0.98，6 步 ≈ 1.40，8 步 ≈ 1.85
              （8 步比实时慢将近一倍，所以长稿子要等很久）
            · 中文口播大约每秒 5 个字

          流式播放下第一块音频大约在总耗时的 63% 处出来；
          但文本短的时候引擎只回调一次，那时就得等满全程。
        """
        try:
            profile = self.engine.kit.get(voice_key) if voice_key else None
            if profile is None:
                profiles = self.engine.kit.profiles()
                profile = profiles[0] if profiles else None
            ref_seconds = float(profile.duration) if profile else 4.0
        except Exception:
            ref_seconds = 4.0

        steps = int(getattr(self.engine.tts_cfg, "num_steps", 8) or 8)
        rtf = {4: 0.98, 5: 1.19, 6: 1.40, 7: 1.62}.get(steps, 1.85 if steps <= 8 else 2.4)

        fixed = 0.67 * max(2.0, ref_seconds)
        audio_seconds = len(text) / 5.0
        total = fixed + rtf * audio_seconds

        # 文本够长才有多次回调，才能提前出声；短的就得等满
        ratio = 0.63 if audio_seconds > 11.0 else 1.0
        return max(1.5, ratio * total)

    def _note_failure(self, exc: Exception) -> None:
        """模型连续失败时别刷屏，直接停下并告诉你出了什么事。"""
        self.state.error_streak += 1
        first_line = str(exc).strip().splitlines()[0][:160]
        self.state.last_error = first_line
        # 配置类错误（key 不对、没余额）不需要堆栈，说明白就行
        log.error("模型调用失败（连续第 %s 次）：%s", self.state.error_streak, first_line)

        if self.state.error_streak >= 3 and not self.state.paused:
            self.state.paused = True
            log.error(
                "连续 %s 次调用模型失败，已自动暂停说话，避免刷屏。\n"
                "  请先运行  python run.py check  确认问题，改好之后：\n"
                "    - 在控制台点「继续」，或\n"
                "    - 重启程序",
                self.state.error_streak,
            )

    async def _produce_text(self, job: Job) -> str:
        if job.kind == "say":
            return clean_speech(job.say_text)
        prompt = job.prompt or self._build_prompt(job)
        if not prompt:
            return ""

        persona = self.personas.current
        system = build_system_prompt(
            persona,
            host_name=self.cfg.app.host_name or "主播",
            game=self.cfg.app.game,
            max_chars=120,
        ) if persona else "你是一位电台主持人。"

        messages = [Message("system", system)]
        messages += self.memory.messages()
        messages.append(Message("user", prompt))

        temperature = persona.temperature if (persona and persona.temperature is not None) else None
        text = await self.llm.generate(messages, temperature=temperature)
        return clean_speech(text)

    # ------------------------------------------------------------------
    # 提示词
    # ------------------------------------------------------------------

    def _build_prompt(self, job: Job) -> str:
        if job.kind == "idle":
            return self._idle_prompt()
        if job.message is not None:
            return self._reply_prompt(job.message)
        return ""

    def _reply_prompt(self, message: ChatMessage) -> str:
        who = message.username or "观众"
        bits = []
        if message.medal:
            bits.append(f"粉丝牌「{message.medal}」{message.medal_level} 级")
        if message.is_admin:
            bits.append("房管")
        tag = f"（{'，'.join(bits)}）" if bits else ""

        if message.kind == KIND_SUPER_CHAT:
            head = f"观众「{who}」{tag} 发了一条醒目留言（{message.price:.0f} 元）："
            ask = "这是付费留言，请认真、热情地回应他。"
        elif message.kind == KIND_GIFT:
            head = f"观众「{who}」{tag} 送了礼物：{message.text}。"
            ask = "请自然地道个谢，别太夸张。"
        else:
            head = f"观众「{who}」{tag} 在弹幕里跟你说话："
            ask = "请回应这条弹幕。"

        return (
            f"{head}\n「{message.text}」\n\n"
            f"{ask}直接开口说话即可，不要复述观众的原文。"
        )

    def _idle_prompt(self) -> str:
        # 只看一眼，不推进游标。等这段真的念出来了，_speech_loop 里再提交。
        # 否则中途 LLM 失败，这段稿子就被白白吃掉了（念都没念过）。
        segment = self.scripts.peek_segment()
        self._pending_script_commit = segment is not None
        self.state.idle_round += 1

        # 字数上限：稿子越长，等你听到声音的静默越久。
        # 8 步 + 8 秒样本时，150 字要合成将近 1 分钟 —— 那就成了"莫名停顿"。
        limit = int(getattr(self.cfg.idle, "max_chars", 80) or 80)
        limit = max(20, min(200, limit))

        if segment is None:
            return (
                "直播间现在很安静，没有人说话。\n"
                f"主播正在玩《{self.cfg.app.game}》。\n\n"
                "请像电台主持人串场一样，自己找一个轻松的话题说一段，"
                "可以聊聊游戏、聊聊心情、聊聊这个时间的氛围。\n"
                f"**控制在 {limit} 字以内**，自然一点，不要写成小作文。"
            )

        source, text = segment
        text = self._shorten_for_speech(text, limit)
        ratio = max(0.0, min(1.0, float(self.cfg.idle.improvise_ratio or 0)))
        if not self.cfg.idle.improvise or ratio <= 0.01:
            style = "请把下面的稿子自然地念出来，内容不要改动。"
        elif ratio >= 0.99:
            style = "下面是一段素材，请用你自己的话重新讲一遍，讲成一段自然的电台口播。"
        else:
            style = "下面是一段稿子素材，请用你自己的语气讲出来，可以适当展开和润色，但不要跑题。"

        log.info("念稿：%s · %s", source, text[:30])
        return (
            "直播间现在很安静。以下是节目素材：\n"
            f"「{text}」\n\n"
            f"{style}直接开口说，不要提到「稿子」「素材」这几个字。\n"
            f"**说完就停，总共控制在 {limit} 字以内** —— 说太长听众要等很久才能听到声音。"
        )

    @staticmethod
    def _shorten_for_speech(text: str, limit: int) -> str:
        """把过长的稿件裁到 limit 字以内，尽量在句子边界断开。"""
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        head = text[:limit]
        for mark in ("。", "！", "？", "；", "…", ".", "!", "?", "\n"):
            cut = head.rfind(mark)
            if cut >= limit * 0.5:
                return head[: cut + 1]
        return head

    # ------------------------------------------------------------------
    # 控制接口
    # ------------------------------------------------------------------

    def say(self, text: str, source: str = "手动") -> None:
        """直接念一段指定的话（不过大模型）。"""
        self._enqueue(Job(kind="say", say_text=text, source=source))

    def ask(self, prompt: str, source: str = "手动提问") -> None:
        """把一句话作为问题交给大模型，然后念出来。"""
        self._enqueue(Job(kind="reply", prompt=prompt, source=source))

    def skip(self) -> bool:
        """跳过当前这段。"""
        self.engine.interrupt()
        return True

    def clear_queue(self) -> int:
        count = len(self._pending)
        self._pending.clear()
        self.state.pending = 0
        return count

    def pause(self) -> None:
        self.state.paused = True
        self.engine.interrupt()

    def resume(self) -> None:
        self.state.paused = False
        self.state.error_streak = 0
        self._last_activity = time.monotonic()
        self._wake.set()

    def switch_persona(self, key: str) -> bool:
        if not self.personas.switch(key):
            return False
        persona = self.personas.current
        self.state.persona = persona.display_name
        self.state.voice = persona.voice or self.cfg.tts.voice
        log.info("已切换人设 -> %s", persona.display_name)
        self._emit_sync("persona", {"name": persona.display_name, "key": persona.key})
        return True

    def reload_personas(self) -> int:
        self.personas.reload()
        if self.personas.current:
            self.state.persona = self.personas.current.display_name
        return len(self.personas.keys())

    def reload_scripts(self) -> int:
        self.scripts.reload()
        return self.scripts.total

    # ------------------------------------------------------------------
    # 聊天里的控制指令
    # ------------------------------------------------------------------

    def _maybe_command(self, message: ChatMessage) -> bool:
        cfg = self.cfg.control
        if not cfg.allow_chat_commands or message.kind != KIND_DANMAKU:
            return False
        text = (message.text or "").strip()
        if not text:
            return False

        if cfg.admin_only and not message.is_admin:
            return False

        prefix = (cfg.persona_command or "!人设").strip()
        if prefix and text.startswith(prefix):
            wanted = text[len(prefix):].strip()
            if not wanted:
                names = "、".join(self.personas.keys())
                self.say(f"现在可选的人设有：{names}")
                return True
            if self.switch_persona(wanted):
                self.say(f"好，我是{self.personas.current.display_name}，接下来由我来陪你。")
            else:
                self.say(f"没找到「{wanted}」这个人设。")
            return True

        skip_prefix = (cfg.skip_command or "!下一条").strip()
        if skip_prefix and text.startswith(skip_prefix):
            self.skip()
            self.clear_queue()
            return True

        return False

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            result = self.on_event(event, payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    def _emit_sync(self, event: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            result = self.on_event(event, payload)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:
            pass
