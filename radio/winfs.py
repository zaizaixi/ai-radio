"""Windows 文件安全标记处理（Mark-of-the-Web）。

为什么需要这个：
    从浏览器下载的文件，Windows 会给它加一条名为 Zone.Identifier 的
    备用数据流（ADS），俗称 Mark-of-the-Web。带着这个标记的可执行文件
    一运行，就会弹「Windows 保护了你的电脑」那个蓝色大警告。

    本程序会从 GitHub 下载 sherpa-onnx 的 exe 和 dll —— 它们天然带标记，
    所以装完之后必须把标记清掉，否则用户第一次开麦就会撞上安全提示。

清标记的做法，就是资源管理器里「右键 → 属性 → 解除锁定」干的事：
    删掉文件上那条 Zone.Identifier 备用数据流。

安全性说明：
    这个操作是把「来自互联网」的标记去掉，不改变文件内容，
    也不需要管理员权限。它不能、也不会绕过杀毒软件对文件本身的判定 ——
    如果文件真的是恶意的，杀毒照样拦。这里只是省掉「未知发布者」的提示。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("radio.winfs")

ZONE_SUFFIX = ":Zone.Identifier"
IS_WINDOWS = os.name == "nt"

# 只处理这些类型就够了 —— 真正会被系统拦的是可执行文件和库
DEFAULT_EXTS = (".exe", ".dll", ".bat", ".cmd", ".ps1", ".pyd")


def is_blocked(path: str | Path) -> bool:
    """这个文件带不带「来自互联网」的标记。"""
    if not IS_WINDOWS:
        return False
    try:
        return os.path.exists(str(path) + ZONE_SUFFIX)
    except OSError:
        return False


def unblock(path: str | Path) -> bool:
    """清掉一个文件的标记。返回是否真的清掉了。"""
    if not IS_WINDOWS:
        return False
    target = str(Path(path))
    if not os.path.exists(target):
        return False
    try:
        import ctypes

        # DeleteFileW 对备用数据流同样有效：路径写成 "文件:流名" 即可
        ok = ctypes.windll.kernel32.DeleteFileW(
            ctypes.c_wchar_p(str(Path(target).resolve()) + ZONE_SUFFIX))
        return bool(ok)
    except Exception as exc:
        log.debug("解除锁定失败 %s：%s", target, exc)
        return False


def unblock_tree(root: str | Path, exts: tuple[str, ...] | None = None,
                 limit: int = 20000) -> int:
    """把一棵目录里所有该处理的文件都解除锁定，返回处理了几个。

    默认只碰可执行文件和库 —— 那些才是会触发系统提示的。
    传 exts=None 用默认表；传空元组表示处理所有文件。
    """
    if not IS_WINDOWS:
        return 0
    root = Path(root)
    if not root.exists():
        return 0
    wanted = DEFAULT_EXTS if exts is None else exts
    count = 0
    try:
        walker = root.rglob("*")
        for path in walker:
            if count >= limit:
                break
            try:
                if not path.is_file():
                    continue
                if wanted and path.suffix.lower() not in wanted:
                    continue
                if unblock(path):
                    count += 1
            except OSError:
                continue
    except Exception as exc:
        log.debug("遍历 %s 时出错：%s", root, exc)
    if count:
        log.info("已为 %s 个文件解除「来自互联网」标记（%s）", count, root)
    return count


def unblock_project(root: str | Path, quiet: bool = True) -> tuple[int, int]:
    """首次运行时把会被 Windows 拦的文件解锁定。

    只扫「会触发系统提示的地方」，不递归整个项目：
        * 项目根目录下的 .bat / .cmd / .ps1（启动器就是这里）
        * sherpa/ 下的 exe 和 dll（要真正执行的二进制）
        * tools/ 下的 .py 一般不用管 —— Python 执行文件不走 ShellExecute，
          不会触发「未知发布者」提示

    返回 (清掉几个, 发现几个带标记的)。
    """
    if not IS_WINDOWS:
        return 0, 0
    root = Path(root)
    targets: list[Path] = []

    # 根目录的启动器
    try:
        for child in root.iterdir():
            if child.is_file() and child.suffix.lower() in (".bat", ".cmd", ".ps1"):
                targets.append(child)
    except OSError:
        pass

    # 要执行的二进制
    for sub in ("sherpa", "tools"):
        directory = root / sub
        if not directory.is_dir():
            continue
        try:
            for path in directory.rglob("*"):
                if path.is_file() and path.suffix.lower() in (".exe", ".dll", ".pyd"):
                    targets.append(path)
        except OSError:
            continue

    found = cleared = 0
    for path in targets:
        if is_blocked(path):
            found += 1
            if unblock(path):
                cleared += 1
    if cleared and not quiet:
        log.info("已清除 %s 个文件的「来自互联网」标记", cleared)
    return cleared, found


def blocked_launchers(root: str | Path) -> list[str]:
    """列出还带着标记的启动器 —— 用来给用户一句明确提示。"""
    if not IS_WINDOWS:
        return []
    root = Path(root)
    out: list[str] = []
    try:
        for child in root.iterdir():
            if child.is_file() and child.suffix.lower() in (".bat", ".cmd", ".ps1"):
                if is_blocked(child):
                    out.append(child.name)
    except OSError:
        pass
    return out
