"""音色微调：给克隆参考音频做变调、明暗调节。

用途：
    克隆出来的声音 = 参考音频的声音。所以想改音色，改参考音频就行。
    比如「邓超但要更低沉」→ 把参考降 3 个半音再喂给模型。

为什么用重采样变调，而不是相位声码器：
    克隆只关心**音色**，不关心时长。
    -3 个半音只让参考长 19%（固定开销多约 1 秒），代价可以忽略；
    而重采样是纯插值，不会往参考里引入任何伪影。
    相位声码器虽然能保持时长，但会把"相位感"糊进参考里，
    克隆模型会把这个伪影一起学走 —— 得不偿失。

全部是标准库实现（array + math），不依赖 numpy。
"""

from __future__ import annotations

import array
import logging
import math

log = logging.getLogger("radio.dsp")


# ---------------------------------------------------------------------------
# 重采样
# ---------------------------------------------------------------------------


def resample(samples: array.array, ratio: float) -> array.array:
    """线性插值重采样。ratio = 输出长度 / 输入长度。"""
    if ratio <= 0 or abs(ratio - 1.0) < 1e-6:
        return samples
    src_len = len(samples)
    if src_len < 2:
        return samples
    dst_len = max(2, int(round(src_len * ratio)))

    # 变短（升高音调）会引入混叠，先做个简单低通
    if ratio < 0.98:
        samples = _lowpass(samples, max(1, int(1.0 / ratio)))

    out = array.array("h", bytes(dst_len * 2))
    step = src_len / dst_len
    for index in range(dst_len):
        position = index * step
        left = int(position)
        if left >= src_len - 1:
            out[index] = samples[src_len - 1]
            continue
        frac = position - left
        out[index] = int(samples[left] * (1.0 - frac) + samples[left + 1] * frac)
    return out


def _lowpass(samples: array.array, width: int) -> array.array:
    """简单的滑动平均低通，防止重采样时混叠。"""
    if width <= 1:
        return samples
    out = array.array("h", bytes(len(samples) * 2))
    running = 0
    for index in range(len(samples)):
        running += samples[index]
        if index >= width:
            running -= samples[index - width]
        out[index] = running // min(index + 1, width)
    return out


# ---------------------------------------------------------------------------
# 变调
# ---------------------------------------------------------------------------


def change_pitch(samples: array.array, semitones: float) -> array.array:
    """改变音调。

    semitones < 0 = 更低沉（波形拉长，播放时频率降低）
    semitones > 0 = 更尖细

    代价：样本时长会按 1/2^(semitones/12) 变化。
    降 3 个半音 → 长 19%；升 3 个半音 → 短 16%。
    """
    if abs(semitones) < 0.01:
        return samples
    semitones = max(-12.0, min(12.0, float(semitones)))
    ratio = 2.0 ** (semitones / 12.0)
    # 音调降 ratio 倍 → 波形要拉长 1/ratio 倍
    return resample(samples, 1.0 / ratio)


# ---------------------------------------------------------------------------
# 明暗（音色厚度）
# ---------------------------------------------------------------------------


def change_tone(samples: array.array, amount: float) -> array.array:
    """调节明暗。

    amount < 0 = 更浑厚（压高频）
    amount > 0 = 更清亮（提高频）

    做法：把信号分成低频（滑动平均）和高频（原始 - 低频），
    再按比例把高频加回去或减掉。简单但有效，且不改变时长。
    """
    amount = max(-1.0, min(1.0, float(amount)))
    if abs(amount) < 0.02:
        return samples

    width = 3 if amount > 0 else 6
    low = _lowpass(samples, width)
    out = array.array("h", bytes(len(samples) * 2))
    gain = 1.0 + amount * 0.8
    for index in range(len(samples)):
        high = samples[index] - low[index]
        value = low[index] + high * gain
        out[index] = 32767 if value > 32767 else (-32768 if value < -32768 else int(value))
    return out


# ---------------------------------------------------------------------------
# 去底噪 / 去混响尾巴
# ---------------------------------------------------------------------------


def gate(samples: array.array, strength: float = 0.5,
         sample_rate: int = 24000) -> array.array:
    """噪声门：把字与字之间的低电平残余压下去。

    为什么对「回音」有用：
        影视片段、手机录音几乎都带房间混响。克隆模型会把你整段参考音频的
        声学特征一起学走 —— 包括词与词之间拖着的混响尾巴，
        合成出来就带"回音"。把停顿处的电平压掉，模型就没得学了。

    做法：估出噪声底（能量最低那 15% 帧的均值），低于阈值的压下去，
    再做短时间平滑，避免出现生硬的"断气"感。
    """
    if not samples:
        return samples
    strength = max(0.0, min(1.0, float(strength)))
    if strength < 0.02:
        return samples

    frame = max(32, int(sample_rate * 0.02))       # 20ms 一帧
    count = len(samples) // frame
    if count < 4:
        return samples

    energies = []
    for index in range(count):
        start = index * frame
        chunk = samples[start:start + frame]
        energies.append(sum(abs(v) for v in chunk) / frame)

    ordered = sorted(energies)
    floor = ordered[max(0, int(len(ordered) * 0.15))] or 1.0
    threshold = floor * (2.0 + strength * 6.0)
    ceiling = max(threshold * 3.0, 1.0)

    out = array.array("h", bytes(len(samples) * 2))
    gain = 1.0
    attack = 1.0 / max(1.0, sample_rate * 0.005)   # 5ms 打开
    release = 1.0 / max(1.0, sample_rate * 0.040)  # 40ms 关闭：慢一点，不出咔哒声
    for index, value in enumerate(samples):
        energy = energies[min(index // frame, count - 1)]
        if energy >= ceiling:
            target = 1.0
        elif energy <= threshold:
            target = 0.0
        else:
            target = (energy - threshold) / (ceiling - threshold)
        gain += (target - gain) * (attack if target > gain else release)
        out[index] = int(value * gain)
    return out


# ---------------------------------------------------------------------------
# 电平
# ---------------------------------------------------------------------------


def normalize(samples: array.array, target_peak: float = 0.85) -> array.array:
    """把峰值归一到目标值。参考音频太轻或太重都会影响克隆效果。"""
    peak = 0
    for value in samples:
        if abs(value) > peak:
            peak = abs(value)
    if peak < 100:
        return samples                       # 几乎是静音，别放大噪声
    gain = (target_peak * 32767.0) / peak
    if abs(gain - 1.0) < 0.05:
        return samples
    out = array.array("h", bytes(len(samples) * 2))
    for index in range(len(samples)):
        value = samples[index] * gain
        out[index] = 32767 if value > 32767 else (-32768 if value < -32768 else int(value))
    return out


def trim_silence(samples: array.array, threshold: float = 0.02,
                 keep_ms: int = 80, sample_rate: int = 24000) -> array.array:
    """去掉首尾的静音。静音会被模型当成"背景"学进去。"""
    if not samples:
        return samples
    limit = int(threshold * 32767)
    head = 0
    while head < len(samples) and abs(samples[head]) < limit:
        head += 1
    tail = len(samples) - 1
    while tail > head and abs(samples[tail]) < limit:
        tail -= 1
    if head >= tail:
        return samples
    pad = int(sample_rate * keep_ms / 1000)
    head = max(0, head - pad)
    tail = min(len(samples) - 1, tail + pad)
    return samples[head:tail + 1]


# ---------------------------------------------------------------------------


def describe(semitones: float, tone: float) -> str:
    """把参数说成人话。"""
    parts = []
    if semitones <= -5:
        parts.append("很低沉")
    elif semitones <= -1.5:
        parts.append("偏低沉")
    elif semitones < 1.5:
        parts.append("原音高")
    elif semitones < 5:
        parts.append("偏清亮")
    else:
        parts.append("很高")
    if tone <= -0.4:
        parts.append("浑厚")
    elif tone <= -0.12:
        parts.append("稍厚")
    elif tone < 0.12:
        parts.append("中性")
    elif tone < 0.4:
        parts.append("稍亮")
    else:
        parts.append("清亮")
    return " · ".join(parts)


PRESETS: list[tuple[str, float, float, int]] = [
    # (名字, 半音, 明暗, 语速百分比)
    ("原声", 0.0, 0.0, 0),
    ("更低沉", -3.0, -0.25, -6),
    ("电台男声", -4.0, -0.15, -8),
    ("沉稳旁白", -2.5, -0.45, -4),
    ("更年轻", 2.0, 0.30, 4),
    ("更明亮", 1.5, 0.55, 0),
]


def apply_tuning(samples: array.array, semitones: float = 0.0,
                 tone: float = 0.0, denoise: float = 0.0,
                 normalize_peak: float = 0.85,
                 do_trim: bool = True, sample_rate: int = 24000) -> array.array:
    """把一组微调参数作用到参考音频上。

    顺序有讲究：
      1. 先裁掉首尾静音（静音会被当成"背景"学进去）
      2. 噪声门压掉词间的混响尾巴（消除"回音"）
      3. 变调
      4. 调明暗
      5. 最后归一化电平
    """
    out = samples
    if do_trim:
        out = trim_silence(out, sample_rate=sample_rate)
    if denoise > 0.02:
        out = gate(out, denoise, sample_rate=sample_rate)
    if abs(semitones) >= 0.01:
        out = change_pitch(out, semitones)
    if abs(tone) >= 0.02:
        out = change_tone(out, tone)
    out = normalize(out, normalize_peak)
    return out
