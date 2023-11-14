from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Reference = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")]
Period = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
Currency = Literal["USD", "EUR", "GBP"]
PositiveUnits = Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
Money = Annotated[int, Field(strict=True, ge=1, le=1_000_000_000_000)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerInput(Input):
    external_id: Reference
    currency: Currency
    rate_cents: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]


class UsageInput(Input):
    event_id: Reference
    customer_id: UUID
    service_date: date
    units: PositiveUnits


class InvoiceInput(Input):
    customer_id: UUID
    period: Period

    @field_validator("period")
    @classmethod
    def valid_year(cls, value: str) -> str:
        date.fromisoformat(value + "-01")
        return value


class PaymentInput(Input):
    provider_ref: Reference
    invoice_id: UUID
    period: Period
    currency: Currency
    amount_cents: Money

    @field_validator("period")
    @classmethod
    def valid_year(cls, value: str) -> str:
        date.fromisoformat(value + "-01")
        return value


class StatementInput(Input):
    batch_id: Reference
    entries: Annotated[list[PaymentInput], Field(min_length=1, max_length=500)]

    @field_validator("entries")
    @classmethod
    def unique_references(cls, value: list[PaymentInput]) -> list[PaymentInput]:
        if len({x.provider_ref for x in value}) != len(value):
            raise ValueError("provider_ref must be unique within a batch")
        return value


class ReconcileInput(Input):
    period: Period

    @field_validator("period")
    @classmethod
    def valid_year(cls, value: str) -> str:
        date.fromisoformat(value + "-01")
        return value
