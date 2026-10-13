"""图形界面的配色和字体。

用 ttk 的 clam 主题做底，这样 Windows 上也能完全控制颜色，
不至于出现系统原生控件和深色背景打架的情况。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ---- 配色 ----
BG = "#13151a"
PANEL = "#1b1e26"
PANEL_2 = "#22262f"
BORDER = "#2c313d"
FG = "#e7e9ef"
FG_MUTED = "#8b93a7"
FG_DIM = "#5f6779"
ACCENT = "#3b6ef0"
ACCENT_HOVER = "#4d7bf5"
SUCCESS = "#3fb950"
WARN = "#d29922"
DANGER = "#f0525f"
LOG_BG = "#0f1116"

FONT_FAMILY = "Microsoft YaHei UI"
FONT_MONO = "Consolas"


def pick_font(root: tk.Misc, family: str, size: int, weight: str = "normal") -> tkfont.Font:
    """挑一个系统里真有的字体，没有就退回默认。"""
    available = set(tkfont.families(root))
    for candidate in (family, "Microsoft YaHei", "微软雅黑", "SimHei"):
        if candidate in available:
            return tkfont.Font(root=root, family=candidate, size=size, weight=weight)
    return tkfont.Font(root=root, size=size, weight=weight)


def setup_theme(root: tk.Tk) -> dict[str, tkfont.Font]:
    """把整套样式装到 root 上，返回字体表。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    fonts = {
        "body": pick_font(root, FONT_FAMILY, 10),
        "small": pick_font(root, FONT_FAMILY, 9),
        "tiny": pick_font(root, FONT_FAMILY, 8),
        "bold": pick_font(root, FONT_FAMILY, 10, "bold"),
        "h1": pick_font(root, FONT_FAMILY, 17, "bold"),
        "h2": pick_font(root, FONT_FAMILY, 12, "bold"),
        "mono": tkfont.Font(root=root, family=FONT_MONO, size=9),
    }

    root.configure(bg=BG)
    root.option_add("*Font", fonts["body"])

    # ---- 通用 ----
    style.configure(".", background=BG, foreground=FG, font=fonts["body"],
                    borderwidth=0, focuscolor=ACCENT)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("Panel2.TFrame", background=PANEL_2)

    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Muted.TLabel", background=BG, foreground=FG_MUTED, font=fonts["small"])
    style.configure("Panel.TLabel", background=PANEL, foreground=FG)
    style.configure("PanelMuted.TLabel", background=PANEL, foreground=FG_MUTED,
                    font=fonts["small"])
    style.configure("H1.TLabel", background=BG, foreground=FG, font=fonts["h1"])
    style.configure("H2.TLabel", background=BG, foreground=FG, font=fonts["h2"])
    style.configure("PanelH2.TLabel", background=PANEL, foreground=FG, font=fonts["h2"])
    style.configure("Ok.TLabel", background=PANEL, foreground=SUCCESS, font=fonts["small"])
    style.configure("Warn.TLabel", background=PANEL, foreground=WARN, font=fonts["small"])
    style.configure("Bad.TLabel", background=PANEL, foreground=DANGER, font=fonts["small"])

    # ---- 按钮 ----
    style.configure("TButton", background=PANEL_2, foreground=FG,
                    bordercolor=BORDER, lightcolor=PANEL_2, darkcolor=PANEL_2,
                    focuscolor=PANEL_2, padding=(12, 7), relief="flat")
    style.map("TButton",
              background=[("active", BORDER), ("disabled", PANEL)],
              foreground=[("disabled", FG_DIM)])

    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
    style.map("Accent.TButton",
              background=[("active", ACCENT_HOVER), ("disabled", "#26406f")],
              foreground=[("disabled", FG_DIM)])

    style.configure("Danger.TButton", background="#5c2027", foreground="#ffd9dc",
                    bordercolor="#7a2b34", lightcolor="#5c2027", darkcolor="#5c2027")
    style.map("Danger.TButton", background=[("active", "#7a2b34")])

    style.configure("Ghost.TButton", background=PANEL, foreground=FG_MUTED,
                    padding=(10, 5))
    style.map("Ghost.TButton", background=[("active", PANEL_2)],
              foreground=[("active", FG)])

    # ---- 输入 ----
    style.configure("TEntry", fieldbackground=LOG_BG, background=LOG_BG,
                    foreground=FG, bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER, insertcolor=FG, padding=7)
    style.map("TEntry", bordercolor=[("focus", ACCENT)])

    style.configure("TCombobox", fieldbackground=LOG_BG, background=PANEL_2,
                    foreground=FG, bordercolor=BORDER, arrowcolor=FG_MUTED,
                    lightcolor=BORDER, darkcolor=BORDER, padding=5)
    style.map("TCombobox",
              fieldbackground=[("readonly", LOG_BG)],
              foreground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", PANEL_2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    style.configure("TCheckbutton", background=BG, foreground=FG, focuscolor=BG)
    style.map("TCheckbutton", background=[("active", BG)],
              indicatorcolor=[("selected", ACCENT), ("!selected", LOG_BG)])
    style.configure("Panel.TCheckbutton", background=PANEL, foreground=FG, focuscolor=PANEL)
    style.map("Panel.TCheckbutton", background=[("active", PANEL)])

    # ---- 滑块 ----
    style.configure("TScale", background=PANEL, troughcolor=LOG_BG,
                    bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)
    style.map("TScale", background=[("active", PANEL)])

    # ---- 滚动条 ----
    style.configure("Vertical.TScrollbar", background=PANEL_2, troughcolor=BG,
                    bordercolor=BG, arrowcolor=FG_MUTED, relief="flat")
    style.map("Vertical.TScrollbar", background=[("active", BORDER)])
    style.configure("Horizontal.TScrollbar", background=PANEL_2, troughcolor=BG,
                    bordercolor=BG, arrowcolor=FG_MUTED, relief="flat")

    # ---- Notebook（页签）----
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
    style.configure("TNotebook.Tab", background=BG, foreground=FG_MUTED,
                    padding=(20, 10), borderwidth=0, font=fonts["bold"])
    style.map("TNotebook.Tab",
              background=[("selected", PANEL)],
              foreground=[("selected", FG)],
              expand=[("selected", (0, 0, 0, 0))])

    # ---- 分隔线 ----
    style.configure("TSeparator", background=BORDER)

    # ---- 进度条 ----
    style.configure("TProgressbar", background=ACCENT, troughcolor=LOG_BG,
                    bordercolor=PANEL, lightcolor=ACCENT, darkcolor=ACCENT,
                    thickness=8)

    return fonts


def make_text(parent: tk.Misc, fonts: dict, *, mono: bool = False,
              readonly: bool = False, **kwargs) -> tk.Text:
    """统一样式的多行文本框。

    kwargs 里的设置会覆盖这里的默认值（比如 height / wrap）。
    """
    options = dict(
        background=LOG_BG,
        foreground=FG,
        insertbackground=FG,
        selectbackground=ACCENT,
        selectforeground="#ffffff",
        relief="flat",
        borderwidth=0,
        highlightthickness=1,
        highlightbackground=BORDER,
        highlightcolor=ACCENT,
        font=fonts["mono"] if mono else fonts["body"],
        padx=10,
        pady=8,
        wrap="word",
        height=10,
    )
    options.update(kwargs)
    text = tk.Text(parent, **options)
    if readonly:
        text.configure(state="disabled")
    return text
