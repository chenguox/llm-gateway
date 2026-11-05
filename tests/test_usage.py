"""Boundary cases for the public token accounting contract."""

import pytest
from pydantic import ValidationError

from app.core.errors import GatewayError
from app.schemas.anthropic import AnthropicUsage
from app.schemas.chat import ChatCompletionUsage
from app.usage import aggregate_usage, anthropic_usage, compatible_usage


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens"])
@pytest.mark.parametrize("value", [-1, True, "10", 1.5])
def test_compatible_usage_rejects_invalid_counters(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        compatible_usage({field: value})


@pytest.mark.parametrize(
    "details",
    [
        {"prompt_cache_hit_tokens": 11},
        {"prompt_tokens_details": {"cached_tokens": 11}},
        {"completion_tokens_details": {"reasoning_tokens": 6}},
    ],
)
def test_usage_classification_cannot_exceed_total(details: dict) -> None:
    with pytest.raises(GatewayError) as error:
        compatible_usage({"prompt_tokens": 10, "completion_tokens": 5, **details})
    assert error.value.code == "UPSTREAM_INVALID_RESPONSE"


def test_zero_counters_are_known_and_provider_total_is_ignored() -> None:
    usage = compatible_usage({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 999})
    assert usage.model_dump() == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


@pytest.mark.parametrize("raw", [None, {}, {"prompt_tokens": 3}])
def test_missing_counters_are_not_invented(raw: dict | None) -> None:
    usage = compatible_usage(raw)
    if raw is None:
        assert usage is None
    else:
        assert usage.input_tokens == raw.get("prompt_tokens")
        assert usage.output_tokens is None
        assert usage.total_tokens is None


def test_messages_input_includes_both_cache_categories_once() -> None:
    usage = anthropic_usage(
        AnthropicUsage(
            input_tokens=10,
            cache_read_input_tokens=20,
            cache_creation_input_tokens=30,
            output_tokens=5,
        )
    )
    assert usage.model_dump() == {"input_tokens": 60, "output_tokens": 5, "total_tokens": 65}
    assert usage._details["cache_read_input_tokens"] == 20
    assert usage._details["cache_creation_input_tokens"] == 30


@pytest.mark.parametrize("usages", [[], [None], [None, None]])
def test_aggregating_missing_usage_stays_unknown(usages: list) -> None:
    assert aggregate_usage(usages) is None


def test_aggregation_requires_each_attempt_counter_to_be_known() -> None:
    first = ChatCompletionUsage(input_tokens=10, output_tokens=5)
    second = ChatCompletionUsage(input_tokens=2)
    assert aggregate_usage([first, second]).model_dump() == {
        "input_tokens": 12,
        "output_tokens": None,
        "total_tokens": None,
    }
    assert aggregate_usage([first, None]).model_dump() == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }


def test_aggregation_adds_attempts_and_keeps_classifications_private() -> None:
    first = compatible_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "prompt_cache_hit_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 3},
        }
    )
    second = ChatCompletionUsage(input_tokens=2, output_tokens=0)
    assert aggregate_usage([first, second]).model_dump() == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
    }
