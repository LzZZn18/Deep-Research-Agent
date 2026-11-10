from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # Tavily
    tavily_api_key: str = ""

    # PostgreSQL (pgvector)
    db_url: str = "postgresql://deepsearch:deepsearch@localhost:5432/deepsearch"

    # Redis（任务队列）
    redis_url: str = "redis://localhost:6379/0"

    # Embedding（本地 BGE）
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    hf_endpoint: str = ""  # 国内下载模型慢时，设为 https://hf-mirror.com

    # Agent
    max_agent_steps: int = 8
    researcher_steps: int = 5
    max_sub_questions: int = 3
    max_critic_rounds: int = 2

    # 可靠性：超时与重试
    tool_timeout: float = 30.0  # 单次工具调用超时（秒）
    tool_retries: int = 2       # 工具失败重试次数
    llm_timeout: float = 60.0   # LLM 调用超时（秒）
    llm_retries: int = 2        # LLM 失败重试次数
    task_timeout: float = 600.0 # 整个研究任务超时（秒）

    # 成本控制
    token_budget: int = 200_000 # 单任务 token 预算上限（prompt+completion 合计）


@lru_cache
def get_settings() -> Settings:
    return Settings()
