"""「人设」页：在软件里直接选人设、改人设、新建人设。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from ..persona import (Persona, create_persona_file, list_personas,
                       save_persona)
from . import theme
from .widgets import Card, labeled_scale


class PersonaTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, app) -> None:
        super().__init__(parent, style="TFrame", padding=16)
        self.app = app
        self.fonts = app.fonts
        self.backend = app.backend
        self._current: Persona | None = None
        self._dirty = False

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self._build_list()
        self._build_editor()
        self.reload()

    # ------------------------------------------------------------------
    # 左侧：人设列表
    # ------------------------------------------------------------------

    def _build_list(self) -> None:
        card = Card(self, "人设列表", self.fonts)
        card.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        card.configure(width=210)

        self.listbox = tk.Listbox(
            card.content, background=theme.LOG_BG, foreground=theme.FG,
            selectbackground=theme.ACCENT, selectforeground="#ffffff",
            relief="flat", highlightthickness=1, highlightbackground=theme.BORDER,
            activestyle="none", font=self.fonts["body"], width=16, height=14,
        )
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_pick)

        row = ttk.Frame(card.content, style="Panel.TFrame")
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="＋ 新建", command=self._create).pack(side="left")
        ttk.Button(row, text="复制", command=self._duplicate).pack(side="left", padx=6)
        ttk.Button(row, text="删除", style="Ghost.TButton",
                   command=self._delete).pack(side="left")

        ttk.Button(card.content, text="设为当前人设", style="Accent.TButton",
                   command=self._activate).pack(fill="x", pady=(10, 0))

    # ------------------------------------------------------------------
    # 右侧：编辑器
    # ------------------------------------------------------------------

    def _build_editor(self) -> None:
        card = Card(self, "编辑", self.fonts)
        card.grid(row=0, column=1, sticky="nsew")

        params = ttk.Frame(card.content, style="Panel.TFrame")
        params.pack(fill="x")
        for col in (1, 3):
            params.columnconfigure(col, weight=1)

        self.name_var = tk.StringVar()
        self.alias_var = tk.StringVar()
        self.voice_var = tk.StringVar()
        self.rate_var = tk.StringVar()
        self.maxchars_var = tk.StringVar()

        self._field(params, "主持人名字", self.name_var, 0, 0)
        self._field(params, "别名（逗号分隔）", self.alias_var, 0, 2)
        self._field(params, "音色", self.voice_var, 1, 0, widget="combo")
        self._field(params, "语速", self.rate_var, 1, 2)

        row3 = ttk.Frame(card.content, style="Panel.TFrame")
        row3.pack(fill="x", pady=(10, 0))
        self.temp_box = labeled_scale(
            row3, "发挥程度", self.fonts, from_=0.4, to=1.6, value=1.0,
            fmt=lambda v: f"{v:.2f}", on_change=lambda v: self._mark_dirty())
        self.temp_box.pack(side="left", fill="x", expand=True, padx=(0, 16))

        self.maxchars_box = ttk.Frame(row3, style="Panel.TFrame")
        self.maxchars_box.pack(side="left")
        ttk.Label(self.maxchars_box, text="一次说多少字以内",
                  style="Panel.TLabel").pack(anchor="w")
        ttk.Entry(self.maxchars_box, textvariable=self.maxchars_var,
                  width=8).pack(anchor="w", pady=(6, 0))

        self.rules_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(card.content, text="自动追加运行规则"
                        "（只出口语、不提自己是 AI、控制长度）",
                        variable=self.rules_var,
                        style="Panel.TCheckbutton",
                        command=self._mark_dirty).pack(anchor="w", pady=(14, 0))

        ttk.Separator(card.content, orient="horizontal").pack(fill="x", pady=12)

        ttk.Label(card.content,
                  text="提示词正文 —— 下面这段会原样交给 AI，你想怎么写就怎么写",
                  style="Panel.TLabel").pack(anchor="w")
        ttk.Label(card.content,
                  text="就是给 AI 看的「人物设定书」：写它是什么人、怎么说话、不能做什么。",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(2, 8))

        wrapper = ttk.Frame(card.content, style="Panel.TFrame")
        wrapper.pack(fill="both", expand=True)
        self.editor = theme.make_text(wrapper, self.fonts, height=18, undo=True)
        self.editor.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=self.editor.yview)
        scroll.pack(side="right", fill="y")
        self.editor.configure(yscrollcommand=scroll.set)
        self.editor.bind("<<Modified>>", self._on_modified)

        actions = ttk.Frame(card.content, style="Panel.TFrame")
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="💾 保存", style="Accent.TButton",
                   command=self._save).pack(side="left")
        ttk.Button(actions, text="放弃修改", command=self._reload_current).pack(
            side="left", padx=8)
        ttk.Button(actions, text="试听这个音色", style="Ghost.TButton",
                   command=self._preview).pack(side="left")
        self.dirty_label = ttk.Label(actions, text="", style="PanelMuted.TLabel")
        self.dirty_label.pack(side="right")

    def _field(self, parent, label, var, row, col, widget="entry") -> None:
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(
            row=row * 2, column=col, columnspan=2, sticky="w", pady=(8, 2))
        if widget == "combo":
            self.voice_combo = ttk.Combobox(parent, textvariable=var, state="readonly",
                                            values=self._voice_values())
            self.voice_combo.grid(row=row * 2 + 1, column=col, columnspan=2, sticky="ew")
        else:
            ttk.Entry(parent, textvariable=var).grid(
                row=row * 2 + 1, column=col, columnspan=2, sticky="ew",
                padx=(0, 12) if col == 0 else 0)

    def _voice_choices(self) -> list[tuple[str, str]]:
        """返回 [(显示名, 音色key), ...]。显示名给人看，key 写进文件。"""
        out = [(profile.display, profile.key)
               for profile in self.backend.kit.profiles() if profile.ready]
        if not out:
            key = self.backend.cfg.tts.voice or "默认音色"
            out = [(key + "（还没有克隆音色）", key)]
        return out

    def _voice_values(self) -> list[str]:
        return [label for label, _ in self._voice_choices()]

    def _voice_label(self, key: str) -> str:
        for label, candidate in self._voice_choices():
            if candidate == key:
                return label
        return key

    def _voice_key(self, label: str) -> str:
        for candidate_label, key in self._voice_choices():
            if candidate_label == label:
                return key
        return label.strip()

    # ------------------------------------------------------------------
    # 数据流
    # ------------------------------------------------------------------

    def reload(self) -> None:
        self.backend.reload_config()
        directory = self.backend.cfg.persona_dir
        self._personas = list_personas(directory)
        self.listbox.delete(0, "end")
        current_key = self.backend.cfg.app.persona
        select_index = 0
        for index, persona in enumerate(self._personas):
            mark = "● " if persona.key == current_key else "  "
            self.listbox.insert("end", f"{mark}{persona.display_name}")
            if persona.key == current_key:
                select_index = index
        if self._personas:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(select_index)
            self._load(self._personas[select_index])
        self.app.refresh_voice_combo()

    def _on_pick(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        if self._dirty and not messagebox.askyesno(
                "有未保存的修改", "当前人设有改动还没保存，要放弃吗？"):
            return
        self._load(self._personas[selection[0]])

    def _load(self, persona: Persona) -> None:
        self._current = persona
        self.name_var.set(persona.name)
        self.alias_var.set("，".join(persona.alias) if persona.alias else "")
        self.voice_var.set(self._voice_label(persona.voice or self.backend.cfg.tts.voice))
        self.rate_var.set(persona.rate or self.backend.cfg.tts.rate)
        self.maxchars_var.set(str(persona.max_chars or 130))
        if persona.temperature is not None:
            self.temp_box.var.set(persona.temperature)
        self.rules_var.set(persona.append_rules)

        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", persona.prompt)
        self.editor.edit_modified(False)
        self._set_dirty(False)

    def _collect(self) -> Persona | None:
        if self._current is None:
            return None
        alias_text = self.alias_var.get().replace("，", ",").replace("、", ",")
        alias = [a.strip() for a in alias_text.split(",") if a.strip()]
        try:
            max_chars = int(self.maxchars_var.get() or 130)
        except ValueError:
            max_chars = 130

        persona = Persona(
            key=self._current.key,
            prompt=self.editor.get("1.0", "end").rstrip() + "\n",
            name=self.name_var.get().strip() or self._current.key,
            alias=alias,
            voice=self._voice_key(self.voice_var.get()),
            rate=self.rate_var.get().strip(),
            pitch=self._current.pitch,
            volume=self._current.volume,
            temperature=round(float(self.temp_box.var.get()), 2),
            max_chars=max_chars,
            append_rules=bool(self.rules_var.get()),
            extra=dict(self._current.extra),
            path=self._current.path,
        )
        return persona

    # ------------------------------------------------------------------

    def _mark_dirty(self) -> None:
        self.editor.edit_modified(False)
        self._set_dirty(True)

    def _on_modified(self, _event=None) -> None:
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self.dirty_label.configure(text="● 有未保存的修改" if dirty else "")

    def _save(self) -> None:
        persona = self._collect()
        if persona is None or persona.path is None:
            return
        try:
            save_persona(persona)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._set_dirty(False)
        self.app.log(f"人设「{persona.display_name}」已保存")
        self.app.set_hint(f"已保存：{persona.path.name}")

        # 运行中改人设，热重载一下，不用重启
        if self.backend.running:
            self.backend._with_director("reload_personas")
        self.reload()

    def _reload_current(self) -> None:
        if self._current is None:
            return
        refreshed = next((p for p in list_personas(self.backend.cfg.persona_dir)
                          if p.key == self._current.key), None)
        if refreshed:
            self._load(refreshed)

    def _activate(self) -> None:
        if self._current is None:
            return
        key = self._current.key
        self.backend.cfg.app.persona = key
        self.backend.persist_config()
        if self.backend.running:
            self.backend.switch_persona(key)
        self.app.log(f"当前人设 -> {self._current.display_name}")
        self.app.set_hint(f"已切换到「{self._current.display_name}」")
        self.reload()

    def _create(self) -> None:
        name = simpledialog.askstring("新建人设", "给人设起个名字（也是文件名）：",
                                      parent=self)
        if not name:
            return
        try:
            path = create_persona_file(self.backend.cfg.persona_dir, name)
        except Exception as exc:
            messagebox.showerror("新建失败", str(exc))
            return
        self.app.log(f"已新建人设 {path.name}")
        self.reload()
        keys = [p.key for p in self._personas]
        if path.stem in keys:
            index = keys.index(path.stem)
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(index)
            self._load(self._personas[index])

    def _duplicate(self) -> None:
        if self._current is None:
            return
        name = simpledialog.askstring("复制人设", "新名字：",
                                      initialvalue=self._current.key + "-副本",
                                      parent=self)
        if not name:
            return
        try:
            path = create_persona_file(self.backend.cfg.persona_dir, name)
        except Exception as exc:
            messagebox.showerror("复制失败", str(exc))
            return
        clone = self._collect()
        if clone is not None:
            clone.key = path.stem
            save_persona(clone, path)
        self.app.log(f"已复制为 {path.name}")
        self.reload()

    def _delete(self) -> None:
        if self._current is None:
            return
        if self._current.key == self.backend.cfg.app.persona:
            messagebox.showwarning("删不了", "这是当前正在用的人设，先切到别的再删。")
            return
        if not messagebox.askyesno("确认删除", f"删除人设「{self._current.display_name}」？"):
            return
        try:
            self._current.path.unlink(missing_ok=True)
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        self.app.log(f"已删除人设 {self._current.key}")
        self.reload()

    def _preview(self) -> None:
        voice = self._voice_key(self.voice_var.get()) or self.backend.cfg.tts.voice
        rate = self.rate_var.get().strip() or self.backend.cfg.tts.rate
        self.app.set_hint("正在试听…")
        self.backend.preview("各位听众晚上好，欢迎收听今晚的节目。", voice, rate,
                             on_done=lambda r: self.app.set_hint(str(r)))
