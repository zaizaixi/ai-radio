"""「音色」页：安装克隆引擎、用音频样本创建音色、试听、切换。

一个音色 = 一段音频样本 + 这段样本对应的文字。
引擎学会这段声音之后，就能用它念任何新文本。
"""

from __future__ import annotations

import threading
import tkinter as tk
import wave
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..dsp import PRESETS
from ..voices import normalize_wav
from . import theme
from .widgets import Card, ScrollFrame, labeled_scale

PREVIEW_TEXT = "各位听众晚上好，欢迎收听今晚的深夜电台。"


class VoiceTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, app) -> None:
        super().__init__(parent, style="TFrame", padding=16)
        self.app = app
        self.fonts = app.fonts
        self.backend = app.backend

        outer = ScrollFrame(self)
        outer.pack(fill="both", expand=True)
        body = outer.body
        body.columnconfigure(0, weight=1)

        self._build_mode(body)
        self._build_engine(body)
        self._build_builtin(body)
        self._build_profiles(body)
        self._build_settings(body)
        self.refresh()

    # ------------------------------------------------------------------
    # 选引擎：内置音色 or 克隆音色
    # ------------------------------------------------------------------

    def _build_mode(self, parent: ttk.Frame) -> None:
        card = Card(parent, "第 1 步 · 用什么声音", self.fonts)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        self.mode_var = tk.StringVar(value=self.backend.provider_kind())
        for value, title, desc in (
            ("vits", "用角色音色（推荐）",
             "日漫 / 国漫 / 游戏配音风格的 13 个角色模型，每个 804 个说话人编号。\n"
             "实测 RTF 0.25 —— 合成比播放快 4 倍，念稿不会断断续续；\n"
             "自带词级词典和消歧 FST，多音字基本不会念错。"),
            ("zipvoice", "克隆你自己的声音",
             "录一段 5~8 秒的干声，让 AI 用那个声音说话。\n"
             "需要先装克隆引擎（约 176MB），而且样本质量直接决定效果 ——\n"
             "样本里有什么底噪、混响，克隆出来就会带上。"),
        ):
            row = ttk.Frame(card.content, style="Panel.TFrame")
            row.pack(fill="x", pady=(0, 8))
            ttk.Radiobutton(row, text=title, value=value,
                            variable=self.mode_var,
                            command=self._on_mode).pack(anchor="w")
            ttk.Label(row, text=desc, style="PanelMuted.TLabel",
                      justify="left").pack(anchor="w", padx=(24, 0))

    def _on_mode(self) -> None:
        kind = self.mode_var.get()
        self.backend.set_provider(kind)
        self.app.set_hint(
            "已切换到角色音色" if kind == "vits"
            else "已切换到克隆音色（ZipVoice）")
        self.refresh()

    # ------------------------------------------------------------------
    # 内置音色（Kokoro）
    # ------------------------------------------------------------------

    def _build_builtin(self, parent: ttk.Frame) -> None:
        card = Card(parent, "第 2 步 · 选一个角色音色", self.fonts)
        self.builtin_card = card
        card.grid(row=2, column=0, sticky="ew", pady=(0, 12))

        ttk.Label(card.content,
                  text="这些是日漫 / 国漫 / 游戏配音风格的声音。每个模型有 804 个说话人编号，\n"
                       "所以同一个角色能挑出很多不同音色 —— 用下面的编号慢慢试。\n"
                       "实测 RTF 0.25，合成比播放快 4 倍，念稿不会再断断续续。",
                  style="PanelMuted.TLabel", justify="left").pack(anchor="w", pady=(0, 10))

        # 关键词找（按标签匹配）
        find_row = ttk.Frame(card.content, style="Panel.TFrame")
        find_row.pack(fill="x", pady=(0, 6))
        ttk.Label(find_row, text="按标签找", style="Panel.TLabel").pack(side="left")
        self.find_var = tk.StringVar()
        entry = ttk.Entry(find_row, textvariable=self.find_var)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<Return>", lambda _e: self._find_voice())
        ttk.Button(find_row, text="🔍 找", style="Ghost.TButton",
                   command=self._find_voice).pack(side="left")
        self.find_result = ttk.Label(card.content, text="", style="PanelMuted.TLabel",
                                     justify="left")
        self.find_result.pack(anchor="w", pady=(0, 6))

        # 筛选
        filter_row = ttk.Frame(card.content, style="Panel.TFrame")
        filter_row.pack(fill="x", pady=(0, 8))
        self.filter_var = tk.StringVar(value="全部")
        for label in ("全部", "男声", "女声", "已下载"):
            ttk.Radiobutton(filter_row, text=label, value=label,
                            variable=self.filter_var,
                            command=self._fill_builtin).pack(side="left", padx=(0, 14))

        self.builtin_box = ttk.Frame(card.content, style="Panel.TFrame")
        self.builtin_box.pack(fill="x")

        # 选中模型之后的说话人调节
        ttk.Separator(card.content, orient="horizontal").pack(fill="x", pady=12)
        sid_row = ttk.Frame(card.content, style="Panel.TFrame")
        sid_row.pack(fill="x")
        ttk.Label(sid_row, text="说话人编号", style="Panel.TLabel").pack(side="left")
        self.sid_var = tk.IntVar(value=0)
        spin = ttk.Spinbox(sid_row, from_=0, to=803, width=6,
                           textvariable=self.sid_var, command=self._on_sid)
        spin.pack(side="left", padx=8)
        self.sid_label = ttk.Label(sid_row, text="/ 804", style="PanelMuted.TLabel")
        self.sid_label.pack(side="left")

        for text, delta in (("◀ 上一个", -1), ("下一个 ▶", 1)):
            ttk.Button(sid_row, text=text, style="Ghost.TButton",
                       command=lambda d=delta: self._step_sid(d)).pack(
                side="left", padx=(10, 0))

        act = ttk.Frame(card.content, style="Panel.TFrame")
        act.pack(fill="x", pady=(10, 0))
        ttk.Button(act, text="🎧 试听这个编号", style="Ghost.TButton",
                   command=self._preview_sid).pack(side="left")
        ttk.Button(act, text="✓ 就用这个", style="Accent.TButton",
                   command=self._use_sid).pack(side="left", padx=8)
        self.sid_hint = ttk.Label(act, text="", style="PanelMuted.TLabel")
        self.sid_hint.pack(side="left", padx=8)

        # —— 韵律：最接近"情感"的三个旋钮 ——
        ttk.Separator(card.content, orient="horizontal").pack(fill="x", pady=12)
        ttk.Label(card.content, text="念稿的语感", style="Panel.TLabel",
                  font=self.fonts["bold"]).pack(anchor="w")
        ttk.Label(card.content,
                  text="这三个参数决定「听起来像不像真人在念」。左边更平，右边更起伏。",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(2, 8))

        self.noise_box = labeled_scale(
            card.content, "语调起伏（越大越有感情，太大容易飘）", self.fonts,
            from_=0.3, to=1.0, value=float(self.backend.cfg.tts.noise_scale),
            fmt=lambda v: f"{v:.2f}" + ("　平淡" if v < 0.55 else
                                        "　自然" if v < 0.75 else "　起伏大"),
            on_change=self._on_prosody)
        self.noise_box.pack(fill="x")

        self.noise_w_box = labeled_scale(
            card.content, "节奏松紧（越大越像随口说，越小越像播报）", self.fonts,
            from_=0.4, to=1.2, value=float(self.backend.cfg.tts.noise_scale_w),
            fmt=lambda v: f"{v:.2f}", on_change=self._on_prosody)
        self.noise_w_box.pack(fill="x", pady=(8, 0))

        self.silence_box = labeled_scale(
            card.content, "句子之间停顿", self.fonts,
            from_=0.0, to=0.8, value=float(self.backend.cfg.tts.silence_scale),
            fmt=lambda v: f"{v:.2f}" + ("　紧凑" if v < 0.15 else
                                        "　正常" if v < 0.35 else "　明显停顿"),
            on_change=self._on_prosody)
        self.silence_box.pack(fill="x", pady=(8, 0))

        # —— 标点语气 ——
        ttk.Separator(card.content, orient="horizontal").pack(fill="x", pady=12)
        head = ttk.Frame(card.content, style="Panel.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="标点语气", style="Panel.TLabel",
                  font=self.fonts["bold"]).pack(side="left")
        self.delivery_var = tk.BooleanVar(
            value=bool(self.backend.cfg.tts.punctuation_delivery))
        ttk.Checkbutton(head, text="按标点变念法", variable=self.delivery_var,
                        command=self._on_delivery).pack(side="right")

        ttk.Label(card.content,
                  text="同一句话，写成「好。」「好！」「好？」「好……」应该是四种念法。\n"
                       "打开之后，感叹句会念得快一点响一点，省略号会拖长并停久一点。",
                  style="PanelMuted.TLabel", justify="left").pack(anchor="w", pady=(2, 8))

        self.strength_box = labeled_scale(
            card.content, "语气强度（0 全拉平 ↔ 2 夸张）", self.fonts,
            from_=0.0, to=2.0,
            value=float(self.backend.cfg.tts.punctuation_strength),
            fmt=lambda v: f"{v:.1f}" + ("　全拉平" if v < 0.15 else
                                        "　默认" if v < 1.4 else "　夸张"),
            on_change=self._on_delivery)
        self.strength_box.pack(fill="x")

        self.delivery_table = ttk.Label(card.content, text="",
                                        style="PanelMuted.TLabel", justify="left")
        self.delivery_table.pack(anchor="w", pady=(6, 0))
        self._refresh_delivery_table()

    # ------------------------------------------------------------------

    def _on_delivery(self, _value=None) -> None:
        """标点语气改了直接落盘，下次说话生效。"""
        cfg = self.backend.cfg
        try:
            cfg.tts.punctuation_delivery = bool(self.delivery_var.get())
            cfg.tts.punctuation_strength = float(self.strength_box.var.get())
            self.backend.persist_config()
            if self.backend._director is not None:
                engine = self.backend._director.engine
                engine.delivery_on = cfg.tts.punctuation_delivery
                engine.delivery_strength = cfg.tts.punctuation_strength
        except Exception:
            pass
        self._refresh_delivery_table()
        self.app.set_hint("标点语气已保存，下次说话生效")

    def _refresh_delivery_table(self) -> None:
        from ..delivery import preview_table

        if not self.delivery_var.get():
            self.delivery_table.configure(
                text="已关闭 —— 不管什么标点都是一个调。")
            return
        strength = float(self.strength_box.var.get())
        rows = preview_table(strength)
        lines = ["    ".join(f"{p} {lbl} {desc.split('（')[0]}"
                             for p, lbl, desc in rows[:4]),
                 "    ".join(f"{p} {lbl} {desc.split('（')[0]}"
                             for p, lbl, desc in rows[4:8])]
        self.delivery_table.configure(text="\n".join(l for l in lines if l.strip()))

    def _filtered_voices(self):
        from ..tts.characters import CHARACTER_VOICES

        choice = self.filter_var.get()
        kit = self.backend.kit
        if choice == "男声":
            return [v for v in CHARACTER_VOICES if v.gender == "男"]
        if choice == "女声":
            return [v for v in CHARACTER_VOICES if v.gender == "女"]
        if choice == "已下载":
            return [v for v in CHARACTER_VOICES if kit.character_ready(v.key)]
        return list(CHARACTER_VOICES)

    def _fill_builtin(self) -> None:
        from ..tts.characters import RECOMMENDED

        for child in self.builtin_box.winfo_children():
            child.destroy()

        kit = self.backend.kit
        chosen = self.backend.current_character_key()
        voices = self._filtered_voices()
        if not voices:
            ttk.Label(self.builtin_box, text="这个筛选下没有音色",
                      style="PanelMuted.TLabel").pack(anchor="w")
            return

        for voice in voices:
            installed = kit.character_ready(voice.key)
            active = voice.key == chosen
            row = ttk.Frame(self.builtin_box, style="Panel2.TFrame", padding=8)
            row.pack(fill="x", pady=2)

            left = ttk.Frame(row, style="Panel2.TFrame")
            left.pack(side="left", fill="x", expand=True)
            mark = "★ " if voice.key in RECOMMENDED else "　"
            ttk.Label(left, text=f"{mark}{voice.name}",
                      style="Panel.TLabel",
                      foreground=theme.ACCENT if active else None).pack(anchor="w")
            detail = f"{voice.origin} · {voice.gender} · {' '.join(voice.tags)}"
            if voice.note:
                detail += f"　—　{voice.note}"
            ttk.Label(left, text=detail, style="PanelMuted.TLabel").pack(anchor="w")

            right = ttk.Frame(row, style="Panel2.TFrame")
            right.pack(side="right")
            if installed:
                ttk.Button(right, text="选中" if not active else "当前",
                           style="Accent.TButton" if not active else "Ghost.TButton",
                           command=lambda v=voice: self._select_character(v)).pack(side="left")
            else:
                ttk.Button(right,
                           text=f"下载（{voice.size_mb:.0f}MB）",
                           style="Ghost.TButton",
                           command=lambda v=voice: self._download_character(v)).pack(side="left")

    def _on_prosody(self, _value=None) -> None:
        """韵律参数改了就直接落盘，下次说话生效。"""
        cfg = self.backend.cfg
        try:
            cfg.tts.noise_scale = float(self.noise_box.var.get())
            cfg.tts.noise_scale_w = float(self.noise_w_box.var.get())
            cfg.tts.silence_scale = float(self.silence_box.var.get())
            self.backend.persist_config()
        except Exception:
            pass
        self.app.set_hint("语感参数已保存，下次说话生效")

    def _select_character(self, voice) -> None:
        self.backend.set_provider("vits")
        self.backend.set_character(voice.key, self.sid_var.get())
        self._fill_builtin()
        self.app.refresh_voice_combo()
        self.app.set_hint(f"已选角色音色 {voice.name}，编号 {self.sid_var.get()}")

    def _download_character(self, voice) -> None:
        self.app.set_hint(f"开始下载 {voice.name}（{voice.size_mb:.0f}MB）…")
        self.progress.pack(fill="x", pady=(10, 4))
        self.progress_label.pack(anchor="w")
        self.backend.install_character(voice.key, on_done=self._character_done)

    def _character_done(self, result) -> None:
        self.app.log(str(result))
        self.app.set_hint(str(result))
        self.progress.pack_forget()
        self.progress_label.pack_forget()
        self._fill_builtin()

    def _on_sid(self) -> None:
        value = max(0, min(803, int(self.sid_var.get() or 0)))
        self.sid_var.set(value)
        self.sid_hint.configure(text="改了编号，点「试听这个编号」听听看")

    def _step_sid(self, delta: int) -> None:
        self.sid_var.set(max(0, min(803, int(self.sid_var.get() or 0) + delta)))
        self._preview_sid()

    def _preview_sid(self) -> None:
        key = self.backend.current_character_key()
        if not key:
            self.app.set_hint("先在上面选一个角色音色")
            return
        sid = int(self.sid_var.get() or 0)
        self.backend.set_provider("vits")
        self.backend.preview(PREVIEW_TEXT, f"{key}:{sid}")
        self.sid_hint.configure(text=f"正在试听 {key} #{sid}…")
        self.app.set_hint(f"正在试听 {key} 的编号 {sid}（第一次要加载模型）")

    def _use_sid(self) -> None:
        key = self.backend.current_character_key()
        if not key:
            self.app.set_hint("先在上面选一个角色音色")
            return
        sid = int(self.sid_var.get() or 0)
        self.backend.set_provider("vits")
        self.backend.set_character(key, sid)
        self._fill_builtin()
        self.app.refresh_voice_combo()
        self.app.set_hint(f"已改用 {key} 的编号 {sid}")

    # ------------------------------------------------------------------

    def _find_voice(self) -> None:
        """按中文描述找音色。这里按标签匹配，匹配不到就提示手动挑。"""
        text = (self.find_var.get() or "").strip()
        if not text:
            self.app.set_hint("先写一句你想要的声音，比如「低沉男声」")
            return

        from ..tts.characters import CHARACTER_VOICES

        hits = []
        for voice in CHARACTER_VOICES:
            score = 0.0
            for word in voice.keywords():
                if word and word in text:
                    score += 1.0
            if score:
                hits.append((voice, score))
        hits.sort(key=lambda item: -item[1])

        if hits:
            best = hits[0][1]
            lines = [f"「{text}」最接近这 {len(hits[:6])} 个："]
            for voice, score in hits[:6]:
                installed = "✓" if self.backend.kit.character_ready(voice.key) else "需下载"
                lines.append(f"    {voice.name}（{voice.origin}）"
                             f"　{' '.join(voice.tags)}　{installed}")
            self.find_result.configure(text="\n".join(lines))
            self.app.set_hint("在下面列表里点「选中」或「下载」，然后用编号试听")
        else:
            self.find_result.configure(
                text="标签里没找到。直接看下面的列表吧 —— 每个模型有 804 个编号，\n"
                     "选一个模型之后用「下一个 ▶」连着试听，比按描述找更靠谱。")
            self.app.set_hint("没匹配到，直接在列表里挑一个")

    def _build_engine(self, parent: ttk.Frame) -> None:
        card = Card(parent, "引擎状态", self.fonts)
        self.engine_card = card
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        row = ttk.Frame(card.content, style="Panel.TFrame")
        row.pack(fill="x")
        self.engine_status = ttk.Label(row, text="检测中…", style="Panel.TLabel")
        self.engine_status.pack(side="left")
        self.install_button = ttk.Button(row, text="一键安装", style="Accent.TButton",
                                         command=self._install)
        self.install_button.pack(side="right")

        self.progress = ttk.Progressbar(card.content, mode="determinate", maximum=100)
        self.progress_label = ttk.Label(card.content, text="", style="PanelMuted.TLabel")

        self.engine_hint = ttk.Label(
            card.content,
            text="引擎 = 「会说话」这个能力本身。纯 ONNX，跑在 CPU 上：\n"
                 "不需要显卡、不需要 PyTorch，断网也能用，你说的话不会上传到任何地方。",
            style="PanelMuted.TLabel", justify="left",
        )
        self.engine_hint.pack(anchor="w", pady=(10, 0))

        # 模型精度
        quality_row = ttk.Frame(card.content, style="Panel.TFrame")
        self.quality_row = quality_row
        quality_row.pack(fill="x", pady=(12, 0))
        ttk.Label(quality_row, text="模型精度", style="Panel.TLabel").pack(side="left")
        self.quality_var = tk.StringVar(value=self.backend.cfg.tts.model_quality)
        for value, label in (("fp32", "全精度（音质干净，推荐）"),
                             ("int8", "量化（省内存，有沙沙声）")):
            ttk.Radiobutton(quality_row, text=label, value=value,
                            variable=self.quality_var,
                            command=self._on_quality).pack(side="left", padx=(12, 0))

    def _build_profiles(self, parent: ttk.Frame) -> None:
        card = Card(parent, "我的克隆音色", self.fonts)
        self.profiles_card = card
        card.grid(row=3, column=0, sticky="ew", pady=(0, 12))

        head = ttk.Frame(card.content, style="Panel.TFrame")
        head.pack(fill="x", pady=(0, 10))
        ttk.Label(head, text="每个音色 = 一段音频样本 + 这段样本里说的内容",
                  style="PanelMuted.TLabel").pack(side="left")
        self.new_button = ttk.Button(head, text="＋ 新建音色", style="Accent.TButton",
                                     command=self._new_profile_dialog)
        self.new_button.pack(side="right")

        # ---- 关键词生成 ----
        kw = ttk.Frame(card.content, style="Panel2.TFrame", padding=14)
        kw.pack(fill="x", pady=(0, 12))
        ttk.Label(kw, text="✨ 用一句话描述你想要的音色",
                  style="Panel.TLabel", font=self.fonts["bold"]).pack(anchor="w")
        ttk.Label(
            kw,
            text="把关键词翻译成变调 / 明暗 / 语速，作用在模型自带的干净样本上。\n"
                 "例：低沉磁性的中年男声，适合深夜电台　｜　活泼可爱的少女音　｜　沉稳的纪录片旁白",
            style="PanelMuted.TLabel", justify="left", wraplength=640).pack(
            anchor="w", pady=(4, 8))

        row = ttk.Frame(kw, style="Panel2.TFrame")
        row.pack(fill="x")
        self.keyword_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.keyword_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _e: self._generate_from_keywords())
        ttk.Button(row, text="生成", style="Accent.TButton",
                   command=self._generate_from_keywords).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="示例音色", style="Ghost.TButton",
                   command=self._install_examples).pack(side="left", padx=(6, 0))

        self.keyword_hint = ttk.Label(kw, text="", style="PanelMuted.TLabel",
                                      justify="left", wraplength=640)
        self.keyword_hint.pack(anchor="w", pady=(8, 0))

        self.profiles_box = ttk.Frame(card.content, style="Panel.TFrame")
        self.profiles_box.pack(fill="x")

    def _build_settings(self, parent: ttk.Frame) -> None:
        card = Card(parent, "合成参数与试听", self.fonts)
        card.grid(row=4, column=0, sticky="ew")

        self.steps_scale = labeled_scale(
            card.content, "质量 / 速度（flow-matching 步数）", self.fonts,
            from_=2, to=12, value=float(self.backend.cfg.tts.num_steps or 8),
            on_change=self._on_steps_change,
            fmt=lambda v: f"{v:.0f} 步" + ("（快，但有沙沙声）" if v <= 5 else
                                           "（推荐，最干净）" if v <= 10 else "（更慢，收益很小）"),
        )
        self.steps_scale.pack(fill="x")

        self.threads_scale = labeled_scale(
            card.content, "CPU 线程数", self.fonts,
            from_=2, to=16, value=float(self.backend.cfg.tts.num_threads or 8),
            on_change=self._on_threads_change,
            fmt=lambda v: f"{v:.0f}（实测 8 最优）",
        )
        self.threads_scale.pack(fill="x", pady=(12, 0))

        self.rate_scale = labeled_scale(
            card.content, "语速", self.fonts, from_=-40, to=60,
            value=self._rate_number(self.backend.cfg.tts.rate),
            on_change=self._on_rate_change, fmt=lambda v: f"{v:+.0f}%",
        )
        self.rate_scale.pack(fill="x", pady=(12, 0))

        ttk.Label(card.content, text="试听内容", style="Panel.TLabel").pack(
            anchor="w", pady=(14, 0))
        row = ttk.Frame(card.content, style="Panel.TFrame")
        row.pack(fill="x", pady=(4, 0))
        self.preview_var = tk.StringVar(value=PREVIEW_TEXT)
        ttk.Entry(row, textvariable=self.preview_var).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="试听", style="Accent.TButton",
                   command=lambda: self._preview()).pack(side="left", padx=(8, 0))

    # ------------------------------------------------------------------

    @staticmethod
    def _rate_number(rate: str) -> float:
        try:
            return float((rate or "+0%").strip().rstrip("%"))
        except ValueError:
            return 0.0

    def _on_steps_change(self, value: float) -> None:
        self.backend.cfg.tts.num_steps = int(round(value))
        self._save_later()

    def _on_threads_change(self, value: float) -> None:
        self.backend.cfg.tts.num_threads = int(round(value))
        self._save_later()

    def _on_rate_change(self, value: float) -> None:
        self.backend.cfg.tts.rate = f"{value:+.0f}%"
        persona = self.app.current_persona()
        if persona is not None and persona.rate:
            self.app.set_hint(f"注意：人设「{persona.display_name}」里写死了语速 "
                              f"({persona.rate})，会覆盖这里。")
        self._save_later()

    def _save_later(self) -> None:
        try:
            self.backend.persist_config()
        except Exception:
            pass

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self.backend.reload_config()
        kit = self.backend.kit
        kind = self.backend.provider_kind()
        self.mode_var.set(kind)

        # 按引擎显示不同的卡片：角色音色不需要克隆那套东西
        if kind == "vits":
            self.profiles_card.grid_remove()
            self.builtin_card.grid()
            self._fill_builtin()
            self._refresh_character_status(kit)
        elif kind == "kokoro":
            self.profiles_card.grid_remove()
            self.builtin_card.grid()
            self._fill_builtin()
            self._refresh_character_status(kit)
        else:
            self.builtin_card.grid_remove()
            self.profiles_card.grid()
            self._refresh_engine_status(kit)
            self._refresh_profiles(kit)
        self._refresh_settings()

    def _refresh_character_status(self, kit) -> None:
        from ..tts.characters import CHARACTER_VOICES

        installed = [v for v in CHARACTER_VOICES if kit.character_ready(v.key)]
        if installed:
            self.engine_status.configure(
                text=f"✓ 角色音色已就绪（{len(installed)}/{len(CHARACTER_VOICES)} 个模型）",
                style="Ok.TLabel")
            self.install_button.configure(text="重新检查", style="Ghost.TButton")
            self.engine_hint.configure(
                text=f"已下载 {'、'.join(v.name for v in installed[:6])}"
                     + ("…" if len(installed) > 6 else "") + "\n"
                     "每个模型 804 个说话人编号。实测 RTF 0.25 —— 合成比播放快 4 倍。")
        else:
            self.engine_status.configure(
                text="✗ 还没下载任何角色音色（每个约 115MB）", style="Bad.TLabel")
            self.install_button.configure(text="下载推荐音色", style="Accent.TButton")
            self.engine_hint.configure(
                text="点右边下载推荐的男声（禅雅塔或毁灭战士），约 115MB。\n"
                     "装完就能用 804 个说话人编号挑声音。")

    def _refresh_engine_status(self, kit) -> None:
        if kit.ready():
            self.engine_status.configure(text="✓ 克隆引擎已就绪", style="Ok.TLabel")
            self.install_button.configure(text="重新检查", style="Ghost.TButton")
        else:
            missing = "、".join(kit.missing_parts())
            self.engine_status.configure(
                text=f"✗ 还没装好，缺少：{missing}（点右边一键安装，约 176MB）",
                style="Bad.TLabel")
            self.install_button.configure(text="🔧 一键安装", style="Accent.TButton")

    def _refresh_settings(self) -> None:
        """参数卡片随引擎变化：模型精度和步数都是 ZipVoice 专有的。"""
        kind = self.backend.provider_kind()
        row = getattr(self, "quality_row", None)
        if row is None:
            return
        if kind == "zipvoice":
            row.pack(fill="x", pady=(12, 0))
            if hasattr(self, "steps_scale"):
                self.steps_scale.pack(fill="x", pady=(10, 0))
        else:
            # 角色音色和 Kokoro 都不吃这些参数
            row.pack_forget()
            if hasattr(self, "steps_scale"):
                self.steps_scale.pack_forget()

    def _refresh_profiles(self, kit) -> None:
        for child in self.profiles_box.winfo_children():
            child.destroy()

        profiles = kit.profiles()
        current = self.backend.cfg.tts.voice
        if not profiles:
            self._empty_state()
        else:
            for profile in profiles:
                self._profile_row(profile, profile.key == current)

    def _empty_state(self) -> None:
        """一个音色都没有的时候，把"你现在该干什么"说清楚。"""
        box = ttk.Frame(self.profiles_box, style="Panel2.TFrame", padding=20)
        box.pack(fill="x")

        ttk.Label(box, text="还没有音色 —— 这里需要你自己提供一个声音",
                  style="Panel.TLabel", font=self.fonts["bold"]).pack(anchor="w")
        ttk.Label(
            box,
            text="引擎（第 1 步）只是「会说话」的能力，它本身不带任何声音。\n"
                 "你要给它一段 5~8 秒的干声，它才能学会用那个声音念稿子。\n"
                 "这段声音等于你的声纹，只存在你自己电脑的 voices/ 目录里。",
            style="PanelMuted.TLabel", justify="left",
        ).pack(anchor="w", pady=(8, 16))

        buttons = ttk.Frame(box, style="Panel2.TFrame")
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="🎙 现在录一个（30 秒搞定）", style="Accent.TButton",
                   command=self._new_profile_dialog).pack(side="left")
        ttk.Button(buttons, text="📁 从 WAV 文件导入",
                   command=self._new_profile_dialog).pack(side="left", padx=(10, 0))
        ttk.Button(buttons, text="✨ 先用示例音色",
                   command=self._install_examples).pack(side="left", padx=(10, 0))

        ttk.Label(
            box,
            text="录音要点：一个人、安静环境、正常语速念一句话，说完停 1 秒再点停止。\n"
                 "示例音色用的是模型自带的样本，可以先用着，之后换成你自己的。",
            style="PanelMuted.TLabel", justify="left",
        ).pack(anchor="w", pady=(14, 0))

    # ------------------------------------------------------------------
    # 关键词生成 / 示例音色
    # ------------------------------------------------------------------

    def _generate_from_keywords(self) -> None:
        query = (self.keyword_var.get() or "").strip()
        if not query:
            self.keyword_hint.configure(
                text="先写一句描述，比如「低沉磁性的中年男声，适合深夜电台」")
            return

        from ..presets import examples_available, plan_from_text, unique_name

        if not examples_available(self.backend.kit):
            messagebox.showerror(
                "缺少基础样本",
                "关键词生成需要一个干净的声音底子，但模型自带的示例样本找不到了。\n\n"
                "它们应该在 models/*zipvoice*/test_wavs/ 下面 —— "
                "重新装一次引擎就能恢复。\n\n"
                "也可以直接点「＋ 新建音色」录一段你自己的声音。",
                parent=self.app.root)
            return

        plan = plan_from_text(query)
        name = unique_name(self.backend.kit, plan.name)
        source = plan.base.path(self.backend.kit)

        matched = "、".join(plan.matched) if plan.matched else "（没识别到关键词，用默认配置）"
        self.keyword_hint.configure(
            text=f"识别到：{matched}\n方案：{plan.describe()}\n正在生成…")

        try:
            wav = source.read_bytes()
        except Exception as exc:
            messagebox.showerror("读不到样本", str(exc), parent=self.app.root)
            return

        self.backend.create_profile(
            name, wav, plan.base.transcript, name,
            note=f"关键词生成：{query}",
            semitones=plan.semitones, tone=plan.tone,
            on_done=lambda result: self._after_keyword(result, name, plan))

    def _after_keyword(self, result, name: str, plan) -> None:
        self.app.log(str(result))
        self.keyword_hint.configure(
            text=f"✓ 已生成「{name}」 — {plan.describe()}\n"
                 f"在下面点「设为当前」或「试听」就能用上。")
        self.keyword_var.set("")
        self._after_profile(result)

    def _install_examples(self) -> None:
        from ..presets import examples_available, install_examples

        if not examples_available(self.backend.kit):
            messagebox.showerror(
                "缺少示例样本",
                "模型自带的示例样本找不到了。\n重新装一次引擎就能恢复。",
                parent=self.app.root)
            return

        self.app.set_hint("正在生成示例音色…")
        self.keyword_hint.configure(text="正在生成示例音色…")

        def work() -> None:
            try:
                made = install_examples(self.backend.kit)
                message = (f"已添加 {len(made)} 个示例音色：{'、'.join(made)}"
                           if made else "示例音色都已经有了")
            except Exception as exc:
                message = f"生成示例音色失败：{exc}"
            self.app.root.after(0, lambda: self._after_examples(message))

        threading.Thread(target=work, daemon=True, name="examples").start()

    def _after_examples(self, message: str) -> None:
        self.app.log(message)
        self.app.set_hint(message)
        self.keyword_hint.configure(text=message)
        self.refresh()
        self.app.refresh_voice_combo()

    def _profile_row(self, profile, is_current: bool) -> None:
        row = ttk.Frame(self.profiles_box, style="Panel2.TFrame", padding=12)
        row.pack(fill="x", pady=(0, 8))

        left = ttk.Frame(row, style="Panel2.TFrame")
        left.pack(side="left", fill="x", expand=True)

        title = ("● " if is_current else "") + profile.display
        ttk.Label(left, text=title, style="Panel.TLabel",
                  font=self.fonts["bold"]).pack(anchor="w")

        info = profile.describe()
        ttk.Label(left, text=info, style="PanelMuted.TLabel").pack(anchor="w")
        if profile.transcript:
            snippet = profile.transcript[:36] + ("…" if len(profile.transcript) > 36 else "")
            ttk.Label(left, text=f"「{snippet}」", style="PanelMuted.TLabel").pack(
                anchor="w", pady=(2, 0))

        right = ttk.Frame(row, style="Panel2.TFrame")
        right.pack(side="right")
        if not is_current and profile.ready:
            ttk.Button(right, text="设为当前", style="Accent.TButton",
                       command=lambda k=profile.key: self._set_current(k)).pack(side="left")
        ttk.Button(right, text="试听", style="Ghost.TButton",
                   command=lambda k=profile.key: self._preview(k)).pack(side="left", padx=6)
        ttk.Button(right, text="编辑", style="Ghost.TButton",
                   command=lambda p=profile: self._edit_profile_dialog(p)).pack(side="left")
        ttk.Button(right, text="删除", style="Ghost.TButton",
                   command=lambda k=profile.key: self._delete(k)).pack(side="left", padx=6)

    # ------------------------------------------------------------------
    # 新建 / 编辑音色
    # ------------------------------------------------------------------

    def _new_profile_dialog(self) -> None:
        self._profile_dialog(None)

    def _edit_profile_dialog(self, profile) -> None:
        self._profile_dialog(profile)

    def _profile_dialog(self, profile) -> None:
        is_edit = profile is not None
        dialog = tk.Toplevel(self.app.root)
        dialog.title("编辑音色" if is_edit else "新建克隆音色")
        dialog.configure(bg=theme.BG)
        dialog.transient(self.app.root)
        dialog.grab_set()
        dialog.geometry("660x780")
        dialog.resizable(False, False)

        state = {"wav": None, "path": None, "changed": False,
                 "semitones": float(profile.semitones) if is_edit else 0.0,
                 "tone": float(profile.tone) if is_edit else 0.0,
                 "denoise": float(profile.denoise) if is_edit else 0.0}

        pad = ttk.Frame(dialog, style="TFrame", padding=20)
        pad.pack(fill="both", expand=True)

        ttk.Label(pad, text="编辑音色" if is_edit else "新建克隆音色",
                  style="H2.TLabel").pack(anchor="w")
        ttk.Label(pad,
                  text="引擎需要「一段干净的说话声」+「这段话的文字」来学会这个声音。\n"
                       "建议：5~8 秒、只有一个人说话、没有背景音乐、WAV 格式。",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(4, 14))

        # ---- 名字 ----
        ttk.Label(pad, text="音色名字", style="TLabel").pack(anchor="w")
        name_var = tk.StringVar(value=(profile.display if is_edit else ""))
        ttk.Entry(pad, textvariable=name_var).pack(fill="x", pady=(4, 12))

        # ---- 样本 ----
        ttk.Label(pad, text="音频样本（16 位 PCM WAV）", style="TLabel").pack(anchor="w")
        file_row = ttk.Frame(pad, style="TFrame")
        file_row.pack(fill="x", pady=(4, 4))
        file_var = tk.StringVar(value=(str(profile.sample) if is_edit and profile.sample else ""))
        entry = ttk.Entry(file_row, textvariable=file_var, state="readonly")
        entry.pack(side="left", fill="x", expand=True)
        if is_edit and profile.sample:
            state["path"] = profile.sample
            try:
                state["wav"] = profile.sample.read_bytes()
            except Exception:
                pass
        ttk.Button(file_row, text="🎙 录音",
                   command=lambda: self._record_dialog(state, file_var, info_label)).pack(
            side="left", padx=(8, 0))
        ttk.Button(file_row, text="选文件…",
                   command=lambda: self._pick_sample(file_var, state, info_label)).pack(
            side="left", padx=(6, 0))

        info_label = ttk.Label(pad, text="", style="Muted.TLabel")
        info_label.pack(anchor="w")
        self._update_sample_info(info_label, state, profile)

        # ---- 音色微调 ----
        ttk.Separator(pad, orient="horizontal").pack(fill="x", pady=12)
        tune_head = ttk.Frame(pad, style="TFrame")
        tune_head.pack(fill="x")
        ttk.Label(tune_head, text="音色微调（可选）",
                  style="TLabel", font=self.fonts["bold"]).pack(side="left")
        self._tune_label = ttk.Label(tune_head, text="", style="Ok.TLabel")
        self._tune_label.pack(side="right")

        ttk.Label(pad,
                  text="克隆出来是什么声音，完全取决于这段参考音频。"
                       "想让声音更低沉，就把参考音频降调再喂给模型 —— "
                       "模型会照着变调后的声音去学。",
                  style="Muted.TLabel", justify="left", wraplength=600).pack(
            anchor="w", pady=(2, 8))

        preset_row = ttk.Frame(pad, style="TFrame")
        preset_row.pack(fill="x")
        for preset in PRESETS:
            name, semi, tone = preset[0], preset[1], preset[2]
            ttk.Button(preset_row, text=name, style="Ghost.TButton",
                       command=lambda s=semi, t=tone: self._apply_preset(s, t)).pack(
                side="left", padx=(0, 5))

        self._semi_scale = labeled_scale(
            pad, "音调（往左 = 更低沉）", self.fonts, from_=-8, to=8,
            value=state["semitones"], fmt=lambda v: f"{v:+.1f} 半音",
            on_change=lambda v: self._on_tune(semitones=v))
        self._semi_scale.pack(fill="x", pady=(8, 0))

        self._tone_scale = labeled_scale(
            pad, "厚度（往左 = 更浑厚）", self.fonts, from_=-1.0, to=1.0,
            value=state["tone"], fmt=lambda v: f"{v:+.2f}",
            on_change=lambda v: self._on_tune(tone=v))
        self._tone_scale.pack(fill="x", pady=(8, 0))

        self._denoise_scale = labeled_scale(
            pad, "去底噪 / 去回音（往右更狠）", self.fonts, from_=0.0, to=1.0,
            value=state["denoise"],
            fmt=lambda v: "关闭" if v < 0.02 else f"{v:.2f}",
            on_change=lambda v: self._on_tune(denoise=v))
        self._denoise_scale.pack(fill="x", pady=(8, 0))
        ttk.Label(pad,
                  text="影视片段、手机录音都带房间混响，模型会连混响一起学走 —— "
                       "合成出来就是「回音」。这个开关把字与字之间的残余压掉。\n"
                       "底子干净的正常录音用不上，开到 0.4~0.6 就够。",
                  style="Muted.TLabel", justify="left", wraplength=600).pack(
            anchor="w", pady=(2, 0))

        self._tune_state = state
        self._update_tune_label()

        # ---- 文本 ----
        ttk.Label(pad, text="这段音频里说了什么（一字不差）", style="TLabel").pack(
            anchor="w", pady=(12, 0))
        ttk.Label(pad, text="必须和音频内容完全一致，标点可以有出入。",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 6))
        text_box = theme.make_text(pad, self.fonts, height=4)
        text_box.pack(fill="both", expand=True)
        if is_edit:
            text_box.insert("1.0", profile.transcript)

        # ---- 按钮 ----
        buttons = ttk.Frame(pad, style="TFrame")
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="💾 保存", style="Accent.TButton",
                   command=lambda: self._submit_profile(
                       dialog, profile, name_var.get(), text_box.get("1.0", "end"),
                       state, file_var.get())).pack(side="right", padx=8)

    # ------------------------------------------------------------------

    def _apply_preset(self, semitones: float, tone: float) -> None:
        self._semi_scale.var.set(semitones)
        self._tone_scale.var.set(tone)
        self._on_tune(semitones=semitones, tone=tone)

    def _on_tune(self, semitones=None, tone=None, denoise=None) -> None:
        state = getattr(self, "_tune_state", None)
        if state is None:
            return
        if semitones is not None:
            state["semitones"] = float(semitones)
        if tone is not None:
            state["tone"] = float(tone)
        if denoise is not None:
            state["denoise"] = float(denoise)
        self._update_tune_label()

    def _update_tune_label(self) -> None:
        state = getattr(self, "_tune_state", None)
        if state is None:
            return
        from ..dsp import describe as describe_tune

        text = describe_tune(state["semitones"], state["tone"])
        extra = ""
        if state["semitones"] < -0.5:
            longer = 1.0 / (2.0 ** (state["semitones"] / 12.0))
            extra = f"　样本会变长到 {longer*100:.0f}%（每次合成约多花 {0.67*(longer-1)*8:.1f} 秒）"
        self._tune_label.configure(text=text + extra)

    # ------------------------------------------------------------------
    # 录音
    # ------------------------------------------------------------------

    def _record_dialog(self, state, file_var, info_label) -> None:
        from ..recorder import MicRecorder, RecorderError, list_input_devices

        devices = list_input_devices()
        if not devices:
            messagebox.showerror(
                "没有找到麦克风",
                "系统里没有可用的录音设备。\n\n"
                "检查一下：设置 → 隐私和安全性 → 麦克风，"
                "确认「允许桌面应用访问你的麦克风」是打开的。",
                parent=self.app.root)
            return

        max_seconds = 15.0
        dialog = tk.Toplevel(self.app.root)
        dialog.title("录一段声音")
        dialog.configure(bg=theme.BG)
        dialog.transient(self.app.root)
        dialog.geometry("520x330")
        dialog.resizable(False, False)

        pad = ttk.Frame(dialog, style="TFrame", padding=22)
        pad.pack(fill="both", expand=True)

        ttk.Label(pad, text="录一段你自己说话的声音", style="H2.TLabel").pack(anchor="w")
        ttk.Label(pad,
                  text="对着麦克风自然地念一句话，5~8 秒最好。\n"
                       "念完之后，把这句话一字不差地填到「说了什么」框里。\n"
                       "说完停 1 秒再点停止，避免把最后一个字切掉。",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(6, 16))

        # 音量条
        meter = tk.Canvas(pad, height=18, background=theme.LOG_BG,
                          highlightthickness=1, highlightbackground=theme.BORDER)
        meter.pack(fill="x")
        bar = meter.create_rectangle(0, 0, 0, 18, fill=theme.SUCCESS, outline="")

        status = ttk.Label(pad, text="准备中…", style="Muted.TLabel")
        status.pack(anchor="w", pady=(10, 0))

        recorder = MicRecorder()
        try:
            recorder.start(max_seconds=max_seconds)
        except RecorderError as exc:
            dialog.destroy()
            messagebox.showerror("打不开麦克风", str(exc), parent=self.app.root)
            return

        finished = {"done": False}

        def tick() -> None:
            if finished["done"]:
                return
            try:
                level = recorder.level
                width = meter.winfo_width()
                meter.coords(bar, 0, 0, int(width * min(1.0, level * 1.6)), 18)
                meter.itemconfigure(
                    bar, fill=theme.DANGER if level > 0.85 else theme.SUCCESS)
                elapsed = recorder.elapsed
                hint = ""
                if level < 0.03 and elapsed > 1.2:
                    hint = "　⚠ 声音太小，靠近一点麦克风"
                status.configure(
                    text=f"● 录音中 {elapsed:4.1f}s / {max_seconds:.0f}s{hint}",
                    style="Warn.TLabel" if hint else "Muted.TLabel")
                if elapsed >= max_seconds:
                    finish()
                    return
            except Exception:
                pass
            dialog.after(60, tick)

        def finish() -> None:
            if finished["done"]:
                return
            finished["done"] = True
            try:
                wav = recorder.stop()
            except RecorderError as exc:
                dialog.destroy()
                messagebox.showerror("录音失败", str(exc), parent=self.app.root)
                return
            state["wav"] = wav
            state["path"] = None
            state["changed"] = True
            file_var.set("（刚刚录的）")
            self._update_sample_info(info_label, state, None)
            dialog.destroy()

        def cancel() -> None:
            if finished["done"]:
                return
            finished["done"] = True
            recorder.cancel()
            dialog.destroy()

        buttons = ttk.Frame(pad, style="TFrame")
        buttons.pack(fill="x", pady=(20, 0))
        ttk.Button(buttons, text="取消", command=cancel).pack(side="right")
        ttk.Button(buttons, text="⏹ 停止并保存", style="Accent.TButton",
                   command=finish).pack(side="right", padx=8)

        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.after(80, tick)

    def _pick_sample(self, file_var, state, info_label) -> None:
        path = filedialog.askopenfilename(
            title="选择一段说话声（WAV）",
            filetypes=[("WAV 音频", "*.wav"), ("所有文件", "*.*")],
            parent=self.app.root,
        )
        if not path:
            return
        p = Path(path)
        try:
            data = p.read_bytes()
            normalize_wav(data)          # 提前验证格式
        except Exception as exc:
            messagebox.showerror(
                "这个文件用不了",
                f"{exc}\n\n"
                "只支持 16 位 PCM 的 WAV。\n"
                "如果是 mp3 / m4a / 录音笔格式，先用音频软件导出成 WAV 再试。\n"
                "（Audacity 免费，导出时选「WAV (Microsoft) 16-bit PCM」）",
                parent=self.app.root)
            return
        state["wav"] = data
        state["path"] = p
        state["changed"] = True
        file_var.set(str(p))
        self._update_sample_info(info_label, state, None)

    def _update_sample_info(self, label, state, profile) -> None:
        data = state.get("wav")
        if not data:
            label.configure(text="还没选文件")
            return
        try:
            import io
            with wave.open(io.BytesIO(normalize_wav(data))) as w:
                duration = w.getnframes() / w.getframerate()
                rate = w.getframerate()
        except Exception:
            label.configure(text="读不出音频信息")
            return
        limit = float(self.backend.cfg.tts.max_reference_seconds or 8.0)
        note = ""
        if duration < 2:
            note = "　⚠ 太短了，建议 5 秒以上"
        elif duration > limit:
            note = f"　⚠ 偏长（每次合成都会变慢），建议 5~8 秒"
        label.configure(
            text=f"{duration:.1f} 秒 · {rate}Hz" + note,
            style="Warn.TLabel" if note else "Ok.TLabel",
        )

    def _submit_profile(self, dialog, profile, name, transcript, state, path_text) -> None:
        name = (name or "").strip()
        transcript = (transcript or "").strip()
        if not name:
            messagebox.showwarning("还差一点", "给这个音色起个名字吧", parent=dialog)
            return
        if not transcript:
            messagebox.showwarning(
                "还差一点",
                "必须填写「这段音频里说了什么」。\n"
                "克隆就是靠这段文字学会声音的。", parent=dialog)
            return

        semitones = float(state.get("semitones") or 0.0)
        tone = float(state.get("tone") or 0.0)
        denoise = float(state.get("denoise") or 0.0)

        if profile is not None:
            wav = state.get("wav")
            if wav and state.get("changed"):
                # 换了原始样本：按原来的名字重建
                self.backend.create_profile(
                    profile.key, wav, transcript, name,
                    semitones=semitones, tone=tone, denoise=denoise,
                    on_done=self._after_profile)
            else:
                self.backend.update_profile(
                    profile.key, name=name, transcript=transcript,
                    semitones=semitones, tone=tone, denoise=denoise,
                    on_done=self._after_profile)
            dialog.destroy()
            return

        wav = state.get("wav")
        if not wav:
            messagebox.showwarning("还差一点", "先选一段音频样本", parent=dialog)
            return
        self.backend.create_profile(name, wav, transcript, name,
                                    semitones=semitones, tone=tone,
                                    denoise=denoise,
                                    on_done=self._after_profile)
        dialog.destroy()

    def _after_profile(self, result) -> None:
        self.app.log(str(result))
        self.app.set_hint(str(result))
        self.refresh()
        self.app.refresh_voice_combo()

    # ------------------------------------------------------------------

    def _on_quality(self) -> None:
        value = self.quality_var.get()
        if value == self.backend.cfg.tts.model_quality:
            return
        if value == "int8" and not (self.backend.kit.model_dir
                                    / "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia").is_dir():
            answer = messagebox.askyesno(
                "需要先下载量化模型",
                "量化版模型还没下载（约 104MB）。\n\n"
                "确定要切换吗？切过去之后点「一键安装」下载。\n\n"
                "建议：除非内存特别紧张，否则全精度版音质更好。",
                parent=self.app.root)
            if not answer:
                self.quality_var.set(self.backend.cfg.tts.model_quality)
                return
        self.backend.set_quality(value)
        self.backend.reload_engine()
        self.app.set_hint(f"模型精度已切到 {value}，下次说话生效")
        self.refresh()

    def _install(self) -> None:
        kit = self.backend.kit
        kind = self.backend.provider_kind()

        if kind == "vits":
            from ..tts.characters import CHARACTER_VOICES

            if any(kit.character_ready(v.key) for v in CHARACTER_VOICES):
                self.refresh()
                self.app.set_hint("角色音色已就绪 —— 在下面选一个模型，然后用编号试听。")
                return
            self.app.set_hint("开始下载推荐的角色音色（约 115MB）…")
            self.progress.pack(fill="x", pady=(10, 4))
            self.progress_label.pack(anchor="w")
            self.backend.install_character("zenyatta", on_done=self._character_done)
            return

        if kind == "kokoro":
            if kit.kokoro_ready():
                self.refresh()
                self.app.set_hint("内置音色已就绪 —— 在上面挑一个「试听」，满意就点「用这个」。")
                return
            self.app.set_hint("开始下载内置音色（Kokoro），约 140MB…")
            self.progress.pack(fill="x", pady=(10, 4))
            self.progress_label.pack(anchor="w")
            self.backend.install_engine(on_done=self._install_done, kind="kokoro")
            return

        if kit.ready():
            # 已经装好了，按钮这时候的作用就是重新检查
            self.refresh()
            self.app.set_hint("克隆引擎已就绪。接下来需要建一个音色（第 2 步）。")
            if not kit.profiles():
                self.app.root.after(200, self._prompt_new_profile)
            return

        self.app.set_hint("开始下载本地克隆引擎，约 176MB，请耐心等待…")
        self.progress.pack(fill="x", pady=(10, 4))
        self.progress_label.pack(anchor="w")
        self.backend.install_engine(on_done=self._install_done, kind="zipvoice")

    def _install_done(self, result) -> None:
        self.app.log(str(result))
        self.app.set_hint(str(result))
        self.progress.pack_forget()
        self.progress_label.pack_forget()
        self.refresh()

        kit = self.backend.kit
        if self.backend.provider_kind() == "vits":
            return
        if self.backend.provider_kind() == "kokoro":
            if kit.kokoro_ready():
                self.app.set_hint("内置音色装好了 —— 挑一个「试听」，满意点「用这个」。")
            return

        # 装完引擎直接引导去建音色 —— 不然用户会以为"下载完就该能用了"
        if kit.ready() and not kit.profiles():
            self.app.root.after(300, self._prompt_new_profile)

    def _prompt_new_profile(self) -> None:
        if self.backend.kit.profiles():
            return
        answer = messagebox.askyesno(
            "引擎装好了，还差一个声音",
            "引擎已经装好了，但它还不知道该用谁的声音说话。\n\n"
            "现在录一段你自己的声音吗？（5~8 秒，对着麦克风念一句话就行）\n\n"
            "选「否」的话，你也可以稍后点「＋ 新建音色」自己弄。",
            parent=self.app.root)
        if answer:
            self._new_profile_dialog()

    def _set_current(self, key: str) -> None:
        cfg = self.backend.cfg
        cfg.tts.voice = key
        self.backend.persist_config()
        self.refresh()
        self.app.refresh_voice_combo()
        self.app.set_hint(f"当前音色已切换为「{key}」")

    def _delete(self, key: str) -> None:
        if not messagebox.askyesno("确认", f"删除音色「{key}」？\n"
                                          "样本文件会被删掉，以后可以重新建。"):
            return
        result = self.backend.delete_profile(key)
        self.app.log(result)
        self.app.set_hint(result)
        self.refresh()
        self.app.refresh_voice_combo()

    def _preview(self, key: str = "") -> None:
        if not key:
            key = self.backend.cfg.tts.voice
        text = self.preview_var.get().strip() or PREVIEW_TEXT
        self.app.set_hint("正在合成试听…（第一次会加载模型，需要几秒）")
        self.backend.preview(text, key, self.backend.cfg.tts.rate,
                             on_done=lambda r: self.app.set_hint(str(r)))

    # ------------------------------------------------------------------

    def on_progress(self, payload: dict) -> None:
        if not self.progress.winfo_ismapped():
            return
        total = payload.get("total") or 0
        done = payload.get("done") or 0
        if total:
            from ..voices import human_size
            self.progress.configure(mode="determinate", value=done / total * 100)
            self.progress_label.configure(
                text=f"{payload.get('message','')}  "
                     f"{human_size(done)} / {human_size(total)}  "
                     f"({done/total*100:.0f}%)")
        else:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
            self.progress_label.configure(text=payload.get("message", ""))
