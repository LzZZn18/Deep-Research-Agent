import asyncio

from app.rag.embedding import Embedder
from app.rag.store import VectorStore
from app.tools.base import Tool


class VectorSearchTool(Tool):
    name = "search_documents"
    description = (
        "在已抓取入库的资料中做语义检索，返回相关片段及其引用编号 [n] 和来源链接。"
        "优先检索本次任务已抓取的资料，不足时会自动从长期知识库补充。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词或问题"},
        },
        "required": ["query"],
    }

    def __init__(self, store: VectorStore, embedder: Embedder, task_id: int):
        self.store = store
        self.embedder = embedder
        self.task_id = task_id
        self.top_k = 5

    async def run(self, query: str) -> str:
        emb = await asyncio.to_thread(self.embedder.embed, [query])
        results = await asyncio.to_thread(
            self.store.search, self.task_id, emb[0], self.top_k
        )
        # 本任务命中不足 top_k 时，回退长期库补齐（双阶段检索）
        if len(results) < self.top_k:
            lib = await asyncio.to_thread(
                self.store.search_library, self.task_id, emb[0], self.top_k - len(results)
            )
            results = results + lib
        if not results:
            return "没有找到相关资料（可能尚未抓取任何来源，请先用 search + fetch 收集资料）。"
        lines = []
        for r in results:
            title = r["title"] or r["url"]
            lines.append(f"[{r['citation_id']}] {title}\n  URL: {r['url']}\n  {r['chunk']}")
        return "\n\n".join(lines)
