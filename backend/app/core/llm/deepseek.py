import json

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
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

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

        return LLMResponse(content=content, tool_calls=tool_calls)
