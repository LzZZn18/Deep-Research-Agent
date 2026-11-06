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

    # Embedding（本地 BGE）
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    hf_endpoint: str = ""  # 国内下载模型慢时，设为 https://hf-mirror.com

    # Agent
    max_agent_steps: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
