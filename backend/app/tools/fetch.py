import asyncio
import re

import httpx
import trafilatura

from app.core.security import check_public_url
from app.rag.chunker import chunk_text
from app.rag.embedding import Embedder
from app.rag.store import VectorStore
from app.tools.base import Tool


class FetchTool(Tool):
    name = "fetch"
    description = (
        "抓取并保存某个 URL 的网页正文。抓取后自动切块、向量化并入库，"
        "返回该来源的引用编号 [n] 和正文摘要。用于收集和保存资料。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的网页 URL"},
        },
        "required": ["url"],
    }

    def __init__(self, store: VectorStore, embedder: Embedder, task_id: int):
        self.store = store
        self.embedder = embedder
        self.task_id = task_id

    async def run(self, url: str) -> str:
        # 1. 抓取缓存：已入库的来源直接复用，跳过抓取+向量化
        cached = await asyncio.to_thread(self.store.get_source, url)
        if cached is not None:
            citation_id = await asyncio.to_thread(
                self.store.link_source, self.task_id, cached["id"]
            )
            return (
                f"已缓存（复用之前抓取的内容），引用编号 [{citation_id}]\n"
                f"标题：{cached['title'] or url}\n"
                f"URL：{url}"
            )

        # 2. 防 SSRF：只允许公网 http/https
        blocked = await asyncio.to_thread(check_public_url, url)
        if blocked is not None:
            return f"拒绝抓取：{blocked}"

        # 3. 抓取 + 提取 + 切块 + 向量化 + 入库
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        text = (await asyncio.to_thread(trafilatura.extract, html, url=url) or "").strip()
        if not text:
            return f"无法从 {url} 提取正文内容。"

        title = await asyncio.to_thread(self._extract_title, html) or url
        chunks = chunk_text(text)
        embeddings = await asyncio.to_thread(self.embedder.embed, chunks)
        citation_id = await asyncio.to_thread(
            self.store.add_source, self.task_id, url, title, chunks, embeddings
        )

        return (
            f"已入库，引用编号 [{citation_id}]（共 {len(chunks)} 个片段）\n"
            f"标题：{title}\n"
            f"URL：{url}\n\n"
            f"正文摘要（前 500 字）：\n{text[:500]}"
        )

    @staticmethod
    def _extract_title(html: str) -> str:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else ""
