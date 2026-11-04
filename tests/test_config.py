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


def test_model_keys_use_individual_variables_and_environment_precedes_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_MODEL_A_KEY=file-a\nTEST_MODEL_B_KEY=file-b\n", encoding="utf-8")
    monkeypatch.setenv("TEST_MODEL_A_KEY", "environment-a")
    monkeypatch.delenv("TEST_MODEL_B_KEY", raising=False)
    settings = Settings(_env_file=env_file)

    first_key = settings.resolve_model_key("TEST_MODEL_A_KEY")
    second_key = settings.resolve_model_key("TEST_MODEL_B_KEY")

    assert first_key is not None
    assert second_key is not None
    assert first_key.get_secret_value() == "environment-a"
    assert second_key.get_secret_value() == "file-b"
    assert "environment-a" not in repr(first_key)
    assert "file-b" not in repr(second_key)
    assert "file-b" not in repr(settings)


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_environment_key_does_not_fall_back_to_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path, value: str
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_MODEL_A_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("TEST_MODEL_A_KEY", value)

    key = Settings(_env_file=env_file).resolve_model_key("TEST_MODEL_A_KEY")

    assert key is not None
    assert key.get_secret_value() == value


def test_disabling_dotenv_also_disables_model_key_file_loading(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    (tmp_path / ".env").write_text("TEST_MODEL_A_KEY=file-key\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_MODEL_A_KEY", raising=False)

    assert Settings(_env_file=None).resolve_model_key("TEST_MODEL_A_KEY") is None


def test_model_keys_honor_multiple_dotenv_files(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    first.write_text("TEST_MODEL_A_KEY=first-key\n", encoding="utf-8")
    second.write_text("TEST_MODEL_A_KEY=second-key\n", encoding="utf-8")
    monkeypatch.delenv("TEST_MODEL_A_KEY", raising=False)

    key = Settings(_env_file=(first, second)).resolve_model_key("TEST_MODEL_A_KEY")

    assert key is not None
    assert key.get_secret_value() == "second-key"
