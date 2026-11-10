import asyncio
import json
from functools import lru_cache

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.agent.loop import EmitFn
from app.agent.orchestrator import run_deep_research
from app.core.config import Settings, get_settings
from app.core.llm.deepseek import DeepSeekProvider
from app.rag.embedding import FastEmbedEmbedder
from app.rag.store import VectorStore
from app.schemas.research import ResearchRequest
from app.tools.fetch import FetchTool
from app.tools.search import SearchTool
from app.tools.vector_search import VectorSearchTool

router = APIRouter()

_redis_pool = None
_redis_lock = asyncio.Lock()


async def _get_redis(settings: Settings):
    """懒加载的 arq Redis 连接池（进程内复用）。"""
    global _redis_pool
    if _redis_pool is None:
        async with _redis_lock:
            if _redis_pool is None:
                _redis_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _redis_pool


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@lru_cache
def get_embedder() -> FastEmbedEmbedder:
    s = get_settings()
    return FastEmbedEmbedder(s.embedding_model, s.embedding_dim)


@lru_cache
def get_store() -> VectorStore:
    s = get_settings()
    return VectorStore(s.db_url, s.embedding_dim)


def _make_provider(settings: Settings) -> DeepSeekProvider:
    return DeepSeekProvider(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.llm_timeout,
        retries=settings.llm_retries,
    )


async def _execute_research(
    settings: Settings,
    store: VectorStore,
    embedder: FastEmbedEmbedder,
    provider: DeepSeekProvider,
    task_id: int,
    question: str,
    emit: EmitFn,
) -> None:
    """执行一次研究：装配工具 → 跑流水线 → 落库 → 推送结果。新建与续跑共用。"""
    tools = [
        SearchTool(api_key=settings.tavily_api_key),
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
                max_sub_questions=settings.max_sub_questions,
                max_critic_rounds=settings.max_critic_rounds,
                researcher_steps=settings.researcher_steps,
                emit=emit,
            ),
            timeout=settings.task_timeout,
        )
    except asyncio.TimeoutError:
        await asyncio.to_thread(store.finish_task, task_id, "failed")
        await emit("error", {"message": f"研究超时（超过 {settings.task_timeout:g}s），已自动中止"})
        return
    await asyncio.to_thread(store.save_report, task_id, content)
    await asyncio.to_thread(store.finish_task, task_id, "done")
    await emit("answer", {"content": content})
    await emit("task_end", {})


def _stream(run_body) -> StreamingResponse:
    """把 run_body(emit) 包装成 SSE 流：负责队列、客户端断连时的取消。"""

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, data: dict) -> None:
        await queue.put((event, data))

    async def producer() -> None:
        try:
            await run_body(emit)
        finally:
            await queue.put(None)

    async def event_stream():
        task = asyncio.create_task(producer())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                event, data = item
                yield _sse(event, data)
            await task  # 正常结束：等待后台任务收尾
        finally:
            # 客户端断开（取消/关闭页面）时，终止仍在运行的后台研究
            if not task.done():
                task.cancel()
                try:
                    await asyncio.shield(task)
                except BaseException:
                    pass

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


@router.post("/research")
async def research(req: ResearchRequest, settings: Settings = Depends(get_settings)):
    if not settings.deepseek_api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_API_KEY 未配置，请在 backend/.env 中填写")
    if not settings.tavily_api_key:
        raise HTTPException(status_code=400, detail="TAVILY_API_KEY 未配置，请在 backend/.env 中填写")

    store = get_store()
    await asyncio.to_thread(store.init_schema)
    task_id = await asyncio.to_thread(store.create_task, req.question, "pending")

    redis = await _get_redis(settings)
    job = await redis.enqueue_job("run_research_task", task_id, req.question)
    return {"task_id": task_id, "job_id": job.job_id}


@router.post("/tasks/{task_id}/resume")
async def resume(task_id: int, settings: Settings = Depends(get_settings)):
    store = get_store()
    await asyncio.to_thread(store.init_schema)

    # 校验：任务存在、未完成/未在跑、且存在可恢复的检查点
    task = await asyncio.to_thread(store.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] == "done":
        raise HTTPException(status_code=409, detail="任务已完成，无需续跑")
    if task["status"] in ("pending", "running"):
        raise HTTPException(status_code=409, detail="任务正在进行中，无需续跑")
    cp = await asyncio.to_thread(store.load_checkpoint, task_id)
    if cp is None:
        raise HTTPException(status_code=400, detail="该任务没有可恢复的检查点")

    await asyncio.to_thread(store.set_task_status, task_id, "pending")
    redis = await _get_redis(settings)
    job = await redis.enqueue_job("run_research_task", task_id, task["question"])
    return {"task_id": task_id, "job_id": job.job_id}


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: int):
    store = get_store()
    await asyncio.to_thread(store.init_schema)
    task = await asyncio.to_thread(store.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    await asyncio.to_thread(store.set_task_status, task_id, "cancelled")
    return {"status": "cancelled"}


@router.get("/tasks")
async def list_tasks():
    store = get_store()
    await asyncio.to_thread(store.init_schema)
    return await asyncio.to_thread(store.list_tasks, 50)


@router.get("/tasks/lookup")
async def lookup_task(question: str):
    store = get_store()
    await asyncio.to_thread(store.init_schema)
    task = await asyncio.to_thread(store.find_done_task, question)
    return {"task": task}


@router.get("/tasks/{task_id}")
async def get_task(task_id: int):
    store = get_store()
    await asyncio.to_thread(store.init_schema)
    task = await asyncio.to_thread(store.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    task["stats"] = await asyncio.to_thread(store.task_stats, task_id)
    task["trace"] = await asyncio.to_thread(store.list_trace, task_id)
    cp = await asyncio.to_thread(store.load_checkpoint, task_id)
    task["plan"] = cp.get("plan") if cp else {}
    task["findings"] = cp.get("findings") if cp else []
    task["critic_rounds"] = cp.get("critic_rounds") if cp else []
    task["stage"] = cp.get("stage") if cp else None
    return task
