"""角色音色目录：日漫 / 国漫 / 游戏配音风格的声音。

这些是 sherpa-onnx 社区基于 pypinyin 词级词典训练的 VITS 中文多说话人模型
（每个模型 804 个说话人编号），模型作者按角色给它们命了名。

为什么换到这一系：
    * 速度：实测 RTF 0.33（Kokoro 是 2.05，ZipVoice 更慢）—— 念稿不再追不上播放
    * 多音字：lexicon.txt 里有 45494 条多字词组，
      「行长 háng zhǎng」「重要 zhòng yào」「音乐 yīn yuè」这类都能读对
    * 自带 new_heteronym.fst 做上下文消歧

诚实说明：
    模型名字是训练者起的标签。我没法用耳朵逐个确认它们听起来真的像那个角色，
    所以下面每个音色都标了出处；你试听之后如果不满意，可以换 sid 或换模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field

_GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"


@dataclass
class CharacterVoice:
    key: str                 # 模型目录名后缀，也是下载用的名字
    name: str                # 中文显示名
    origin: str              # 出处作品
    gender: str              # 男 / 女 / 中性
    tags: list[str] = field(default_factory=list)
    speakers: int = 804
    note: str = ""

    @property
    def archive(self) -> str:
        return f"vits-zh-hf-{self.key}"

    @property
    def url(self) -> str:
        return f"{_GH}/{self.archive}.tar.bz2"

    @property
    def size_mb(self) -> float:
        return 115.0

    @property
    def label(self) -> str:
        return f"{self.name}（{self.origin}·{self.gender}）"

    def keywords(self) -> list[str]:
        return [self.name, self.origin, self.gender, *self.tags]


# 顺序按「当电台主播的合适程度」大致排了一下：先男声主播，再女声，最后特殊音
CHARACTER_VOICES: list[CharacterVoice] = [
    CharacterVoice(
        key="doom", name="毁灭战士", origin="DOOM", gender="男",
        tags=["低沉", "粗犷", "厚重", "硬汉", "燃"],
        note="最厚重的男声，适合硬核游戏解说",
    ),
    CharacterVoice(
        key="zenyatta", name="禅雅塔", origin="守望先锋", gender="男",
        tags=["低沉", "平静", "温和", "磁性", "僧侣"],
        note="沉稳平缓，适合深夜电台",
    ),
    CharacterVoice(
        key="abyssinvoker", name="阿巴瑟", origin="星际争霸", gender="男",
        tags=["怪异", "嘶哑", "非人", "特效"],
        note="特殊音效型，不适合正常念稿",
    ),
    CharacterVoice(
        key="keqing", name="刻晴", origin="原神", gender="女",
        tags=["清冷", "干练", "御姐", "明亮"],
        note="清爽利落，适合资讯播报",
    ),
    CharacterVoice(
        key="eula", name="优菈", origin="原神", gender="女",
        tags=["高冷", "贵族", "优雅", "从容"],
    ),
    CharacterVoice(
        key="bronya", name="布洛妮娅", origin="崩坏3", gender="女",
        tags=["三无", "清冷", "平淡", "少女"],
    ),
    CharacterVoice(
        key="theresa", name="德丽莎", origin="崩坏3", gender="女",
        tags=["萝莉", "元气", "活泼", "可爱"],
    ),
    CharacterVoice(
        key="echo", name="回声", origin="守望先锋", gender="女",
        tags=["机械", "电子", "特效"],
        note="电子音，适合科幻风节目",
    ),
    CharacterVoice(
        key="fanchen-C", name="凡尘·综合", origin="凡尘系列", gender="中性",
        tags=["多风格", "旁白", "通用"],
        note="187 个说话人，风格跨度大",
    ),
    CharacterVoice(
        key="fanchen-wnj", name="凡尘·男声", origin="凡尘系列", gender="男",
        tags=["男声", "旁白", "温和"],
        note="单说话人，稳定",
    ),
    CharacterVoice(
        key="fanchen-unity", name="凡尘·联合", origin="凡尘系列", gender="中性",
        tags=["通用", "旁白"],
    ),
    CharacterVoice(
        key="fanchen-ZhiHuiLaoZhe", name="凡尘·智慧老者", origin="凡尘系列", gender="男",
        tags=["老年", "沧桑", "低沉", "讲故事"],
        note="适合说书、讲故事",
    ),
    CharacterVoice(
        key="fanchen-ZhiHuiLaoZhe_new", name="凡尘·智慧老者（新）", origin="凡尘系列", gender="男",
        tags=["老年", "沧桑", "低沉", "讲故事"],
    ),
]

BY_KEY = {v.key: v for v in CHARACTER_VOICES}

# 试听时优先推荐这几个（其余要么是特殊音、要么风格太偏）
RECOMMENDED = ["zenyatta", "doom", "keqing", "eula", "bronya", "theresa",
               "fanchen-wnj", "fanchen-ZhiHuiLaoZhe"]


def get(key: str) -> CharacterVoice | None:
    """按名字或 key 找音色。"""
    text = (key or "").strip()
    if text in BY_KEY:
        return BY_KEY[text]
    for voice in CHARACTER_VOICES:
        if text in (voice.name, voice.label, voice.archive):
            return voice
    low = text.lower()
    for voice in CHARACTER_VOICES:
        if voice.key.lower() == low:
            return voice
    return None
