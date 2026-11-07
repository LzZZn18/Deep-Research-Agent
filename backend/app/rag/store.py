import psycopg


class VectorStore:
    """基于 PostgreSQL + pgvector 的向量存储（方案 C：任务隔离）。

    - research_task：顶层任务
    - source：来源（url 全局唯一，只存一份）
    - chunk：来源切出的片段 + 向量（归 source，不重复）
    - task_source：任务↔来源关联，citation_no 为任务内从 1 起的引用编号
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
                CREATE TABLE IF NOT EXISTS research_task (
                    id SERIAL PRIMARY KEY,
                    question TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS source (
                    id SERIAL PRIMARY KEY,
                    url TEXT UNIQUE NOT NULL,
                    title TEXT
                );
                CREATE TABLE IF NOT EXISTS chunk (
                    id SERIAL PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
                    chunk TEXT NOT NULL,
                    embedding vector({self.dim})
                );
                CREATE TABLE IF NOT EXISTS task_source (
                    task_id INTEGER NOT NULL REFERENCES research_task(id) ON DELETE CASCADE,
                    source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
                    citation_no INTEGER NOT NULL,
                    PRIMARY KEY (task_id, source_id)
                );
                CREATE INDEX IF NOT EXISTS chunk_embedding_idx
                    ON chunk USING hnsw (embedding vector_cosine_ops);
                """
            )
            conn.commit()

    def create_task(self, question: str) -> int:
        """创建研究任务，返回 task_id。"""
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO research_task (question) VALUES (%s) RETURNING id",
                (question,),
            ).fetchone()
            conn.commit()
        return row[0]

    def add_source(
        self,
        task_id: int,
        url: str,
        title: str,
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> int:
        """把一个来源入库到指定任务，返回任务内引用编号 citation_no。

        - 来源全局去重：同 url 复用同一个 source
        - chunk 幂等：同 source 重复入库时先删旧 chunk 再插入
        - 任务内编号：同任务内从 1 起，同 url 已存在则复用编号
        """
        with self._connect() as conn:
            source_id = conn.execute(
                "INSERT INTO source (url, title) VALUES (%s, %s) "
                "ON CONFLICT (url) DO UPDATE SET title = EXCLUDED.title RETURNING id",
                (url, title),
            ).fetchone()[0]

            conn.execute("DELETE FROM chunk WHERE source_id = %s", (source_id,))
            for chunk, emb in zip(chunks, embeddings):
                conn.execute(
                    "INSERT INTO chunk (source_id, chunk, embedding) VALUES (%s, %s, %s::vector)",
                    (source_id, chunk, _vec_str(emb)),
                )

            conn.execute(
                """
                INSERT INTO task_source (task_id, source_id, citation_no)
                SELECT %s, %s, COALESCE(MAX(citation_no), 0) + 1
                FROM task_source WHERE task_id = %s
                ON CONFLICT (task_id, source_id) DO NOTHING
                """,
                (task_id, source_id, task_id),
            )
            citation_no = conn.execute(
                "SELECT citation_no FROM task_source WHERE task_id = %s AND source_id = %s",
                (task_id, source_id),
            ).fetchone()[0]

            conn.commit()
        return citation_no

    def search(
        self, task_id: int, query_embedding: list[float], top_k: int = 5
    ) -> list[dict]:
        """只在本任务抓取的来源范围内做余弦检索，返回引用编号、来源与相似度。"""
        vec = _vec_str(query_embedding)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts.citation_no, s.url, s.title, c.chunk,
                       1 - (c.embedding <=> %s::vector) AS similarity
                FROM chunk c
                JOIN source s ON s.id = c.source_id
                JOIN task_source ts ON ts.source_id = c.source_id
                WHERE ts.task_id = %s
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                (vec, task_id, vec, top_k),
            ).fetchall()
        return [
            {"citation_id": r[0], "url": r[1], "title": r[2], "chunk": r[3], "similarity": r[4]}
            for r in rows
        ]

    def finish_task(self, task_id: int, status: str = "done") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE research_task SET status = %s, updated_at = now() WHERE id = %s",
                (status, task_id),
            )
            conn.commit()


def _vec_str(v: list[float]) -> str:
    """把向量转成 pgvector 接受的 '[0.1,0.2,...]' 字符串。"""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
