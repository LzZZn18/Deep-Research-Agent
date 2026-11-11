import os
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict

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
    """基于 fastembed 的本地 BGE 实现（免费、离线，无需额外 key）。

    自带按文本去重的 LRU 缓存：重复 query / chunk 不重复推理，直接复用向量。
    单实例共享给并发任务时，用一把锁同时保护缓存与 onnxruntime 推理。
    """

    def __init__(self, model_name: str, dim: int, cache_size: int = 4096):
        self.dim = dim
        self._model = TextEmbedding(model_name=model_name)
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_size = cache_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            result: list = [None] * len(texts)
            miss_idx: list[int] = []
            miss_texts: list[str] = []

            for i, t in enumerate(texts):
                vec = self._cache.get(t)
                if vec is not None:
                    self._cache.move_to_end(t)  # LRU：命中即移到末尾
                    result[i] = list(vec)        # 拷贝，避免外部改动污染缓存
                else:
                    miss_idx.append(i)
                    miss_texts.append(t)

            if miss_texts:
                vectors = [v.tolist() for v in self._model.embed(miss_texts)]
                for idx, text, vec in zip(miss_idx, miss_texts, vectors):
                    result[idx] = list(vec)
                    self._cache[text] = vec
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)

        return result
