import json
import time

import httpx

from app.core.llm.base import LLMProvider, LLMResponse, ToolCall


class DeepSeekProvider(LLMProvider):
    """DeepSeek 实现（OpenAI 兼容协议，直接用 httpx 手写，不依赖 SDK）。"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        payload: dict = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools

        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
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
