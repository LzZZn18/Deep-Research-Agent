# Deep Research

多智能体深度研究流水线：输入一个问题，自动完成「拆解 → 检索 → 写作 → 审核」，输出带引用来源、可追溯的调研报告。

当前进度：**M3** —— 多智能体编排（Planner/Researcher/Writer/Critic）+ RAG 引用溯源 + 历史记录 + 全链路 trace/成本统计 + 超时/重试 + Web 界面，Docker 一键部署。

## 快速开始（Docker 一键部署）

```bash
# 1. 准备环境变量（填入 DeepSeek 与 Tavily 的 key）
cp backend/.env.example backend/.env
# 编辑 backend/.env 填入：
#   DEEPSEEK_API_KEY=...
#   TAVILY_API_KEY=...
#   （国内下载嵌入模型慢时，取消注释 HF_ENDPOINT=https://hf-mirror.com）

# 2. 一键启动（首次会拉镜像 + 下载 BGE 嵌入模型，稍等）
docker compose up -d --build

# 3. 打开前端
#    http://localhost
```

首次研究时会下载 BGE 嵌入模型，缓存在 `embedding-cache` 卷里，之后无需重新下载。

## 本地开发

后端：

```bash
cd backend
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
cp .env.example .env            # 填 key
uvicorn app.main:app --reload
```

前端（另开终端）：

```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173，已代理 /api 到 8000
```

测试：

```bash
curl -N -X POST http://localhost:8000/api/research \
  -H "Content-Type: application/json" \
  -d '{"question": "什么是向量数据库？"}'
```

## 目录结构

```
backend/            # FastAPI 后端
  app/
    main.py         # 入口
    api/            # 路由（SSE 流式）
    agent/          # 编排器 + 手写 tool-calling 循环
    core/           # 配置 + LLM Provider + 成本/重试
    tools/          # 搜索/抓取/向量检索
    rag/            # 向量库 + 嵌入
    schemas/        # Pydantic 模型
frontend/           # Vue3 + Vite 前端
docs/               # 需求与技术选型文档
docker-compose.yml  # 一键部署（db + backend + frontend）
```

## 技术栈

FastAPI · DeepSeek（LLM）· Tavily（搜索）· PostgreSQL + pgvector（RAG）· fastembed/BGE（嵌入）· Vue3 + Vite · Docker Compose

详见 [docs/requirements.md](docs/requirements.md)。
