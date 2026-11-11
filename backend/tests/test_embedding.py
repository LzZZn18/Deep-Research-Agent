"""embedder LRU 缓存的单元测试（用假模型，不加载真实 BGE）。"""

import threading
from collections import OrderedDict

from app.rag.embedding import FastEmbedEmbedder


class _Vec:
    def __init__(self, v):
        self._v = v

    def tolist(self):
        return list(self._v)


class _FakeModel:
    """记录每次调用，并返回由文本长度决定的假向量，便于区分不同文本。"""

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [_Vec([float(len(t)), 1.0]) for t in texts]


def _make_embedder(cache_size: int = 100):
    emb = FastEmbedEmbedder.__new__(FastEmbedEmbedder)  # 跳过 __init__，不加载模型
    emb._model = _FakeModel()
    emb._lock = threading.Lock()
    emb._cache = OrderedDict()
    emb._cache_size = cache_size
    return emb


def test_embed_cache_reuses_vectors():
    emb = _make_embedder()

    out1 = emb.embed(["a", "bb"])          # 两个都是 miss
    assert emb._model.calls == [["a", "bb"]]
    assert out1 == [[1.0, 1.0], [2.0, 1.0]]

    out2 = emb.embed(["bb", "ccc", "a"])   # bb、a 命中缓存，只有 ccc 是 miss
    assert emb._model.calls == [["a", "bb"], ["ccc"]]
    assert out2 == [[2.0, 1.0], [3.0, 1.0], [1.0, 1.0]]


def test_embed_cache_evicts_lru():
    emb = _make_embedder(cache_size=2)

    emb.embed(["a", "b", "c"])   # 装满后淘汰最早插入的 "a"
    emb.embed(["a"])             # "a" 已被淘汰 → 重新 miss
    assert emb._model.calls == [["a", "b", "c"], ["a"]]


def test_embed_cache_returns_copy():
    emb = _make_embedder()
    v1 = emb.embed(["a"])[0]
    v2 = emb.embed(["a"])[0]
    assert v1 == v2
    v1[0] = 999.0                # 修改返回的向量，不应污染缓存
    assert emb.embed(["a"])[0] == [1.0, 1.0]
