"""大模型大脑：DeepSeek / 任意 OpenAI 兼容接口。

只用标准库 urllib 实现，不依赖 openai 包：
  * 支持流式（SSE），让 AI 更快开口
  * 流式失败自动退回一次性请求
  * 网络抖动自动重试
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterator

from .config import LLMConfig

log = logging.getLogger("radio.llm")

_RETRYABLE = (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError)

# 这类错误重试也没用（key 不对、没余额、模型名写错），直接报错别浪费一次请求
_HARD_HTTP_ERRORS = ("HTTP 400", "HTTP 401", "HTTP 402", "HTTP 403", "HTTP 404")


def _is_hard_error(exc: Exception) -> bool:
    text = str(exc)
    return any(code in text for code in _HARD_HTTP_ERRORS)


class LLMError(Exception):
    """调用大模型失败。"""


@dataclass
class Message:
    role: str      # system | user | assistant
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class LLMClient:
    """OpenAI 兼容的对话客户端。"""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.base_url = (cfg.base_url or "https://api.deepseek.com").rstrip("/")
        self.model = cfg.model
        self.timeout = float(cfg.timeout or 40)

    # ------------------------------------------------------------------

    @property
    def endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        if not self.cfg.api_key:
            raise LLMError(
                "没有配置 API Key。\n"
                "  方式一：编辑 config.toml，把 [llm] 下的 api_key 填上\n"
                "  方式二：在项目根目录建一个 .env，写一行 DEEPSEEK_API_KEY=sk-xxxx\n"
            )
        return {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "User-Agent": "ai-radio/1.0",
        }

    def _payload(self, messages: list[Message], stream: bool,
                 temperature: float | None, max_tokens: int | None) -> bytes:
        body = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _request(self, payload: bytes) -> Any:
        req = urllib.request.Request(
            self.endpoint, data=payload, headers=self._headers(), method="POST"
        )
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            hint = ""
            if exc.code == 401:
                hint = "（API Key 不对或已失效）"
            elif exc.code == 402:
                hint = "（余额不足）"
            elif exc.code == 429:
                hint = "（请求太频繁或额度用完）"
            raise LLMError(f"模型接口返回 HTTP {exc.code}{hint}：{detail}") from exc
        except _RETRYABLE as exc:
            raise LLMError(f"连不上模型接口 {self.endpoint}：{exc}") from exc

    # ------------------------------------------------------------------
    # 非流式
    # ------------------------------------------------------------------

    def _complete_sync(self, messages: list[Message],
                       temperature: float | None, max_tokens: int | None) -> str:
        payload = self._payload(messages, False, temperature, max_tokens)
        with self._request(payload) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _extract_content(data)

    async def complete(self, messages: list[Message], *,
                       temperature: float | None = None,
                       max_tokens: int | None = None,
                       retries: int = 2) -> str:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return await asyncio.to_thread(
                    self._complete_sync, messages, temperature, max_tokens
                )
            except LLMError as exc:
                last = exc
                if _is_hard_error(exc):
                    raise
                if attempt < retries and any(k in str(exc) for k in ("连不上", "HTTP 5")):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
            except Exception as exc:  # 未预料的错误也不要炸掉整个电台
                last = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMError(f"调用模型失败：{exc}") from exc
        raise LLMError(str(last))

    # ------------------------------------------------------------------
    # 流式
    # ------------------------------------------------------------------

    def _stream_sync(self, messages: list[Message],
                     temperature: float | None, max_tokens: int | None) -> Iterator[str]:
        payload = self._payload(messages, True, temperature, max_tokens)
        with self._request(payload) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    return
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                piece = _extract_delta(data)
                if piece:
                    yield piece

    async def stream(self, messages: list[Message], *,
                     temperature: float | None = None,
                     max_tokens: int | None = None) -> AsyncIterator[str]:
        """逐块产出模型输出。出错时抛出 LLMError。"""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        done = object()

        def worker() -> None:
            try:
                for piece in self._stream_sync(messages, temperature, max_tokens):
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", piece))
            except Exception as exc:  # noqa: BLE001 - 必须带回主线程
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("end", done))

        task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            while True:
                kind, value = await queue.get()
                if kind == "chunk":
                    yield value
                elif kind == "error":
                    raise LLMError(f"流式生成失败：{value}") from value
                else:
                    break
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def generate(self, messages: list[Message], *,
                       temperature: float | None = None,
                       max_tokens: int | None = None,
                       on_delta=None) -> str:
        """统一入口：优先流式；流式不可用则退回一次性请求。

        on_delta 会在每收到一小段文字时被调用（用于实时打印）。
        """
        if self.cfg.stream:
            pieces: list[str] = []
            try:
                async for piece in self.stream(messages, temperature=temperature,
                                               max_tokens=max_tokens):
                    pieces.append(piece)
                    if on_delta is not None:
                        result = on_delta(piece)
                        if asyncio.iscoroutine(result):
                            await result
                text = "".join(pieces).strip()
                if text:
                    return text
                log.warning("流式返回内容为空，改用一次性请求重试")
            except LLMError as exc:
                if _is_hard_error(exc):
                    raise  # key 不对 / 没余额之类，再试一次也是白试
                log.warning("%s；改用一次性请求", exc)

        text = await self.complete(messages, temperature=temperature,
                                   max_tokens=max_tokens)
        if on_delta is not None and text:
            result = on_delta(text)
            if asyncio.iscoroutine(result):
                await result
        return text.strip()

    # ------------------------------------------------------------------

    async def check(self) -> str:
        """自检：发一句最短的请求，确认 key / 网络 / 模型都对。"""
        raw = await self.complete(
            [Message("user", "只回复两个字：正常")], max_tokens=16, temperature=0
        )
        return raw or "(空)"


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


def _extract_content(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"模型返回内容为空：{json.dumps(data, ensure_ascii=False)[:300]}")
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if not content:
        # DeepSeek reasoner 之类会把思考过程放这里
        content = message.get("reasoning_content") or ""
    return str(content)


def _extract_delta(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    return str(delta.get("content") or "")
