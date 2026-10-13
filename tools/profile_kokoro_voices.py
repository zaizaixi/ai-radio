"""给 Kokoro 的每个中文音色做一次「体检」，量出音高和明亮度。

为什么需要：
    Kokoro 的音色名字是 zm_009 / zf_042 这种编号，光看名字不知道是什么声音。
    用户想「输入关键词找音色」，就必须先知道每个音色到底低沉还是清亮。
    机器听不了，但可以量：
      * 基频（F0）      —— 越低越低沉
      * 频谱重心         —— 越低越浑厚，越高越清亮

    量完之后写进 radio/tts/kokoro_voices.json，之后按关键词找音色就有依据了。

用法：
    python tools/profile_kokoro_voices.py            # 全部中文音色
    python tools/profile_kokoro_voices.py --limit 8  # 只测前 8 个（试跑）
    python tools/profile_kokoro_voices.py --force    # 重测已经测过的
"""

from __future__ import annotations

import argparse
import array
import cmath
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radio.tts.base import VoiceParams                      # noqa: E402
from radio.tts.kokoro import (KOKORO_VOICES, PROFILE_FILE,  # noqa: E402
                              KokoroTTS, load_profiles)
from radio.voices import VoiceKit                           # noqa: E402

# 用一句话当「试音文本」。选中性内容，避免语调影响基频测量。
PROBE_TEXT = "这里是深夜电台，欢迎收听今晚的节目。"


# ---------------------------------------------------------------------------
# 纯 Python 的 radix-2 FFT
# ---------------------------------------------------------------------------


def _fft(values: list[complex]) -> list[complex]:
    n = len(values)
    if n <= 1:
        return values
    even = _fft(values[0::2])
    odd = _fft(values[1::2])
    out = [0j] * n
    for k in range(n // 2):
        t = cmath.exp(-2j * math.pi * k / n) * odd[k]
        out[k] = even[k] + t
        out[k + n // 2] = even[k] - t
    return out


# ---------------------------------------------------------------------------
# 测量
# ---------------------------------------------------------------------------


def estimate_pitch(samples: array.array, rate: int,
                   frame: int = 1024, hop: int = 512,
                   fmin: float = 60.0, fmax: float = 400.0) -> float:
    """自相关法估基频，返回所有「有声帧」的中位数。

    自相关比过零率靠谱得多：它能扛住共振峰和噪声。
    """
    lag_min = max(2, int(rate / fmax))
    lag_max = min(frame - 1, int(rate / fmin))

    pitches: list[float] = []
    for start in range(0, len(samples) - frame, hop):
        chunk = samples[start:start + frame]
        energy = sum(v * v for v in chunk) / frame
        if energy < 200 * 200:
            continue                      # 太安静，跳过

        mean = sum(chunk) / frame
        centered = [v - mean for v in chunk]
        # 只算需要的 lag 范围，省时间
        best_lag, best_score = 0, 0.0
        zero_lag = sum(v * v for v in centered) or 1
        for lag in range(lag_min, lag_max):
            score = 0
            for index in range(0, frame - lag, 4):   # 每 4 个采一次，够用
                score += centered[index] * centered[index + lag]
            score = score / (zero_lag / 4)
            if score > best_score:
                best_score, best_lag = score, lag
        if best_lag and best_score > 0.28:
            pitches.append(rate / best_lag)

    if not pitches:
        return 0.0
    # 去掉离群值再取中位数
    median = statistics.median(pitches)
    kept = [p for p in pitches if abs(p - median) < median * 0.35]
    return statistics.median(kept) if kept else median


def spectral_centroid(samples: array.array, rate: int,
                      frame: int = 1024, hop: int = 512) -> float:
    """频谱重心：越高说明高频成分越多，听感越「清亮」。"""
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (frame - 1))
              for i in range(frame)]
    centroids: list[float] = []
    for start in range(0, len(samples) - frame, hop * 4):
        chunk = samples[start:start + frame]
        if sum(v * v for v in chunk) / frame < 200 * 200:
            continue
        spec = _fft([complex(chunk[i] * window[i], 0) for i in range(frame)])
        total = 0.0
        weighted = 0.0
        for k in range(1, frame // 2):
            magnitude = abs(spec[k])
            freq = k * rate / frame
            total += magnitude
            weighted += magnitude * freq
        if total > 0:
            centroids.append(weighted / total)
    return statistics.median(centroids) if centroids else 0.0


def speaking_rate(samples: array.array, rate: int, chars: int) -> float:
    """每秒说几个字（粗略：按有声段占比估）。"""
    frame, hop = 1024, 512
    voiced = 0
    total = 0
    for start in range(0, len(samples) - frame, hop):
        chunk = samples[start:start + frame]
        total += 1
        if sum(v * v for v in chunk) / frame > 300 * 300:
            voiced += 1
    duration = len(samples) / rate
    if duration <= 0 or total == 0:
        return 0.0
    return chars * (voiced / total) / duration


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="测量 Kokoro 音色特征")
    parser.add_argument("--limit", type=int, default=0, help="只测前 N 个")
    parser.add_argument("--force", action="store_true", help="重测已测过的")
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING)

    kit = VoiceKit(ROOT / "sherpa", ROOT / "models", ROOT / "voices")
    if not kit.kokoro_ready():
        print("Kokoro 模型没装好，先跑 python run.py setup")
        return 1

    existing = 0
    if PROFILE_FILE.exists():
        try:
            data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
            existing = len(data.get("voices") or {})
        except Exception:
            existing = 0
    load_profiles()
    print(f"已有 {existing} 条记录")

    targets = [v for v in KOKORO_VOICES if v.region == "中文"]
    if not args.force:
        targets = [v for v in targets if not (v.pitch > 0)]
    if args.limit:
        targets = targets[:args.limit]

    if not targets:
        print("都测过了，不用再测（要重测加 --force）")
        return 0

    print(f"要测 {len(targets)} 个中文音色，每个约 3~5 秒…\n")
    engine = KokoroTTS(kit, sid=targets[0].sid, num_threads=args.threads)
    engine._ensure_ready()

    result: dict = {}
    if PROFILE_FILE.exists():
        try:
            result = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        except Exception:
            result = {}
    voices = result.setdefault("voices", {})

    started = time.time()
    ok = 0
    for index, voice in enumerate(targets, 1):
        chunks: list[bytes] = []
        try:
            engine.synthesize_stream(PROBE_TEXT, VoiceParams(voice=voice.name),
                                     lambda pcm: (chunks.append(pcm), True)[1])
        except Exception as exc:
            print(f"  [{index:3d}/{len(targets)}] {voice.name:8s} 合成失败：{exc}")
            continue
        pcm = b"".join(chunks)
        samples = array.array("h")
        samples.frombytes(pcm)
        if len(samples) < 2400:
            print(f"  [{index:3d}/{len(targets)}] {voice.name:8s} 音频太短，跳过")
            continue

        pitch = estimate_pitch(samples, engine._sample_rate)
        bright = spectral_centroid(samples, engine._sample_rate)
        rate = speaking_rate(samples, engine._sample_rate, len(PROBE_TEXT))

        voices[voice.name] = {
            "sid": voice.sid,
            "gender": voice.gender,
            "pitch": round(pitch, 1),
            "brightness": round(bright, 1),
            "speed": round(rate, 2),
            "duration": round(len(samples) / engine._sample_rate, 2),
        }
        ok += 1
        elapsed = time.time() - started
        eta = elapsed / index * (len(targets) - index)
        print(f"  [{index:3d}/{len(targets)}] {voice.name:8s} "
              f"音高 {pitch:6.1f}Hz  明亮 {bright:6.0f}Hz  语速 {rate:4.2f}字/秒  "
              f"剩余约 {eta/60:.1f} 分钟")

        if index % 10 == 0:
            PROFILE_FILE.write_text(
                json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    result["probe_text"] = PROBE_TEXT
    result["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    PROFILE_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    engine.close()

    print(f"\n完成 {ok}/{len(targets)} 个，共花 {(time.time()-started)/60:.1f} 分钟")
    print(f"写入 {PROFILE_FILE}")

    # 按音高排一下，方便肉眼确认测出来的是不是有意义
    rows = sorted(voices.items(), key=lambda kv: kv[1].get("pitch", 0))
    print("\n最低沉的 5 个：")
    for name, item in rows[:5]:
        print(f"  {name:8s} {item['pitch']:6.1f}Hz  明亮 {item['brightness']:.0f}Hz")
    print("最清亮的 5 个：")
    for name, item in rows[-5:]:
        print(f"  {name:8s} {item['pitch']:6.1f}Hz  明亮 {item['brightness']:.0f}Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
