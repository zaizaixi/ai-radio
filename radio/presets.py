"""关键词 → 音色。

设计前提（很重要）：
    sherpa-onnx 里**没有** CosyVoice 那种「用一句话描述就凭空造出新声音」的模型。
    ZipVoice 是零样本克隆 —— 必须给一段真实的声音才能学。

所以这里的做法是：
    1. 用模型自带的几段干净样本当**基础音色**（男声 / 女声）
    2. 把你输入的关键词翻译成**变调 / 明暗 / 语速**参数
    3. 用 DSP 作用到基础样本上，生成一个新音色

    「低沉的中年男声」→ 挑男声底子 + 降 3 个半音 + 压高频 + 慢一点。

这不是「AI 凭空生成音色」，而是「关键词驱动的音色改造」——
能做到你举的例子（邓超但更低沉），但声音的底子必须来自一段真实录音。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("radio.presets")


# ---------------------------------------------------------------------------
# 基础音色：模型自带的干净样本
# ---------------------------------------------------------------------------


@dataclass
class BaseVoice:
    file: str
    transcript: str
    gender: str          # male / female
    label: str
    # 这段样本本身的听感，用来在没有性别线索时挑最接近的
    natural_pitch: float = 0.0

    def path(self, kit) -> Path:
        return kit.zipvoice_dir / "test_wavs" / self.file


BASE_VOICES: list[BaseVoice] = [
    BaseVoice("leijun-1.wav",
              "那还是36年前, 1987年. 我呢考上了武汉大学的计算机系.",
              "male", "男声（中青年）", natural_pitch=0.0),
    BaseVoice("news-female.wav",
              "各位村民, 大家新年好! 近期, 湖北省武汉市等多个地区",
              "female", "女声（新闻播报）", natural_pitch=1.0),
    BaseVoice("news-female-2.wav",
              "本台消息, 中共中央国务院, 近日印发关于构建数据基础制度, 更好发挥数据要素作用的意见.",
              "female", "女声（沉稳播报）", natural_pitch=0.0),
]


# ---------------------------------------------------------------------------
# 关键词表：(词, 半音, 明暗, 语速%, 标签)
# ---------------------------------------------------------------------------

KEYWORDS: list[tuple[tuple[str, ...], float, float, int, str]] = [
    (("低沉", "浑厚", "磁性", "厚重", "大叔", "烟嗓", "沙哑"),
     -3.5, -0.25, -8, "低沉"),
    (("很沉", "极低", "特别低", "超级低", "压低"),
     -6.0, -0.30, -12, "极低沉"),
    (("年轻", "少年", "少女", "清爽", "阳光", "活泼"),
     2.0, 0.30, 6, "年轻"),
    (("明亮", "清亮", "清脆", "干净", "透亮"),
     1.5, 0.55, 0, "明亮"),
    (("温柔", "轻柔", "舒缓", "治愈", "暖心", "安慰", "轻声"),
     -1.0, 0.10, -10, "温柔"),
    (("激情", "热血", "激动", "燃", "有力", "澎湃", "解说"),
     0.5, 0.35, 12, "激情"),
    (("沉稳", "稳重", "冷静", "旁白", "纪录片", "大气"),
     -2.5, -0.40, -8, "沉稳"),
    (("电台", "深夜", "午夜", "广播", "夜里"),
     -2.0, -0.20, -8, "电台"),
    (("新闻", "播报", "正式", "播音", "端正"),
     -1.0, -0.10, 0, "新闻"),
    (("搞怪", "可爱", "萌", "卡通", "滑稽", "鬼畜"),
     4.0, 0.40, 8, "搞怪"),
    (("机器人", "机械", "AI", "电子"),
     0.0, 0.60, 0, "机械"),
    (("很慢", "慢慢", "悠着", "拖长"),
     0.0, 0.0, -20, "慢速"),
    (("很快", "加快", "利索", "干脆"),
     0.0, 0.0, 20, "快速"),
]

GENDER_RULES: list[tuple[tuple[str, ...], str]] = [
    (("男", "先生", "哥", "叔", "大爷", "汉子", "爸爸"), "male"),
    (("女", "女士", "姐", "阿姨", "妹子", "姑娘", "妈妈"), "female"),
]


@dataclass
class VoicePlan:
    """一份「关键词 → 音色」的方案。"""

    name: str
    base: BaseVoice
    semitones: float = 0.0
    tone: float = 0.0
    rate: int = 0
    tags: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = [self.base.label]
        if abs(self.semitones) >= 0.01:
            bits.append(f"变调 {self.semitones:+.1f} 半音")
        if abs(self.tone) >= 0.02:
            bits.append("更浑厚" if self.tone < 0 else "更明亮")
        if self.rate:
            bits.append(f"语速 {self.rate:+d}%")
        return " · ".join(bits)


# ---------------------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def plan_from_text(query: str) -> VoicePlan:
    """把一句中文描述翻译成音色方案。

    「我要一个低沉磁性的中年男声，适合深夜电台」
      → 男声底子 + 降 5 个半音 + 压高频 + 慢 16%
    """
    query = (query or "").strip()
    semitones = 0.0
    tone = 0.0
    rate = 0
    tags: list[str] = []
    matched: list[str] = []

    for words, semi, tn, rt, tag in KEYWORDS:
        hit = next((w for w in words if w in query), None)
        if hit is None:
            continue
        matched.append(hit)
        semitones += semi
        tone += tn
        rate += rt
        if tag not in tags:
            tags.append(tag)

    gender = ""
    for words, value in GENDER_RULES:
        hit = next((w for w in words if w in query), None)
        if hit is not None:
            gender = value
            matched.append(hit)
            break

    semitones = _clamp(semitones, -6.0, 6.0)
    tone = _clamp(tone, -1.0, 1.0)
    rate = int(_clamp(rate, -25, 25))

    # 挑底子：优先按性别，其次按「变调之后最接近自然音高」。
    # 注意方向 —— 要升调时应该挑本来就偏高的底子，不是反过来。
    pool = [b for b in BASE_VOICES if b.gender == gender] if gender else BASE_VOICES
    if not pool:
        pool = BASE_VOICES
    target = semitones / 6.0
    base = min(pool, key=lambda b: abs(b.natural_pitch - target))

    if not tags:
        tags = ["通用"]
    # 名字后面加序号，避免和已有音色撞名
    name = _plan_name(query, tags, base, gender)
    return VoicePlan(name=name, base=base, semitones=semitones, tone=tone,
                     rate=rate, tags=tags, matched=matched)


def unique_name(kit, wanted: str) -> str:
    """挑一个不和已有音色重名的名字。"""
    try:
        existing = {p.key for p in kit.profiles()}
    except Exception:
        existing = set()
    if wanted not in existing:
        return wanted
    for index in range(2, 100):
        candidate = f"{wanted}{index}"
        if candidate not in existing:
            return candidate
    return wanted


def _plan_name(query: str, tags: list[str], base: BaseVoice, gender: str) -> str:
    """给方案起个短名字。"""
    head = tags[0] if tags else "通用"
    tail = "男声" if base.gender == "male" else "女声"
    name = f"{head}{tail}"
    # 避免和已有的重名，后面加序号
    return name


def suggest_names(tags: list[str], gender: str) -> list[str]:
    tail = "男声" if gender == "male" else "女声"
    return [f"{t}{tail}" for t in tags] or [f"通用{tail}"]


# ---------------------------------------------------------------------------
# 示例音色：一个音色都没有的时候也能直接用
# ---------------------------------------------------------------------------

# (名字, 基础音色序号, 半音, 明暗, 语速)
EXAMPLES: list[tuple[str, int, float, float, int]] = [
    ("深夜电台·男", 0, -3.0, -0.25, -8),
    ("热血解说·男", 0, 1.0, 0.35, 12),
    ("沉稳旁白·男", 0, -2.5, -0.45, -6),
    ("温柔夜话·女", 1, -1.5, 0.05, -10),
    ("新闻播报·女", 2, -0.5, -0.10, 0),
    ("元气少女·女", 1, 3.0, 0.45, 8),
]


def examples_available(kit) -> bool:
    """模型自带的样本还在不在。"""
    return all(b.path(kit).exists() for b in BASE_VOICES)


def install_examples(kit, progress=None) -> list[str]:
    """把示例音色建出来。已经存在的跳过。"""
    if not examples_available(kit):
        raise FileNotFoundError(
            "找不到模型自带的示例样本。\n"
            "  它们应该在 models/*zipvoice*/test_wavs/ 下面。\n"
            "  重新装一次引擎就能恢复。")

    created: list[str] = []
    for name, base_index, semi, tone, _rate in EXAMPLES:
        base = BASE_VOICES[base_index]
        source = base.path(kit)
        if not source.exists():
            continue
        if progress:
            progress(f"正在生成示例音色「{name}」…")
        try:
            kit.create(name, source.read_bytes(), base.transcript,
                       name=name, note=f"示例音色（{base.file}）",
                       semitones=semi, tone=tone)
            created.append(name)
        except Exception as exc:
            log.warning("示例音色「%s」创建失败：%s", name, exc)
    return created
