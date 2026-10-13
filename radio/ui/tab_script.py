"""「稿件」页：在软件里写、改、加稿子。

没人跟 AI 说话的时候，它就照着这里的稿子念。
稿件用空行分段，一段就是一次"开口"的内容。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from ..memory import split_script
from . import theme
from .widgets import Card

SCRIPT_SUFFIX = (".md", ".markdown", ".txt")

TEMPLATE = """\
# {name}

（第一行以 # 开头的是标题，不会被念出来）

在这里写第一段。空行分段，每一段会被单独念一次。

这是第二段。你可以随便写，AI 会按人设的语气把它讲出来。

---
单独一行的 --- 是硬分隔，前后肯定是两段。

// 以两个斜杠开头的行是注释，也不会被念
"""


class ScriptTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, app) -> None:
        super().__init__(parent, style="TFrame", padding=16)
        self.app = app
        self.fonts = app.fonts
        self.backend = app.backend
        self._files: list = []
        self._current = None
        self._dirty = False

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self._build_list()
        self._build_editor()
        self.reload()

    # ------------------------------------------------------------------

    def _build_list(self) -> None:
        card = Card(self, "稿件文件", self.fonts)
        card.grid(row=0, column=0, sticky="nsw", padx=(0, 12))

        self.listbox = tk.Listbox(
            card.content, background=theme.LOG_BG, foreground=theme.FG,
            selectbackground=theme.ACCENT, selectforeground="#ffffff",
            relief="flat", highlightthickness=1, highlightbackground=theme.BORDER,
            activestyle="none", font=self.fonts["body"], width=18, height=14,
        )
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_pick)

        row = ttk.Frame(card.content, style="Panel.TFrame")
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="＋ 新建", command=self._create).pack(side="left")
        ttk.Button(row, text="删除", style="Ghost.TButton",
                   command=self._delete).pack(side="left", padx=6)

        self.summary = ttk.Label(card.content, text="", style="PanelMuted.TLabel",
                                 wraplength=170, justify="left")
        self.summary.pack(anchor="w", pady=(10, 0))

    def _build_editor(self) -> None:
        card = Card(self, "编辑稿件", self.fonts)
        card.grid(row=0, column=1, sticky="nsew")

        head = ttk.Frame(card.content, style="Panel.TFrame")
        head.pack(fill="x")
        self.title = ttk.Label(head, text="—", style="Panel.TLabel",
                               font=self.fonts["bold"])
        self.title.pack(side="left")
        self.counter = ttk.Label(head, text="", style="PanelMuted.TLabel")
        self.counter.pack(side="right")

        wrapper = ttk.Frame(card.content, style="Panel.TFrame")
        wrapper.pack(fill="both", expand=True, pady=(10, 0))
        self.editor = theme.make_text(wrapper, self.fonts, height=20, undo=True)
        self.editor.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=self.editor.yview)
        scroll.pack(side="right", fill="y")
        self.editor.configure(yscrollcommand=scroll.set)
        self.editor.bind("<<Modified>>", self._on_modified)
        self.editor.bind("<KeyRelease>", lambda e: self._update_counter())

        actions = ttk.Frame(card.content, style="Panel.TFrame")
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="💾 保存", style="Accent.TButton",
                   command=self._save).pack(side="left")
        ttk.Button(actions, text="放弃修改", command=self._reload_current).pack(
            side="left", padx=8)
        ttk.Button(actions, text="在文件夹里打开", style="Ghost.TButton",
                   command=self._open_folder).pack(side="left")
        self.dirty_label = ttk.Label(actions, text="", style="PanelMuted.TLabel")
        self.dirty_label.pack(side="right")

    # ------------------------------------------------------------------

    def reload(self) -> None:
        self.backend.reload_config()
        directory = self.backend.cfg.script_dir
        directory.mkdir(parents=True, exist_ok=True)
        self._files = sorted(
            (p for p in directory.iterdir()
             if p.is_file() and p.suffix.lower() in SCRIPT_SUFFIX
             and not p.name.startswith("_")),
            key=lambda p: p.name,
        )
        self.listbox.delete(0, "end")
        for path in self._files:
            self.listbox.insert("end", "  " + path.name)
        if self._files:
            self.listbox.selection_set(0)
            self._load(self._files[0])
        else:
            self._current = None
            self.editor.delete("1.0", "end")
            self.title.configure(text="（还没有稿件）")
            self.summary.configure(text="点「＋ 新建」加一份")
        self._update_summary()

    def _update_summary(self) -> None:
        total = 0
        for path in self._files:
            try:
                total += len(split_script(path.read_text(encoding="utf-8")))
            except Exception:
                pass
        self.summary.configure(
            text=f"共 {len(self._files)} 个文件 / {total} 段稿子\n"
                 f"（{self.backend.cfg.script_dir}）"
        )

    def _on_pick(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        if self._dirty and not messagebox.askyesno(
                "有未保存的修改", "当前稿件有改动还没保存，要放弃吗？"):
            return
        self._load(self._files[selection[0]])

    def _load(self, path) -> None:
        self._current = path
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.editor.edit_modified(False)
        self.title.configure(text=path.name)
        self._set_dirty(False)
        self._update_counter()

    def _update_counter(self) -> None:
        text = self.editor.get("1.0", "end")
        segments = len(split_script(text))
        self.counter.configure(text=f"{len(text.strip())} 字 · {segments} 段可念")

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self.dirty_label.configure(text="● 有未保存的修改" if dirty else "")

    def _on_modified(self, _event=None) -> None:
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            self._set_dirty(True)
            self._update_counter()

    # ------------------------------------------------------------------

    def _save(self) -> None:
        if self._current is None:
            self._create()
            return
        try:
            self._current.write_text(self.editor.get("1.0", "end"), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._set_dirty(False)
        self._update_summary()
        self.app.log(f"稿件 {self._current.name} 已保存")
        self.app.set_hint(f"已保存：{self._current.name}")

        if self.backend.running:
            self.backend._with_director("reload_scripts")

    def _reload_current(self) -> None:
        if self._current is not None:
            self._load(self._current)

    def _create(self) -> None:
        name = simpledialog.askstring("新建稿件", "文件名（不用写扩展名）：",
                                      parent=self)
        if not name:
            return
        name = name.strip()
        if any(ch in name for ch in '\\/:*?"<>|'):
            messagebox.showerror("名字不合法", '不能包含 \\ / : * ? " < > |')
            return
        path = self.backend.cfg.script_dir / f"{name}.md"
        if path.exists():
            messagebox.showerror("已存在", f"{path.name} 已经在了")
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(TEMPLATE.format(name=name), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("创建失败", str(exc))
            return
        self.app.log(f"已新建稿件 {path.name}")
        self.reload()
        for index, item in enumerate(self._files):
            if item == path:
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(index)
                self._load(path)
                break

    def _delete(self) -> None:
        if self._current is None:
            return
        if not messagebox.askyesno("确认删除", f"删除 {self._current.name}？"):
            return
        try:
            self._current.unlink()
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        self.app.log(f"已删除稿件 {self._current.name}")
        self.reload()

    def _open_folder(self) -> None:
        import subprocess
        import sys
        folder = self.backend.cfg.script_dir
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(folder)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror("打不开", str(exc))
