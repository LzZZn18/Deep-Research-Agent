import asyncio

from app.rag.embedding import Embedder
from app.rag.store import VectorStore
from app.tools.base import Tool


class VectorSearchTool(Tool):
    name = "search_documents"
    description = (
        "在已抓取入库的资料中做语义检索，返回相关片段及其引用编号 [n] 和来源链接。"
        "用于写作前查证已收集的资料。"
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

    async def run(self, query: str) -> str:
        emb = await asyncio.to_thread(self.embedder.embed, [query])
        results = await asyncio.to_thread(self.store.search, self.task_id, emb[0], 5)
        if not results:
            return "没有找到相关资料（可能尚未抓取任何来源，请先用 search + fetch 收集资料）。"
        lines = []
        for r in results:
            title = r["title"] or r["url"]
            lines.append(f"[{r['citation_id']}] {title}\n  URL: {r['url']}\n  {r['chunk']}")
        return "\n\n".join(lines)
