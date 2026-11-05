# Deep Research

多智能体深度研究流水线：输入一个问题，自动完成「拆解 → 检索 → 写作 → 审核」，输出带引用来源的调研报告。

当前进度：**M0** —— 单 Agent 闭环（搜索 + 手写 tool-calling 循环 + SSE 流式输出）。

## 目录结构

```
backend/          # Python 后端（FastAPI）
  app/
    main.py       # 入口
    api/          # 路由（SSE 流式）
    core/         # 配置 + LLM Provider 抽象
    agent/        # 手写 tool-calling 循环
    tools/        # 工具抽象 + 搜索工具
    schemas/      # Pydantic 模型
docs/             # 需求与技术选型文档
```

## 快速开始

```bash
cd backend
python -m venv .venv
# Windows (Git Bash):
source .venv/Scripts/activate
pip install -r requirements.txt

# 复制 .env.example 为 .env，填入 DeepSeek 与 Tavily 的 key
cp .env.example .env

uvicorn app.main:app --reload
```

测试（另开一个终端）：

```bash
curl -N -X POST http://localhost:8000/api/research \
  -H "Content-Type: application/json" \
  -d '{"question": "什么是向量数据库？"}'
```

## 技术栈

FastAPI · DeepSeek（LLM）· Tavily（搜索）· httpx · pydantic-settings

详见 [docs/requirements.md](docs/requirements.md)。
