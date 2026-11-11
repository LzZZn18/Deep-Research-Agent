"""chunker 与 chunk 去重的单元测试。"""

from app.rag.chunker import chunk_text, dedupe_chunks


def test_chunk_text_basic():
    assert chunk_text("abcdefghij", chunk_size=4, overlap=0) == ["abcd", "efgh", "ij"]


def test_chunk_text_with_overlap():
    assert chunk_text("abcdefgh", chunk_size=4, overlap=2) == ["abcd", "cdef", "efgh"]


def test_dedupe_removes_exact_duplicates():
    assert dedupe_chunks(["段落A", "段落A", "段落B"]) == ["段落A", "段落B"]


def test_dedupe_removes_near_duplicates():
    base = ("向量数据库用于存储和检索高维向量，广泛应用于语义搜索与推荐系统。") * 4
    near = base[:-1] + "！"  # 只改最后一个字符，仍属近重复
    distinct = ("关系型数据库基于表结构，使用 SQL 进行精确查询。") * 4
    assert dedupe_chunks([base, near, distinct]) == [base, distinct]


def test_dedupe_keeps_distinct_chunks():
    chunks = ["向量数据库", "关系型数据库", "图数据库"]
    assert dedupe_chunks(chunks) == chunks


def test_dedupe_skips_blank_chunks():
    assert dedupe_chunks(["", "   ", "内容", "\n\t"]) == ["内容"]


def test_dedupe_preserves_first_occurrence():
    assert dedupe_chunks(["第一版", "第二版", "第一版"]) == ["第一版", "第二版"]
