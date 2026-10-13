"""配置系统：从 config.toml 读取，环境变量可覆盖敏感项。

设计原则：
  * 零第三方依赖 —— 用标准库 tomllib 读 TOML
  * 首次运行自动生成一份带中文注释的 config.toml
  * 所有字段都有合理默认值，配置写错也不会崩
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _get(data: dict, key: str, default: Any) -> Any:
    value = data.get(key, default)
    return default if value is None else value


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    return [value]


def load_dotenv(path: Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """读一个极简的 .env（KEY=VALUE），不覆盖已存在的环境变量。"""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        env[key] = value
        os.environ.setdefault(key, value)
    return env


# ---------------------------------------------------------------------------
# 各配置段
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    persona: str = "深夜电台"
    persona_dir: str = "personas"
    script_dir: str = "scripts"
    log_dir: str = "logs"
    # 克隆引擎的资源目录
    sherpa_dir: str = "sherpa"      # sherpa-onnx 运行时（软件自动下载）
    model_dir: str = "models"       # ZipVoice 模型 + 声码器
    voices_dir: str = "voices"      # 克隆音色（每个人一个文件夹）
    log_level: str = "INFO"
    # 主播在玩什么游戏 —— 会作为上下文喂给 AI，让它能接上话
    game: str = ""
    host_name: str = "主播"


@dataclass
class ChatConfig:
    platform: str = "bilibili"          # bilibili | console
    room_id: int = 0
    cookies: dict[str, str] = field(default_factory=dict)
    ignore_enter: bool = True
    reconnect_delay: float = 5.0
    console_prompt: str = "弹幕> "


@dataclass
class TriggerConfig:
    # 弹幕里出现这些词才算"艾特 AI"
    mention_keywords: list[str] = field(default_factory=list)
    # 留言里必须出现关键词才回复；false = 所有弹幕都可能被挑中回应
    require_mention: bool = True
    # 两条 AI 播报之间最少隔多久（秒）
    min_interval: float = 4.0
    # 同一个人的弹幕多久内不重复回（秒）
    same_user_cooldown: float = 45.0
    # 待回复队列上限，超出丢弃最旧的
    queue_max: int = 6
    # 醒目留言（SC）永远回复
    super_chat_always: bool = True
    # 礼物价值达到多少元时一定回应；0 = 不因礼物触发
    gift_threshold: float = 0.0
    # 忽略自己发的 / 忽略太短或太长的
    min_text_length: int = 1
    max_text_length: int = 120
    # 屏蔽词
    block_words: list[str] = field(default_factory=list)


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 1.1
    max_tokens: int = 400
    timeout: float = 20.0
    stream: bool = True
    # 送进上下文的历史弹幕条数
    history_size: int = 12


@dataclass
class TTSConfig:
    # kokoro   = 内置音色（103 个声音随便挑，不会有克隆的杂音）
    # zipvoice = 本地零样本克隆（用你自己录的样本）
    # none     = 不出声，只打印文字
    provider: str = "vits"
    # 用哪个克隆音色（voices/ 目录下的文件夹名）。人设里写了就以人设为准
    voice: str = "zenyatta:0"
    # 单次合成的最大字数。
    # 每切一段就要重新处理一遍参考音频（固定开销），所以别切太碎：
    # 一般 AI 一次回复在这个字数以内，正好合成一整段、音频连续播出
    max_chars: int = 220
    # 神经网络线程数。实测 8 最优，再多反而变慢
    num_threads: int = 8
    # —— VITS 角色音色的韵律（最接近"情感"的三个旋钮）——
    # noise_scale   语调起伏。0.5 平淡 / 0.667 自然（默认）/ 0.8 起伏大
    # noise_scale_w 音节长短变化。0.8 默认，调高会更"随意"，像真人
    # silence_scale 句子之间的停顿长度。0.2 默认，0.4 会明显停顿
    noise_scale: float = 0.667
    noise_scale_w: float = 0.8
    silence_scale: float = 0.2
    # 标点感知：按 。！？…… 决定每句的语速/音量/停顿
    #   感叹句快 7% 响 2.5dB，省略号慢 10% 轻 2dB 且停 0.55 秒
    punctuation_delivery: bool = True
    # 语气强度：0 = 全拉平（不管什么标点都一个调）
    #          1 = 默认（标点该有的差异）
    #          2 = 夸张（感叹更冲、省略号拖更久）
    punctuation_strength: float = 1.0
    # 多音字纠正：把「银行行长」这类词换成同音替身再合成
    # 规则在 voices/pronunciation.txt，可自己加
    fix_polyphones: bool = True
    # 模型精度："fp32" 音质干净（推荐）；"int8" 小、快，但有量化噪声
    model_quality: str = "fp32"
    # flow-matching 步数：越小越快，但噪声明显变大。
    #   实测频谱平坦度（越低越干净）：4 步 0.0155 / 8 步 0.0089 / 16 步 0.0092
    #   所以默认 8 —— 4 步虽然快一倍，但听起来会有沙沙声
    num_steps: int = 8
    # 分类器无关引导。实测调高反而更脏，保持 1.0（关闭）
    guidance_scale: float = 1.0
    # 参考音频超过这个秒数就在新建音色时提醒（样本越长，每次合成越慢）
    max_reference_seconds: float = 8.0
    # 人设没写语速时的默认值
    rate: str = "+0%"
    # 合成失败时重试次数
    retries: int = 1


@dataclass
class AudioConfig:
    # 播放音量，百分比，0-200。直接改 PCM 采样实现，不动系统音量
    volume: int = 100
    # 输出设备序号；留空或 -1 = 系统默认设备
    device: str = ""
    # 启动时打印可用设备
    list_devices_on_start: bool = False


@dataclass
class IdleConfig:
    enabled: bool = True
    # 安静多少秒之后开始念稿（会在 min_gap ~ max_gap 之间随机）
    # 注意：真实出声还要加上 大模型(~1s) + 合成(样本越长越久) 的时间，
    # 所以实际静默 ≈ 这里的间隔 + 大模型 + 合成，别设太大
    # 注意：这只是「决定开口」的间隔。真正出声还要加上大模型和合成的时间，
    # 实测 8 步 + 8 秒样本时，一段 60 字的稿子从决定开口到出声要 20 秒以上。
    min_gap: float = 6.0
    max_gap: float = 12.0
    # 单次念稿最多多少字。稿子越长，等你听到声音的静默就越久：
    #   60 字  ≈ 12 秒音频
    #   150 字 ≈ 30 秒音频，8 步下光合成就要将近 1 分钟
    # 宁可多念几次、每次短一点。
    max_chars: int = 80
    # True  = 只有"冲 AI 来的"弹幕才会打断念稿节奏（推荐）
    #         普通观众聊天不影响，AI 会像真电台一样持续有输出
    # False = 只要有任何人说话，AI 就让位不说话
    only_mentions_reset: bool = True
    # 稿件文件（相对 script_dir），空 = 用 script_dir 下全部 .md/.txt
    scripts: list[str] = field(default_factory=list)
    shuffle: bool = True
    # 念完一遍后是否重新循环
    loop: bool = True
    # 允许 AI 围绕稿子自由发挥，而不是逐字念
    improvise: bool = True
    # 0.0 = 完全照稿念，1.0 = 完全自由发挥
    improvise_ratio: float = 0.35


@dataclass
class ControlConfig:
    # 本地 HTTP 控制台（浏览器/手机/Stream Deck 都能点）
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8770
    # 允许弹幕指令切换人设
    allow_chat_commands: bool = True
    # 指令前缀，例如 "!人设 热血解说" / "!点歌 ..."
    persona_command: str = "!人设"
    skip_command: str = "!下一条"
    # 只有主播/房管能发指令
    admin_only: bool = True


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    idle: IdleConfig = field(default_factory=IdleConfig)
    control: ControlConfig = field(default_factory=ControlConfig)

    # 配置文件所在目录，用于解析相对路径
    path: Path = field(default=DEFAULT_CONFIG_PATH)

    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self.path.resolve().parent

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)

    @property
    def persona_dir(self) -> Path:
        return self.resolve(self.app.persona_dir)

    @property
    def script_dir(self) -> Path:
        return self.resolve(self.app.script_dir)

    @property
    def log_dir(self) -> Path:
        return self.resolve(self.app.log_dir)

    @property
    def sherpa_dir(self) -> Path:
        return self.resolve(self.app.sherpa_dir)

    @property
    def model_dir(self) -> Path:
        return self.resolve(self.app.model_dir)

    @property
    def voices_dir(self) -> Path:
        return self.resolve(self.app.voices_dir)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def _fill(cls, data: Any):
    """按 dataclass 字段名从 dict 取值填入，忽略未知键，缺失项用默认值。"""
    if not isinstance(data, dict):
        data = {}
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        value = data.get(f.name)
        if value is None:
            continue
        if f.name == "cookies":
            kwargs[f.name] = {str(k): str(v) for k, v in dict(value).items()}
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def read_text_smart(path: Path) -> str:
    """读文本文件，容忍 UTF-8 BOM 和一些老编辑器存的 GBK。

    （中文 Windows 上，记事本以外的编辑器、或者从别处复制粘贴过来的
      config.toml 有可能是 GBK 编码，直接按 UTF-8 读会抛异常。）
    """
    data = path.read_bytes()
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """读取配置。文件不存在时抛出 FileNotFoundError，由调用方决定是否生成。"""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    env_values = load_dotenv(config_path.parent / ".env")

    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = tomllib.loads(read_text_smart(config_path))

    def section(name: str, cls):
        data = raw.get(name) or {}
        return _fill(cls, data)

    cfg = Config(
        app=section("app", AppConfig),
        chat=section("chat", ChatConfig),
        trigger=section("trigger", TriggerConfig),
        llm=section("llm", LLMConfig),
        tts=section("tts", TTSConfig),
        audio=section("audio", AudioConfig),
        idle=section("idle", IdleConfig),
        control=section("control", ControlConfig),
        path=config_path,
    )

    # ---- 环境变量 / .env 覆盖敏感项 ----
    key = (
        os.environ.get("AI_RADIO_API_KEY")
        or os.environ.get(f"{cfg.llm.provider.upper()}_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or env_values.get("DEEPSEEK_API_KEY", "")
    )
    if key:
        cfg.llm.api_key = key

    base = os.environ.get("AI_RADIO_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL")
    if base:
        cfg.llm.base_url = base
    model = os.environ.get("AI_RADIO_MODEL")
    if model:
        cfg.llm.model = model

    room_env = os.environ.get("BILI_ROOM_ID")
    if room_env and room_env.strip().isdigit():
        cfg.chat.room_id = int(room_env.strip())

    return cfg


# ---------------------------------------------------------------------------
# 默认配置模板
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_TEMPLATE = '''\
# =====================================================================
#  AI 电台 · 配置文件
#  改完保存即可，不用动任何 Python 代码
# =====================================================================

[app]
# 用哪个人设 —— 填 personas/ 目录下的文件名（不带扩展名）
persona      = "深夜电台"
persona_dir  = "personas"
script_dir   = "scripts"
log_dir      = "logs"
sherpa_dir   = "sherpa"     # 克隆引擎运行时（软件自动下载）
model_dir    = "models"     # ZipVoice 模型和声码器
voices_dir   = "voices"     # 克隆音色，每个人一个文件夹
log_level    = "INFO"
# 你（主播）的名字，AI 会这样称呼你
host_name    = "主播"
# 你正在玩的游戏，会作为上下文告诉 AI，让它能接上话
# 比如 "艾尔登法环" / "英雄联盟 排位" / "《黑神话：悟空》二周目"
game         = ""


[chat]
# bilibili = 接 B 站直播间弹幕；console = 本地手动敲字调试
platform = "bilibili"

# ★★★ 改成你自己的直播间房间号（短号和真实号都行，也可以直接粘直播间链接）★★★
room_id  = 0

# 是否忽略"XX 进入直播间"这类进场消息（默认忽略，避免刷屏）
ignore_enter    = true
reconnect_delay = 5.0
console_prompt  = "弹幕> "

# 强烈建议填上登录后的 Cookie，弹幕会更全、也不容易被风控拦。
# 浏览器 F12 -> Application -> Cookies -> live.bilibili.com，
# 把 SESSDATA、bili_jct、buvid3、DedeUserID 的值抄到下面。
#
# 注意：Cookie 这一节必须放在 [chat] 所有普通配置的**后面**，
# 否则 TOML 会把后面的键都算进 cookies 表里（这是 TOML 的规矩，不是 bug）。
[chat.cookies]
# SESSDATA   = ""
# bili_jct   = ""
# buvid3     = ""
# DedeUserID = ""


[trigger]
# 弹幕里出现这些词，AI 才认为是在叫它
# 人设文件里的名字和别名会自动加进来，一般不用手写
mention_keywords = ["@AI", "@小夜"]

# true  = 只有被 @ 才回应（推荐，不打断你打游戏）
# false = 随意挑弹幕回应，更热闹但更吵
require_mention = true

min_interval        = 4.0    # 两次开口之间最少隔几秒
same_user_cooldown  = 45.0   # 同一个人多久内不重复回
queue_max           = 6      # 待回复队列上限
super_chat_always   = true   # 醒目留言一定回
gift_threshold      = 0.0    # 礼物满多少元一定回；0 = 不看礼物
min_text_length     = 1
max_text_length     = 120
block_words         = ["广告", "加群"]


[llm]
provider    = "deepseek"
base_url    = "https://api.deepseek.com"
# ★★★ 在这里填你的 DeepSeek API Key，或者写进 .env 的 DEEPSEEK_API_KEY ★★★
api_key     = ""
model       = "deepseek-chat"
temperature = 1.1        # 越高越放飞，电台主持建议 1.0 ~ 1.3
max_tokens  = 400
timeout     = 20.0   # 单次请求超时。配合 retries 决定最坏等多久
stream      = true       # 流式生成，开口更快
history_size = 12        # 带多少条历史弹幕进上下文


[tts]
# 语音引擎（纯本地，不需要联网，不需要显卡，不调用任何语音 API）
#   vits     = 角色音色：内置 13 个角色模型，每个 804 个说话人编号（推荐，最快）
#   kokoro   = 通用内置音色：103 个声音
#   zipvoice = 零样本克隆：给一段音频样本，它就用那个声音说话
#   none     = 不出声，只在窗口里显示文字
provider = "vits"

# 用哪个声音
#   vits 模式：写「模型:编号」，例如 zenyatta:0 / keqing:12 / doom:0
#   其它模式：写音色名
# 在软件「音色」页可以直接试听和切换，不用手改这里
voice = "zenyatta:0"

# 单次合成的最大字数。
# 注意：每切一段都要重新处理一遍参考音频（约 0.67 秒 × 样本秒数），
# 切得越碎总耗时越长、段与段之间越容易有空档。
# 一般 AI 一次回复不会超过这个数，正好整段合成、连续播出
max_chars = 220

# 神经网络线程数。实测 8 最优，调到 12 反而更慢
num_threads = 8

# —— VITS 角色音色的韵律 ——
# noise_scale   语调起伏：0.5 平淡 / 0.667 自然 / 0.8 起伏明显
# noise_scale_w 音节长短变化：0.8 默认，调高更像真人随意说话
# silence_scale 句子间停顿：0.2 默认，0.4 会明显停顿
noise_scale = 0.667
noise_scale_w = 0.8
silence_scale = 0.2

# 多音字纠正：把「银行行长」这类词换成同音替身再合成
# 规则在 voices/pronunciation.txt，可自己加
punctuation_delivery = true
# 语气强度：0 = 全拉平 / 1 = 默认 / 2 = 夸张
punctuation_strength = 1.0

fix_polyphones = true

# 模型精度：fp32 = 全精度（解码器 455MB，音质干净，推荐）
#           int8 = 8 位量化（解码器 119MB，快但会有沙沙声）
model_quality = "fp32"

# flow-matching 步数：越小越快，但噪声明显变大
#   实测频谱平坦度（越低越干净）：4 步 0.0155 / 8 步 0.0089 / 16 步 0.0092
#   所以默认 8 —— 4 步虽然快一倍，但听起来会有沙沙声
num_steps = 8

# 分类器无关引导强度。实测调高反而更脏，保持 1.0（关闭）
guidance_scale = 1.0

# 参考音频超过这个秒数就提醒你（样本越长，每次合成都越慢）
max_reference_seconds = 8.0

rate    = "+0%"   # 语速，人设里写了就以人设为准
retries = 1


[audio]
volume = 100     # 0-200，直接改采样实现，不会动你的系统音量
device = ""      # 播放设备序号，留空 = 系统默认。运行 python run.py devices 可列出
list_devices_on_start = false


[idle]
# 没人理 AI 的时候，它自己念稿子
enabled = true
# 决定开口的间隔。注意实际静默 = 这个间隔 + 大模型(~1s) + 合成时间，
# 所以别设太大，否则听起来像卡住了
min_gap = 6.0
max_gap = 12.0
max_chars = 80   # 单次念稿最多多少字。越长，等你听到声音的静默越久

# true  = 只有"冲 AI 来的"弹幕才会打断念稿节奏（推荐）
#         普通观众互相聊天不影响，AI 会像真电台一样持续有输出
# false = 只要有人在聊天，AI 就不说话
only_mentions_reset = true

scripts = []     # 留空 = 自动用 scripts/ 目录下全部 .md / .txt
shuffle = true
loop    = true
improvise       = true   # 允许 AI 围绕稿子自由发挥，而不是像机器一样逐字念
improvise_ratio = 0.35   # 0 = 完全照念，1 = 完全自己编


[control]
# 本地控制台：浏览器打开 http://127.0.0.1:8770 就能切人设、跳过、让它说句话
enabled = true
host    = "127.0.0.1"
port    = 8770

allow_chat_commands = true      # 允许用弹幕指令切人设
persona_command     = "!人设"   # 例如：!人设 热血解说
skip_command        = "!下一条" # 例如：!下一条
admin_only          = true      # 只有主播/房管发的指令才生效
'''


def ensure_config(path: str | os.PathLike[str] | None = None) -> Path:
    """配置文件不存在时生成默认配置，返回配置路径。"""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return config_path


# ---------------------------------------------------------------------------
# 引擎种类判断
# ---------------------------------------------------------------------------

# 用户可能在 config.toml 里写中文别名，这里统一归一化。
# 这个判断以前在 run.py / backend.py / tts/__init__.py 各写了一遍，
# 结果加了 vits 之后漏改了 run.py —— 抽出来只写一份。
_PROVIDER_ALIASES: dict[str, str] = {
    "vits": "vits", "角色": "vits", "角色音色": "vits", "anime": "vits",
    "kokoro": "kokoro", "内置": "kokoro", "虚拟音": "kokoro",
    "zipvoice": "zipvoice", "clone": "zipvoice", "克隆": "zipvoice", "本地": "zipvoice",
    "none": "none", "off": "none", "无声": "none",
}

PROVIDER_LABELS = {
    "vits": "角色音色（VITS）",
    "kokoro": "通用内置音色（Kokoro）",
    "zipvoice": "克隆音色（ZipVoice）",
    "none": "无声",
}


def provider_kind(provider: str | None) -> str:
    """把配置里的引擎名归一化成 vits / kokoro / zipvoice / none。"""
    key = (provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(key, "vits")


# ---------------------------------------------------------------------------
# 保存配置（供图形界面用）
# ---------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        items = ", ".join(_toml_value(v) for v in value)
        return f"[{items}]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _flatten(cfg: Config) -> dict[str, str]:
    """把 Config 摊平成 {"section.key": "TOML 值"}。"""
    out: dict[str, str] = {}
    for section_name, section in (
        ("app", cfg.app), ("chat", cfg.chat), ("trigger", cfg.trigger),
        ("llm", cfg.llm), ("tts", cfg.tts), ("audio", cfg.audio),
        ("idle", cfg.idle), ("control", cfg.control),
    ):
        for f in fields(section):
            if f.name == "cookies":
                continue
            out[f"{section_name}.{f.name}"] = _toml_value(getattr(section, f.name))
    return out


def render_config(cfg: Config, template: str | None = None) -> str:
    """按模板重新渲染 config.toml —— 只改值，注释和排版原样保留。

    这样图形界面保存设置时，不会把你手写的注释和排版冲掉。
    """
    template = DEFAULT_CONFIG_TEMPLATE if template is None else template
    values = _flatten(cfg)
    cookies = {str(k): str(v) for k, v in (cfg.chat.cookies or {}).items() if v}

    out: list[str] = []
    section = ""
    cookie_written = False
    key_re = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*)=\s*(.*)$")

    for line in template.splitlines():
        stripped = line.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            out.append(line)
            continue

        if section == "chat.cookies":
            # 这一节整体重写：注释掉的模板行只在需要时被真实值替换
            if stripped.startswith("#") and "=" in stripped:
                key = stripped.lstrip("# ").split("=", 1)[0].strip()
                if not cookie_written:
                    cookie_written = True
                    if cookies:
                        for k, v in cookies.items():
                            out.append(f"{k} = {_toml_value(v)}")
                    else:
                        out.append(line)
                continue
            if stripped and not stripped.startswith("#"):
                continue          # 已经写过真实值了，跳过模板里的占位
            continue

        match = key_re.match(line)
        if match and not stripped.startswith("#"):
            full_key = f"{section}.{match.group(2)}" if section else match.group(2)
            if full_key in values:
                out.append(f"{match.group(1)}{match.group(2)}{match.group(3)}= {values[full_key]}")
                continue

        out.append(line)

    return "\n".join(out).rstrip() + "\n"


def save_config(cfg: Config, path: str | os.PathLike[str] | None = None) -> Path:
    """把配置写回文件（保留原有注释）。"""
    config_path = Path(path) if path else cfg.path
    template = DEFAULT_CONFIG_TEMPLATE
    if config_path.exists():
        try:
            existing = read_text_smart(config_path)
            if existing.count("[") >= 6:
                template = existing
        except Exception:
            pass
    text = render_config(cfg, template)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")
    return config_path


def describe(cfg: Config) -> str:
    """给启动日志用的一行摘要。"""
    return (
        f"平台={cfg.chat.platform} 房间={cfg.chat.room_id or '-'} "
        f"人设={cfg.app.persona} 模型={cfg.llm.model} 语音={cfg.tts.provider}"
    )
