"""图形界面自测：把每个页签都建出来、切一遍，看有没有异常。

用法：
    python tools/selftest_gui.py              # 只测不显示（会快速开关一遍）
    python tools/selftest_gui.py --cycle 3    # 每个页签停 3 秒，方便外面截图
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radio.config import ensure_config  # noqa: E402

TABS = ["电台", "音色", "人设", "稿件", "设置"]
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle", type=float, default=0.0,
                        help="每个页签停留多少秒（0 = 不显示，只做构造测试）")
    parser.add_argument("--hold", type=int, default=-1,
                        help="停在第 N 个页签上不动，方便截图（0=电台 1=音色 …）")
    args = parser.parse_args()

    # 用副本跑，别碰用户的真实配置 ——
    # 界面里有些操作（切引擎、换音色、拖参数）会把设置写回 config.toml，
    # 自测不应该改动用户自己的设置。
    #
    # 副本必须放在项目根目录：配置里的相对路径（personas/ models/ 等）
    # 是相对配置文件所在目录解析的，放到 logs/ 下面会全部落空。
    # config.*.toml 已经在 .gitignore 里，不会被提交。
    real_config = ensure_config()
    scratch = ROOT / "config.selftest.toml"
    try:
        scratch.write_text(real_config.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        scratch.write_text("", encoding="utf-8")
    config_path = scratch
    print(f"（配置副本：{scratch.name}，不会动你的 config.toml）")

    from radio.ui.app import RadioGUI

    print("=" * 60)
    print(" AI 电台 · 图形界面自测")
    print("=" * 60)

    try:
        gui = RadioGUI(config_path)
    except Exception:
        print("界面构造失败：")
        traceback.print_exc()
        return 1

    check("主窗口构造成功", True)

    # 各页签在构造时就已经建好了，这里逐个切过去并强制重绘
    for index, name in enumerate(TABS):
        try:
            gui.notebook.select(index)
            gui.root.update_idletasks()
            gui.root.update()
            check(f"页签「{name}」渲染正常", True)
        except Exception as exc:
            check(f"页签「{name}」渲染正常", False, f"{type(exc).__name__}: {exc}")

    # 顺手验一下界面上那些真实控件在不在
    try:
        check("音色页有音色列表", hasattr(gui.tab_voice, "profiles_box"))
        check("人设页载入了人设", len(gui.tab_persona._personas) >= 1,
              f"{len(gui.tab_persona._personas)} 个")
        check("稿件页载入了稿件", len(gui.tab_script._files) >= 1,
              f"{len(gui.tab_script._files)} 个")
        check("设置页读到了配置", gui.tab_settings.platform_var.get() != "")
        check("后台事件循环在跑", gui.backend._thread.is_alive())

        profiles = gui.backend.kit.profiles()
        check("音色库能读出音色列表", isinstance(profiles, list),
              "、".join(p.display for p in profiles) or "（当前没有音色）")
        check("克隆引擎资源齐备", gui.backend.kit.ready(),
              "、".join(gui.backend.kit.missing_parts()) or "全部就位")
        if profiles:
            gui.tab_voice._preview(profiles[0].key)
            gui.root.update()
            check("试听按钮能触发（后台合成中）", True)
    except Exception as exc:
        check("界面控件检查", False, f"{type(exc).__name__}: {exc}")

    if args.hold >= 0:
        index = max(0, min(args.hold, len(TABS) - 1))
        gui.notebook.select(index)
        gui.root.update()
        print(f"\n  停在页签「{TABS[index]}」，线程保持运行，截图后可手动结束。",
              flush=True)
        print("READY", flush=True)
        gui.root.mainloop()
    elif args.cycle > 0:
        print(f"\n  开始轮播页签，每页 {args.cycle:.0f} 秒，现在可以截图了……")
        for index, name in enumerate(TABS):
            gui.notebook.select(index)
            gui.root.update()
            print(f"    TAB {index} = {name}", flush=True)
            end = gui.root.tk.call("clock", "milliseconds") + int(args.cycle * 1000)
            while gui.root.tk.call("clock", "milliseconds") < end:
                gui.root.update()
                gui.root.after(30)
    else:
        gui.root.update()
        gui.root.after(1500, gui.root.destroy)
        gui.root.mainloop()

    try:
        gui.backend.shutdown()
    except Exception:
        pass
    try:
        gui.root.destroy()
    except Exception:
        pass
    scratch.unlink(missing_ok=True)      # 清掉配置副本

    print("\n" + "=" * 60)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print(f"  ✗ {item}")
        return 1
    print("图形界面自测全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
