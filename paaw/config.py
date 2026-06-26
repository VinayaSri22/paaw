"""
PAAW Configuration

Loads settings from environment variables with sensible defaults.
Uses Pydantic for validation.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """LLM provider configuration."""

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Default model - set via LLM_DEFAULT_MODEL env var
    default_model: str = Field(default="claude-sonnet-4-6", description="Default model")
    
    # Optional: separate model for complex reasoning (defaults to same)
    reasoning_model: str = Field(default="claude-sonnet-4-6", description="Model for complex reasoning")
    
    # Fallback model for when primary fails (lightweight/local)
    fallback_model: str | None = Field(default=None, description="Fallback model when primary fails")
    fallback_api_base: str | None = Field(default=None, description="API base URL for fallback model")
    fallback_max_tokens: int = Field(default=1024, description="Reduced max tokens for fallback")
    fallback_context_limit: int = Field(default=2048, description="Reduced context for fallback")
    fallback_disable_cache: bool = Field(default=True, description="Disable prompt caching for fallback")
    
    # Embeddings are optional - will be added in Phase 3 for semantic search
    embedding_model: str | None = Field(default=None, description="Optional embedding model")

    # API Keys (loaded from env without prefix)
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    ollama_base_url: str | None = Field(default=None, validation_alias="OLLAMA_BASE_URL")

    # Limits
    max_tokens: int = Field(default=4096, description="Max tokens for response")
    temperature: float = Field(default=0.7, ge=0, le=2)
    timeout: int = Field(default=120, description="Request timeout in seconds")
    
    # Caching control
    disable_prompt_cache: bool = Field(default=False, description="Disable prompt caching globally")


class DatabaseSettings(BaseSettings):
    """Database configuration."""

    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    url: PostgresDsn = Field(
        default="postgresql://paaw:paaw@localhost:5432/paaw",
        alias="DATABASE_URL",
    )
    pool_size: int = Field(default=10, ge=1, le=100)
    pool_max_overflow: int = Field(default=20, ge=0)


class ValkeySettings(BaseSettings):
    """Valkey (Redis) configuration."""

    model_config = SettingsConfigDict(env_prefix="VALKEY_")

    url: str = Field(default="valkey://localhost:6379", alias="VALKEY_URL")
    pool_size: int = Field(default=10, ge=1, le=100)


class WebSettings(BaseSettings):
    """Web server configuration."""

    model_config = SettingsConfigDict(env_prefix="WEB_")

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8080, ge=1, le=65535)
    cors_origins: list[str] = Field(default=["http://localhost:3000", "http://localhost:8080"])


class ChannelSettings(BaseSettings):
    """Channel configuration."""

    # Telegram
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")

    # Slack
    slack_bot_token: str | None = Field(default=None, alias="SLACK_BOT_TOKEN")
    slack_app_token: str | None = Field(default=None, alias="SLACK_APP_TOKEN")

    # Discord
    discord_bot_token: str | None = Field(default=None, alias="DISCORD_BOT_TOKEN")


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "PAAW"
    debug: bool = Field(default=False, alias="DEBUG")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )

    # Default user ID (single-user mode)
    default_user_id: str = "00000000-0000-0000-0000-000000000001"

    # Sub-settings
    llm: LLMSettings = Field(default_factory=LLMSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    valkey: ValkeySettings = Field(default_factory=ValkeySettings)
    web: WebSettings = Field(default_factory=WebSettings)
    channels: ChannelSettings = Field(default_factory=ChannelSettings)

    # Memory settings
    memory_decay_rate: float = Field(default=0.99, ge=0, le=1)
    memory_decay_days: int = Field(default=7, ge=1)
    context_max_tokens: int = Field(default=4000, ge=100)
    recent_conversations_count: int = Field(default=5, ge=1)

    @field_validator("log_level", mode="before")
    @classmethod
    def uppercase_log_level(cls, v: str) -> str:
        return v.upper() if isinstance(v, str) else v
    
    @property
    def database_url(self) -> str:
        """Get database URL as string for asyncpg."""
        return str(self.database.url)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Convenience export
settings = get_settings()
