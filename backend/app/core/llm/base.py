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


class LLMProvider(ABC):
    """LLM 的统一抽象，屏蔽厂商差异。messages/tools 均用 OpenAI 兼容格式。"""

    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """非流式调用。"""
        raise NotImplementedError
