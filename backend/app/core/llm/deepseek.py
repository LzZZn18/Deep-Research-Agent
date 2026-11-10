import asyncio
import json
import time
from collections.abc import AsyncIterator

import httpx

from app.core.llm.base import LLMProvider, LLMResponse, StreamChunk, ToolCall
from app.core.retry import call_with_retry


class DeepSeekProvider(LLMProvider):
    """DeepSeek 实现（OpenAI 兼容协议，直接用 httpx 手写，不依赖 SDK）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        retries: int = 2,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.retries = retries

    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        payload: dict = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools

        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"

        async def _post() -> httpx.Response:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                await resp.aread()  # 先读完整响应体，客户端关闭后 resp 仍可用
                if resp.status_code >= 500:
                    resp.raise_for_status()  # 5xx → 抛 HTTPStatusError，触发重试
                return resp  # 2xx/4xx 返回，交给外层处理

        start = time.perf_counter()
        try:
            resp = await call_with_retry(
                _post,
                retries=self.retries,
                timeout=self.timeout,
                retry_on=(httpx.RequestError, httpx.HTTPStatusError),
            )
            resp.raise_for_status()  # 4xx 在此直接抛，不重试
            data = resp.json()
        except httpx.RequestError as e:
            # 覆盖 DNS 解析失败 / 连接被拒 / 超时等网络层错误，附上目标域名
            raise RuntimeError(
                f"无法连接 DeepSeek（{self.base_url}）：网络或 DNS 解析失败，"
                f"请检查网络/代理/防火墙。原始错误：{e}"
            ) from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"DeepSeek 返回 HTTP {e.response.status_code}：{e.response.text[:300]}"
            ) from e
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"调用 DeepSeek 超时（>{self.timeout:g}s，已重试 {self.retries} 次）"
            ) from e
        duration_ms = int((time.perf_counter() - start) * 1000)

        message = data["choices"][0]["message"]
        content = message.get("content") or ""

        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc["id"], name=fn["name"], arguments=args))

        usage = data.get("usage") or {}
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            total_tokens=usage.get("total_tokens") or 0,
            duration_ms=duration_ms,
        )

    async def stream(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> AsyncIterator[StreamChunk]:
        """流式调用：逐段产出正文，最后一个 chunk 的 usage 字段携带 token 用量。"""
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools

        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        raise RuntimeError(
                            f"DeepSeek 返回 HTTP {resp.status_code}：{body[:300]}"
                        )
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:") :].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        usage = obj.get("usage")
                        choices = obj.get("choices") or []
                        delta = ""
                        if choices:
                            delta = (choices[0].get("delta") or {}).get("content") or ""
                        yield StreamChunk(delta=delta, usage=usage)
        except httpx.RequestError as e:
            raise RuntimeError(
                f"无法连接 DeepSeek（{self.base_url}）：网络或 DNS 解析失败，"
                f"请检查网络/代理/防火墙。原始错误：{e}"
            ) from e
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"调用 DeepSeek 流式超时（>{self.timeout:g}s）") from e
