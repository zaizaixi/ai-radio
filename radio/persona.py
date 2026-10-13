"""人设系统。

一个人设 = 一个 Markdown 文件。文件正文**完全由你自由撰写**，程序几乎不做
任何加工，直接当作系统提示词交给大模型。

文件开头可以（也可以不）写一个参数块，用来指定音色等机器参数：

    ---
    name: 小夜
    alias: [小夜, 夜夜, 电台姬]
    voice: zh-CN-XiaoxiaoNeural
    rate: +8%
    temperature: 1.15
    max_chars: 140
    append_rules: true
    ---

    你是一位深夜电台主持人……（下面随便你怎么写）

`append_rules = false` 可以彻底关掉程序自动追加的运行规则，整个提示词
就 100% 是你写的内容。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("radio.persona")

_SUPPORTED_SUFFIX = (".md", ".markdown", ".txt")


# ---------------------------------------------------------------------------
# 参数块解析（不依赖 PyYAML，自己写一个够用的）
# ---------------------------------------------------------------------------


def _coerce(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value[0] in "\"'" and value[-1:] == value[0] and len(value) >= 2:
        return value[1:-1]
    low = value.lower()
    if low in ("true", "yes", "on", "是"):
        return True
    if low in ("false", "no", "off", "否"):
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1]
        items: list[Any] = []
        for part in inner.split(","):
            part = part.strip()
            if part:
                items.append(_coerce(part))
        return items
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """把开头的 `--- ... ---` 参数块和正文分开。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() not in ("---", "+++"):
        return {}, text

    meta: dict[str, Any] = {}
    end = None
    for i in range(1, len(lines)):
        stripped = lines[i].strip()
        if stripped in ("---", "+++"):
            end = i
            break
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        meta[key.strip()] = _coerce(value)

    if end is None:
        # 参数块没闭合，当正文处理
        return {}, text
    body = "\n".join(lines[end + 1:]).strip()
    return meta, body


# ---------------------------------------------------------------------------
# 人设
# ---------------------------------------------------------------------------


@dataclass
class Persona:
    key: str                                  # 文件名（不带扩展名），用于切换
    prompt: str                               # ★ 完全自由的提示词正文
    name: str = ""                            # 主持人名字
    alias: list[str] = field(default_factory=list)
    # 机器参数（留空表示沿用 config.toml）
    voice: str = ""
    rate: str = ""
    pitch: str = ""
    volume: str = ""
    temperature: float | None = None
    max_chars: int = 0
    # 是否追加程序自带的运行规则
    append_rules: bool = True
    # 额外自定义字段，原样保留，方便你在人设里加自己的变量
    extra: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    # ------------------------------------------------------------------

    @property
    def display_name(self) -> str:
        return self.name or self.key

    def keywords(self) -> list[str]:
        """用来判断弹幕是不是在叫它。"""
        words = [self.name] + list(self.alias)
        return [w for w in dict.fromkeys(words) if w]

    def system_prompt(self) -> str:
        return self.prompt.strip()


def load_persona(path: Path, key: str | None = None) -> Persona:
    """从 Markdown 文件载入人设。"""
    text = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(text)

    known = {"name", "alias", "voice", "rate", "pitch", "volume",
             "temperature", "max_chars", "append_rules"}
    extra = {k: v for k, v in meta.items() if k not in known}

    alias = meta.get("alias") or []
    if isinstance(alias, str):
        alias = [a.strip() for a in re.split(r"[,，、]", alias) if a.strip()]

    max_chars = meta.get("max_chars") or 0
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = 0

    temperature = meta.get("temperature")
    if temperature is not None:
        try:
            temperature = float(temperature)
        except (TypeError, ValueError):
            temperature = None

    return Persona(
        key=key or path.stem,
        prompt=body,
        name=str(meta.get("name") or path.stem),
        alias=[str(a) for a in alias],
        voice=str(meta.get("voice") or ""),
        rate=str(meta.get("rate") or ""),
        pitch=str(meta.get("pitch") or ""),
        volume=str(meta.get("volume") or ""),
        temperature=temperature,
        max_chars=max_chars,
        append_rules=bool(meta.get("append_rules", True)),
        extra=extra,
        path=path,
    )


def list_persona_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files = [p for p in sorted(directory.iterdir())
             if p.is_file() and p.suffix.lower() in _SUPPORTED_SUFFIX and not p.name.startswith("_")]
    return files


def list_personas(directory: Path) -> list[Persona]:
    out: list[Persona] = []
    for path in list_persona_files(directory):
        try:
            out.append(load_persona(path))
        except Exception as exc:
            log.warning("人设文件 %s 读取失败: %s", path.name, exc)
    return out


class PersonaRegistry:
    """管理当前人设，并支持直播中热切换。"""

    def __init__(self, directory: Path, current_key: str = "") -> None:
        self.directory = directory
        self._cache: dict[str, Persona] = {}
        self.current: Persona | None = None
        self.reload()
        if current_key:
            if not self.switch(current_key):
                if self.current is not None:
                    log.warning("配置里的人设 %r 找不到，改用 %r",
                                current_key, self.current.key)
        elif self.current is None:
            raise FileNotFoundError(
                f"人设目录 {directory} 里没有任何人设文件。\n"
                f"请放进一个 .md 文件（例如 {directory / '深夜电台.md'}）。"
            )

    # ------------------------------------------------------------------

    def reload(self) -> None:
        """重新扫描目录（支持直播中改完文件即时生效）。"""
        self._cache.clear()
        previous_key = self.current.key if self.current else ""
        for path in list_persona_files(self.directory):
            try:
                self._cache[path.stem] = load_persona(path)
            except Exception as exc:
                log.warning("人设文件 %s 读取失败: %s", path.name, exc)
        if not self._cache:
            self.current = None
            return
        if previous_key and previous_key in self._cache:
            self.current = self._cache[previous_key]
        elif self.current is None:
            self.current = next(iter(self._cache.values()))

    def keys(self) -> list[str]:
        return list(self._cache.keys())

    def all(self) -> list[Persona]:
        return list(self._cache.values())

    def get(self, key: str) -> Persona | None:
        return self._cache.get(key)

    def switch(self, key: str) -> bool:
        """切换人设，支持用名字或别名。"""
        if not key:
            return False
        key = key.strip()
        if key in self._cache:
            self.current = self._cache[key]
            return True
        # 允许用主持人名字/别名切
        for persona in self._cache.values():
            if key == persona.name or key in persona.alias:
                self.current = persona
                return True
        # 允许前缀匹配
        matches = [p for k, p in self._cache.items() if k.startswith(key)]
        if len(matches) == 1:
            self.current = matches[0]
            return True
        return False

    def find_by_keyword(self, text: str) -> Persona | None:
        for persona in self._cache.values():
            for word in persona.keywords():
                if word and word in text:
                    return persona
        return None


# ---------------------------------------------------------------------------
# 系统提示词组装
# ---------------------------------------------------------------------------

_RULES_TEMPLATE = """\
--- 以下是直播间运行规则，请严格遵守 ---

【你是谁】
你是一位正在直播的电台节目主持人，名字叫「{name}」。你不是助手、不是客服，
你就是在做一档陪伴类直播节目。

【现场情况】
- 现在时间：{now}
- 主播「{host}」正在玩：{game}
- 你听不到主播和观众的声音，只能读到观众打出来的弹幕文字

【硬性要求】
1. 只输出你准备用嘴说出来的话。绝对不要写旁白、舞台动作、括号里的表情或心理描写，
   也不要写「（笑）」「（停顿）」「*叹气*」这类东西。
2. 不要用 Markdown、不要用列表、不要用表情符号。就是一段自然的口语。
3. 一次说话控制在 {max_chars} 个字左右，长了观众会走神。
4. 直接开口说，不要复述别人说的话，不要说"我作为AI"。
5. 全程用中文，语气自然，像真人电台主持一样有起伏、有停顿感。
"""


def build_system_prompt(
    persona: Persona,
    *,
    host_name: str = "主播",
    game: str = "",
    max_chars: int = 120,
) -> str:
    """把人设正文和运行规则拼成最终的系统提示词。"""
    parts = [persona.system_prompt()]

    if persona.append_rules:
        limit = persona.max_chars or max_chars
        rules = _RULES_TEMPLATE.format(
            name=persona.display_name,
            now=time.strftime("%Y-%m-%d %H:%M %A"),
            host=host_name,
            game=game or "（主播没填，就当是随便玩点什么）",
            max_chars=limit,
        )
        parts.append(rules)

    # 人设里的额外字段原样透传，方便高级玩法
    if persona.extra:
        lines = [f"- {k}: {v}" for k, v in persona.extra.items()]
        parts.append("【附加设定】\n" + "\n".join(lines))

    return "\n\n".join(p for p in parts if p and p.strip())


# ---------------------------------------------------------------------------
# 说话内容的清洗（防止 TTS 念出奇怪的东西）
# ---------------------------------------------------------------------------

_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.S), ""),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),
    (re.compile(r"\*([^*]*)\*"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),
    (re.compile(r"^\s*\d+[.、]\s+", re.M), ""),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"https?://\S+"), ""),
]

# 括号里的动作/旁白：「（笑）」「(停顿)」「【音效】」
_PAREN_PATTERNS = [
    re.compile(r"[（(【\[][^）)】\]]{0,20}[）)】\]]"),
]

# 常见的旁白前缀，如 "小夜：" / "主持人："
_SPEAKER_PREFIX = re.compile(r"^\s*[「『\[]?[\u4e00-\u9fa5A-Za-z0-9_]{1,10}[」』\]]?\s*[:：]\s*")

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u2B00-\u2BFF]"
)


def clean_speech(text: str) -> str:
    """把模型输出清洗成"适合念出来"的纯口语文本。"""
    if not text:
        return ""
    out = text.strip()

    for pattern, repl in _MD_PATTERNS:
        out = pattern.sub(repl, out)
    for pattern in _PAREN_PATTERNS:
        out = pattern.sub("", out)
    out = _EMOJI.sub("", out)
    out = re.sub(r"[ \t]{2,}", " ", out)

    lines = [ln.strip() for ln in out.splitlines()]
    lines = [ln for ln in lines if ln]
    out = " ".join(lines)

    if lines:
        out = _SPEAKER_PREFIX.sub("", out, count=1)

    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([，。！？、；：])", r"\1", out)
    return out.strip()


# ---------------------------------------------------------------------------
# 长句切分：让 TTS 能边合成边播，减少等待
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[。！？!?；;\n]")
_SOFT_BREAK = re.compile(r"[，,、]")


def split_for_tts(text: str, max_chars: int = 90) -> list[str]:
    """把一段话切成适合逐句合成的短句。"""
    text = (text or "").strip()
    if not text:
        return []
    if max_chars <= 0:
        max_chars = 90

    chunks: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if _SENTENCE_END.search(char) and len(buffer) >= max_chars * 0.35:
            chunks.append(buffer.strip())
            buffer = ""
        elif len(buffer) >= max_chars:
            # 没到句号也太长了，找一个逗号断
            cut = -1
            for m in _SOFT_BREAK.finditer(buffer):
                cut = m.end()
            if cut <= 0:
                cut = len(buffer)
            chunks.append(buffer[:cut].strip())
            buffer = buffer[cut:]
    if buffer.strip():
        chunks.append(buffer.strip())

    # 合并过短的碎片
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(merged[-1]) < max_chars * 0.3 and len(merged[-1]) + len(chunk) <= max_chars:
            merged[-1] = merged[-1] + chunk
        else:
            merged.append(chunk)
    return [c for c in merged if c]


def iter_persona_paths(directory: Path) -> Iterator[Path]:
    yield from list_persona_files(directory)


# ---------------------------------------------------------------------------
# 保存人设（供图形界面用）
# ---------------------------------------------------------------------------


def render_persona(persona: Persona) -> str:
    """把人设对象还原成 Markdown 文本（参数块 + 自由正文）。"""
    lines = ["---"]
    if persona.name:
        lines.append(f"name: {persona.name}")
    if persona.alias:
        lines.append("alias: [" + ", ".join(persona.alias) + "]")
    if persona.voice:
        lines.append(f"voice: {persona.voice}")
    if persona.rate:
        lines.append(f"rate: {persona.rate}")
    if persona.pitch:
        lines.append(f"pitch: {persona.pitch}")
    if persona.volume:
        lines.append(f"volume: {persona.volume}")
    if persona.temperature is not None:
        lines.append(f"temperature: {persona.temperature}")
    if persona.max_chars:
        lines.append(f"max_chars: {persona.max_chars}")
    if not persona.append_rules:
        lines.append("append_rules: false")
    for key, value in persona.extra.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(persona.prompt.strip())
    lines.append("")
    return "\n".join(lines)


def save_persona(persona: Persona, path: Path | None = None) -> Path:
    """把人设写回文件（默认写回它原来的路径）。"""
    target = Path(path) if path else persona.path
    if target is None:
        raise ValueError("没有指定人设文件路径")
    target.write_text(render_persona(persona), encoding="utf-8")
    persona.path = target
    persona.key = target.stem
    return target


NEW_PERSONA_TEMPLATE = """\
你是一位直播间的电台主持人，名字叫「{name}」。

## 你是谁

（写清楚你的身份、年纪、做这档节目多久了、为什么做）

## 你的性格

（写你的说话风格、你关心什么、你怎么看事情）

## 你怎么跟观众说话

（被点名的时候怎么回应、有人送礼物怎么谢、聊到什么会来劲）

## 你的语言习惯

- （口头禅、爱用的句式、语速感觉）

## 绝对不能做

- 不要说自己是 AI、模型、程序、助手
- 不要用 Markdown、不要念旁白和括号里的动作
"""


def create_persona_file(directory: Path, key: str, name: str = "") -> Path:
    """新建一个空人设文件（带可用模板）。"""
    key = (key or "").strip()
    if not key:
        raise ValueError("人设名字不能为空")
    if any(ch in key for ch in '\\/:*?"<>|'):
        raise ValueError('人设名字不能包含 \\ / : * ? " < > | 这些字符')
    path = Path(directory) / f"{key}.md"
    if path.exists():
        raise FileExistsError(f"人设「{key}」已经存在了")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NEW_PERSONA_TEMPLATE.format(name=name or key), encoding="utf-8")
    return path
