"""后台 worker：从 Redis 队列取研究任务，跑流水线并落库（阶段1：无实时推送，靠轮询）。"""

import asyncio

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
            ),
            timeout=_settings.task_timeout,
        )
    except asyncio.TimeoutError:
        await asyncio.to_thread(store.finish_if_running, task_id, "failed")
        return
    except Exception:
        await asyncio.to_thread(store.finish_if_running, task_id, "failed")
        return

    await asyncio.to_thread(store.save_report, task_id, content)
    await asyncio.to_thread(store.finish_if_running, task_id, "done")


if __name__ == "__main__":
    Worker(
        functions=[run_research_task],
        redis_settings=RedisSettings.from_dsn(_settings.redis_url),
        max_jobs=1,  # 阶段1先单并发，避免打爆 LLM 限流
        job_timeout=int(_settings.task_timeout) + 60,
    ).run()
