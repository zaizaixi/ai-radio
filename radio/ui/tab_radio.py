"""「电台」页：开播、状态、快捷操作、日志。"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

from . import theme
from .widgets import Card, ScrollFrame

MAX_LOG_LINES = 600


class RadioTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, app) -> None:
        super().__init__(parent, style="TFrame", padding=16)
        self.app = app
        self.fonts = app.fonts
        self.backend = app.backend
        self._log_lines = 0

        outer = ScrollFrame(self)
        outer.pack(fill="both", expand=True)
        body = outer.body
        body.columnconfigure(0, weight=1)

        self._build_status(body)
        self._build_actions(body)
        self._build_stream(body)

    # ------------------------------------------------------------------

    def _build_status(self, parent: ttk.Frame) -> None:
        card = Card(parent, "电台状态", self.fonts)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        head = ttk.Frame(card.content, style="Panel.TFrame")
        head.pack(fill="x")

        self.dot = tk.Canvas(head, width=14, height=14, background=theme.PANEL,
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self._dot_id = self.dot.create_oval(2, 2, 12, 12, fill=theme.FG_DIM, outline="")

        self.status_label = ttk.Label(head, text="未开播", style="Panel.TLabel",
                                      font=self.fonts["h2"])
        self.status_label.pack(side="left")

        self.btn_stop = ttk.Button(head, text="■ 停播", style="Danger.TButton",
                                   command=self._stop, state="disabled")
        self.btn_stop.pack(side="right")
        self.btn_start = ttk.Button(head, text="▶ 开播", style="Accent.TButton",
                                    command=self._start)
        self.btn_start.pack(side="right", padx=(0, 8))

        grid = ttk.Frame(card.content, style="Panel.TFrame")
        grid.pack(fill="x", pady=(14, 0))
        for i in range(4):
            grid.columnconfigure(i, weight=1)

        self.info_labels: dict[str, ttk.Label] = {}
        for index, (key, title) in enumerate((("persona", "人设"), ("voice", "音色"),
                                              ("pending", "排队"), ("spoken", "已开口"))):
            cell = ttk.Frame(grid, style="Panel.TFrame")
            cell.grid(row=0, column=index, sticky="w")
            ttk.Label(cell, text=title, style="PanelMuted.TLabel").pack(anchor="w")
            label = ttk.Label(cell, text="—", style="Panel.TLabel")
            label.pack(anchor="w")
            self.info_labels[key] = label

        self.last_text = ttk.Label(card.content, text="（还没说过话）",
                                   style="Panel.TLabel", wraplength=760,
                                   justify="left")
        self.last_text.pack(anchor="w", pady=(12, 0))

        self.error_label = ttk.Label(card.content, text="", style="Bad.TLabel",
                                     wraplength=760, justify="left")

    def _build_actions(self, parent: ttk.Frame) -> None:
        card = Card(parent, "快捷操作", self.fonts)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        row = ttk.Frame(card.content, style="Panel.TFrame")
        row.pack(fill="x")
        self.btn_skip = ttk.Button(row, text="⏭ 跳过这条", command=self.backend.skip)
        self.btn_skip.pack(side="left")
        self.btn_pause = ttk.Button(row, text="⏸ 暂停说话", command=self._toggle_pause)
        self.btn_pause.pack(side="left", padx=8)
        ttk.Button(row, text="🗑 清空队列", command=self.backend.clear_queue).pack(side="left")

        ttk.Separator(card.content, orient="horizontal").pack(fill="x", pady=14)

        ttk.Label(card.content, text="让它说点什么（不用等弹幕）",
                  style="Panel.TLabel").pack(anchor="w")
        entry_row = ttk.Frame(card.content, style="Panel.TFrame")
        entry_row.pack(fill="x", pady=(6, 0))
        self.say_var = tk.StringVar()
        entry = ttk.Entry(entry_row, textvariable=self.say_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self._say())
        ttk.Button(entry_row, text="直接念", style="Accent.TButton",
                   command=self._say).pack(side="left", padx=(8, 0))
        ttk.Button(entry_row, text="让它发挥",
                   command=self._ask).pack(side="left", padx=(6, 0))
        ttk.Label(card.content,
                  text="「直接念」= 原样读出来；「让它发挥」= 交给 AI 自己组织语言",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(6, 0))

    def _build_stream(self, parent: ttk.Frame) -> None:
        card = Card(parent, "直播间动态", self.fonts)
        card.grid(row=2, column=0, sticky="nsew")
        parent.rowconfigure(2, weight=1)

        head = ttk.Frame(card.content, style="Panel.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="弹幕 + AI 播报 + 运行日志",
                  style="PanelMuted.TLabel").pack(side="left")
        ttk.Button(head, text="清空", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right")

        wrapper = ttk.Frame(card.content, style="Panel.TFrame")
        wrapper.pack(fill="both", expand=True, pady=(8, 0))

        self.stream = theme.make_text(wrapper, self.fonts, height=16, wrap="word")
        self.stream.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=self.stream.yview)
        scroll.pack(side="right", fill="y")
        self.stream.configure(yscrollcommand=scroll.set, state="disabled")

        self.stream.tag_configure("time", foreground=theme.FG_DIM)
        self.stream.tag_configure("chat", foreground=theme.FG_MUTED)
        self.stream.tag_configure("hit", foreground="#7fb2ff")
        self.stream.tag_configure("say", foreground=theme.FG)
        self.stream.tag_configure("idle", foreground="#c9a2ff")
        self.stream.tag_configure("say_label", foreground=theme.SUCCESS)
        self.stream.tag_configure("error", foreground=theme.DANGER)
        self.stream.tag_configure("info", foreground=theme.FG_DIM)

    # ------------------------------------------------------------------

    def _start(self) -> None:
        if self.backend.start_radio():
            self.append("info", "正在启动电台…")

    def _stop(self) -> None:
        self.backend.stop_radio()

    def _toggle_pause(self) -> None:
        state = self.app.last_state or {}
        if state.get("paused"):
            self.backend.resume()
        else:
            self.backend.pause()

    def _say(self) -> None:
        text = self.say_var.get().strip()
        if not text:
            return
        self.backend.say(text)
        self.say_var.set("")

    def _ask(self) -> None:
        text = self.say_var.get().strip()
        if not text:
            return
        self.backend.ask(text)
        self.say_var.set("")

    def _clear_log(self) -> None:
        self.stream.configure(state="normal")
        self.stream.delete("1.0", "end")
        self.stream.configure(state="disabled")
        self._log_lines = 0

    # ------------------------------------------------------------------

    def append(self, kind: str, text: str, prefix: str = "") -> None:
        stamp = time.strftime("%H:%M:%S")
        self.stream.configure(state="normal")
        self.stream.insert("end", f"{stamp} ", "time")
        if prefix:
            self.stream.insert("end", prefix, kind)
        self.stream.insert("end", text + "\n", kind)
        self._log_lines += 1
        if self._log_lines > MAX_LOG_LINES:
            self.stream.delete("1.0", "200.0")
            self._log_lines -= 200
        self.stream.see("end")
        self.stream.configure(state="disabled")

    # ------------------------------------------------------------------

    def on_state(self, state: dict) -> None:
        running = state.get("running", False)
        speaking = state.get("speaking", False)
        paused = state.get("paused", False)

        if not running:
            text, color = "未开播", theme.FG_DIM
        elif paused:
            text, color = "已暂停", theme.WARN
        elif speaking:
            text, color = "正在说话", theme.SUCCESS
        else:
            text, color = "待命（等弹幕或念稿）", theme.ACCENT

        self.status_label.configure(text=text)
        self.dot.itemconfigure(self._dot_id, fill=color)

        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.btn_pause.configure(text="▶ 继续说话" if paused else "⏸ 暂停说话")

        self.info_labels["persona"].configure(text=state.get("persona") or "—")
        voice = state.get("voice") or "—"
        self.info_labels["voice"].configure(text=self._short_voice(voice))
        self.info_labels["pending"].configure(text=str(state.get("pending", 0)))
        self.info_labels["spoken"].configure(text=f"{state.get('spoken_count', 0)} 次")

        last = state.get("last_text") or ""
        if last:
            self.last_text.configure(text=f"上一句：{last}")

        error = state.get("last_error") or ""
        if error and state.get("error_streak"):
            self.error_label.configure(
                text=f"⚠ 最近出错（连续 {state['error_streak']} 次）：{error}")
            self.error_label.pack(anchor="w", pady=(8, 0))
        else:
            self.error_label.pack_forget()

    @staticmethod
    def _short_voice(voice: str) -> str:
        text = voice.split(" rate=")[0]
        for prefix in ("zh_CN-", "zh-CN-", "en_US-"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        return text
