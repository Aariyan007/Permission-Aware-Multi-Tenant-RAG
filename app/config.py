from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The API connects as the restricted `rag_app` role; migrations use the owner URL.
    database_url: str = "postgresql://rag_app:rag_app_pw@localhost:5432/rag"
    admin_database_url: str = "postgresql://postgres:postgres@localhost:5432/rag"
    app_db_password: str = "rag_app_pw"

    jwt_secret: str = "dev-secret-change-me"
    jwt_ttl_seconds: int = 3600
    bootstrap_key: str = "dev-bootstrap-key"  # guards POST /tenants

    embed_dim: int = 256  # must match vector(256) in migrations
    voyage_api_key: str | None = None
    voyage_model: str = "voyage-3.5"
    rerank_enabled: bool = True
    rerank_model: str = "rerank-2.5"
    search_candidates: int = 20  # how many each retriever returns before fusion / reranking
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5-5"

    exact_search_max_chunks: int = 5000  # tenants at or below this are searched exactly, not via ANN
    rate_limit_per_minute: int = 120  # per tenant
    otel_endpoint: str | None = None  # e.g. http://jaeger:4318


@lru_cache
def get_settings() -> Settings:
    return Settings()
