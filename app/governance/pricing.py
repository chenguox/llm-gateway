"""Versioned Decimal pricing with explicit treatment of unknown usage."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.schemas.chat import ChatCompletionUsage

Money = Decimal


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["deepseek", "openai_compatible", "anthropic"]
    model: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=64)
    effective_at: AwareDatetime
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    input_per_million: Money = Field(ge=0, allow_inf_nan=False)
    output_per_million: Money = Field(ge=0, allow_inf_nan=False)
    input_pricing: Literal["flat", "cache_split"] = "flat"
    cache_read_per_million: Money | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_creation_per_million: Money | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_rates(self) -> "Price":
        if self.input_pricing == "flat" and (
            self.cache_read_per_million is not None or self.cache_creation_per_million is not None
        ):
            raise ValueError("Cache prices require cache_split input pricing")
        return self


class Prices(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prices: list[Price] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_versions(self) -> "Prices":
        versions = [(price.provider, price.model, price.version) for price in self.prices]
        dates = [(price.provider, price.model, price.effective_at) for price in self.prices]
        if len(set(versions)) != len(versions) or len(set(dates)) != len(dates):
            raise ValueError("Model price versions and effective dates must be unique")
        return self


class Cost(BaseModel):
    status: Literal["not_incurred", "estimated", "partial", "unpriced"] = "unpriced"
    currency: str | None = None
    amount: Money | None = None
    known_amount: Money | None = None
    by_currency: dict[str, Money] = Field(default_factory=dict)
    price_snapshot: Price | None = None


class PriceBook:
    def __init__(self, prices: Prices) -> None:
        self.prices = prices.prices

    @classmethod
    def from_file(cls, path: Path) -> "PriceBook":
        try:
            return cls(Prices.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            raise ValueError("Invalid pricing configuration") from None

    def select(self, provider: str, model: str, timestamp: datetime) -> Price | None:
        matches = [
            price
            for price in self.prices
            if price.provider == provider
            and price.model == model
            and price.effective_at <= timestamp
        ]
        return max(matches, key=lambda price: price.effective_at) if matches else None


def estimate(price: Price | None, usage: ChatCompletionUsage | None, complete: bool) -> Cost:
    if price is None:
        return Cost()
    known = Decimal(0)
    valid = complete and usage is not None
    if usage is None:
        valid = False
    else:
        if usage.output_tokens is None:
            valid = False
        else:
            known += Decimal(usage.output_tokens) * price.output_per_million
        if usage.input_tokens is None:
            valid = False
        elif price.input_pricing == "flat":
            known += Decimal(usage.input_tokens) * price.input_per_million
        else:
            read = usage._details.get("cache_read_input_tokens")
            created = usage._details.get("cache_creation_input_tokens")
            if read is None or created is None or read + created > usage.input_tokens:
                valid = False
            else:
                known += Decimal(usage.input_tokens - read - created) * price.input_per_million
                for count, rate in (
                    (read, price.cache_read_per_million),
                    (created, price.cache_creation_per_million),
                ):
                    if count and rate is None:
                        valid = False
                    elif rate is not None:
                        known += Decimal(count) * rate
    known /= Decimal(1_000_000)
    return Cost(
        status="estimated" if valid else "partial",
        currency=price.currency,
        amount=known if valid else None,
        known_amount=known,
        by_currency={price.currency: known},
        price_snapshot=price,
    )


def aggregate_cost(costs: list[Cost]) -> Cost:
    if not costs or all(cost.status == "not_incurred" for cost in costs):
        return Cost(status="not_incurred", amount=Decimal(0), known_amount=Decimal(0))
    amounts: dict[str, Decimal] = {}
    for cost in costs:
        for currency, amount in cost.by_currency.items():
            amounts[currency] = amounts.get(currency, Decimal(0)) + amount
    if not amounts:
        return Cost()
    currency = next(iter(amounts)) if len(amounts) == 1 else None
    complete = currency is not None and all(
        cost.status in {"estimated", "not_incurred"} for cost in costs
    )
    known = amounts[currency] if currency is not None else None
    return Cost(
        status="estimated" if complete else "partial",
        currency=currency,
        amount=known if complete else None,
        known_amount=known,
        by_currency=amounts,
    )
