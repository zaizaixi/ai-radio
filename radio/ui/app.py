"""主窗口。

结构很简单：标题栏 + 五个页签 + 底部状态条。
所有耗时的活都在后台线程，界面靠一个 120ms 的定时器取事件，永远不会卡住。
"""

from __future__ import annotations

import queue
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from . import theme
from .backend import EV_CHAT, EV_DONE, EV_LOG, EV_PROGRESS, EV_SAY, EV_STATUS, RadioBackend
from .tab_persona import PersonaTab
from .tab_radio import RadioTab
from .tab_script import ScriptTab
from .tab_settings import SettingsTab
from .tab_voice import VoiceTab

APP_TITLE = "AI 电台 · 直播主持助手"


class RadioGUI:
    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1120x780")
        self.root.minsize(980, 660)
        self.fonts = theme.setup_theme(self.root)

        self.events: queue.Queue = queue.Queue()
        self.last_state: dict = {}
        self.backend = RadioBackend(self.config_path, self.events)

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._pump)
        self.root.after(400, self._first_run_check)

    # ------------------------------------------------------------------

    def _build(self) -> None:
        header = ttk.Frame(self.root, style="TFrame", padding=(18, 14, 18, 0))
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, style="H1.TLabel").pack(side="left")
        self.header_status = ttk.Label(header, text="未开播", style="Muted.TLabel")
        self.header_status.pack(side="left", padx=(14, 0), pady=(6, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(10, 0))

        self.tab_radio = RadioTab(self.notebook, self)
        self.tab_voice = VoiceTab(self.notebook, self)
        self.tab_persona = PersonaTab(self.notebook, self)
        self.tab_script = ScriptTab(self.notebook, self)
        self.tab_settings = SettingsTab(self.notebook, self)

        self.notebook.add(self.tab_radio, text="  电台  ")
        self.notebook.add(self.tab_voice, text="  音色  ")
        self.notebook.add(self.tab_persona, text="  人设  ")
        self.notebook.add(self.tab_script, text="  稿件  ")
        self.notebook.add(self.tab_settings, text="  设置  ")

        status = ttk.Frame(self.root, style="TFrame", padding=(18, 8, 18, 12))
        status.pack(fill="x")
        self.hint_label = ttk.Label(status, text="", style="Muted.TLabel")
        self.hint_label.pack(side="left")

        self.control_label = ttk.Label(status, text="", style="Muted.TLabel")
        self.control_label.pack(side="right")

    # ------------------------------------------------------------------

    def _first_run_check(self) -> None:
        cfg = self.backend.cfg
        kit = self.backend.kit
        missing = []
        if not kit.ready():
            missing.append("本地克隆引擎")
        elif not any(p.ready for p in kit.profiles()):
            missing.append("克隆音色")
        if not cfg.llm.api_key:
            missing.append("API Key")
        if not cfg.chat.room_id and cfg.chat.platform == "bilibili":
            missing.append("直播间房间号")

        if missing:
            self.notebook.select(self.tab_voice)
            if "克隆音色" in missing:
                self.set_hint(
                    "引擎已就绪，但还缺一个音色 —— 点「🎙 现在录一个」，"
                    "念一句话（5~8 秒）就能用你自己的声音了。")
            else:
                self.set_hint(f"还差几样东西才能开播：{'、'.join(missing)}。"
                              "先在「音色」页点「一键安装」。")
            self.log(f"首次使用检查：缺少 {'、'.join(missing)}")
        else:
            self.set_hint("一切就绪，点「电台」页的「▶ 开播」就可以开始了。")
            self.log("首次使用检查：全部就绪")

    # ------------------------------------------------------------------
    # 界面给页签用的接口
    # ------------------------------------------------------------------

    def log(self, text: str) -> None:
        self.tab_radio.append("info", text)

    def set_hint(self, text: str) -> None:
        self.hint_label.configure(text=text)

    def current_persona(self):
        return getattr(self.tab_persona, "_current", None)

    def refresh_voice_combo(self) -> None:
        try:
            values = self.tab_persona._voice_values()
            self.tab_persona.voice_combo.configure(values=values)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 事件泵
    # ------------------------------------------------------------------

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                try:
                    self._handle(kind, payload)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _handle(self, kind: str, payload: dict) -> None:
        if kind == EV_LOG:
            self.tab_radio.append("info", payload.get("message", ""))
        elif kind == EV_SAY:
            label = payload.get("kind", "开口")
            tag = "idle" if label == "念稿" else "say"
            self.tab_radio.append(tag, payload.get("text", ""), prefix=f"[{label}] ")
        elif kind == EV_CHAT:
            who = payload.get("username") or "观众"
            text = payload.get("text", "")
            if payload.get("hit"):
                self.tab_radio.append("hit", f"{who}：{text}", prefix="✦ 点名 ")
            else:
                self.tab_radio.append("chat", f"{who}：{text}")
        elif kind == EV_STATUS:
            self.last_state = payload
            self.tab_radio.on_state(payload)
            self._update_header(payload)
        elif kind == EV_PROGRESS:
            self.tab_voice.on_progress(payload)
        elif kind == EV_DONE:
            callback = payload.get("callback")
            result = payload.get("result")
            if callback is not None:
                try:
                    callback(result)
                except Exception:
                    pass

    def _update_header(self, state: dict) -> None:
        if state.get("running"):
            if state.get("paused"):
                text = "⏸ 已暂停"
            elif state.get("speaking"):
                text = "🔊 正在说话"
            else:
                text = "● 播出中"
            self.header_status.configure(text=text)
            port = self.backend.cfg.control.port
            self.control_label.configure(
                text=f"远程控制台（手机也能开）：http://127.0.0.1:{port}")
        else:
            self.header_status.configure(text="未开播")
            self.control_label.configure(text="")

    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        self.set_hint("正在退出…")
        self.root.update_idletasks()
        try:
            self.backend.shutdown()
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def launch(config_path: Path) -> int:
    gui = RadioGUI(config_path)
    gui.run()
    return 0
