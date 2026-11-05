import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.agent.loop import run_agent
from app.core.config import Settings, get_settings
from app.core.llm.deepseek import DeepSeekProvider
from app.schemas.research import ResearchRequest
from app.tools.search import SearchTool

router = APIRouter()


def _sse(event: str, data: dict) -> str:
    """把一条事件编码成 SSE 帧。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/research")
async def research(req: ResearchRequest, settings: Settings = Depends(get_settings)):
    provider = DeepSeekProvider(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
    )
    tools = [SearchTool(api_key=settings.tavily_api_key)]

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, data: dict) -> None:
        await queue.put((event, data))

    async def run() -> None:
        try:
            if not settings.deepseek_api_key:
                await emit("error", {"message": "DEEPSEEK_API_KEY 未配置，请在 backend/.env 中填写"})
                return
            if not settings.tavily_api_key:
                await emit("error", {"message": "TAVILY_API_KEY 未配置，请在 backend/.env 中填写"})
                return
            await emit("task_start", {"question": req.question})
            response = await run_agent(
                provider=provider,
                tools=tools,
                query=req.question,
                max_steps=settings.max_agent_steps,
                emit=emit,
            )
            await emit("answer", {"content": response.content})
            await emit("task_end", {})
        except Exception as e:  # 任何异常都推给前端，不让流挂死
            await emit("error", {"message": str(e)})
        finally:
            await queue.put(None)  # 结束哨兵

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
