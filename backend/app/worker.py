"""后台 worker：从 Redis 队列取研究任务，跑流水线并落库。

阶段2：通过 Redis pub/sub 把进度事件实时发布到 ``task:{id}`` 频道，
后端 SSE 端点订阅该频道转发给前端。
"""

import asyncio
import json

import redis.asyncio as aioredis
from arq.connections import RedisSettings
from arq.worker import Worker

from app.agent.orchestrator import run_deep_research
from app.core.config import get_settings
from app.core.llm.deepseek import DeepSeekProvider
from app.rag.embedding import FastEmbedEmbedder
from app.rag.store import VectorStore
from app.tools.fetch import FetchTool
from app.tools.search import SearchTool
from app.tools.vector_search import VectorSearchTool

_settings = get_settings()
_store = None
_embedder = None
_provider = None
_redis = None


def _redis_client() -> aioredis.Redis:
    """进程内复用发布用的 Redis 连接。"""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(_settings.redis_url, decode_responses=True)
    return _redis


def _deps():
    """进程内复用 store/embedder/provider（embedder 加载模型较慢，只加载一次）。"""
    global _store, _embedder, _provider
    if _embedder is None:
        _store = VectorStore(_settings.db_url, _settings.embedding_dim)
        _embedder = FastEmbedEmbedder(_settings.embedding_model, _settings.embedding_dim)
        _provider = DeepSeekProvider(
            api_key=_settings.deepseek_api_key,
            base_url=_settings.deepseek_base_url,
            model=_settings.deepseek_model,
            timeout=_settings.llm_timeout,
            retries=_settings.llm_retries,
        )
    return _store, _embedder, _provider


async def run_research_task(ctx, task_id: int, question: str) -> None:
    """执行一个深度研究任务（新建或续跑，读 checkpoint 自动断点恢复）。"""
    store, embedder, provider = _deps()
    await asyncio.to_thread(store.init_schema)

    # 排队期间可能已被取消：仅 pending -> running 才继续
    started = await asyncio.to_thread(store.start_if_pending, task_id)
    if not started:
        return

    channel = f"task:{task_id}"

    async def emit(event: str, data: dict) -> None:
        """把进度事件发布到 Redis 频道，由后端 SSE 端点转发给前端。"""
        await _redis_client().publish(
            channel, json.dumps({"event": event, "data": data})
        )

    tools = [
        SearchTool(api_key=_settings.tavily_api_key),
        FetchTool(store=store, embedder=embedder, task_id=task_id),
        VectorSearchTool(store=store, embedder=embedder, task_id=task_id),
    ]

    try:
        content = await asyncio.wait_for(
            run_deep_research(
                provider=provider,
                store=store,
                tools=tools,
                question=question,
                task_id=task_id,
                max_sub_questions=_settings.max_sub_questions,
                max_critic_rounds=_settings.max_critic_rounds,
                researcher_steps=_settings.researcher_steps,
                emit=emit,
            ),
            timeout=_settings.task_timeout,
        )
    except asyncio.TimeoutError:
        await emit("error", {"message": f"研究超时（超过 {_settings.task_timeout:g}s），已自动中止"})
        await emit("task_end", {})
        await asyncio.to_thread(store.finish_if_running, task_id, "failed")
        return
    except Exception as e:
        await emit("error", {"message": f"研究失败：{e}"})
        await emit("task_end", {})
        await asyncio.to_thread(store.finish_if_running, task_id, "failed")
        return

    await asyncio.to_thread(store.save_report, task_id, content)
    await asyncio.to_thread(store.finish_if_running, task_id, "done")
    await emit("answer", {"content": content})
    await emit("task_end", {})


if __name__ == "__main__":
    Worker(
        functions=[run_research_task],
        redis_settings=RedisSettings.from_dsn(_settings.redis_url),
        max_jobs=1,  # 阶段1先单并发，避免打爆 LLM 限流
        job_timeout=int(_settings.task_timeout) + 60,
    ).run()
