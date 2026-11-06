import os
from abc import ABC, abstractmethod

from app.core.config import get_settings

# huggingface_hub 在 import 时读取 HF_ENDPOINT，所以必须在导入 fastembed 之前设置好镜像地址。
if get_settings().hf_endpoint:
    os.environ.setdefault("HF_ENDPOINT", get_settings().hf_endpoint)

from fastembed import TextEmbedding


class Embedder(ABC):
    """Embedding 抽象，后续可替换成云端 API 等实现。"""

    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """把文本列表转成向量列表。"""


class FastEmbedEmbedder(Embedder):
    """基于 fastembed 的本地 BGE 实现（免费、离线，无需额外 key）。"""

    def __init__(self, model_name: str, dim: int):
        self.dim = dim
        self._model = TextEmbedding(model_name=model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(texts)]
