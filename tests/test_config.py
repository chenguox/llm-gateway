import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_environment_variables_override_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('APP_NAME="From file"\nLOG_LEVEL=INFO\n', encoding="utf-8")
    monkeypatch.setenv("APP_NAME", "From environment")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings(_env_file=env_file)

    assert settings.app_name == "From environment"
    assert settings.log_level == "DEBUG"


def test_invalid_log_level_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "INVALID")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
