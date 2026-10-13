"""实测 B 站弹幕接入：自动找一个正在直播的房间，连上去打印弹幕。

用法：
    python tools/probe_bili_live.py            # 自动找热门直播间
    python tools/probe_bili_live.py 22637261   # 指定房间号
    python tools/probe_bili_live.py --seconds 40
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio.chat.bilibili import BilibiliChatSource, resolve_room_id  # noqa: E402

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")


def api(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://live.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def find_live_room() -> tuple[int, str, int]:
    """在几个热门分区里找一个在线人数最多的直播间。"""
    best = None
    for pid in (1, 2, 3, 4, 5, 6):
        try:
            data = api("https://api.live.bilibili.com/xlive/web-interface/v1/second/getList"
                       f"?platform=web&parent_area_id={pid}&area_id=0&sort_type=online&page=1")
        except Exception as e:
            print(f"  分区 {pid} 查询失败: {e}")
            continue
        for room in (data.get("data") or {}).get("list") or []:
            if room.get("live_status") != 1:
                continue
            online = int(room.get("online") or 0)
            if best is None or online > best[2]:
                best = (int(room["roomid"]), str(room.get("title") or ""), online)
        if best:
            break
    if best is None:
        raise SystemExit("没能找到正在直播的房间，请手动指定房间号")
    return best


async def main() -> None:
    args = [a for a in sys.argv[1:]]
    seconds = 30.0
    if "--seconds" in args:
        i = args.index("--seconds")
        seconds = float(args[i + 1])
        del args[i:i + 2]

    if args and args[0].isdigit():
        room_id = int(args[0])
        title = ""
    else:
        room_id, title, online = find_live_room()
        print(f"选中直播间 {room_id}  《{title}》  在线 {online}")

    real = resolve_room_id(room_id)
    print(f"真实房间号: {real}")

    source = BilibiliChatSource(room_id=room_id, ignore_enter=False)
    counts: dict[str, int] = {}
    samples: list[str] = []
    start = time.time()

    async def consume():
        async for msg in source.messages():
            counts[msg.kind] = counts.get(msg.kind, 0) + 1
            if len(samples) < 25:
                samples.append(msg.display())
                print("  " + msg.display())
            if time.time() - start > seconds:
                return

    try:
        await asyncio.wait_for(consume(), timeout=seconds + 15)
    except asyncio.TimeoutError:
        pass
    finally:
        await source.close()

    print(f"\n=== {seconds:.0f} 秒内统计 ===")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:12s} {v}")
    if not counts:
        print("  没有收到任何消息")
    print("RESULT:", "BILI_OK" if counts else "BILI_NO_MESSAGES")


asyncio.run(main())
