import asyncio
import json
from functools import lru_cache

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

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


@router.post("/research")
async def research(req: ResearchRequest, settings: Settings = Depends(get_settings)):
    provider = DeepSeekProvider(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
    )
    store = get_store()
    embedder = get_embedder()

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, data: dict) -> None:
        await queue.put((event, data))

    async def run() -> None:
        task_id = None
        try:
            if not settings.deepseek_api_key:
                await emit("error", {"message": "DEEPSEEK_API_KEY 未配置，请在 backend/.env 中填写"})
                return
            if not settings.tavily_api_key:
                await emit("error", {"message": "TAVILY_API_KEY 未配置，请在 backend/.env 中填写"})
                return
            await asyncio.to_thread(store.init_schema)
            task_id = await asyncio.to_thread(store.create_task, req.question)
            tools = [
                SearchTool(api_key=settings.tavily_api_key),
                FetchTool(store=store, embedder=embedder, task_id=task_id),
                VectorSearchTool(store=store, embedder=embedder, task_id=task_id),
            ]
            await emit("task_start", {"question": req.question, "task_id": task_id})
            content = await run_deep_research(
                provider=provider,
                store=store,
                tools=tools,
                question=req.question,
                task_id=task_id,
                max_sub_questions=settings.max_sub_questions,
                max_critic_rounds=settings.max_critic_rounds,
                researcher_steps=settings.researcher_steps,
                emit=emit,
            )
            await asyncio.to_thread(store.finish_task, task_id, "done")
            await emit("answer", {"content": content})
            await emit("task_end", {})
        except Exception as e:
            if task_id is not None:
                try:
                    await asyncio.to_thread(store.finish_task, task_id, "failed")
                except Exception:
                    pass
            await emit("error", {"message": str(e)})
        finally:
            await queue.put(None)

    async def event_stream():
        task = asyncio.create_task(run())
        while True:
            item = await queue.get()
            if item is None:
                break
            event, data = item
            yield _sse(event, data)
        await task

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
