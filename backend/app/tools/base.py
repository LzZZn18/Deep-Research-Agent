from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """工具抽象。每个工具需要：名称、描述、JSON Schema 参数、执行逻辑。"""

    name: str
    description: str
    parameters: dict[str, Any]

    @abstractmethod
    async def run(self, **kwargs: Any) -> str:
        """执行工具，返回给模型的字符串结果。"""
        raise NotImplementedError

    def to_schema(self) -> dict:
        """转换成 OpenAI 兼容的 function 定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
