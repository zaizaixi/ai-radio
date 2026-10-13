"""标点感知的语气控制。

要解决的问题：
    同一句话，写成「好。」「好！」「好？」「好……」应该是四种完全不同的念法。
    但 TTS 只拿到一串字，它不知道标点代表什么。

做法：
    按标点把文本切成「语气单元」，每个单元根据它的收尾标点，
    调整合成参数（语速、起伏、停顿），念完再补一段对应长度的静音。

    这些参数是 VITS 上真实可调的：
        speed        语速     —— 激动时快，迟疑时慢
        noise_scale  语调起伏 —— 感叹时大，叙述时小
        silence      句后停顿 —— 省略号最长，逗号最短

    不是神经网络的情绪建模，但「标点变了念法就变」这件事是真的。

关于「让 AI 理解标点」：
    规则的映射解决不了语义歧义（「你可真行」是夸还是损？）。
    radio/delivery.py 的结构留了 LLM 标注的接口（见 annotate_with_llm 的说明），
    需要时可以把每个语气单元交给大模型判断情绪再映射。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 标点 → 念法
# ---------------------------------------------------------------------------


@dataclass
class Delivery:
    """一种念法。

    三个参数里，只有这三个是**能逐句改**的：
        speed    语速     —— 合成时传，引擎每次调用都吃
        gain_db  音量增益 —— 在播放层缩放 PCM 实现
        pause    句后停顿 —— 直接写一段静音进声卡

    注意：语调起伏（noise_scale）是**模型级**参数，建模型时就定死了，
    没法一句一变。想要真正逐句变语调，得换模型，不是调参数能解决的。
    """

    key: str
    label: str
    speed: float = 1.0          # 语速倍数
    gain_db: float = 0.0        # 音量增益（分贝）
    pause: float = 0.20         # 念完之后的停顿秒数

    def describe(self) -> str:
        bits = []
        if abs(self.speed - 1.0) > 0.02:
            bits.append(f"{'快' if self.speed > 1 else '慢'}{abs(self.speed-1)*100:.0f}%")
        if abs(self.gain_db) > 0.3:
            bits.append(f"{'响' if self.gain_db > 0 else '轻'}{abs(self.gain_db):.0f}dB")
        bits.append(f"停{self.pause:.1f}秒")
        return " · ".join(bits)


# 收尾标点 → 念法
BY_PUNCT: dict[str, Delivery] = {
    "。": Delivery("calm", "陈述", speed=1.00, gain_db=0.0, pause=0.22),
    ".": Delivery("calm", "陈述", speed=1.00, gain_db=0.0, pause=0.22),
    "！": Delivery("excited", "感叹", speed=1.07, gain_db=+2.5, pause=0.16),
    "!": Delivery("excited", "感叹", speed=1.07, gain_db=+2.5, pause=0.16),
    "？": Delivery("question", "疑问", speed=0.97, gain_db=+1.0, pause=0.28),
    "?": Delivery("question", "疑问", speed=0.97, gain_db=+1.0, pause=0.28),
    "……": Delivery("trailing", "拖长", speed=0.90, gain_db=-2.0, pause=0.55),
    "…": Delivery("trailing", "拖长", speed=0.90, gain_db=-2.0, pause=0.55),
    "——": Delivery("emphatic", "强调", speed=0.95, gain_db=+1.8, pause=0.32),
    "—": Delivery("emphatic", "强调", speed=0.95, gain_db=+1.8, pause=0.32),
    "；": Delivery("semicolon", "分句", speed=1.00, gain_db=0.0, pause=0.26),
    ";": Delivery("semicolon", "分句", speed=1.00, gain_db=0.0, pause=0.26),
    "：": Delivery("colon", "提示", speed=0.98, gain_db=+0.5, pause=0.30),
    ":": Delivery("colon", "提示", speed=0.98, gain_db=+0.5, pause=0.30),
    "，": Delivery("comma", "顿一下", speed=1.00, gain_db=0.0, pause=0.13),
    ",": Delivery("comma", "顿一下", speed=1.00, gain_db=0.0, pause=0.13),
    "、": Delivery("comma", "顿一下", speed=1.00, gain_db=0.0, pause=0.10),
}

# 没有标点时用哪个
DEFAULT = BY_PUNCT["。"]

# 长标点优先匹配（…… 要在 … 之前）
_SORTED = sorted(BY_PUNCT, key=len, reverse=True)
_END_RE = re.compile("|".join(re.escape(p) for p in _SORTED))


@dataclass
class Unit:
    """一个语气单元：文本 + 用什么念 + 念完停多久。"""

    text: str
    delivery: Delivery
    ending: str = ""

    def __str__(self) -> str:
        return f"{self.text}{self.ending}"


def split_units(text: str, max_chars: int = 0) -> list[Unit]:
    """按标点切成语气单元。

    和 split_for_tts 的区别：那个只关心「别太长」，
    这个关心「这句话该怎么念」，所以会保留收尾标点并且不合并不同语气的句子。
    """
    text = (text or "").strip()
    if not text:
        return []

    units: list[Unit] = []
    buffer = ""
    index = 0
    while index < len(text):
        match = _END_RE.match(text, index)
        if match:
            punct = match.group(0)
            index += len(punct)
            # 连续的标点（如「？！」）归到同一个单元
            while index < len(text):
                more = _END_RE.match(text, index)
                if not more:
                    break
                punct += more.group(0)
                index += len(more)
            body = buffer.strip()
            buffer = ""
            if body:
                delivery = BY_PUNCT.get(punct[0], DEFAULT)
                if punct in BY_PUNCT:
                    delivery = BY_PUNCT[punct]
                units.append(Unit(body, delivery, punct))
            continue
        buffer += text[index]
        index += 1

    tail = buffer.strip()
    if tail:
        units.append(Unit(tail, DEFAULT, ""))

    # 可选：把过长的单元再按字数切开（保留原来的念法）
    if max_chars and max_chars > 0:
        out: list[Unit] = []
        for unit in units:
            if len(unit.text) <= max_chars:
                out.append(unit)
                continue
            # 找中间的逗号切
            pieces = _split_long(unit.text, max_chars)
            for i, piece in enumerate(pieces):
                last = i == len(pieces) - 1
                out.append(Unit(piece, unit.delivery if last else BY_PUNCT["，"],
                                unit.ending if last else ""))
        units = out

    return [u for u in units if u.text]


def _split_long(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    rest = text
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = max(window.rfind(p) for p in "，,、；;")
        if cut <= max_chars * 0.4:
            cut = max_chars
        else:
            cut += 1
        pieces.append(rest[:cut].strip())
        rest = rest[cut:]
    if rest.strip():
        pieces.append(rest.strip())
    return pieces


def summarize(units: list[Unit]) -> str:
    """给界面用的一句话概括。"""
    if not units:
        return "没有内容"
    kinds: dict[str, int] = {}
    for unit in units:
        kinds[unit.delivery.label] = kinds.get(unit.delivery.label, 0) + 1
    parts = [f"{label}×{count}" for label, count in kinds.items()]
    total_pause = sum(u.delivery.pause for u in units)
    return f"{len(units)} 个语气单元（{'、'.join(parts)}），停顿合计 {total_pause:.1f} 秒"


# 停顿的基准值，强度为 0 时回落到这里
_NEUTRAL_PAUSE = 0.18


def scale(units: list[Unit], strength: float) -> list[Unit]:
    """把语气差异放大或压小。

    strength = 0    全部拉平 —— 不管什么标点都一个调
    strength = 1    默认（标点该有的差异）
    strength = 2    夸张 —— 感叹更冲、省略号拖更久

    只缩放「相对基准的差异」，不改基准本身：
    语速永远是围绕 1.0 收放，停顿永远是围绕 _NEUTRAL_PAUSE 收放。
    """
    strength = max(0.0, min(2.5, float(strength)))
    if abs(strength - 1.0) < 0.01:
        return units

    out: list[Unit] = []
    for unit in units:
        d = unit.delivery
        out.append(Unit(
            unit.text,
            Delivery(
                key=d.key,
                label=d.label,
                speed=1.0 + (d.speed - 1.0) * strength,
                gain_db=d.gain_db * strength,
                pause=_NEUTRAL_PAUSE + (d.pause - _NEUTRAL_PAUSE) * strength,
            ),
            unit.ending,
        ))
    return out


def preview_table(strength: float = 1.0) -> list[tuple[str, str, str]]:
    """给界面看的对照表：标点 → 念法 → 参数。"""
    rows = []
    seen = set()
    for punct, d in BY_PUNCT.items():
        if d.key in seen and punct not in ("。", "！", "？"):
            continue
        seen.add(d.key)
        scaled = scale([Unit("x", d, punct)], strength)[0].delivery
        rows.append((punct, scaled.label, scaled.describe()))
    order = ["。", "，", "！", "？", "……", "——", "；", "："]
    rows.sort(key=lambda r: order.index(r[0]) if r[0] in order else 99)
    return rows


# ---------------------------------------------------------------------------
# 和 LLM 结合（预留）
# ---------------------------------------------------------------------------


ANNOTATION_PROMPT = """你是配音导演。下面是一段要念出来的稿子，请逐句判断该用什么语气念。

可选的语气只有这几种：
{labels}

只输出 JSON 数组，每项形如 {{"text": "这一句原文", "tone": "语气"}}，不要解释。

稿子：
{text}"""


def annotation_prompt(text: str) -> str:
    """如果要让大模型来判断语气，用这个提示词。

    规则映射解决不了语义歧义 ——「你可真行」到底是夸还是损，
    得看上下文。所以留了这个接口：把每个语气单元丢给大模型，
    它返回语气标签，再套用 BY_PUNCT 里对应的参数。

    目前没有默认启用：多一次 API 调用意味着多一秒延迟，
    对直播念稿来说不一定划算。需要时在 config.toml 里开。
    """
    labels = "、".join(f"{d.label}（{k}）" for k, d in sorted(BY_PUNCT.items())[:6])
    return ANNOTATION_PROMPT.format(labels=labels, text=text)


def apply_annotation(units: list[Unit], tones: dict[str, str]) -> list[Unit]:
    """把大模型给的语气标签套回语气单元上。

    tones 形如 {"这一句原文": "感叹"}。
    """
    by_label = {d.label: d for d in BY_PUNCT.values()}
    for unit in units:
        label = tones.get(unit.text)
        if label and label in by_label:
            unit.delivery = by_label[label]
    return units
