"""Typed configuration loaded from environment variables and .env."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, HttpUrl, PrivateAttr, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="LLM Gateway", min_length=1)
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    models_config_path: Path = Path(__file__).resolve().parents[2] / "config" / "models.json"
    prices_config_path: Path = Path(__file__).resolve().parents[2] / "config" / "prices.json"
    call_records_path: Path = Path(__file__).resolve().parents[2] / "data" / "calls.sqlite3"
    call_records_retention_days: int = Field(default=30, ge=1, le=3650)
    audit_hmac_key: SecretStr | None = None
    audit_hmac_key_id: str = Field(default="v1", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: HttpUrl = HttpUrl("https://api.deepseek.com")
    deepseek_timeout_seconds: float = Field(default=60.0, gt=0)

    _credentials_env_file: Any = PrivateAttr(default=None)
    _credentials_env_encoding: str = PrivateAttr(default="utf-8")

    @field_validator("audit_hmac_key", mode="before")
    @classmethod
    def validate_audit_key(cls, value: Any) -> Any:
        key = value.get_secret_value() if isinstance(value, SecretStr) else value
        if key is None or isinstance(key, str) and not key.strip():
            return None
        if isinstance(key, str) and len(key.encode("utf-8")) < 32:
            raise ValueError("Audit HMAC key must contain at least 32 bytes")
        return value

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        self._credentials_env_file = values.get("_env_file", self.model_config.get("env_file"))
        self._credentials_env_encoding = (
            values.get("_env_file_encoding")
            or self.model_config.get("env_file_encoding")
            or "utf-8"
        )

    def resolve_model_key(self, variable: str) -> SecretStr | None:
        """Read only the named credential, honoring this instance's dotenv settings."""

        class ModelCredential(BaseSettings):
            model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

            api_key: SecretStr | None = Field(default=None, validation_alias=variable)

        return ModelCredential(
            _env_file=self._credentials_env_file,
            _env_file_encoding=self._credentials_env_encoding,
        ).api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
