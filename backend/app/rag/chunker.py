import re

_WS_RE = re.compile(r"\s+")


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """按字符做固定大小切块（带重叠），用于后续向量化。"""
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def _normalize(text: str) -> str:
    """折叠所有连续空白为单个空格，用于比较。"""
    return _WS_RE.sub(" ", text).strip()


def _char_ngrams(text: str, n: int) -> set[str]:
    """字符级 n-gram 集合（中文无分词，用字 n-gram 判重更稳）。"""
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe_chunks(chunks: list[str], threshold: float = 0.9, n: int = 3) -> list[str]:
    """去掉重复 / 近重复的 chunk（导航栏、页脚等重复样板）。

    完全相同的用归一化文本判断；近重复的用字符 n-gram 的 Jaccard 相似度判断
    （>= threshold 视为重复）。保留第一个出现的，顺序不变。
    """
    kept: list[str] = []
    seen: set[str] = set()      # 归一化后完全相同的文本
    sigs: list[set[str]] = []   # 已保留 chunk 的 n-gram 签名

    for chunk in chunks:
        norm = _normalize(chunk)
        if not norm:
            continue
        if norm in seen:
            continue
        sig = _char_ngrams(norm, n)
        if sig and any(_jaccard(sig, s) >= threshold for s in sigs):
            continue
        seen.add(norm)
        if sig:
            sigs.append(sig)
        kept.append(chunk)
    return kept
