import re
import urllib.parse

import psycopg

# ---- 长期库（方案 C 第二阶段）评分与晋升参数 ----
REUSE_WEIGHT = 2              # 复用次数权重
AUTHORITATIVE_BONUS = 2       # 权威域名加分
EXPIRY_PENALTY = 3            # 易过期内容扣分
PROMOTION_REUSE_MIN = 2       # 至少被 N 个任务复用才可能晋升
PROMOTION_SCORE_THRESHOLD = 2  # 晋升分阈值
EVERGREEN_TTL_DAYS = 90       # 常规来源晋升后存活天数
TIME_SENSITIVE_TTL_DAYS = 7   # 易过期来源晋升后存活天数
LIBRARY_MIN_SIMILARITY = 0.3  # 长期库回退检索最低相似度

AUTHORITATIVE_DOMAINS = (
    "wikipedia.org", "nature.com", "science.org", "arxiv.org", "who.int",
    "gov.cn", "gov", "edu.cn", "edu",
)
TIME_SENSITIVE_HINTS = (
    "新闻", "最新", "今日", "昨日", "本周", "股价", "股票", "价格", "行情",
    "汇率", "天气", "预测", "公告", "快讯",
)

_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _domain_score(url: str) -> int:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for d in AUTHORITATIVE_DOMAINS:
        if host == d or host.endswith("." + d):
            return AUTHORITATIVE_BONUS
    return 0


def _is_time_sensitive(url: str, title: str) -> bool:
    text = f"{url} {title or ''}"
    if _YEAR_RE.search(text):
        return True
    return any(h in text for h in TIME_SENSITIVE_HINTS)


class VectorStore:
    """基于 PostgreSQL + pgvector 的向量存储（方案 C：任务隔离 + 长期库）。

    - research_task：顶层任务
    - source：来源（url 全局唯一，只存一份）
    - chunk：来源切出的片段 + 向量（归 source，不重复）
    - task_source：任务↔来源关联，citation_no 为任务内从 1 起的引用编号
    - source_pool：长期库，来源复用达阈值后晋升，带评分与到期淘汰
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
                CREATE TABLE IF NOT EXISTS source_pool (
                    source_id INTEGER PRIMARY KEY REFERENCES source(id) ON DELETE CASCADE,
                    is_long_term BOOLEAN NOT NULL DEFAULT false,
                    quality_score INTEGER NOT NULL DEFAULT 0,
                    reuse_count INTEGER NOT NULL DEFAULT 0,
                    last_referenced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    promoted_at TIMESTAMPTZ,
                    expires_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS chunk_embedding_idx
                    ON chunk USING hnsw (embedding vector_cosine_ops);
                CREATE INDEX IF NOT EXISTS source_pool_long_term_idx
                    ON source_pool (is_long_term) WHERE is_long_term;
                """
            )
            # 历史记录：保存最终报告（已存在的库用 IF NOT EXISTS 平滑加列）
            conn.execute("ALTER TABLE research_task ADD COLUMN IF NOT EXISTS report TEXT")
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

    def _assign_citation(self, conn, task_id: int, source_id: int) -> int:
        """把来源关联到任务并分配任务内引用号（已存在则复用）。

        先对任务行加 FOR UPDATE 锁，保证并发 Researcher 下引用编号不重号。
        """
        conn.execute(
            "SELECT id FROM research_task WHERE id = %s FOR UPDATE", (task_id,)
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
        return conn.execute(
            "SELECT citation_no FROM task_source WHERE task_id = %s AND source_id = %s",
            (task_id, source_id),
        ).fetchone()[0]

    def _refresh_pool(self, conn, source_id: int) -> None:
        """重算该来源的复用次数与晋升分，并按阈值晋升/续期。"""
        row = conn.execute(
            "SELECT url, title FROM source WHERE id = %s", (source_id,)
        ).fetchone()
        if row is None:
            return
        url, title = row
        reuse = conn.execute(
            "SELECT COUNT(*) FROM task_source WHERE source_id = %s", (source_id,)
        ).fetchone()[0]

        time_sensitive = _is_time_sensitive(url, title)
        score = reuse * REUSE_WEIGHT + _domain_score(url) - (
            EXPIRY_PENALTY if time_sensitive else 0
        )
        is_long_term = reuse >= PROMOTION_REUSE_MIN and score >= PROMOTION_SCORE_THRESHOLD
        ttl_days = TIME_SENSITIVE_TTL_DAYS if time_sensitive else EVERGREEN_TTL_DAYS

        conn.execute(
            """
            INSERT INTO source_pool (source_id, is_long_term, quality_score, reuse_count, last_referenced_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (source_id) DO UPDATE SET
                is_long_term = EXCLUDED.is_long_term,
                quality_score = EXCLUDED.quality_score,
                reuse_count = EXCLUDED.reuse_count,
                last_referenced_at = now()
            """,
            (source_id, is_long_term, score, reuse),
        )
        if is_long_term:
            conn.execute(
                "UPDATE source_pool SET promoted_at = now() WHERE source_id = %s AND promoted_at IS NULL",
                (source_id,),
            )
            conn.execute(
                "UPDATE source_pool SET expires_at = now() + (%s * interval '1 day') WHERE source_id = %s",
                (ttl_days, source_id),
            )
        else:
            conn.execute(
                "UPDATE source_pool SET expires_at = NULL WHERE source_id = %s",
                (source_id,),
            )

    def add_source(
        self,
        task_id: int,
        url: str,
        title: str,
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> int:
        """抓取后入库一个来源：来源全局去重、chunk 幂等重写、关联到任务。返回任务内引用号。"""
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

            citation_no = self._assign_citation(conn, task_id, source_id)
            self._refresh_pool(conn, source_id)
            conn.commit()
        return citation_no

    def get_source(self, url: str) -> dict | None:
        """按 url 查来源（抓取缓存用）。返回 {'id', 'title'} 或 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, title FROM source WHERE url = %s", (url,)
            ).fetchone()
        return {"id": row[0], "title": row[1]} if row else None

    def link_source(self, task_id: int, source_id: int) -> int:
        """把已存在的来源关联到当前任务（复用已有 chunk，不重新抓取）。返回任务内引用号。"""
        with self._connect() as conn:
            citation_no = self._assign_citation(conn, task_id, source_id)
            self._refresh_pool(conn, source_id)
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

    def search_library(
        self, task_id: int, query_embedding: list[float], top_k: int = 5
    ) -> list[dict]:
        """在长期库中检索（排除本任务已有来源），命中后自动关联引用号并计入复用。"""
        vec = _vec_str(query_embedding)
        with self._connect() as conn:
            # 先淘汰到期来源
            conn.execute(
                "UPDATE source_pool SET is_long_term = false, expires_at = NULL "
                "WHERE is_long_term AND expires_at IS NOT NULL AND expires_at < now()"
            )
            rows = conn.execute(
                """
                SELECT c.source_id, s.url, s.title, c.chunk,
                       1 - (c.embedding <=> %s::vector) AS similarity
                FROM chunk c
                JOIN source s ON s.id = c.source_id
                JOIN source_pool p ON p.source_id = c.source_id
                WHERE p.is_long_term
                  AND (p.expires_at IS NULL OR p.expires_at > now())
                  AND c.source_id NOT IN (SELECT source_id FROM task_source WHERE task_id = %s)
                  AND 1 - (c.embedding <=> %s::vector) >= %s
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                (vec, task_id, vec, LIBRARY_MIN_SIMILARITY, vec, top_k),
            ).fetchall()

            results = []
            seen: set[int] = set()
            for r in rows:
                source_id, url, title, chunk, sim = r
                if source_id in seen:
                    continue
                seen.add(source_id)
                citation_no = self._assign_citation(conn, task_id, source_id)
                self._refresh_pool(conn, source_id)
                results.append({
                    "citation_id": citation_no,
                    "url": url,
                    "title": title,
                    "chunk": chunk,
                    "similarity": sim,
                })
            conn.commit()
            return results

    def list_task_sources(self, task_id: int) -> list[dict]:
        """列出任务所有来源的引用表（[n] → 标题/链接），供撰写/审核使用。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts.citation_no, s.title, s.url
                FROM task_source ts
                JOIN source s ON s.id = ts.source_id
                WHERE ts.task_id = %s
                ORDER BY ts.citation_no
                """,
                (task_id,),
            ).fetchall()
        return [{"citation_id": r[0], "title": r[1], "url": r[2]} for r in rows]

    def finish_task(self, task_id: int, status: str = "done") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE research_task SET status = %s, updated_at = now() WHERE id = %s",
                (status, task_id),
            )
            conn.commit()

    def save_report(self, task_id: int, report: str) -> None:
        """保存最终报告内容。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE research_task SET report = %s, updated_at = now() WHERE id = %s",
                (report, task_id),
            )
            conn.commit()

    def list_tasks(self, limit: int = 50) -> list[dict]:
        """按时间倒序列出任务（仅元信息，不含报告正文）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, question, status, created_at
                FROM research_task
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [
            {"id": r[0], "question": r[1], "status": r[2], "created_at": r[3].isoformat()}
            for r in rows
        ]

    def get_task(self, task_id: int) -> dict | None:
        """返回单个任务详情（问题、状态、报告、引用来源）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, question, status, report, created_at FROM research_task WHERE id = %s",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            sources = conn.execute(
                """
                SELECT ts.citation_no, s.title, s.url
                FROM task_source ts
                JOIN source s ON s.id = ts.source_id
                WHERE ts.task_id = %s
                ORDER BY ts.citation_no
                """,
                (task_id,),
            ).fetchall()
        return {
            "id": row[0],
            "question": row[1],
            "status": row[2],
            "report": row[3],
            "created_at": row[4].isoformat(),
            "sources": [{"citation_id": r[0], "title": r[1], "url": r[2]} for r in sources],
        }


def _vec_str(v: list[float]) -> str:
    """把向量转成 pgvector 接受的 '[0.1,0.2,...]' 字符串。"""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
