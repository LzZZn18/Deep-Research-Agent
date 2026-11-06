import psycopg


class VectorStore:
    """基于 PostgreSQL + pgvector 的向量存储。

    - sources：来源（url 唯一，分配引用编号 citation_id）
    - chunks：该来源切出的片段 + 向量
    """

    def __init__(self, dsn: str, dim: int):
        self.dsn = dsn
        self.dim = dim

    def _connect(self):
        return psycopg.connect(self.dsn)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS sources (
                    citation_id SERIAL PRIMARY KEY,
                    url TEXT UNIQUE NOT NULL,
                    title TEXT
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id SERIAL PRIMARY KEY,
                    citation_id INTEGER NOT NULL REFERENCES sources(citation_id) ON DELETE CASCADE,
                    chunk TEXT NOT NULL,
                    embedding vector({self.dim})
                );
                CREATE INDEX IF NOT EXISTS chunks_embedding_idx
                    ON chunks USING hnsw (embedding vector_cosine_ops);
                """
            )
            conn.commit()

    def add_source(self, url: str, title: str, chunks: list[str], embeddings: list[list[float]]) -> int:
        """入库一个来源及其片段，返回引用编号 citation_id（同 url 复用）。"""
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO sources (url, title) VALUES (%s, %s) "
                "ON CONFLICT (url) DO UPDATE SET title = EXCLUDED.title "
                "RETURNING citation_id",
                (url, title),
            ).fetchone()
            citation_id = row[0]
            for chunk, emb in zip(chunks, embeddings):
                conn.execute(
                    "INSERT INTO chunks (citation_id, chunk, embedding) VALUES (%s, %s, %s::vector)",
                    (citation_id, chunk, _vec_str(emb)),
                )
            conn.commit()
        return citation_id

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        """按余弦相似度检索最相关的片段，返回引用编号、来源与相似度。"""
        vec = _vec_str(query_embedding)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.citation_id, s.url, s.title, c.chunk,
                       1 - (c.embedding <=> %s::vector) AS similarity
                FROM chunks c
                JOIN sources s ON s.citation_id = c.citation_id
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                (vec, vec, top_k),
            ).fetchall()
        return [
            {"citation_id": r[0], "url": r[1], "title": r[2], "chunk": r[3], "similarity": r[4]}
            for r in rows
        ]


def _vec_str(v: list[float]) -> str:
    """把向量转成 pgvector 接受的 '[0.1,0.2,...]' 字符串。"""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
