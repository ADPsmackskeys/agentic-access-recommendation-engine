"""Application configuration.

All configuration is environment-variable driven (Pydantic Settings). Nothing
that resembles a credential is ever hard-coded here; the defaults are only
useful for a throwaway local database.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

McpClientMode = Literal["inmemory", "stdio", "http", "direct"]
LlmProvider = Literal["none", "anthropic", "openai"]


class Settings(BaseSettings):
    """Runtime configuration for the whole system."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---------------------------------------------------------
    app_name: str = "Agentic Access Recommendation Engine"
    environment: str = "local"
    debug: bool = False
    api_prefix: str = "/api/v1"

    # --- Database ------------------------------------------------------------
    database_url: str | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = ""
    postgres_db: str = "newjoiner"
    postgres_sslmode: str = "prefer"

    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    db_echo: bool = False

    # --- Governance thresholds ----------------------------------------------
    affinity_threshold: float = Field(default=70.0, ge=0.0, le=100.0)
    peer_confidence_saturation: int = Field(default=8, ge=1)
    min_peer_count: int = Field(default=2, ge=1)

    risk_low_max: int = Field(default=30, ge=0, le=100)
    risk_medium_max: int = Field(default=69, ge=0, le=100)
    risk_high_max: int = Field(default=89, ge=0, le=100)

    # --- SailPoint (simulated) ----------------------------------------------
    sailpoint_source_name: str = "Agentic Access Recommendation Engine"
    sailpoint_request_type: str = "GrantAccess"
    sailpoint_included_statuses: list[str] = Field(
        default_factory=lambda: ["AUTO_APPROVED", "MANAGER_APPROVAL"]
    )

    # --- LLM -----------------------------------------------------------------
    llm_provider: LlmProvider = "none"
    llm_model: str = "claude-sonnet-5"
    llm_api_key: str | None = None
    llm_temperature: float = 0.0
    llm_timeout_seconds: int = 30
    llm_max_tokens: int = 1024

    # --- Demo mode -----------------------------------------------------------
    demo_mode: bool = True

    # --- MCP -----------------------------------------------------------------
    mcp_server_name: str = "agentic-access-recommendation-engine"
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8081
    mcp_client_mode: McpClientMode = "inmemory"
    mcp_client_url: str = "http://localhost:8081/mcp"
    mcp_client_timeout_seconds: int = 120

    # --- HTTP ----------------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    max_request_bytes: int = 1_048_576

    # --- Logging -------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("cors_origins", "sailpoint_included_statuses", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept comma-separated strings for list-valued settings."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):  # let pydantic parse JSON form
                return value
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("sailpoint_included_statuses")
    @classmethod
    def _upper_statuses(cls, value: list[str]) -> list[str]:
        return [item.upper() for item in value]

    @model_validator(mode="after")
    def _validate_risk_bands(self) -> "Settings":
        if not self.risk_low_max < self.risk_medium_max < self.risk_high_max:
            raise ValueError(
                "Risk band bounds must satisfy risk_low_max < risk_medium_max < risk_high_max "
                f"(got {self.risk_low_max}, {self.risk_medium_max}, {self.risk_high_max})"
            )
        return self

    @model_validator(mode="after")
    def _assemble_database_url(self) -> "Settings":
        """Build the SQLAlchemy URL from discrete parts when not given directly."""
        if self.database_url:
            normalised = _normalise_pg_driver(self.database_url)
            object.__setattr__(self, "database_url", normalised)
            return self

        dsn = PostgresDsn.build(
            scheme="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password or None,
            host=self.postgres_host,
            port=self.postgres_port,
            path=self.postgres_db,
        )
        url = str(dsn)
        if self.postgres_sslmode:
            url = f"{url}?sslmode={self.postgres_sslmode}"
        object.__setattr__(self, "database_url", url)
        return self

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def sqlalchemy_url(self) -> str:
        assert self.database_url is not None  # guaranteed by validator
        return self.database_url

    @property
    def llm_enabled(self) -> bool:
        """True only when a real LLM should be contacted.

        Demo mode always wins: the governance workflow must stay runnable and
        testable without any external AI dependency.
        """
        return not self.demo_mode and self.llm_provider != "none" and bool(self.llm_api_key)

    def safe_database_url(self) -> str:
        """Database URL with the password redacted, for logging."""
        return _redact_password(self.sqlalchemy_url)


def _normalise_pg_driver(url: str) -> str:
    """Force the psycopg (v3) driver and reject non-PostgreSQL databases."""
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgresql+"):
        # e.g. postgresql+asyncpg -> swap to the supported sync driver
        _, _, rest = url.partition("://")
        return f"postgresql+psycopg://{rest}"
    raise ValueError(
        "DATABASE_URL must point at PostgreSQL. This system does not support "
        f"any other database engine (got: {url.split('://', 1)[0]}://...)"
    )


def _redact_password(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    creds, _, host = rest.rpartition("@")
    if not creds:
        return url
    user, sep, _ = creds.partition(":")
    return f"{scheme}://{user}{':***' if sep else ''}@{host}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (used by tests that patch the environment)."""
    get_settings.cache_clear()
