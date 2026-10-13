"""验证音频播放链路：手工造一个正弦波 WAV，用真实的 waveOut 播出来。

这段代码不依赖任何 TTS，所以能在没有语音引擎的机器上单独验证播放器。
"""
import io
import math
import struct
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radio.audio import WavPlayer, apply_volume, list_devices, make_player, parse_wav  # noqa: E402


def make_tone(freq=440.0, seconds=1.0, rate=24000, amplitude=0.35) -> bytes:
    frames = bytearray()
    total = int(rate * seconds)
    for i in range(total):
        value = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", value)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def main() -> int:
    print("音频设备：")
    for d in list_devices():
        print(f"  [{d.index}] {d.name}")

    data = make_tone(440.0, 1.0)
    wav = parse_wav(data)
    print(f"\n测试音频: 440Hz 正弦波, {len(data)} 字节, {wav.duration:.2f} 秒, "
          f"{wav.sample_rate}Hz {wav.channels}ch")

    player = make_player(volume=100)
    print(f"播放器后端: {player.backend}")

    if not isinstance(player, WavPlayer):
        print("RESULT: 没有使用 winmm 后端")
        return 1

    # 用外部 stop 事件打断，验证打断功能
    import threading
    stop_check = threading.Event()

    print("\n[1] 完整播放 1 秒正弦波（应该能听到一声嘟）……")
    t0 = time.monotonic()
    ok = player.play(data, 100, None)
    elapsed = time.monotonic() - t0
    print(f"    play() 返回 {ok}，耗时 {elapsed:.2f} 秒")
    full_ok = ok and elapsed > 0.9

    print("\n[2] 测试打断：播 0.3 秒后强制停止……")
    threading.Timer(0.3, stop_check.set).start()
    t0 = time.monotonic()
    ok2 = player.play(data, 100, stop_check)
    elapsed2 = time.monotonic() - t0
    print(f"    play() 返回 {ok2}，耗时 {elapsed2:.2f} 秒")
    interrupt_ok = (ok2 is False) and elapsed2 < 0.7

    print("\n[3] 测试音量缩放后播放（30%）……")
    quiet = apply_volume(parse_wav(data).pcm, 30)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(quiet)
    ok3 = player.play(buf.getvalue(), 100, None)
    print(f"    play() 返回 {ok3}")

    print("\n" + "=" * 50)
    print(f"完整播放   : {'OK' if full_ok else '失败'}")
    print(f"打断功能   : {'OK' if interrupt_ok else '失败'}")
    print(f"音量后播放 : {'OK' if ok3 else '失败'}")
    if full_ok and interrupt_ok and ok3:
        print("RESULT: AUDIO_OK")
        return 0
    print("RESULT: AUDIO_FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
