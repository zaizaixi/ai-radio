"""单元测试：把不依赖外部服务、但逻辑容易错的代码都过一遍。

重点覆盖：
  * LLM 的 SSE 流式解析 + 线程到事件循环的桥接（这段最容易出问题）
  * 音频 PCM 音量缩放、WAV 解析
  * 人设文件头部参数块的解析（各种写法）
  * 配置加载与默认值
  * 文本清洗与切句的边界情况
  * B 站协议包的打包/解包

    python tools/selftest_units.py
"""
from __future__ import annotations

import asyncio
import array
import ctypes
import io
import json
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radio.config import LLMConfig, _fill, load_config                    # noqa: E402
from radio.llm import LLMClient, LLMError, Message                      # noqa: E402
from radio.memory import split_script                                   # noqa: E402
from radio.persona import (build_system_prompt, clean_speech,            # noqa: E402
                           load_persona, parse_front_matter, split_for_tts)
from radio.audio import apply_volume, parse_wav                         # noqa: E402
from radio.chat.bilibili import iter_packets, pack_packet               # noqa: E402
from radio.tts.base import VoiceParams                                  # noqa: E402
from radio.voices import VoiceKit                                   # noqa: E402

failures: list[str] = []
skips: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'  ✓' if ok else '  ✗'} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def skip(name: str, why: str) -> None:
    """跳过一项检查 —— 通常是「还没下载模型」这种预期情况。

    全新克隆跑自测时模型目录是空的。那不是失败，只是还没装。
    报成 ✗ 会让刚装好的人以为程序坏了。
    """
    print(f"  ○ {name}   （跳过：{why}）")
    skips.append(name)


# ---------------------------------------------------------------------------
# 假 LLM 服务：返回 SSE 流
# ---------------------------------------------------------------------------


class _SSEHandler(BaseHTTPRequestHandler):
    mode = "stream"

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        auth = self.headers.get("Authorization", "")

        if auth == "Bearer bad-key":
            payload = json.dumps({"error": {"message": "Invalid API key"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            pieces = ["各位", "听众", "晚上好", "，欢迎", "收听。"]
            for piece in pieces:
                chunk = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            # 夹一条无关的 SSE 事件，验证解析器能跳过
            self.wfile.write(b": keep-alive\n\n")
            self.wfile.write(b"data: {\"choices\":[]}\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            payload = json.dumps({
                "choices": [{"message": {"content": "一次性回复"}}]
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


def start_fake_llm() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


# ---------------------------------------------------------------------------


async def test_llm() -> None:
    print("\n[1] LLM 客户端（本地假服务）")
    server, base = start_fake_llm()
    try:
        cfg = LLMConfig(base_url=base, api_key="good-key", model="fake",
                        stream=True, timeout=10)
        client = LLMClient(cfg)
        check("endpoint 拼接正确", client.endpoint == f"{base}/v1/chat/completions",
              client.endpoint)

        # 流式
        pieces = []
        async for piece in client.stream([Message("user", "hi")]):
            pieces.append(piece)
        check("流式解析出全部片段", "".join(pieces) == "各位听众晚上好，欢迎收听。",
              repr("".join(pieces)))

        # generate 走流式
        text = await client.generate([Message("user", "hi")])
        check("generate 用流式拼出完整文本", text == "各位听众晚上好，欢迎收听。", repr(text))

        # on_delta 回调
        got = []
        await client.generate([Message("user", "hi")],
                              on_delta=lambda p: got.append(p))
        check("on_delta 逐段回调", "".join(got) == text, f"{len(got)} 次回调")

        # 非流式
        cfg2 = LLMConfig(base_url=base, api_key="good-key", stream=False)
        text2 = await LLMClient(cfg2).generate([Message("user", "hi")])
        check("非流式也能用", text2 == "一次性回复", repr(text2))

        # 流式失败自动退回一次性
        class Broken(LLMClient):
            async def stream(self, *a, **k):
                raise LLMError("模拟流式失败")
                yield  # pragma: no cover

        cfg3 = LLMConfig(base_url=base, api_key="good-key", stream=True)
        broken = Broken(cfg3)
        text3 = await broken.generate([Message("user", "hi")])
        check("流式失败会自动退回一次性请求", text3 == "一次性回复", repr(text3))

        # 401 报错友好
        cfg4 = LLMConfig(base_url=base, api_key="bad-key", stream=False)
        try:
            await LLMClient(cfg4).complete([Message("user", "hi")])
            check("401 会抛 LLMError", False, "没有抛异常")
        except LLMError as exc:
            check("401 报错信息友好", "401" in str(exc) and "API Key" in str(exc),
                  str(exc)[:60])

        # 没有 key
        try:
            await LLMClient(LLMConfig(base_url=base, api_key="")).complete(
                [Message("user", "hi")])
            check("缺 key 会提前报错", False)
        except LLMError as exc:
            check("缺 key 的提示包含解决方式", "api_key" in str(exc) and ".env" in str(exc))
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------


def test_audio() -> None:
    print("\n[2] 音频处理")
    # 造一个 24kHz 16bit 单声道 WAV
    import array
    samples = array.array("h", [1000, -1000, 30000, -30000])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(samples.tobytes())
    data = buf.getvalue()

    wav = parse_wav(data)
    check("WAV 解析参数正确",
          wav.channels == 1 and wav.sample_rate == 24000 and wav.sample_width == 2,
          f"{wav.channels}ch {wav.sample_rate}Hz")
    check("时长计算正确", abs(wav.duration - 4 / 24000) < 1e-6, f"{wav.duration:.5f}s")

    quiet = apply_volume(samples.tobytes(), 50)
    out = array.array("h")
    out.frombytes(quiet)
    check("音量 50% 采样减半", list(out) == [500, -500, 15000, -15000], str(list(out)))

    loud = array.array("h")
    loud.frombytes(apply_volume(samples.tobytes(), 200))
    check("音量 200% 且不溢出", list(loud) == [2000, -2000, 32767, -32768], str(list(loud)))

    check("音量 100% 原样返回", apply_volume(samples.tobytes(), 100) == samples.tobytes())


def test_persona() -> None:
    print("\n[3] 人设解析")
    meta, body = parse_front_matter(
        "---\nname: 小夜\nalias: [小夜, 夜夜]\nrate: +8%\ntemperature: 1.2\n"
        "append_rules: false\ncustom_key: 随便什么\n---\n正文第一行\n正文第二行"
    )
    check("参数块键值解析", meta.get("name") == "小夜" and meta.get("rate") == "+8%",
          str(meta))
    check("列表解析", meta.get("alias") == ["小夜", "夜夜"], str(meta.get("alias")))
    check("布尔解析", meta.get("append_rules") is False)
    check("浮点解析", meta.get("temperature") == 1.2)
    check("正文与参数块分离", body == "正文第一行\n正文第二行", repr(body))

    meta2, body2 = parse_front_matter("没有参数块，直接就是正文\n第二行")
    check("没有参数块时整段都是正文", meta2 == {} and body2.startswith("没有参数块"), repr(body2[:10]))

    # 真实人设文件
    p = load_persona(ROOT / "personas" / "深夜电台.md")
    check("真实人设载入", p.name == "小夜" and len(p.prompt) > 200, f"{len(p.prompt)} 字")
    check("真实人设参数正确", p.max_chars == 130, f"max_chars={p.max_chars}")
    check("人设的 voice 留空（跟随当前音色）或指向已有克隆音色",
          p.voice == "" or p.voice in {v.key for v in
              VoiceKit(ROOT / "sherpa", ROOT / "models", ROOT / "voices").profiles()},
          repr(p.voice))
    check("关键词包含名字与别名", set(p.keywords()) >= {"小夜", "夜夜", "电台姬"},
          str(p.keywords()))

    sys_prompt = build_system_prompt(p, host_name="老王", game="黑神话")
    check("系统提示词含人设正文", "深夜陪伴电台" in sys_prompt)
    check("系统提示词含运行规则", "直播间运行规则" in sys_prompt and "老王" in sys_prompt)

    p.append_rules = False
    only = build_system_prompt(p, host_name="老王")
    check("append_rules=false 时只输出人设正文", "直播间运行规则" not in only and "小夜" in only)

    # 关闭规则的原文一致性
    check("关闭规则后正文完全一致", only.strip() == p.prompt.strip())


def test_text() -> None:
    print("\n[4] 文本清洗与切句")
    cases = [
        ("**你好**世界", "你好世界"),
        ("（笑）你好", "你好"),
        ("你好[链接](http://a.com)呀", "你好链接呀"),
        ("# 标题\n正文内容", "标题 正文内容"),
        ("你好 😊 世界", "你好 世界"),
        ("小夜：今天不错", "今天不错"),
        ("```code```正文", "正文"),
    ]
    for raw, expect in cases:
        got = clean_speech(raw)
        check(f"清洗 {raw!r}", got == expect, repr(got))

    check("空输入返回空", clean_speech("") == "" and clean_speech(None) == "")

    chunks = split_for_tts("短句。", max_chars=50)
    check("短句不切", chunks == ["短句。"], str(chunks))

    long_text = "这是一个很长的句子，" * 6
    chunks = split_for_tts(long_text, max_chars=30)
    check("超长文本被切开", len(chunks) > 1 and all(len(c) <= 40 for c in chunks),
          f"{len(chunks)} 段，最长 {max(len(c) for c in chunks)}")
    check("切分不丢字", "".join(chunks) == long_text.replace(" ", ""),
          f"{len(''.join(chunks))} vs {len(long_text)}")

    segs = split_script("# 标题\n\n第一段\n\n第二段\n---\n第三段\n// 注释\n第四段")
    check("稿件分段正确", segs == ["第一段", "第二段", "第三段", "第四段"], str(segs))


def test_config() -> None:
    print("\n[5] 配置加载")
    cfg = load_config(ROOT / "config.toml")
    check("能读到生成的 config.toml", cfg.app.persona == "深夜电台", cfg.app.persona)
    check("默认值生效", cfg.llm.model == "deepseek-chat" and cfg.tts.provider == "vits",
          f"provider={cfg.tts.provider}")
    check("路径解析正确", cfg.persona_dir.name == "personas", str(cfg.persona_dir))
    check("人设目录真实存在", cfg.persona_dir.is_dir())

    from radio.config import ChatConfig
    filled = _fill(ChatConfig, {"platform": "console", "room_id": "123",
                                "cookies": {"a": 1}, "不存在的键": "x"})
    check("_fill 忽略未知键并强制类型", filled.platform == "console", str(filled))
    check("cookies 的值被转成字符串", filled.cookies == {"a": "1"} and
          all(isinstance(v, str) for v in filled.cookies.values()), str(filled.cookies))

    _test_template()


def _test_template() -> None:
    """守住一个很容易犯的错误：TOML 里 [section.sub] 后面的键会归到子表。

    之前 [chat.cookies] 写在 chat 中间，导致后面的 ignore_enter 等
    全被算进 cookies，用户改了完全不生效。
    """
    import tomllib
    from dataclasses import fields as dc_fields
    from radio.config import (DEFAULT_CONFIG_TEMPLATE, AppConfig, AudioConfig,
                              ChatConfig, ControlConfig, IdleConfig, LLMConfig,
                              TTSConfig, TriggerConfig)

    raw = tomllib.loads(DEFAULT_CONFIG_TEMPLATE)
    chat = raw.get("chat") or {}
    expected_chat = {"platform", "room_id", "ignore_enter", "reconnect_delay",
                     "console_prompt", "cookies"}
    check("[chat.cookies] 没有吃掉后面 chat 的配置项",
          expected_chat <= set(chat), f"chat 实际字段: {sorted(chat)}")
    check("cookies 是个空表", chat.get("cookies") == {}, str(chat.get("cookies")))

    sections = {"app": AppConfig, "chat": ChatConfig, "trigger": TriggerConfig,
                "llm": LLMConfig, "tts": TTSConfig, "audio": AudioConfig,
                "idle": IdleConfig, "control": ControlConfig}

    missing, unknown = [], []
    for name, cls in sections.items():
        data = raw.get(name) or {}
        allowed = {f.name for f in dc_fields(cls)}
        missing += [f"{name}.{f.name}" for f in dc_fields(cls) if f.name not in data]
        unknown += [f"{name}.{k}" for k in data if k not in allowed]

    check("模板覆盖了全部配置项", not missing,
          ("缺少: " + "、".join(missing)) if missing else "全部覆盖")
    check("模板里没有拼错的配置项", not unknown,
          ("疑似拼错: " + "、".join(unknown)) if unknown else "没有拼写错误")

    # 模板能真正被加载成 Config，且关键值正确
    # （写到项目内的临时文件，不用 tempfile —— 某些受限环境下临时目录会被锁）
    scratch = ROOT / "logs" / "_selftest_config.toml"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    try:
        scratch.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
        loaded = load_config(scratch)
    finally:
        scratch.unlink(missing_ok=True)
    check("模板能被正常加载", loaded.chat.platform == "bilibili" and
          loaded.chat.ignore_enter is True and loaded.idle.enabled is True)
    check("模板里 cookies 默认为空", loaded.chat.cookies == {}, str(loaded.chat.cookies))


def test_bilibili_protocol() -> None:
    print("\n[6] B 站协议打包/解包")
    body = json.dumps({"cmd": "DANMU_MSG", "info": [[], "测试弹幕"]}).encode("utf-8")
    packet = pack_packet(5, body, protover=0)
    parsed = list(iter_packets(packet))
    check("单包能被解出来", len(parsed) == 1, str(len(parsed)))
    ver, op, data = parsed[0]
    check("操作码和内容正确", op == 5 and json.loads(data)["cmd"] == "DANMU_MSG", f"op={op}")

    # 多包拼接
    multi = packet + packet + packet
    check("多包拼接能全部解出", len(list(iter_packets(multi))) == 3)

    # zlib 嵌套
    import zlib
    inner = pack_packet(5, body, protover=0) * 2
    zlib_packet = pack_packet(5, zlib.compress(inner), protover=2)
    nested = list(iter_packets(zlib_packet))
    check("zlib 压缩包能递归解开", len(nested) == 2, f"{len(nested)} 个内层包")

    # 截断数据不能崩
    check("截断数据不会崩", list(iter_packets(packet[:10])) == [])
    check("空数据不会崩", list(iter_packets(b"")) == [])


async def test_zipvoice() -> None:
    print("\n[7] 本地克隆语音（ZipVoice）")
    from radio.tts.zipvoice import ZipVoiceTTS, _rate_to_speed, float_samples_to_wav
    from radio.voices import VoiceKit, normalize_wav

    # 语速换算
    check("语速 +25% -> 1.25 倍速", _rate_to_speed("+25%") == 1.25)
    check("语速 -20% -> 0.8 倍速", _rate_to_speed("-20%") == 0.8)
    check("语速 +0% -> 1.0", _rate_to_speed("+0%") == 1.0)
    check("非法语速不炸", _rate_to_speed("") == 1.0 and _rate_to_speed("abc") == 1.0)
    check("语速被夹在合理范围", _rate_to_speed("+999%") <= 2.5)

    # float 采样 -> WAV
    import ctypes
    buf = (ctypes.c_float * 1000)(*([0.5] * 1000))
    wav_bytes = float_samples_to_wav(ctypes.cast(buf, ctypes.POINTER(ctypes.c_float)),
                                     1000, 24000)
    parsed = parse_wav(wav_bytes)
    check("float 采样能转成合法 WAV",
          wav_bytes[:4] == b"RIFF" and parsed.sample_rate == 24000
          and parsed.channels == 1 and parsed.sample_width == 2,
          f"{parsed.sample_rate}Hz {len(wav_bytes)}B")

    # 立体声 -> 单声道
    import struct as _struct
    import wave as _wave
    stereo = b"".join(_struct.pack("<hh", 1000, 3000) for _ in range(100))
    sbuf = io.BytesIO()
    with _wave.open(sbuf, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(stereo)
    mono = parse_wav(normalize_wav(sbuf.getvalue()))
    check("立体声样本会被混成单声道", mono.channels == 1, f"{mono.channels}ch")
    samples = __import__("array").array("h")
    samples.frombytes(mono.pcm)
    check("混音数值正确（1000和3000平均是2000）", samples[0] == 2000, str(samples[:3]))

    kit = VoiceKit(ROOT / "sherpa", ROOT / "models", ROOT / "voices")

    # 引擎资源是「首次使用时才下载」的。全新克隆里没有它们是正常的，
    # 不是失败 —— 报成 ✗ 会让刚装上的人以为程序坏了。
    if not kit.runtime_ready():
        skip("克隆引擎运行时在位", "还没下载（python run.py setup）")
        skip("ZipVoice 模型在位", "同上")
        skip("有可用的克隆音色", "同上")
        skip("ZipVoice 真实合成成功", "同上")
        skip("相同文本命中缓存", "同上")
        skip("模型常驻，第二句没有加载开销", "同上")
        return
    check("克隆引擎运行时在位", kit.runtime_ready(), str(kit.tts_exe))
    check("ZipVoice 模型在位", kit.model_ready(),
          f"encoder={kit.encoder is not None} decoder={kit.decoder is not None} "
          f"vocoder={kit.vocoder.exists()}")

    ready = [p for p in kit.profiles() if p.ready]
    temp_profile = None
    if not ready and kit.ready():
        # 测试环境可能还没有任何音色，用模型自带的样本临时建一个
        sample = kit.zipvoice_dir / "test_wavs" / "leijun-1.wav"
        if sample.exists():
            temp_profile = kit.create(
                "自测临时音色", sample.read_bytes(),
                "那还是三十六年前, 一九八七年. 我呢考上了武汉大学的计算机系.",
                name="自测临时音色")
            ready = [temp_profile]
    if not kit.ready():
        skip("有可用的克隆音色", "ZipVoice 模型还没下载")
        return
    check("有可用的克隆音色", bool(ready),
          "、".join(p.display for p in ready) or "（没有）")

    if kit.ready() and ready:
        engine = ZipVoiceTTS(kit, num_threads=8, num_steps=4)
        profile = ready[0]
        try:
            started = time.monotonic()
            data = engine._synthesize_sync("你好，这是一句测试。",
                                           VoiceParams(voice=profile.key))
            elapsed = time.monotonic() - started
            wav = parse_wav(data)
            check("ZipVoice 真实合成成功",
                  wav.duration > 0.3 and wav.sample_rate >= 16000,
                  f"{wav.duration:.2f}s {wav.sample_rate}Hz 首次含模型加载 {elapsed:.1f}s")
        except Exception as exc:
            check("ZipVoice 真实合成成功", False, f"{type(exc).__name__}: {exc}")

        started = time.monotonic()
        try:
            engine._synthesize_sync("你好，这是一句测试。",
                                    VoiceParams(voice=profile.key))
        except Exception:
            pass
        elapsed = time.monotonic() - started
        check("相同文本命中缓存（几乎瞬间）", elapsed < 0.1, f"{elapsed*1000:.0f}ms")

        # 模型常驻：第二句不应该再有加载开销
        started = time.monotonic()
        try:
            engine._synthesize_sync("换一句话试试。", VoiceParams(voice=profile.key))
        except Exception:
            pass
        elapsed = time.monotonic() - started
        check("模型常驻，第二句没有加载开销", elapsed < 8.0, f"{elapsed:.2f}s")
        engine.close()

    if temp_profile is not None:
        kit.delete(temp_profile.key)


def test_launcher_files() -> None:
    """守住一类很容易重犯的坑：启动脚本的编码。

    cmd.exe 读 .bat 是按**控制台代码页**逐字节解析的：
      * 文件里有中文 -> 在代码页不匹配的机器上直接乱码成命令
      * 裸 LF 换行   -> 解析错乱（.bat 必须 CRLF）
      * UTF-8 BOM    -> 第一行就废
    PowerShell 5.1 读 .ps1 同理，中文字符需要靠 BOM 才能识别。
    """
    print("\n[8] 启动脚本的编码规范")

    bat = ROOT / "启动.bat"
    check("启动.bat 存在", bat.exists())
    if bat.exists():
        raw = bat.read_bytes()
        non_ascii = sum(1 for byte in raw if byte > 127)
        check("启动.bat 是纯 ASCII（混中文会让 cmd.exe 解析错乱）",
              non_ascii == 0, f"{non_ascii} 个非 ASCII 字节")

        crlf = raw.count(b"\r\n")
        lone_lf = raw.count(b"\n") - crlf
        check("启动.bat 用 CRLF 换行（裸 LF 会让 .bat 解析出错）",
              crlf > 0 and lone_lf == 0, f"CRLF={crlf} 裸LF={lone_lf}")

        check("启动.bat 没有 UTF-8 BOM", raw[:3] != b"\xef\xbb\xbf")

        text = raw.decode("ascii", errors="replace")
        check("启动.bat 里有找不到 Python 的提示",
              "python.org" in text.lower())
        check("启动.bat 会检查 python 是不是 Store 假货",
              "import sys" in text)

    # 启动.bat 里绝对不能出现中文：cmd.exe 按控制台代码页逐字节解析，
    # 中文会让它把字符拆错，报出一堆 "xxx is not recognized"。
    # 所有中文提示都由 Python 输出（Python 读自己的 UTF-8 源文件，天然正确）。
    for rel in ("config.toml", "README.md", ".vscode/launch.json",
                "radio/config.py", "app.py", "radio/tts/zipvoice.py"):
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            path.read_bytes().decode("utf-8")
            check(f"{rel} 是合法 UTF-8", True)
        except UnicodeDecodeError as exc:
            check(f"{rel} 是合法 UTF-8", False, str(exc))


# ---------------------------------------------------------------------------


async def main() -> int:
    print("=" * 62)
    print(" AI 电台 · 单元测试")
    print("=" * 62)
    await test_llm()
    test_audio()
    test_persona()
    test_text()
    test_config()
    test_bilibili_protocol()
    await test_zipvoice()
    test_launcher_files()

    print("\n" + "=" * 62)
    if skips:
        print(f"跳过 {len(skips)} 项（多为还没下载模型，不是问题）：")
        for item in skips:
            print(f"  ○ {item}")
        print()
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print(f"  ✗ {item}")
        return 1
    print("单元测试全部通过 ✓" + (f"（{len(skips)} 项已跳过）" if skips else ""))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
