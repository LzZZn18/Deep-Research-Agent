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
