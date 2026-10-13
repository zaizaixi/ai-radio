#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 电台 · 启动入口。

在 VS Code 里直接按 F5 就是运行（等价于 python run.py）。

常用命令：
    python run.py                启动电台
    python run.py check          自检：API Key、弹幕、语音、音频设备全测一遍
    python run.py devices        列出音频播放设备
    python run.py voices         列出可用音色
    python run.py danmaku 12345  只测弹幕连接，不启动 AI
    python run.py say "你好"      只测一句话的合成和播放
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from radio import __version__  # noqa: E402
from radio.chat import build_chat_source  # noqa: E402
from radio.chat.bilibili import parse_room_input  # noqa: E402
from radio.config import Config, ensure_config, load_config  # noqa: E402
from radio.control import ControlServer  # noqa: E402
from radio.director import Director  # noqa: E402
from radio.llm import LLMClient  # noqa: E402
from radio.persona import PersonaRegistry  # noqa: E402
from radio.memory import ConversationMemory, ScriptLibrary  # noqa: E402
from radio.tts import SpeechEngine, VoiceParams  # noqa: E402
from radio.voices import VoiceKit, human_size  # noqa: E402

log = logging.getLogger("radio")

BANNER = r"""
   ___    ____   ____             _ _
  / _ \  |  _ \ |  _ \  __ _   __| (_) ___
 | |_| | | |_) || |_) |/ _` | / _` | |/ _ \
 |  _  | |  _ < |  _ <| (_| || (_| | | (_) |
 |_| |_| |_| \_\|_| \_\\__,_| \__,_|_|\___/   v{version}
"""


# ---------------------------------------------------------------------------
# 控制台 & 日志
# ---------------------------------------------------------------------------


def setup_console() -> None:
    """让 Windows 终端能正确显示中文。"""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(
        logging, str(cfg.app.log_level or "INFO").upper(), logging.INFO
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-14s | %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(console)

    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.log_dir / "radio.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)-14s | %(message)s"))
        root.addHandler(file_handler)
    except Exception as exc:
        print(f"（日志文件写入失败，只输出到终端：{exc}）")

    # 第三方的 DEBUG 太吵
    for noisy in ("asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def build_components(cfg: Config, verbose: bool = False):
    personas = PersonaRegistry(cfg.persona_dir, cfg.app.persona)
    scripts = ScriptLibrary(cfg.script_dir, cfg.idle.scripts,
                            shuffle=cfg.idle.shuffle, loop=cfg.idle.loop)
    memory = ConversationMemory(cfg.llm.history_size)
    kit = VoiceKit(cfg.sherpa_dir, cfg.model_dir, cfg.voices_dir,
                 cfg.tts.model_quality)
    engine = SpeechEngine(cfg.tts, cfg.audio, cfg.log_dir / "tts_cache", kit)
    llm = LLMClient(cfg.llm)
    return personas, scripts, memory, engine, llm, kit


def format_event(event: str, payload: dict) -> str | None:
    if event == "trigger":
        who = payload.get("username") or "观众"
        return f"✦ 收到点名：{who} — {payload.get('text', '')[:60]}"
    if event == "speak":
        kind = {"reply": "回话", "idle": "念稿", "say": "指定"}.get(payload.get("kind", ""), "开口")
        return f"🔊 [{kind}] {payload.get('text', '')[:80]}"
    if event == "persona":
        return f"🎭 人设切换 -> {payload.get('name')}"
    if event == "interrupted":
        return "⏹ 被打断"
    if event == "error":
        return f"⚠ {payload.get('message')}"
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def run_radio(cfg: Config, verbose: bool = False, duration: float = 0.0) -> int:
    personas, scripts, memory, engine, llm, kit = build_components(cfg, verbose)

    print(BANNER.format(version=__version__))
    voice_params = engine.resolve_params(
        voice=personas.current.voice, rate=personas.current.rate,
        pitch=personas.current.pitch,
    )
    print(f"  人设    : {personas.current.display_name}  ({'、'.join(personas.keys())})")
    print(f"  声音    : {engine.provider_name} / {voice_params.describe()}")
    print(f"  音频后端: {getattr(engine.player, 'backend', '?')}  音量 {cfg.audio.volume}%")
    print(f"  大模型  : {cfg.llm.model} @ {cfg.llm.base_url}")
    print(f"  弹幕    : {cfg.chat.platform} {cfg.chat.room_id or ''}")
    print(f"  稿件    : {scripts.describe()}")
    if not cfg.llm.api_key:
        print("\n  ⚠ 还没配置 API Key，AI 没法说话。"
              "\n    请编辑 config.toml 的 [llm] api_key，或建一个 .env 写 "
              "DEEPSEEK_API_KEY=sk-xxxx")
        await engine.close()
        return 2
    if cfg.chat.platform.lower().startswith("bili") and not cfg.chat.room_id:
        print("\n  ⚠ 还没配置直播间房间号，请编辑 config.toml 的 [chat] room_id")
        await engine.close()
        return 2
    print()

    director = Director(cfg, personas, memory, scripts, engine, llm,
                        on_event=lambda e, p: _on_event(e, p, verbose))
    loop = asyncio.get_running_loop()
    control = ControlServer(cfg.control, director, loop)
    control.start()

    if cfg.audio.list_devices_on_start:
        from radio.audio import list_devices
        for device in list_devices():
            print(f"    音频设备 [{device.index}] {device.name}")
        print()

    source = build_chat_source(cfg.chat)
    log.info("电台开播，用 Ctrl+C 停播")

    runner = asyncio.create_task(director.run(source))
    try:
        if duration > 0:
            await asyncio.wait_for(asyncio.shield(runner), timeout=duration)
        else:
            await runner
    except asyncio.TimeoutError:
        log.info("到达预设运行时长 %.0f 秒，收工", duration)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("收到停止指令")
    finally:
        await director.stop()
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        control.stop()
        await engine.close()
        try:
            source_close = getattr(source, "close", None)
            if source_close:
                await source_close()
        except Exception:
            pass
    return 0


async def _on_event(event: str, payload: dict, verbose: bool) -> None:
    line = format_event(event, payload)
    if line is None:
        if verbose:
            log.debug("event %s: %s", event, payload)
        return
    if event == "error":
        log.error(line)
    else:
        print(line)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


async def cmd_check(cfg: Config) -> int:
    """自检：把每一环都过一遍。"""
    print(BANNER.format(version=__version__))
    print("  开始自检，每一项都会实际发一次请求 / 出一次声\n")
    problems: list[str] = []

    # 1. 配置
    print("[1/5] 配置文件")
    print(f"      路径      : {cfg.path}")
    print(f"      直播间     : {cfg.chat.platform} {cfg.chat.room_id or '（未填）'}")
    print(f"      人设       : {cfg.app.persona}")
    print(f"      API Key    : {'已配置' if cfg.llm.api_key else '★ 没有配置 ★'}")
    if not cfg.llm.api_key:
        problems.append("没有配置 DeepSeek API Key")

    # 2. 人设
    print("\n[2/5] 人设文件")
    try:
        personas = PersonaRegistry(cfg.persona_dir, cfg.app.persona)
        print(f"      载入成功   : {personas.current.display_name}")
        print(f"      可用人设   : {'、'.join(personas.keys())}")
        print(f"      提示词字数 : {len(personas.current.prompt)}")
        if len(personas.current.prompt) < 40:
            problems.append("人设提示词太短，AI 可能没什么性格")
    except Exception as exc:
        print(f"      ★ 失败: {exc}")
        problems.append(f"人设载入失败：{exc}")

    # 3. 大模型
    print("\n[3/5] 大模型接口")
    llm = LLMClient(cfg.llm)
    if cfg.llm.api_key:
        try:
            reply = await llm.check()
            print(f"      连通正常   : 模型回了「{reply}」")
        except Exception as exc:
            print(f"      ★ 失败: {exc}")
            problems.append(f"大模型不可用：{exc}")
    else:
        print("      跳过（没有 API Key）")

    # 4. 语音
    from radio.config import PROVIDER_LABELS, provider_kind

    kind = provider_kind(cfg.tts.provider)
    print(f"\n[4/5] 本地语音 · {PROVIDER_LABELS.get(kind, kind)}")
    kit = VoiceKit(cfg.sherpa_dir, cfg.model_dir, cfg.voices_dir,
                 cfg.tts.model_quality)
    print(f"      引擎       : {cfg.tts.provider}")
    print(f"      运行时     : {'已安装' if kit.runtime_ready() else '★ 未安装 ★'}")

    if kind == "kokoro":
        from radio.tts.kokoro import KOKORO_VOICES, load_profiles, parse_voice

        measured = load_profiles()
        chinese = sum(1 for v in KOKORO_VOICES if v.region == "中文")
        print(f"      Kokoro     : {'已安装' if kit.kokoro_ready() else '★ 未安装 ★'}")
        print(f"      语料参数   : {len(KOKORO_VOICES)} 个说话人（中文 {chinese}）"
              f" / {cfg.tts.num_threads} 线程")
        voice = parse_voice(cfg.tts.voice)
        if voice:
            print(f"      当前音色   : {voice.name}  {voice.label}")
            if voice.pitch:
                print(f"      实测特征   : 音高 {voice.pitch:.0f}Hz  "
                      f"明亮 {voice.brightness:.0f}Hz")
            elif measured:
                print("      ★ 这个音色还没测量过特征，关键词匹配可能不准")
        else:
            print(f"      当前音色   : ★ {cfg.tts.voice!r} 不是内置音色名 ★")

        if not kit.kokoro_ready():
            print("\n      提示：运行  python run.py setup  可以自动下载")
            problems.append("Kokoro 内置音色还没下载（跑一下 python run.py setup）")
        elif not measured:
            print("\n      提示：还没测量音色特征，按关键词找音色会不准。")
            print("            跑一次  python tools/profile_kokoro_voices.py")
    else:
        print(f"      ZipVoice   : {'已安装' if kit.model_ready() else '★ 未安装 ★'}")
        print(f"      参数       : {cfg.tts.num_steps} 步 / {cfg.tts.num_threads} 线程"
              f" / 精度 {kit.quality}")

        profiles = kit.profiles()
        ready = [p for p in profiles if p.ready]
        if profiles:
            print(f"      克隆音色   : {len(profiles)} 个（可用 {len(ready)} 个）")
            for profile in profiles:
                mark = " " if profile.ready else "★"
                print(f"                   {mark} {profile.display}  {profile.describe()}")
        else:
            print("      克隆音色   : ★ 一个都没有 ★")

        if not kit.ready():
            print("\n      提示：运行  python run.py setup  可以自动下载引擎")
            problems.append("本地克隆引擎还没装好（跑一下 python run.py setup）")
        elif not ready:
            print("\n      提示：还没有可用的音色。")
            print("            在软件「音色」页点「＋ 新建音色」，")
            print("            录一段 5~8 秒的干声（WAV）并填上内容即可。")
            problems.append("还没有克隆音色（去软件「音色」页新建一个）")

    # ---- 合成自检：两个引擎共用 ----
    if kind == "kokoro":
        from radio.tts.kokoro import parse_voice as _parse

        _voice = _parse(cfg.tts.voice)
        can_test = kit.kokoro_ready()
        test_voice = _voice.name if _voice else ""
        test_label = f"内置音色「{test_voice}」" if test_voice else "内置音色"
    else:
        can_test = kit.ready() and bool(ready)
        test_voice = ready[0].key if ready else ""
        test_label = f"音色「{ready[0].display}」" if ready else ""

    if can_test:
        engine = SpeechEngine(cfg.tts, cfg.audio, cfg.log_dir / "tts_cache", kit)
        print(f"      音频后端   : {getattr(engine.player, 'backend', '?')}")
        try:
            params = VoiceParams(voice=test_voice)
            print(f"      正在合成   : {test_label}（首次要加载模型，约 8 秒）…")
            started = time.monotonic()
            data = await engine.provider.synthesize("各位听众晚上好，这里是自检测试。", params)
            elapsed = time.monotonic() - started
            from radio.audio import parse_wav
            wav = parse_wav(data)
            print(f"      合成成功   : {wav.duration:.2f} 秒音频 / 耗时 {elapsed:.2f}s "
                  f"/ {wav.sample_rate}Hz")
            print("      正在播放，请留意扬声器……")
            ok = await asyncio.to_thread(engine.player.play, data, None, None)
            print(f"      播放{'完成' if ok else '被中断'}")
        except Exception as exc:
            print(f"      ★ 失败: {exc}")
            problems.append("语音不可用（详见上面提示）")
        finally:
            await engine.close()

    # 5. 弹幕
    print("\n[5/5] 直播间弹幕")
    if cfg.chat.platform.lower().startswith("bili") and cfg.chat.room_id:
        try:
            from radio.chat.bilibili import fetch_danmu_info, fetch_room_status, resolve_room_id
            real = resolve_room_id(cfg.chat.room_id, cfg.chat.cookies)
            status = fetch_room_status(real, cfg.chat.cookies)
            live = {0: "未开播", 1: "直播中", 2: "轮播"}.get(status.get("live_status"), "?")
            print(f"      房间       : {cfg.chat.room_id} -> {real}")
            print(f"      主播       : {status.get('uname') or '?'}  [{live}]")
            print(f"      标题       : {str(status.get('title') or '')[:40]}")
            token, hosts = fetch_danmu_info(real, cfg.chat.cookies)
            print(f"      弹幕服务器 : {len(hosts)} 个，token 长度 {len(token)}")
            print(f"      首选服务器 : {hosts[0]['host']}")
            print("      （能拿到 token 就说明弹幕链路是通的）")
            if not cfg.chat.cookies:
                print("      建议：填上 cookies 能让弹幕更完整、更不容易被风控")
        except Exception as exc:
            print(f"      ★ 失败: {exc}")
            problems.append(f"弹幕不可用：{exc}")
    else:
        print("      跳过（当前是控制台模式或没填房间号）")

    print("\n" + "=" * 58)
    if problems:
        print("自检完成，发现这些问题：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("自检完成，全部正常，可以 python run.py 开播了 ✓")
    return 0


def cmd_devices(cfg: Config) -> int:
    from radio.audio import list_devices, make_player
    devices = list_devices()
    print("可用的音频播放设备：")
    for device in devices:
        print(f"  [{device.index}] {device.name}")
    if not devices:
        print("  （没找到设备）")
    print("\n把想用的序号填进 config.toml 的 [audio] device；留空 = 系统默认。")
    print("\n直播接线提示：")
    print("  想只让观众听到、自己不听：把系统默认输出设为虚拟声卡（如 VB-CABLE），")
    print("  然后在 OBS 里把「音频输出采集」设为同一个虚拟声卡。")
    player = make_player(volume=100)
    print(f"\n当前实际使用的后端：{getattr(player, 'backend', '?')}")
    return 0


async def cmd_voices(cfg: Config) -> int:
    kit = VoiceKit(cfg.sherpa_dir, cfg.model_dir, cfg.voices_dir,
                 cfg.tts.model_quality)

    from radio.tts.kokoro import KOKORO_VOICES, load_profiles, parse_voice

    from radio.config import provider_kind

    kind = provider_kind(cfg.tts.provider)

    print("== 引擎资源 ==")
    print(f"  运行时      : {'✓ 已安装' if kit.runtime_ready() else '✗ 未安装'}")
    print(f"  Kokoro 内置 : {'✓ 已安装' if kit.kokoro_ready() else '✗ 未安装'}")

    from radio.tts.characters import CHARACTER_VOICES, get as get_character

    installed_chars = [v for v in CHARACTER_VOICES if kit.character_ready(v.key)]
    print(f"  角色音色    : {len(installed_chars)}/{len(CHARACTER_VOICES)} 个模型已下载")
    print(f"  ZipVoice    : {'✓ 已安装' if kit.model_ready() else '✗ 未安装'}")
    if kit.model_ready():
        print(f"  模型精度    : {kit.quality}")
    print(f"  当前引擎    : {kind}")

    # ---- 角色音色（默认引擎）----
    print("\n== 角色音色（VITS） ==")
    if installed_chars:
        raw = (cfg.tts.voice or "").strip()
        model_key, sid = raw, 0
        for sep in (":", "#", "/"):
            if sep in raw:
                head, tail = raw.rsplit(sep, 1)
                if tail.strip().isdigit():
                    model_key, sid = head.strip(), int(tail.strip())
                    break
        voice = get_character(model_key)
        if voice is None:
            voice = get_character(installed_chars[0].key)
        print(f"  每个模型 804 个说话人编号")
        for item in installed_chars:
            mark = " ←当前" if voice and item.key == voice.key else ""
            print(f"    {item.name}（{item.origin}·{item.gender}）"
                  f"  {' '.join(item.tags)}{mark}")
        if voice:
            print(f"  当前选用    : {voice.key}:{sid}  —— {voice.name}")
            print(f"  换编号      : 在软件「音色」页拖编号，用「下一个 ▶」连着试听")
            print(f"                找到听着正常的再点「✓ 就用这个」")
        else:
            print(f"  当前选用    : （{raw!r} 认不出来，会回落到第一个模型）")
    else:
        print("  （还没下载任何角色模型，每个约 115MB）")
        print("  安装用：python run.py setup   或在软件「音色」页点「下载推荐音色」")

    # ---- Kokoro ----
    measured = load_profiles()
    chinese = sum(1 for v in KOKORO_VOICES if v.region == "中文")
    print("\n== 通用内置音色（Kokoro） ==")
    if kit.kokoro_ready():
        print(f"  共 {len(KOKORO_VOICES)} 个（中文 {chinese} 个），"
              f"已测量特征 {measured} 个")
        voice = parse_voice(cfg.tts.voice)
        if voice:
            print(f"  当前选用    : {voice.name}  {voice.label}")
            if voice.pitch:
                print(f"  实测        : 音高 {voice.pitch:.0f}Hz  "
                      f"明亮 {voice.brightness:.0f}Hz  语速 {voice.speed:.2f}字/秒")
        if not measured:
            print("  提示：还没测量音色特征，关键词找音色会不准。")
            print("        跑一次 python tools/profile_kokoro_voices.py 即可。")
    else:
        print("  （还没下载，约 140MB）")

    print("\n== 我的克隆音色 ==")
    profiles = kit.profiles()
    if not profiles:
        print("  （还没有音色）")
        print("\n  怎么加：打开软件 app.py → 「音色」页 → 「＋ 新建音色」")
        print("  需要一段 5~8 秒的干声（16 位 PCM WAV），以及这段音频里说的文字。")
    else:
        for profile in profiles:
            flag = " " if profile.ready else "★"
            print(f"  {flag} {profile.key:<20} {profile.display:<22} {profile.describe()}")
            if not profile.ready:
                print(f"      {profile.problem}")
    return 0


async def cmd_setup(cfg: Config, _ignored: str = "") -> int:
    """一键安装本地克隆引擎（运行时 + ZipVoice 模型 + 声码器）。"""
    kit = VoiceKit(cfg.sherpa_dir, cfg.model_dir, cfg.voices_dir,
                 cfg.tts.model_quality)
    print(BANNER.format(version=__version__))
    print("  正在准备本地克隆引擎（约 176MB，之后永久离线可用）\n")

    def status(msg: str) -> None:
        print(f"  {msg}")

    def progress(name: str, done: int, total: int) -> None:
        if total:
            pct = done / total * 100
            bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
            print(f"\r      {bar} {pct:5.1f}%  {human_size(done)}/{human_size(total)}   ",
                  end="", flush=True)
        else:
            print(f"\r      {human_size(done)}   ", end="", flush=True)

    try:
        await asyncio.to_thread(kit.install, progress, status)
        print()
    except Exception as exc:
        print(f"\n  ★ 安装失败：{exc}")
        print("\n  如果网络实在不通，可以手动准备：")
        print(f"    1. 下载 sherpa-onnx-v1.13.8-win-x64-shared-MD-Release.tar.bz2")
        print(f"       解压后把 bin/lib/include 放进  {kit.runtime_dir}")
        print(f"    2. 下载 sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2")
        print(f"       解压后整个目录放进            {kit.model_dir}")
        print(f"    3. 下载 vocos_24khz.onnx 放进    {kit.model_dir}")
        print("    下载地址见 README 的「本地语音怎么装」一节")
        return 1

    print("\n  引擎装好了。")
    if not kit.profiles():
        print("\n  下一步：打开图形界面新建一个音色")
        print("        python app.py  →  「音色」页  →  「＋ 新建音色」")
        print("        需要一段 5~8 秒的干声（WAV）和它对应的文字。")
    else:
        ready = [p for p in kit.profiles() if p.ready]
        if ready:
            engine = SpeechEngine(cfg.tts, cfg.audio,
                                  cfg.log_dir / "tts_cache", kit)
            print(f"\n  用「{ready[0].display}」试一句（首次要加载模型，约 8 秒）：")
            started = time.monotonic()
            ok = await engine.speak("各位听众晚上好，欢迎收听今晚的节目。",
                                    VoiceParams(voice=ready[0].key))
            print(f"  合成播放{'成功' if ok else '被中断'}"
                  f"（耗时 {time.monotonic()-started:.2f}s）")
            await engine.close()

    print("\n  现在可以直接开播了：python run.py")
    print("  或者打开图形界面：python app.py")
    return 0


async def cmd_danmaku(cfg: Config, room: str, seconds: float) -> int:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    room_id = parse_room_input(room)
    print(f"连接直播间 {room_id}，观察 {seconds:.0f} 秒……\n")
    cfg.chat.room_id = room_id
    source = build_chat_source(cfg.chat)
    deadline = time.monotonic() + seconds
    count = 0
    try:
        async for message in source.messages():
            print("  " + message.display())
            count += 1
            if time.monotonic() > deadline:
                break
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"出错：{exc}")
        return 1
    finally:
        close = getattr(source, "close", None)
        if close:
            await close()

    print(f"\n共收到 {count} 条消息")
    if count == 0:
        print("一条都没有。可能原因：房间没开播 / 太冷清 / 被风控。")
        print("建议在 config.toml 的 [chat.cookies] 填上登录后的 Cookie 再试。")
        return 1
    print("弹幕链路正常 ✓")
    return 0


async def cmd_say(cfg: Config, text: str) -> int:
    personas = PersonaRegistry(cfg.persona_dir, cfg.app.persona)
    persona = personas.current
    kit = VoiceKit(cfg.sherpa_dir, cfg.model_dir, cfg.voices_dir,
                 cfg.tts.model_quality)
    engine = SpeechEngine(cfg.tts, cfg.audio, cfg.log_dir / "tts_cache", kit)
    params = engine.resolve_params(voice=persona.voice, rate=persona.rate,
                                   pitch=persona.pitch, volume=persona.volume)
    print(f"人设：{persona.display_name}")
    print(f"音色：{params.describe()}")
    print(f"引擎：{engine.provider_name}  "
          f"{cfg.tts.num_steps} 步 / {cfg.tts.num_threads} 线程 / 音量 {cfg.audio.volume}%")
    print(f"内容：{text}\n")
    try:
        await engine.speak(text, params, on_chunk=lambda c: print(f"  ▶ {c}"))
    except Exception as exc:
        print(f"失败：{exc}")
        return 1
    finally:
        await engine.close()
    return 0


def cmd_init(cfg: Config) -> int:
    path = ensure_config(cfg.path)
    print(f"配置文件：{path}")
    cfg.persona_dir.mkdir(parents=True, exist_ok=True)
    cfg.script_dir.mkdir(parents=True, exist_ok=True)
    (cfg.log_dir / "tts_cache").mkdir(parents=True, exist_ok=True)
    print(f"人设目录：{cfg.persona_dir}")
    print(f"稿件目录：{cfg.script_dir}")
    print("初始化完成。")
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="AI 电台 · 直播时在旁边陪聊的 AI 主持人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "check", "setup", "devices", "voices", "danmaku",
                                 "say", "init"],
                        help="要做什么（默认 run = 启动电台）")
    parser.add_argument("value", nargs="?", default="",
                        help="danmaku 的房间号 / say 的内容")
    parser.add_argument("-c", "--config", default="", help="指定配置文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="run: 跑多少秒后自动退出（用于试跑）")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="danmaku: 观察多少秒，默认 30")
    parser.add_argument("--console", action="store_true",
                        help="强制用控制台模式（手动敲字当弹幕），不连直播平台")
    parser.add_argument("--room", default="",
                        help="临时指定直播间房间号，覆盖 config.toml")
    parser.add_argument("--persona", default="",
                        help="临时指定人设，覆盖 config.toml")
    parser.add_argument("--no-tts", action="store_true",
                        help="不出声，只在终端打印 AI 要说的话")
    parser.add_argument("--version", action="version",
                        version=f"AI 电台 v{__import__('radio').__version__}")
    args = parser.parse_args(argv)

    setup_console()

    # 清掉「来自互联网」标记，免得启动时弹「未知发布者」
    try:
        from pathlib import Path as _Path

        from radio.winfs import unblock_project

        cleared, _found = unblock_project(_Path(__file__).resolve().parent)
        if cleared:
            print(f"已清除 {cleared} 个文件的「来自互联网」标记")
    except Exception:
        pass

    config_path = Path(args.config) if args.config else None
    ensure_config(config_path)
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        print(f"配置文件解析失败：{exc}")
        print(f"请检查 {config_path or 'config.toml'} 的格式（TOML）。")
        return 2

    # ---- 命令行覆盖 ----
    if args.console:
        cfg.chat.platform = "console"
    if args.room:
        try:
            cfg.chat.room_id = parse_room_input(args.room)
        except ValueError as exc:
            print(f"房间号解析失败：{exc}")
            return 2
    if args.persona:
        cfg.app.persona = args.persona
    if args.no_tts:
        cfg.tts.provider = "none"

    setup_logging(cfg, args.verbose)

    try:
        if args.command == "run":
            return asyncio.run(run_radio(cfg, args.verbose, args.seconds))
        if args.command == "check":
            return asyncio.run(cmd_check(cfg))
        if args.command == "devices":
            return cmd_devices(cfg)
        if args.command == "setup":
            return asyncio.run(cmd_setup(cfg, args.value))
        if args.command == "voices":
            return asyncio.run(cmd_voices(cfg))
        if args.command == "danmaku":
            if not args.value:
                print("用法：python run.py danmaku 房间号")
                return 2
            return asyncio.run(cmd_danmaku(cfg, args.value,
                                           args.duration or 30.0))
        if args.command == "say":
            if not args.value:
                print('用法：python run.py say "要说的话"')
                return 2
            return asyncio.run(cmd_say(cfg, args.value))
        if args.command == "init":
            return cmd_init(cfg)
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0
    except Exception as exc:
        log.exception("运行出错")
        print(f"\n出错了：{exc}")
        print("把 logs/radio.log 里的内容发出来，能更快定位问题。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
