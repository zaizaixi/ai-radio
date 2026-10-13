"""「设置」页：房间号、API Key、音量、说话节奏。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..audio import find_virtual_cables, list_devices
from .widgets import Card, ScrollFrame, labeled_scale


class SettingsTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, app) -> None:
        super().__init__(parent, style="TFrame", padding=16)
        self.app = app
        self.fonts = app.fonts
        self.backend = app.backend

        outer = ScrollFrame(self)
        outer.pack(fill="both", expand=True)
        body = outer.body
        body.columnconfigure(0, weight=1)

        self._build_room(body)
        self._build_brain(body)
        self._build_stream(body)
        self._build_audio(body)
        self._build_rhythm(body)

        bar = ttk.Frame(self, style="TFrame")
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="💾 保存设置", style="Accent.TButton",
                   command=self.save).pack(side="left")
        ttk.Button(bar, text="重新载入", command=self.load).pack(side="left", padx=8)

        self.load()

    # ------------------------------------------------------------------

    def _build_room(self, parent) -> None:
        card = Card(parent, "直播间", self.fonts)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        grid = ttk.Frame(card.content, style="Panel.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        self.platform_var = tk.StringVar(value="bilibili")
        ttk.Label(grid, text="平台", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(grid, textvariable=self.platform_var, state="readonly",
                     values=["bilibili", "console"], width=14).grid(
            row=1, column=0, sticky="w", pady=(4, 0))

        ttk.Label(grid, text="房间号", style="Panel.TLabel").grid(
            row=0, column=1, sticky="w", padx=(14, 0))
        self.room_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.room_var).grid(
            row=1, column=1, sticky="ew", padx=(14, 0), pady=(4, 0))

        ttk.Label(grid, text="主播名字（AI 这样称呼你）", style="Panel.TLabel").grid(
            row=0, column=2, sticky="w", padx=(14, 0))
        self.host_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.host_var, width=16).grid(
            row=1, column=2, sticky="ew", padx=(14, 0), pady=(4, 0))

        ttk.Label(grid, text="在玩什么", style="Panel.TLabel").grid(
            row=0, column=3, sticky="w", padx=(14, 0))
        self.game_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.game_var).grid(
            row=1, column=3, sticky="ew", padx=(14, 0), pady=(4, 0))

        ttk.Separator(card.content, orient="horizontal").pack(fill="x", pady=14)

        ttk.Label(card.content, text="B 站 Cookie（可选，但强烈建议填）",
                  style="Panel.TLabel").pack(anchor="w")
        ttk.Label(card.content,
                  text="填了弹幕更全、也不容易被风控拦。浏览器 F12 → Application → "
                       "Cookies → live.bilibili.com，抄下面四个值。\n"
                       "Cookie 等于你的登录凭证，不会上传到任何地方，只存在本地 config.toml。",
                  style="PanelMuted.TLabel", justify="left").pack(anchor="w", pady=(2, 8))

        cookies = ttk.Frame(card.content, style="Panel.TFrame")
        cookies.pack(fill="x")
        self.cookie_vars: dict[str, tk.StringVar] = {}
        for index, key in enumerate(("SESSDATA", "bili_jct", "buvid3", "DedeUserID")):
            col = index % 2
            row = index // 2
            cookies.columnconfigure(col * 2 + 1, weight=1)
            ttk.Label(cookies, text=key, style="PanelMuted.TLabel").grid(
                row=row, column=col * 2, sticky="w", pady=4,
                padx=(0, 8) if col == 0 else (20, 8))
            var = tk.StringVar()
            self.cookie_vars[key] = var
            ttk.Entry(cookies, textvariable=var, show="•").grid(
                row=row, column=col * 2 + 1, sticky="ew", pady=4)

    def _build_brain(self, parent) -> None:
        card = Card(parent, "AI 大脑（大模型）", self.fonts)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        grid = ttk.Frame(card.content, style="Panel.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="API Key", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.key_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.key_var, show="•").grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10))

        ttk.Label(grid, text="接口地址", style="Panel.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(grid, text="模型名", style="Panel.TLabel").grid(
            row=2, column=1, sticky="w", padx=(14, 0))
        self.base_var = tk.StringVar()
        self.model_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.base_var).grid(
            row=3, column=0, sticky="ew", pady=(4, 0))
        ttk.Entry(grid, textvariable=self.model_var).grid(
            row=3, column=1, sticky="ew", padx=(14, 0), pady=(4, 0))

        ttk.Label(card.content,
                  text="默认用 DeepSeek。任何 OpenAI 格式的接口都能用：改接口地址和模型名即可。\n"
                       "Key 只保存在本地 config.toml，不会上传。",
                  style="PanelMuted.TLabel", justify="left").pack(anchor="w", pady=(10, 0))

        row = ttk.Frame(card.content, style="Panel.TFrame")
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="测试连接", style="Ghost.TButton",
                   command=self._test_llm).pack(side="left")
        self.llm_status = ttk.Label(row, text="", style="PanelMuted.TLabel")
        self.llm_status.pack(side="left", padx=10)

    def _build_stream(self, parent) -> None:
        card = Card(parent, "什么时候让 AI 开口", self.fonts)
        card.grid(row=2, column=0, sticky="ew", pady=(0, 12))

        self.mention_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(card.content,
                        text="只有被 @ 到才回话（关掉的话它会主动挑弹幕接话，更热闹但更吵）",
                        variable=self.mention_var,
                        style="Panel.TCheckbutton").pack(anchor="w")

        self.idle_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(card.content, text="没人理它的时候，自己念稿子",
                        variable=self.idle_var,
                        style="Panel.TCheckbutton").pack(anchor="w", pady=(6, 0))

        self.only_mention_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(card.content,
                        text="普通观众互相聊天不打断它（推荐：它会像真电台一样持续有输出）",
                        variable=self.only_mention_var,
                        style="Panel.TCheckbutton").pack(anchor="w", pady=(6, 0))

        ttk.Separator(card.content, orient="horizontal").pack(fill="x", pady=14)
        self.improvise_box = labeled_scale(
            card.content, "念稿时的自由发挥程度", self.fonts,
            from_=0.0, to=1.0, value=0.35,
            fmt=lambda v: ("完全照着念" if v < 0.05 else
                           "完全自己讲" if v > 0.95 else f"{v:.0%} 自由"),
        )
        self.improvise_box.pack(fill="x")

    def _build_audio(self, parent) -> None:
        card = Card(parent, "声音输出", self.fonts)
        card.grid(row=3, column=0, sticky="ew", pady=(0, 12))

        self.volume_box = labeled_scale(
            card.content, "音量", self.fonts, from_=0, to=200, value=100,
            fmt=lambda v: f"{v:.0f}%",
        )
        self.volume_box.pack(fill="x")

        ttk.Label(card.content,
                  text="这个是软件自己的音量，直接改采样实现，不会动你的系统音量。\n"
                       "直播接线：把系统默认输出设成虚拟声卡（如 VB-CABLE），"
                       "OBS 里用「音频输入采集」抓它，观众就只听到 AI 不听到你。",
                  style="PanelMuted.TLabel", justify="left").pack(anchor="w", pady=(10, 0))

        row = ttk.Frame(card.content, style="Panel.TFrame")
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="输出设备", style="Panel.TLabel").pack(side="left")
        self.device_var = tk.StringVar(value="系统默认")
        self.device_combo = ttk.Combobox(row, textvariable=self.device_var,
                                         state="readonly", width=42)
        self.device_combo.pack(side="left", padx=(10, 0))
        ttk.Button(row, text="刷新", style="Ghost.TButton",
                   command=self._refresh_devices).pack(side="left", padx=6)
        self._devices: list = []

        self.cable_hint = ttk.Label(card.content, text="", style="PanelMuted.TLabel",
                                    justify="left")
        self.cable_hint.pack(anchor="w", pady=(8, 0))
        self._refresh_devices()

    def _refresh_devices(self) -> None:
        self._devices = list_devices()
        values = ["系统默认"] + [f"[{d.index}] {d.name}" for d in self._devices]
        self.device_combo.configure(values=values)
        if self.device_var.get() not in values:
            self.device_var.set("系统默认")
        self._update_cable_hint()

    def _update_cable_hint(self) -> None:
        """告诉用户「只让观众听见」这条路现在通不通。"""
        cables = find_virtual_cables()
        if cables:
            lines = [
                f"✓ 检测到虚拟声卡：{'、'.join(d.name for d in cables[:3])}",
                "  想只让观众听见、自己不听：",
                f"    1. 把上面的「输出设备」选成 [{cables[0].index}] {cables[0].name}",
                "    2. OBS 添加「音频输入采集」，设备选对应的 Cable Output",
                "    3. 这样 AI 的声音只进 OBS，你自己的耳机里不会响",
            ]
        else:
            lines = [
                "△ 没检测到虚拟声卡 —— 现在 AI 的声音只能从扬声器出来，你自己也会听到。",
                "  想只让观众听见，装一个免费的 VB-CABLE（vb-audio.com/Cable，2 分钟）：",
                "    1. 装完后回来点「刷新」",
                "    2. 输出设备选 CABLE Input",
                "    3. OBS 添加「音频输入采集」，设备选 CABLE Output",
                "  如果你用 OBS 的「应用程序音频采集」，那就先切到上面那个虚拟设备再采集。",
            ]
        self.cable_hint.configure(text="\n".join(lines))

    def _build_rhythm(self, parent) -> None:
        card = Card(parent, "说话节奏", self.fonts)
        card.grid(row=4, column=0, sticky="ew")

        self.gap_box = labeled_scale(
            card.content, "安静多久之后开始找话说（秒）", self.fonts,
            from_=5, to=120, value=20, fmt=lambda v: f"{v:.0f} 秒")
        self.gap_box.pack(fill="x")

        self.interval_box = labeled_scale(
            card.content, "两次开口之间最少间隔（秒）", self.fonts,
            from_=0, to=60, value=4, fmt=lambda v: f"{v:.0f} 秒")
        self.interval_box.pack(fill="x", pady=(12, 0))

        self.idle_chars_box = labeled_scale(
            card.content, "单次念稿最多多少字", self.fonts,
            from_=20, to=200, value=80, fmt=self._idle_chars_label)
        self.idle_chars_box.pack(fill="x", pady=(12, 0))
        ttk.Label(
            card.content,
            text="稿子越长，从「决定开口」到「听见声音」的静默就越久：\n"
                 "80 字约等 10 秒，150 字要等 30 秒以上（8 步合成时）。\n"
                 "宁可多念几次、每次短一点，听感才像真电台。",
            style="PanelMuted.TLabel", justify="left").pack(anchor="w", pady=(2, 0))

        self.cooldown_box = labeled_scale(
            card.content, "同一个人多久内不重复回（秒）", self.fonts,
            from_=0, to=300, value=45, fmt=lambda v: f"{v:.0f} 秒")
        self.cooldown_box.pack(fill="x", pady=(12, 0))

        self.say_chars_box = labeled_scale(
            card.content, "单次合成的最大字数", self.fonts,
            from_=40, to=320, value=220, fmt=self._say_chars_label)
        self.say_chars_box.pack(fill="x", pady=(12, 0))
        ttk.Label(
            card.content,
            text="超过这个字数就会被切成几段分别合成，段与段之间会出现空档。\n"
                 "所以宁大勿小：一般 AI 一次回复在这个字数以内，正好一整段连续念完。\n"
                 "调小反而更容易「断断续续」。",
            style="PanelMuted.TLabel", justify="left").pack(anchor="w", pady=(2, 0))

    # ------------------------------------------------------------------

    @staticmethod
    def _say_chars_label(value: float) -> str:
        """字数 → 大概多长的音频。"""
        chars = int(value)
        audio = chars / 5.0
        return f"{chars} 字（约 {audio:.0f} 秒音频）"

    @staticmethod
    def _idle_chars_label(value: float) -> str:
        """把字数换算成「大概要等多久出声」，不让人对着数字猜。"""
        chars = int(value)
        audio = chars / 5.0
        estimate = 0.67 * 8.0 + 1.85 * audio
        if audio > 11.0:
            estimate *= 0.63
        return f"{chars} 字（约 {estimate:.0f} 秒后出声）"


    def _test_llm(self) -> None:
        self.llm_status.configure(text="测试中…")
        cfg = self.backend.cfg
        cfg.llm.api_key = self.key_var.get().strip()
        cfg.llm.base_url = self.base_var.get().strip() or cfg.llm.base_url
        cfg.llm.model = self.model_var.get().strip() or cfg.llm.model
        self.backend._submit(self._do_test_llm(),
                             done=lambda r: self.llm_status.configure(text=str(r)))

    async def _do_test_llm(self) -> str:
        from ..llm import LLMClient

        try:
            reply = await LLMClient(self.backend.cfg.llm).check()
            return f"✓ 通了，模型回了「{reply}」"
        except Exception as exc:
            return f"✗ {str(exc)[:120]}"

    # ------------------------------------------------------------------

    def load(self) -> None:
        cfg = self.backend.reload_config()
        self.platform_var.set(cfg.chat.platform)
        self.room_var.set(str(cfg.chat.room_id or ""))
        self.host_var.set(cfg.app.host_name)
        self.game_var.set(cfg.app.game)
        for key, var in self.cookie_vars.items():
            var.set(cfg.chat.cookies.get(key, ""))
        self.key_var.set(cfg.llm.api_key)
        self.base_var.set(cfg.llm.base_url)
        self.model_var.set(cfg.llm.model)
        self.mention_var.set(cfg.trigger.require_mention)
        self.idle_var.set(cfg.idle.enabled)
        self.only_mention_var.set(cfg.idle.only_mentions_reset)
        self.improvise_box.var.set(cfg.idle.improvise_ratio)
        self.volume_box.var.set(cfg.audio.volume)
        self.gap_box.var.set((cfg.idle.min_gap + cfg.idle.max_gap) / 2)
        self.idle_chars_box.var.set(float(cfg.idle.max_chars or 80))
        self.interval_box.var.set(cfg.trigger.min_interval)
        self.cooldown_box.var.set(cfg.trigger.same_user_cooldown)
        self.say_chars_box.var.set(cfg.tts.max_chars)
        if cfg.audio.device:
            for index, device in enumerate(self._devices):
                if str(device.index) == str(cfg.audio.device):
                    self.device_var.set(f"[{device.index}] {device.name}")
                    break
        else:
            self.device_var.set("系统默认")

    def save(self) -> None:
        cfg = self.backend.cfg
        cfg.chat.platform = self.platform_var.get().strip() or "bilibili"
        room = self.room_var.get().strip()
        if room:
            try:
                from ..chat.bilibili import parse_room_input
                cfg.chat.room_id = parse_room_input(room)
            except ValueError:
                cfg.chat.room_id = 0
        else:
            cfg.chat.room_id = 0
        cfg.app.host_name = self.host_var.get().strip() or "主播"
        cfg.app.game = self.game_var.get().strip()
        cfg.chat.cookies = {k: v.get().strip() for k, v in self.cookie_vars.items()
                            if v.get().strip()}
        cfg.llm.api_key = self.key_var.get().strip()
        cfg.llm.base_url = self.base_var.get().strip() or cfg.llm.base_url
        cfg.llm.model = self.model_var.get().strip() or cfg.llm.model
        cfg.trigger.require_mention = bool(self.mention_var.get())
        cfg.idle.enabled = bool(self.idle_var.get())
        cfg.idle.only_mentions_reset = bool(self.only_mention_var.get())
        cfg.idle.improvise_ratio = round(float(self.improvise_box.var.get()), 2)
        cfg.audio.volume = int(self.volume_box.var.get())
        gap = float(self.gap_box.var.get())
        cfg.idle.min_gap = round(max(3.0, gap * 0.7), 1)
        cfg.idle.max_gap = round(gap * 1.3, 1)
        cfg.idle.max_chars = int(self.idle_chars_box.var.get())
        cfg.trigger.min_interval = round(float(self.interval_box.var.get()), 1)
        cfg.trigger.same_user_cooldown = round(float(self.cooldown_box.var.get()), 1)
        cfg.tts.max_chars = int(self.say_chars_box.var.get())

        selected = self.device_var.get()
        if selected.startswith("[") and "]" in selected:
            cfg.audio.device = selected[1:selected.index("]")]
        else:
            cfg.audio.device = ""

        try:
            path = self.backend.persist_config()
        except Exception as exc:
            self.app.set_hint(f"保存失败：{exc}")
            return

        self.app.log(f"设置已保存到 {path.name}")
        self.app.set_hint("设置已保存。如果电台正在播，重启一次才会全部生效。")
        self._refresh_devices()
