"""界面里复用的小组件。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import theme


class ScrollFrame(ttk.Frame):
    """可滚动的容器：内容放 .body 里。"""

    def __init__(self, parent: tk.Misc, **kwargs) -> None:
        super().__init__(parent, style="TFrame", **kwargs)
        self.canvas = tk.Canvas(self, background=theme.BG, highlightthickness=0,
                                borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical",
                                       command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.body = ttk.Frame(self.canvas, style="TFrame")
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda e: self._unbind_wheel())

    def _on_body_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class Card(ttk.Frame):
    """带标题的卡片。往里放东西用 .content。"""

    def __init__(self, parent: tk.Misc, title: str = "",
                 fonts: dict | None = None, pad: int = 16) -> None:
        super().__init__(parent, style="Panel.TFrame", padding=pad)
        fonts = fonts or {}
        if title:
            header = ttk.Label(self, text=title, style="PanelH2.TLabel")
            header.pack(anchor="w", pady=(0, 10))
        self.content = ttk.Frame(self, style="Panel.TFrame")
        self.content.pack(fill="both", expand=True)


class Field(ttk.Frame):
    """一行"标签 + 输入框"，可选右边挂个小按钮。"""

    def __init__(self, parent: tk.Misc, label: str, fonts: dict,
                 hint: str = "", width: int = 30) -> None:
        super().__init__(parent, style="Panel.TFrame")
        self.fonts = fonts
        top = ttk.Frame(self, style="Panel.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text=label, style="Panel.TLabel").pack(side="left")
        if hint:
            ttk.Label(top, text=hint, style="PanelMuted.TLabel").pack(side="left", padx=(8, 0))
        self.row = ttk.Frame(self, style="Panel.TFrame")
        self.row.pack(fill="x", pady=(4, 0))
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self.row, textvariable=self.var, width=width)
        self.entry.pack(side="left", fill="x", expand=True)

    @property
    def value(self) -> str:
        return self.var.get()

    @value.setter
    def value(self, text: str) -> None:
        self.var.set("" if text is None else str(text))


def labeled_scale(parent: tk.Misc, label: str, fonts: dict, *,
                  from_: float, to: float, value: float,
                  on_change: Callable[[float], None] | None = None,
                  fmt: Callable[[float], str] | None = None) -> ttk.Frame:
    """带数值显示和说明的滑块。"""
    frame = ttk.Frame(parent, style="Panel.TFrame")
    head = ttk.Frame(frame, style="Panel.TFrame")
    head.pack(fill="x")
    ttk.Label(head, text=label, style="Panel.TLabel").pack(side="left")
    value_label = ttk.Label(head, text="", style="PanelMuted.TLabel")
    value_label.pack(side="right")

    var = tk.DoubleVar(value=value)

    def update_display(v=None):
        current = var.get()
        value_label.configure(text=fmt(current) if fmt else f"{current:g}")

    def handler(v):
        update_display()
        if on_change:
            on_change(var.get())

    scale = ttk.Scale(frame, from_=from_, to=to, variable=var, command=handler)
    scale.pack(fill="x", pady=(6, 0))
    update_display()
    frame.var = var          # type: ignore[attr-defined]
    frame.scale = scale      # type: ignore[attr-defined]
    return frame


def separator(parent: tk.Misc, pady: int = 10) -> None:
    ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=pady)


def hint(parent: tk.Misc, text: str, fonts: dict) -> ttk.Label:
    label = ttk.Label(parent, text=text, style="PanelMuted.TLabel",
                      font=fonts.get("small"), justify="left", wraplength=640)
    label.pack(anchor="w", pady=(4, 0))
    return label
