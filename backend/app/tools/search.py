import httpx

from app.tools.base import Tool


class SearchTool(Tool):
    name = "search"
    description = "在互联网上搜索给定关键词，返回相关结果的标题、链接和摘要。用于收集研究所需资料。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
        },
        "required": ["query"],
    }

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def run(self, query: str) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": self.api_key, "query": query, "max_results": 5},
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        if not results:
            return "没有找到相关结果。"

        lines = []
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            content = (r.get("content") or "").strip()
            lines.append(f"- {title}\n  {url}\n  {content}")
        return "\n".join(lines)
