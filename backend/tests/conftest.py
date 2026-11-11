"""pytest 公共 fixture。

连一个独立的测试数据库 ``deepsearch_test``（从主配置里派生连接串，仅换库名），
会话级自动创建 + 建 schema，每个用例前后清空所有表，避免污染开发数据。
"""

from urllib.parse import urlparse, urlunparse

import psycopg
import pytest

from app.core.config import get_settings
from app.rag.store import VectorStore

_TEST_DB_NAME = "deepsearch_test"


def _swap_db(dbname: str) -> str:
    """把配置里的数据库名替换成指定库名（其余连接信息保持不变）。"""
    base = urlparse(get_settings().db_url)
    return urlunparse(base._replace(path=f"/{dbname}"))


def _admin_dsn() -> str:
    return _swap_db("postgres")


def _test_dsn() -> str:
    return _swap_db(_TEST_DB_NAME)


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_db():
    """会话级：确保测试库存在并建好 schema（只执行一次）。"""
    conn = psycopg.connect(_admin_dsn())
    conn.autocommit = True
    try:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (_TEST_DB_NAME,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{_TEST_DB_NAME}"')
    finally:
        conn.close()
    VectorStore(_test_dsn(), get_settings().embedding_dim).init_schema()


def _truncate(store: VectorStore) -> None:
    with store._connect() as conn:
        conn.execute(
            "TRUNCATE task_checkpoint, task_trace, task_source, source_pool, "
            "chunk, source, research_task RESTART IDENTITY CASCADE"
        )
        conn.commit()


@pytest.fixture()
def store():
    """每个用例一个表已清空的 VectorStore。"""
    s = VectorStore(_test_dsn(), get_settings().embedding_dim)
    s.init_schema()
    _truncate(s)
    yield s
    _truncate(s)
