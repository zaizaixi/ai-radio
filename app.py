#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 电台 · 图形界面入口。

双击「启动.bat」或者运行：

    python app.py

不想用图形界面的话，命令行模式在 run.py：

    python run.py           # 直接开播
    python run.py setup     # 一键安装本地语音
    python run.py check     # 环境自检
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


def setup_console() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def clear_internet_marks() -> None:
    """清掉「来自互联网」标记，免得一运行就弹「未知发布者」。

    浏览器下载的 ZIP 解压出来，每个文件都会被 Windows 打上
    Zone.Identifier 标记；双击 启动.bat 时 ShellExecute 看到这个标记
    就会弹安全警告。这里把同目录的启动器和 sherpa 的二进制都清一遍，
    所以第一次点过之后就不会再出现。

    注意：Python 执行 .py 文件不走 ShellExecute，本身不会触发提示，
    所以这里只需要管 .bat / .exe / .dll。
    """
    try:
        from radio.winfs import unblock_project

        cleared, found = unblock_project(PROJECT_ROOT)
        if cleared:
            print(f"已清除 {cleared} 个文件的「来自互联网」标记"
                  f"（消除「未知发布者」提示）")
        elif found:
            print(f"注意到 {found} 个文件仍带「来自互联网」标记，"
                  "如果启动时弹出安全警告，点「运行」即可。")
    except Exception:
        pass


def main() -> int:
    setup_console()
    clear_internet_marks()

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("这台机器的 Python 没有带 tkinter，打不开图形界面。")
        print("你可以直接用命令行模式：")
        print("    python run.py setup    # 一键安装本地语音")
        print("    python run.py          # 开播")
        return 2

    from radio.config import ensure_config, load_config
    from radio.ui import launch

    config_path = ensure_config()
    try:
        load_config(config_path)      # 提前报配置错误，别等界面起来才炸
    except Exception as exc:
        print(f"config.toml 解析失败：{exc}")
        print(f"请检查 {config_path} 的格式。")
        return 2

    try:
        return launch(config_path)
    except Exception:
        print("\n界面启动失败，详细信息：\n")
        traceback.print_exc()
        print("\n把上面的内容发出来可以帮你定位问题。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
