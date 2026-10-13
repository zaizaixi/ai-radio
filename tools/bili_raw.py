"""底层探针：连上弹幕服务器后，把收到的每一个原始包都打印出来。

用来区分两种情况：
  A. 服务器根本不给业务消息（限流/房间冷）
  B. 服务器给了消息，但我们的解析逻辑丢了
"""
import asyncio, json, os, struct, sys, time, zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radio.chat.bilibili import (HEADER_LEN, OP_AUTH, OP_AUTH_REPLY, OP_HEARTBEAT,
                                 OP_HEARTBEAT_REPLY, OP_MESSAGE, fetch_danmu_info,
                                 resolve_room_id, pack_packet)
from radio.wsclient import connect

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")

OPNAME = {2: "HEARTBEAT", 3: "POPULARITY", 5: "MESSAGE", 7: "AUTH", 8: "AUTH_REPLY"}


def split_packets(data, depth=0, out=None):
    if out is None:
        out = []
    off = 0
    while off + HEADER_LEN <= len(data):
        total, hlen, ver, op, seq = struct.unpack_from(">IHHII", data, off)
        if total < HEADER_LEN or off + total > len(data):
            out.append((depth, -1, -1, data[off:off + 64], f"截断 total={total} 剩余={len(data)-off}"))
            break
        body = data[off + hlen: off + total]
        off += total
        if ver in (2, 3):
            try:
                dec = zlib.decompress(body) if ver == 2 else None
            except Exception as e:
                dec = None
                out.append((depth, ver, op, b"", f"解压失败 {e}"))
            if ver == 2 and dec is not None:
                split_packets(dec, depth + 1, out)
            elif ver == 3:
                out.append((depth, ver, op, body[:80], "brotli 包（未安装 brotli）"))
        else:
            out.append((depth, ver, op, body, ""))
    return out


async def main():
    room_input = int(sys.argv[1]) if len(sys.argv) > 1 else 21654762
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 25
    protover = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    real = resolve_room_id(room_input)
    token, hosts = fetch_danmu_info(real)
    host = hosts[0]
    print(f"房间 {room_input} -> {real}，服务器 {host['host']}:{host['wss_port']}，protover={protover}")

    ws = await connect(f"wss://{host['host']}:{host['wss_port']}/sub",
                       headers={"User-Agent": UA, "Origin": "https://live.bilibili.com"})
    print("WS 已连接")

    auth = json.dumps({"uid": 0, "roomid": real, "protover": protover,
                       "platform": "web", "type": 2, "key": token},
                      separators=(",", ":")).encode()
    await ws.send_bytes(pack_packet(OP_AUTH, auth))

    start = time.time()
    hb_task = None

    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            await ws.send_bytes(pack_packet(OP_HEARTBEAT, b"[object Object]"))

    hb_task = asyncio.create_task(heartbeat())

    counts = {}
    raw_op5 = 0
    try:
        while time.time() - start < seconds:
            try:
                opcode, payload = await asyncio.wait_for(ws.recv(), timeout=seconds - (time.time() - start) + 1)
            except asyncio.TimeoutError:
                break
            if opcode != 2:
                print(f"  [非二进制帧 opcode={opcode}] {payload[:80]!r}")
                continue
            packets = split_packets(payload)
            for depth, ver, op, body, note in packets:
                key = OPNAME.get(op, f"op{op}")
                counts[key] = counts.get(key, 0) + 1
                if op == OP_AUTH_REPLY:
                    print(f"  AUTH_REPLY: {body[:200]!r}")
                elif op == OP_HEARTBEAT_REPLY:
                    pop = int.from_bytes(body[:4], "big") if len(body) >= 4 else -1
                    print(f"  人气值 = {pop}")
                elif op == OP_MESSAGE:
                    raw_op5 += 1
                    try:
                        j = json.loads(body.decode("utf-8"))
                        cmd = j.get("cmd") if isinstance(j, dict) else f"list[{len(j)}]"
                    except Exception:
                        cmd = f"<非JSON {body[:60]!r}>"
                    print(f"  op5 #{raw_op5} cmd={cmd}  {note}")
                else:
                    print(f"  ver={ver} op={op} len={len(body)} {note} {body[:60]!r}")
    finally:
        hb_task.cancel()
        await ws.close()

    print(f"\n统计: {counts}")
    print("RESULT:", "GOT_MESSAGES" if raw_op5 else "NO_OP5_PACKETS")


asyncio.run(main())
