"""打包一个干净的上传包。

用途：
    把仓库里该上传的东西挑出来打成 zip，方便直接拖到 GitHub 网页上传，
    或者检查 .gitignore 有没有漏掉什么。

会排除：
    sherpa/ models/ voices/ logs/ .tmp/ __pycache__/ .git/
    *.onnx *.tar.bz2 *.part *.wav（引擎资源和音频）
    .env（API Key）

用法：
    python tools/make_release.py            # 打成 ai-radio-v0.8.zip
    python tools/make_release.py --list     # 只列出会打包的文件，不打包
    python tools/make_release.py --check    # 检查有没有该排除却没排除的
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VERSION = "0.8"

# 目录级排除
EXCLUDE_DIRS = {
    ".git", "__pycache__", ".tmp", "logs",
    "sherpa", "models", "voices", "_dlm", "_dlv2", "_voices",
    ".venv", "venv", "env", ".idea", ".vscode-test",
}

# 后缀级排除 —— 引擎资源、音频、临时产物
EXCLUDE_SUFFIX = {
    ".onnx", ".bz2", ".part", ".wav", ".mp3", ".m4a",
    ".pyc", ".pyo", ".log", ".zip",
}

# 文件名级排除
#   config.toml 必须排除 —— 里面装着 API Key、房间号、Cookie。
#   程序首次运行会用内置模板自动生成一份，所以不提交也能跑。
EXCLUDE_NAMES = {
    ".env", "config.toml", "config.toml.bak",
    "pronunciation.txt", "Thumbs.db", "desktop.ini",
}

# 这些虽然不是源码，但必须带上
KEEP_ALWAYS = {".gitignore", ".gitattributes", "LICENSE", "启动.bat"}

# 疑似密钥的特征。命中就直接拒绝打包 —— 宁可多拦，不可漏放。
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "OpenAI/DeepSeek 风格的 API Key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "GitHub Personal Access Token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "GitHub 细粒度 Token"),
    (re.compile(r"SESSDATA=[A-Za-z0-9%._-]{20,}"), "B 站 Cookie（SESSDATA）"),
    (re.compile(r"\bbili_jct=[A-Za-z0-9]{20,}"), "B 站 Cookie（bili_jct）"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
]

# 扫描时跳过的文件类型（二进制、以及本身是示例的文件）
SCAN_SKIP_SUFFIX = {".png", ".jpg", ".ico", ".zip", ".gz"}
# 示例文件里出现假的 key 是正常的
SCAN_SKIP_NAMES = {".env.example"}



def should_skip(path: Path) -> str:
    """返回跳过原因，空字符串表示要打包。"""
    rel = path.relative_to(ROOT)
    for part in rel.parts[:-1]:
        if part in EXCLUDE_DIRS:
            return f"目录 {part}/"
    if path.name in EXCLUDE_NAMES:
        return "敏感或无关文件"
    if path.suffix.lower() in EXCLUDE_SUFFIX:
        return f"资源文件 {path.suffix}"
    return ""


def collect() -> tuple[list[Path], list[tuple[Path, str]]]:
    keep: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        reason = should_skip(path)
        if reason:
            skipped.append((path, reason))
        else:
            keep.append(path)
    return keep, skipped


def scan_secrets(files: list[Path]) -> list[tuple[Path, int, str, str]]:
    """在要打包的文件里找疑似密钥。返回 [(文件, 行号, 命中的规则, 脱敏后的行)]。"""
    hits: list[tuple[Path, int, str, str]] = []
    for path in files:
        if path.suffix.lower() in SCAN_SKIP_SUFFIX:
            continue
        if path.name in SCAN_SKIP_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, label in SECRET_PATTERNS:
                if pattern.search(line):
                    masked = line.strip()
                    if len(masked) > 80:
                        masked = masked[:77] + "…"
                    masked = re.sub(r"[A-Za-z0-9]{16,}",
                                    lambda m: m.group(0)[:4] + "***", masked)
                    hits.append((path, lineno, label, masked))
                    break
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="打包上传用的 zip")
    parser.add_argument("--list", action="store_true", help="只列出，不打包")
    parser.add_argument("--check", action="store_true", help="只检查排除规则")
    parser.add_argument("--force", action="store_true",
                        help="即使扫出疑似密钥也强行打包（不建议）")
    parser.add_argument("-o", "--output", default="", help="输出文件名")
    args = parser.parse_args()

    keep, skipped = collect()
    total = sum(p.stat().st_size for p in keep)

    print(f"AI 电台 v{VERSION} · 上传包")
    print(f"  要打包 {len(keep)} 个文件，共 {total/1024:.1f} KB")
    print(f"  已排除 {len(skipped)} 个文件")

    # ---- 安全扫描 ----
    hits = scan_secrets(keep)
    if hits:
        print("\n" + "!" * 66)
        print("  ★ 发现疑似密钥 —— 已中止打包，请先处理：")
        print("!" * 66)
        for path, lineno, label, masked in hits:
            rel = path.relative_to(ROOT)
            print(f"    {rel}:{lineno}")
            print(f"      {label}")
            print(f"      {masked}")
        print("\n  如果确认不是真密钥，加 --force 强行打包。")
        if not args.force:
            return 3
        print("\n  （--force 已指定，继续打包）")
    else:
        print("  ✓ 没有扫到疑似密钥")

    # 检查有没有漏网的大家伙
    big = [p for p in keep if p.stat().st_size > 512 * 1024]
    if big:
        print("\n  ★ 警告：这些文件超过 512KB，确认一下该不该上传：")
        for p in big:
            print(f"      {p.relative_to(ROOT)}  {p.stat().st_size/1024:.0f}KB")

    if args.list:
        print("\n  将要打包：")
        for p in keep:
            print(f"    {p.relative_to(ROOT)}")
        return 0

    if args.check:
        print("\n  排除原因统计：")
        reasons: dict[str, int] = {}
        for _p, reason in skipped:
            key = reason.split()[0]
            reasons[key] = reasons.get(key, 0) + 1
        for key, count in sorted(reasons.items()):
            print(f"    {key}: {count}")
        return 0

    out = Path(args.output) if args.output else ROOT.parent / f"ai-radio-v{VERSION}.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in keep:
            zf.write(path, Path(f"ai-radio-v{VERSION}") / path.relative_to(ROOT))
    print(f"\n  已生成：{out}")
    print(f"  大小：{out.stat().st_size/1024:.1f} KB")

    # 打完之后再确认一遍：zip 里确实没有敏感文件
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    bad = [n for n in names
           if Path(n).name in EXCLUDE_NAMES or Path(n).name == "config.toml"]
    if bad:
        print("\n  ★ 打包结果里仍含敏感文件，已删除：")
        for n in bad:
            print(f"      {n}")
        out.unlink()
        return 4
    print("  ✓ 已确认包里没有 config.toml / .env / voices")
    return 0


if __name__ == "__main__":
    sys.exit(main())
