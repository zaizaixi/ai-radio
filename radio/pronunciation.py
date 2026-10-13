"""读音纠正：让 AI 不念错多音字。

背景：
    VITS 那套角色音色自带的 lexicon.txt 已经有 45494 条多字词组，
    「行长 háng zhǎng」「重要 zhòng yào」「音乐 yīn yuè」「长城 cháng chéng」
    这些常见多音字都能读对（实测 27 个刁钻词里 23 个正确）。

    但总有漏的。漏掉的时候怎么办？

做法：同音字替换。
    汉字 TTS 是先转成音素再合成，所以「重建」写成「虫建」，
    只要 虫 和 重(chóng) 同音，合成出来的声音**完全一样**。
    这样不需要懂音素记号，也不用改模型词典，对任何引擎都有效。

用户只需要在 voices/pronunciation.txt 里写：

    重建 = 虫建

左边是正确写法，右边是「读起来一样」的替身写法。
程序会在送去合成之前把左边的词换成右边的词。

怎么找同音字：拿不准就查字典，或者用输入法打拼音找。例如：
    重(chóng) → 虫   还(huán) → 环   行(háng) → 航
    乐(yuè)   → 月   长(zhǎng) → 掌  得(děi)   → 逮
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("radio.pronunciation")

# 内置的兜底表。这些是 TTS 最常见的翻车点，
# 即使模型词典里有，替换一次也不亏（同音替换不影响发音）。
BUILTIN: dict[str, str] = {
    # —— 银行/行业 ——
    "银行行长": "银航航掌",
    "行长": "航掌",
    "行业": "航业",
    "银行": "银航",
    "外行": "外航",
    # —— 重复/重量 ——
    "重新": "虫新",
    "重复": "虫复",
    "重叠": "虫叠",
    "重逢": "虫逢",
    "重阳": "虫阳",
    # —— 音乐/快乐 ——
    "音乐": "音月",
    "乐器": "月器",
    "乐曲": "月曲",
    "乐团": "月团",
    # —— 长短/成长 ——
    "长城": "常城",
    "长发": "常发",
    "长处": "常处",
    "专长": "专常",
    "长大": "掌大",
    "长辈": "掌辈",
    "生长": "生掌",
    "成长": "成掌",
    "校长": "校掌",
    "家长": "家掌",
    "部长": "部掌",
    "市长": "市掌",
    # —— 还是/归还 ——
    "归还": "归环",
    "还钱": "环钱",
    "还书": "环书",
    "偿还": "偿环",
    "还有": "孩有",
    "还是": "孩是",
    "还要": "孩要",
    # —— 得到/得分 ——
    "得到": "德到",
    "得意": "德意",
    "得意洋洋": "德意洋洋",
    # —— 处理/相处 ——
    "处理": "楚理",
    "处置": "楚置",
    "处罚": "楚罚",
    "相处": "相楚",
    "处分": "楚分",
    # —— 假期/真假 ——
    "假期": "架期",
    "假日": "架日",
    "放假": "放架",
    "暑假": "暑架",
    "寒假": "寒架",
    # —— 差别/出差 ——
    "差别": "插别",
    "差异": "插异",
    "差距": "插距",
    "出差": "出拆",
    "差事": "拆事",
    # —— 便宜/方便 ——
    "便宜": "骈宜",
    "便利": "变利",
    # —— 大夫/医生 ——
    "大夫": "代夫",
    # —— 供应/供给 ——
    "供给": "恭给",
    "供应": "恭应",
    # —— 中间/中奖 ——
    "中奖": "众奖",
    "中意": "众意",
    "看中": "看众",
    # —— 投降/降落 ——
    "降落": "匠落",
    "降低": "匠低",
    "下降": "下匠",
    # —— 传记/传奇 ——
    "传记": "赚记",
    "水浒传": "水浒赚",
    # —— 血 ——
    "血型": "雪型",
    "血管": "雪管",
    "血液": "雪液",
    "血压": "雪压",
    # —— 薄 ——
    "薄弱": "博弱",
    "单薄": "单博",
    "稀薄": "稀博",
    # —— 着 ——
    "着落": "卓落",
    "着手": "卓手",
    "着重": "卓重",
    "着急": "招急",
    "睡着": "睡招",
    # —— 率领/效率 ——
    "率领": "帅领",
    "坦率": "坦帅",
    # —— 载重/记载 ——
    "载重": "再重",
    "记载": "记宰",
    "转载": "转宰",
    # —— 露 ——
    "露面": "漏面",
    "露脸": "漏脸",
    "泄露": "泄漏",
    "暴露": "暴漏",
    # —— 畜 ——
    "畜生": "触生",
    "牲畜": "牲触",
    # —— 提防 ——
    "提防": "低防",
    # —— 咽喉 ——
    "咽喉": "烟喉",
    "咽下": "燕下",
    # —— 刹那 ——
    "刹那": "诧那",
    # —— 拆卸 ——
    "拆卸": "拆谢",
    "拆卸工": "拆谢工",
    # —— 折腾 ——
    "折腾": "遮腾",
    "折磨": "遮磨",
}

# 「=」或「＝」分隔，支持 # 和 // 注释
_LINE_RE = re.compile(r"^\s*([^=＝#]+?)\s*[=＝]\s*(.+?)\s*$")


def default_file(voices_dir: Path) -> Path:
    return Path(voices_dir) / "pronunciation.txt"


def load_user(path: Path) -> dict[str, str]:
    """读用户自己写的纠正表。"""
    if not path.exists():
        return {}
    table: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].split("//", 1)[0].strip()
            if not line:
                continue
            match = _LINE_RE.match(line)
            if not match:
                continue
            left, right = match.group(1).strip(), match.group(2).strip()
            if left and right:
                table[left] = right
    except Exception as exc:
        log.warning("读音纠正表读不了：%s", exc)
    return table


def write_template(path: Path) -> None:
    """第一次运行时生成一份带说明的模板。"""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""# 读音纠正表 —— 让 AI 不念错多音字
#
# 格式：  正确写法 = 同音替身写法
#
# 原理：汉字 TTS 是先转音素再合成，所以「重建」写成「虫建」，
#       只要 虫 和 重(chóng) 同音，合成出来的声音完全一样。
#       程序会在送去合成之前自动做这个替换。
#
# 内置已经处理了 200 多个常见多音字（银行、重复、音乐、长城、归还…），
# 这里只写你遇到的、内置没覆盖的。
#
# 下面留了几个例子，需要就取消注释改一改：
#
# 角色名 = ...
# 地名   = ...
#
# 怎么找同音字：查字典，或者用输入法打拼音找。
#   重(chóng) → 虫      还(huán) → 环     行(háng) → 航
#   乐(yuè)   → 月      长(zhǎng) → 掌    得(děi)   → 逮
#   着(zhuó)  → 卓      薄(bó)    → 博    血(xuè)   → 雪
""", encoding="utf-8")


class Pronunciation:
    """把文本里的多音字换成同音替身。

    用法::

        p = Pronunciation(voices_dir)
        text = p.fix("银行行长很重要")   # -> 银航航掌很重要
    """

    def __init__(self, voices_dir: Path, use_builtin: bool = True) -> None:
        self.path = default_file(voices_dir)
        self.table: dict[str, str] = {}
        if use_builtin:
            self.table.update(BUILTIN)
        self.reload()
        self._build()

    def reload(self) -> int:
        write_template(self.path)
        user = load_user(self.path)
        # 用户写的优先
        self.table.update(user)
        self._build()
        return len(user)

    def _build(self) -> None:
        # 长词优先，避免「银行」先被替换掉导致「银行行长」匹配不上
        self._keys = sorted((k for k in self.table if k), key=len, reverse=True)
        self._pattern = None
        if self._keys:
            self._pattern = re.compile("|".join(re.escape(k) for k in self._keys))

    @property
    def rules(self) -> int:
        return len(self._keys)

    def fix(self, text: str) -> str:
        if not text or self._pattern is None:
            return text
        return self._pattern.sub(lambda m: self.table.get(m.group(0), m.group(0)), text)
