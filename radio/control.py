"""本地 HTTP 控制台。

直播的时候你不方便切回终端敲命令，所以开一个只监听本机的网页：
浏览器、手机、Stream Deck 都能打开，点一下就能切人设、跳过、让它说句话。

    http://127.0.0.1:8770

只绑 127.0.0.1，外网访问不到，不用担心被观众乱按。
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import ControlConfig

log = logging.getLogger("radio.control")


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 电台控制台</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; font: 15px/1.6 system-ui, "Microsoft YaHei", sans-serif;
         background: #14161c; color: #e6e8ef; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #8b93a7; font-size: 13px; margin-bottom: 20px; }
  .card { background: #1c1f28; border: 1px solid #2a2f3d; border-radius: 12px;
          padding: 18px; margin-bottom: 16px; }
  .card h2 { font-size: 14px; margin: 0 0 12px; color: #8b93a7; font-weight: 600;
             letter-spacing: .05em; }
  button { background: #2b3142; color: #e6e8ef; border: 1px solid #39415a;
           border-radius: 8px; padding: 10px 16px; font-size: 14px; cursor: pointer;
           margin: 0 8px 8px 0; transition: .15s; }
  button:hover { background: #38405a; }
  button.primary { background: #3b6ef0; border-color: #3b6ef0; }
  button.primary:hover { background: #4d7bf5; }
  button.danger { background: #7a2b34; border-color: #8f333e; }
  input[type=text] { width: 100%; padding: 10px 12px; border-radius: 8px;
                     border: 1px solid #39415a; background: #12141a; color: #e6e8ef;
                     font-size: 14px; margin-bottom: 10px; }
  .state { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
           gap: 12px; }
  .state div { background: #12141a; border-radius: 8px; padding: 10px 12px; }
  .state .k { color: #8b93a7; font-size: 12px; }
  .state .v { font-size: 15px; margin-top: 2px; word-break: break-all; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         background: #4ade80; margin-right: 6px; }
  .dot.off { background: #6b7280; }
  .dot.busy { background: #fbbf24; }
  #toast { position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%);
           background: #2b3142; border: 1px solid #39415a; padding: 10px 20px;
           border-radius: 999px; opacity: 0; transition: .25s; pointer-events: none; }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
  <h1>AI 电台控制台</h1>
  <div class="sub">只在本机生效 · 关闭窗口不影响电台运行</div>

  <div class="card">
    <h2>当前状态</h2>
    <div class="state" id="state"></div>
  </div>

  <div class="card">
    <h2>切人设</h2>
    <div id="personas"></div>
  </div>

  <div class="card">
    <h2>让它说话</h2>
    <input type="text" id="saytext" placeholder="直接念出来的话，例如：广告时间到，大家点个关注～">
    <button class="primary" onclick="send('say')">直接念</button>
    <input type="text" id="asktext" placeholder="让它自己想一句话，例如：夸夸今天送礼物的观众">
    <button class="primary" onclick="send('ask')">让它发挥</button>
  </div>

  <div class="card">
    <h2>操作</h2>
    <button onclick="cmd('pause')">⏸ 暂停说话</button>
    <button onclick="cmd('resume')">▶ 继续</button>
    <button onclick="cmd('skip')">⏭ 跳过这条</button>
    <button onclick="cmd('clear')">🗑 清空队列</button>
    <button onclick="cmd('reload')">♻ 重载人设和稿件</button>
  </div>

  <div id="toast"></div>

<script>
async function get(path) {
  const r = await fetch(path);
  return await r.json();
}
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), 1800);
}
function cmd(name) {
  get('/api/' + name).then(d => { toast(d.message || 'ok'); refresh(); });
}
function send(which) {
  const box = document.getElementById(which + 'text');
  const text = box.value.trim();
  if (!text) { toast('先填点内容'); return; }
  get('/api/' + which + '?text=' + encodeURIComponent(text))
    .then(d => { toast(d.message || 'ok'); box.value = ''; refresh(); });
}
async function refresh() {
  const s = await get('/api/state');
  const dot = s.speaking ? '<span class="dot busy"></span>'
            : (s.paused ? '<span class="dot off"></span>' : '<span class="dot"></span>');
  document.getElementById('state').innerHTML = `
    <div><div class="k">状态</div><div class="v">${dot}${s.speaking ? '正在说话' : (s.paused ? '已暂停' : '待命')}</div></div>
    <div><div class="k">人设</div><div class="v">${s.persona || '-'}</div></div>
    <div><div class="k">音色</div><div class="v">${s.voice || '-'}</div></div>
    <div><div class="k">语音引擎</div><div class="v">${s.tts_provider || '-'}</div></div>
    <div><div class="k">排队中</div><div class="v">${s.pending}</div></div>
    <div><div class="k">已开口</div><div class="v">${s.spoken_count} 次</div></div>
    <div style="grid-column:1/-1"><div class="k">上一句</div><div class="v">${s.last_text || '（还没说过话）'}</div></div>
    ${s.last_error ? `<div style="grid-column:1/-1"><div class="k">最近一次出错（连续 ${s.error_streak} 次）</div><div class="v" style="color:#fca5a5">${s.last_error}</div></div>` : ''}
  `;
  const p = await get('/api/personas');
  document.getElementById('personas').innerHTML = p.personas.map(x =>
    `<button class="${x.current ? 'primary' : ''}" onclick="switchTo('${x.key}')">${x.name}</button>`
  ).join('');
}
function switchTo(key) {
  get('/api/persona?key=' + encodeURIComponent(key))
    .then(d => { toast(d.message || 'ok'); refresh(); });
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "AIRadioControl/1.0"

    # 由 ControlServer 注入
    controller: "ControlServer"

    def log_message(self, fmt: str, *args: Any) -> None:  # 别刷屏
        log.debug("control: " + fmt, *args)

    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if route == "/":
            self._send_html(INDEX_HTML)
            return

        api = self.controller
        try:
            if route == "/api/state":
                self._send_json(api.state())
            elif route == "/api/personas":
                self._send_json(api.personas())
            elif route == "/api/persona":
                self._send_json(api.switch_persona(query.get("key", [""])[0]))
            elif route == "/api/skip":
                self._send_json(api.skip())
            elif route == "/api/clear":
                self._send_json(api.clear())
            elif route == "/api/pause":
                self._send_json(api.pause())
            elif route == "/api/resume":
                self._send_json(api.resume())
            elif route == "/api/reload":
                self._send_json(api.reload())
            elif route == "/api/say":
                self._send_json(api.say(query.get("text", [""])[0]))
            elif route == "/api/ask":
                self._send_json(api.ask(query.get("text", [""])[0]))
            else:
                self._send_json({"ok": False, "message": "没有这个接口"}, status=404)
        except Exception as exc:
            log.exception("控制台接口出错")
            self._send_json({"ok": False, "message": str(exc)}, status=500)

    # ------------------------------------------------------------------

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ControlServer:
    """把 Director 的操作暴露成 HTTP 接口。"""

    def __init__(self, cfg: ControlConfig, director, loop) -> None:
        self.cfg = cfg
        self.director = director
        self.loop = loop
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = f"http://{cfg.host}:{cfg.port}"

    # ------------------------------------------------------------------

    def start(self) -> bool:
        if not self.cfg.enabled:
            return False
        handler = type("BoundHandler", (_Handler,), {"controller": self})
        try:
            self._httpd = ThreadingHTTPServer((self.cfg.host, self.cfg.port), handler)
        except OSError as exc:
            log.error("控制台端口 %s 用不了（%s），已跳过。"
                      "可以在 config.toml 的 [control] 里换一个 port。",
                      self.cfg.port, exc)
            return False
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="control-http", daemon=True)
        self._thread.start()
        log.info("控制台已启动：%s", self.url)
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    # ------------------------------------------------------------------
    # 接口实现（HTTP 线程里调用，必须切回事件循环）
    # ------------------------------------------------------------------

    def _call(self, fn: Callable, *args: Any) -> None:
        try:
            self.loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass

    def state(self) -> dict:
        data = self.director.state.as_dict()
        data["ok"] = True
        return data

    def personas(self) -> dict:
        current = self.director.personas.current
        return {
            "ok": True,
            "personas": [
                {"key": p.key, "name": p.display_name,
                 "current": bool(current and p.key == current.key)}
                for p in self.director.personas.all()
            ],
        }

    def switch_persona(self, key: str) -> dict:
        if not key:
            return {"ok": False, "message": "没指定人设"}
        if key not in self.director.personas.keys():
            return {"ok": False, "message": f"没有「{key}」这个人设"}
        self._call(self.director.switch_persona, key)
        return {"ok": True, "message": f"已切到 {key}"}

    def skip(self) -> dict:
        self._call(self.director.skip)
        return {"ok": True, "message": "已跳过"}

    def clear(self) -> dict:
        self._call(self.director.clear_queue)
        return {"ok": True, "message": "队列已清空"}

    def pause(self) -> dict:
        self._call(self.director.pause)
        return {"ok": True, "message": "已暂停"}

    def resume(self) -> dict:
        self._call(self.director.resume)
        return {"ok": True, "message": "已继续"}

    def reload(self) -> dict:
        self._call(self.director.reload_personas)
        self._call(self.director.reload_scripts)
        return {"ok": True, "message": "人设和稿件已重载"}

    def say(self, text: str) -> dict:
        if not text.strip():
            return {"ok": False, "message": "内容为空"}
        self._call(self.director.say, text, "控制台")
        return {"ok": True, "message": "已排入队列"}

    def ask(self, text: str) -> dict:
        if not text.strip():
            return {"ok": False, "message": "内容为空"}
        self._call(self.director.ask, text, "控制台")
        return {"ok": True, "message": "已交给 AI 发挥"}
