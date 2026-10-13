"""端到端集成自测。

不联网、不出声，用假的 LLM 和脚本化的弹幕，把整条链路跑一遍：
    弹幕收进来 -> 触发判定 -> 组装提示词 -> "调用模型" -> 清洗文本
    -> 交给语音引擎 -> 念出来（无声模式）-> 更新记忆/冷却
同时验证本地控制台的 HTTP 接口。

    python tools/selftest_pipeline.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radio.chat.console import ScriptedChatSource            # noqa: E402
from radio.config import Config                              # noqa: E402
from radio.control import ControlServer                      # noqa: E402
from radio.director import Director                          # noqa: E402
from radio.llm import LLMClient, Message                     # noqa: E402
from radio.memory import ConversationMemory, ScriptLibrary   # noqa: E402
from radio.voices import VoiceKit                            # noqa: E402
from radio.persona import PersonaRegistry, clean_speech, split_for_tts  # noqa: E402
from radio.tts import SpeechEngine                           # noqa: E402

PASS, FAIL = "  ✓", "  ✗"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        failures.append(name)


class FakeLLM(LLMClient):
    """不出网的假模型：把收到的提示词加工一下还回去。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls: list[list[Message]] = []
        self.system_prompt = ""
        self.delay = 0.05

    async def generate(self, messages, *, temperature=None, max_tokens=None, on_delta=None):
        self.calls.append(list(messages))
        if not self.system_prompt and messages and messages[0].role == "system":
            self.system_prompt = messages[0].content
        await asyncio.sleep(self.delay)
        user = messages[-1].content if messages else ""
        if "节目素材" in user:
            text = "（假装念稿）嗯，说起来啊，凌晨的便利店其实挺有意思的。"
        elif "醒目留言" in user:
            text = "（假装回SC）谢谢你的醒目留言，我看到了，真的很感谢。"
        else:
            snippet = user.split("「")[-1].split("」")[0][:12] if "「" in user else user[:12]
            text = f"（假装回话）你刚才说{snippet}，我听到了。"
        if on_delta:
            r = on_delta(text)
            if asyncio.iscoroutine(r):
                await r
        return text


def build_cfg() -> Config:
    cfg = Config()
    cfg.path = ROOT / "config.toml"
    cfg.app.persona = "深夜电台"
    cfg.app.game = "艾尔登法环"
    cfg.app.host_name = "老王"
    cfg.llm.api_key = "fake-key-for-selftest"
    cfg.llm.stream = True
    cfg.tts.provider = "none"
    cfg.audio.volume = 100
    cfg.trigger.require_mention = True
    cfg.trigger.min_interval = 0.4
    cfg.trigger.same_user_cooldown = 30.0
    cfg.trigger.queue_max = 4
    cfg.trigger.block_words = ["加群"]
    cfg.idle.enabled = True
    cfg.idle.min_gap = 2.0
    cfg.idle.max_gap = 3.0
    cfg.control.enabled = True
    cfg.control.port = 8799
    return cfg


async def http_get(path: str) -> dict:
    def _do():
        with urllib.request.urlopen(f"http://127.0.0.1:8799{path}", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    return await asyncio.to_thread(_do)


async def main() -> int:
    print("=" * 62)
    print(" AI 电台 · 端到端集成自测")
    print("=" * 62)

    # ---------------------------------------------------------------- 纯函数
    print("\n[1] 文本处理")
    raw = "**小夜**：（笑）你好呀~ [链接](http://x.com) 🎵\n这是第二行"
    cleaned = clean_speech(raw)
    check("Markdown/括号/emoji 被清掉", "**" not in cleaned and "（笑）" not in cleaned
          and "🎵" not in cleaned and "http" not in cleaned, repr(cleaned))

    long_text = "第一句话在这里。第二句稍微长一点点，用来测试切分逻辑是否正常工作。第三句收尾！"
    chunks = split_for_tts(long_text, max_chars=25)
    check("长文本能切句", len(chunks) >= 2 and "".join(chunks).replace(" ", "") ==
          long_text.replace(" ", ""), f"{len(chunks)} 段")

    # ---------------------------------------------------------------- 人设
    print("\n[2] 人设系统")
    registry = PersonaRegistry(ROOT / "personas", "深夜电台")
    check("载入默认人设", registry.current is not None and registry.current.display_name == "小夜",
          registry.current.display_name if registry.current else "None")
    check("能列出全部人设", len(registry.keys()) >= 3, "、".join(registry.keys()))
    check("别名可用于触发", "夜夜" in registry.current.keywords(),
          str(registry.current.keywords()))
    check("能切到另一个人设", registry.switch("热血解说") and
          registry.current.display_name == "阿凯")
    registry.switch("深夜电台")

    # ---------------------------------------------------------------- 稿件
    print("\n[3] 稿件库")
    scripts = ScriptLibrary(ROOT / "scripts")
    check("稿件载入", scripts.total > 10, f"{scripts.total} 段")
    first = scripts.next_segment()
    seen = {first[1]} if first else set()
    for _ in range(min(scripts.total - 1, 200)):
        seg = scripts.next_segment()
        if seg:
            seen.add(seg[1])
    check("轮播不重复（一轮内）", len(seen) == scripts.total, f"{len(seen)}/{scripts.total}")

    # ---------------------------------------------------------------- 主流程
    print("\n[4] 调度中枢（约 16 秒）")
    cfg = build_cfg()
    personas = PersonaRegistry(cfg.persona_dir, cfg.app.persona)
    mem = ConversationMemory(cfg.llm.history_size)
    engine = SpeechEngine(cfg.tts, cfg.audio, cfg.log_dir / "tts_cache",
                 VoiceKit(cfg.sherpa_dir, cfg.model_dir, cfg.voices_dir,
                 cfg.tts.model_quality))
    llm = FakeLLM(cfg.llm)
    director = Director(cfg, personas, mem, scripts, engine, llm)

    events: list[tuple[str, dict]] = []

    def on_event(name, payload):
        events.append((name, payload))

    director.on_event = on_event

    chat = ScriptedChatSource([
        (0.5, "@小夜 晚上好呀", "阿强"),
        (1.0, "今天天气不错", "路人甲"),          # 没点名 -> 不该触发
        (1.6, "@小夜 你在干嘛呢", "小美"),
        (2.2, "大家加群领福利", "广告号"),        # 命中屏蔽词 -> 不该触发
        (2.8, "@小夜 再问一次", "阿强"),          # 同一用户冷却中 -> 跳过
        (3.4, "@小夜 换个话题吧", "老王头"),
    ], username="测试观众")

    loop = asyncio.get_running_loop()
    control = ControlServer(cfg.control, director, loop)
    started = control.start()

    runner = asyncio.create_task(director.run(chat))
    await asyncio.sleep(6.0)

    # ---- 控制台接口 ----
    print("\n[5] 本地控制台 HTTP 接口")
    check("控制台启动", started)
    if started:
        try:
            state = await http_get("/api/state")
            check("/api/state 可用", state.get("ok") and "persona" in state,
                  f"人设={state.get('persona')} 已开口={state.get('spoken_count')}")

            ps = await http_get("/api/personas")
            check("/api/personas 可用", len(ps.get("personas", [])) >= 3)

            sw = await http_get("/api/persona?key=%E7%83%AD%E8%A1%80%E8%A7%A3%E8%AF%B4")
            await asyncio.sleep(0.3)
            check("控制台能切人设", sw.get("ok") and
                  director.state.persona == "阿凯", director.state.persona)

            await http_get("/api/persona?key=%E6%B7%B1%E5%A4%9C%E7%94%B5%E5%8F%B0")
            await asyncio.sleep(0.3)

            r = await http_get("/api/say?text=" + urllib.parse.quote("这是控制台插播的一句话"))
            await asyncio.sleep(0.5)
            check("/api/say 能插播", r.get("ok"))

            await http_get("/api/skip")
            check("/api/skip 可用", True)
        except Exception as exc:
            check("控制台接口", False, str(exc))

    await asyncio.sleep(10.0)
    await director.stop()
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    control.stop()
    await engine.close()

    # ---------------------------------------------------------------- 断言
    print("\n[6] 行为断言")
    replies = [p for e, p in events if e == "speak" and p.get("kind") == "reply"]
    idles = [p for e, p in events if e == "speak" and p.get("kind") == "idle"]
    says = [p for e, p in events if e == "speak" and p.get("kind") == "say"]
    triggers = [p for e, p in events if e == "trigger"]

    check("被点名的弹幕触发了回复", len(triggers) >= 3, f"{len(triggers)} 条触发")
    check("生成了回复内容", len(replies) >= 3, f"{len(replies)} 条回复")
    check("安静时自动念稿", len(idles) >= 1, f"{len(idles)} 条念稿")
    check("控制台插播被执行", len(says) >= 1)

    all_spoken = " ".join(p.get("text", "") for e, p in events if e == "speak")
    check("屏蔽词弹幕没有触发", "加群" not in all_spoken)
    check("未点名弹幕没有触发", "今天天气不错" not in all_spoken)
    # 阿强连发两条，第二条应该被冷却挡在队列之外（连触发日志都不该有）
    repeated = [t for t in triggers if t.get("text", "").startswith("@小夜 再问")]
    check("同一用户冷却生效（第二条不进队列）", len(repeated) == 0,
          f"收到 {len(repeated)} 条")

    print("\n[7] 提示词组装")
    check("系统提示词包含人设正文", "小夜" in llm.system_prompt and "深夜" in llm.system_prompt,
          f"{len(llm.system_prompt)} 字")
    check("系统提示词包含主播和游戏", "老王" in llm.system_prompt and "艾尔登法环" in llm.system_prompt)
    check("系统提示词包含输出约束", "不要用 Markdown" in llm.system_prompt)

    reply_msgs = [c for c in llm.calls if "在弹幕里跟你说话" in c[-1].content]
    check("回复提示词带上了观众昵称",
          any(any(who in m[-1].content for who in ("阿强", "小美", "老王头"))
              for m in reply_msgs),
          f"{len(reply_msgs)} 条回复提示词")
    idle_msgs = [c for c in llm.calls if "节目素材" in c[-1].content]
    check("念稿提示词带上了稿件原文", len(idle_msgs) >= 1)

    print("\n[8] 记忆")
    check("记忆里存了对话", len(mem) >= 2, f"{len(mem)} 轮")

    await test_failure_handling()

    print("\n" + "=" * 62)
    if failures:
        print(f"自测完成：{len(failures)} 项失败")
        for item in failures:
            print(f"  ✗ {item}")
        return 1
    print(f"自测全部通过 ✓   事件 {len(events)} 条，模型调用 {len(llm.calls)} 次")
    return 0


async def test_failure_handling() -> None:
    """模型一直报错时，应该报清楚、并且自动停下来别刷屏。"""
    print("\n[9] 模型连续失败的处理")

    class AlwaysFailLLM(LLMClient):
        async def generate(self, messages, *, temperature=None, max_tokens=None, on_delta=None):
            from radio.llm import LLMError
            raise LLMError("模型接口返回 HTTP 401（API Key 不对或已失效）")

    cfg = build_cfg()
    cfg.control.enabled = False
    cfg.trigger.same_user_cooldown = 0.0     # 关掉冷却，好让它连续失败
    cfg.trigger.min_interval = 0.1
    cfg.idle.enabled = False

    personas = PersonaRegistry(cfg.persona_dir, cfg.app.persona)
    mem = ConversationMemory(8)
    scripts = ScriptLibrary(ROOT / "scripts")
    engine = SpeechEngine(cfg.tts, cfg.audio, cfg.log_dir / "tts_cache",
                 VoiceKit(cfg.sherpa_dir, cfg.model_dir, cfg.voices_dir,
                 cfg.tts.model_quality))
    director = Director(cfg, personas, mem, scripts, engine, AlwaysFailLLM(cfg.llm))

    chat = ScriptedChatSource([(0.2 + i * 0.15, f"@小夜 第{i}句") for i in range(6)])

    runner = asyncio.create_task(director.run(chat))
    for _ in range(60):
        await asyncio.sleep(0.2)
        if director.state.paused:
            break

    check("连续失败后自动暂停", director.state.paused,
          f"失败 {director.state.error_streak} 次")
    check("暂停后清空了积压队列", director.state.pending == 0,
          f"还剩 {director.state.pending} 条")
    check("失败原因被记下来了", "401" in director.state.last_error,
          director.state.last_error[:60])
    check("没有因为报错而说出空话", director.state.spoken_count == 0,
          f"说了 {director.state.spoken_count} 次")

    # 恢复之后应该能重新计数
    director.resume()
    check("恢复后错误计数清零", director.state.error_streak == 0 and not director.state.paused)

    await director.stop()
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    await engine.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
