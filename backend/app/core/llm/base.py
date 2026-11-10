from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """模型返回结果：要么是最终回答，要么是要调用的工具列表。"""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0


@dataclass
class StreamChunk:
    """流式输出的一小段正文；最后一个 chunk 的 usage 字段携带 token 用量。"""

    delta: str = ""
    usage: dict[str, Any] | None = None


class LLMProvider(ABC):
    """LLM 的统一抽象，屏蔽厂商差异。messages/tools 均用 OpenAI 兼容格式。"""

    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """非流式调用。"""
        raise NotImplementedError

    async def stream(self, messages: list[dict], tools: list[dict]):
        """流式调用。默认退化为一次非流式调用，整体作为单个 chunk 产出。

        支持流式的 Provider 应覆写本方法：逐段产出正文，最后一个 chunk
        的 usage 字段携带 token 用量。
        """
        resp = await self.chat(messages, tools)
        yield StreamChunk(
            delta=resp.content,
            usage={
                "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens,
                "total_tokens": resp.total_tokens,
            },
        )
