"""Conditional arithmetic for a face-cash plus excess-share convertible bond.

The input equity value already recognizes the outstanding bond liability. Paying
the face amount extinguishes that liability, so face cash is never subtracted a
second time. This is a scenario calculator, not a settlement forecast or a
general convertible-bond contract engine.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_FLOOR, Decimal, localcontext
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .scenario import finite_decimal

_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)
_TENTH_YEN = Decimal("0.1")


def _date(value: object) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError("date must be ISO YYYY-MM-DD")


class SpecialDividendAdjustment(BaseModel):
    """One contract-derived per-share excess, not the total shareholder dividend."""

    model_config = _CONFIG
    special_dividend_per_share_yen: Decimal
    reference_close_yen: Decimal
    final_record_date: date | None = None
    effective_on: date | None = None
    reference_window_start: date | None = None
    reference_window_end: date | None = None

    @field_validator("special_dividend_per_share_yen", "reference_close_yen", mode="before")
    @classmethod
    def _decimal(cls, value: object) -> Decimal:
        return finite_decimal(value)

    @field_validator(
        "final_record_date",
        "effective_on",
        "reference_window_start",
        "reference_window_end",
        mode="before",
    )
    @classmethod
    def _dates(cls, value: object) -> date | None:
        return None if value is None else _date(value)

    @model_validator(mode="after")
    def _bounds(self) -> SpecialDividendAdjustment:
        if not 0 <= self.special_dividend_per_share_yen < self.reference_close_yen:
            raise ValueError("special dividend must be nonnegative and below reference close")
        if any(
            value != value.quantize(_TENTH_YEN)
            for value in (self.special_dividend_per_share_yen, self.reference_close_yen)
        ):
            raise ValueError("dividend and reference close must use 0.1-yen precision")
        if (self.reference_window_start is None) != (self.reference_window_end is None):
            raise ValueError("reference close window requires both dates")
        if (
            self.reference_window_start is not None
            and self.reference_window_end is not None
            and self.reference_window_start > self.reference_window_end
        ):
            raise ValueError("reference close window dates are reversed")
        if (
            self.reference_window_end
            and self.final_record_date
            and self.reference_window_end >= self.final_record_date
        ):
            raise ValueError("reference close window must precede final record date")
        if (
            self.final_record_date
            and self.effective_on
            and self.effective_on <= self.final_record_date
        ):
            raise ValueError("dividend adjustment must take effect after final record date")
        return self


class CashShareCBInput(BaseModel):
    """Explicit assumptions for one cash-and-excess-share CB issue."""

    model_config = _CONFIG
    valuation_as_of: date
    terminal_as_of: date
    face_value_yen_per_bond: Decimal
    bond_count: Annotated[int, Field(gt=0)]
    settlement_group_bond_counts: tuple[Annotated[int, Field(gt=0)], ...]
    share_unit: Annotated[int, Field(gt=0)]
    odd_lot_cash_price_basis: Literal["settlement_vwap"]
    conversion_price_yen: Decimal
    existing_shares: Annotated[int, Field(gt=0)]
    equity_value_yen_with_bond_liability: Decimal
    special_dividend_adjustments: tuple[SpecialDividendAdjustment, ...] = ()
    settlement_vwap_yen: Decimal | None = None
    settlement_vwap_window_start: date | None = None
    settlement_vwap_window_end: date | None = None
    settlement_notice_on: date | None = None
    settlement_on: date | None = None
    reference_close_sensitivity_yen: tuple[Decimal, ...] = ()

    @field_validator(
        "valuation_as_of",
        "terminal_as_of",
        "settlement_vwap_window_start",
        "settlement_vwap_window_end",
        "settlement_notice_on",
        "settlement_on",
        mode="before",
    )
    @classmethod
    def _dates(cls, value: object) -> date | None:
        return None if value is None else _date(value)

    @field_validator("special_dividend_adjustments", "settlement_group_bond_counts", mode="before")
    @classmethod
    def _adjustments(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator(
        "face_value_yen_per_bond",
        "conversion_price_yen",
        "equity_value_yen_with_bond_liability",
        "settlement_vwap_yen",
        mode="before",
    )
    @classmethod
    def _decimal(cls, value: object) -> Decimal | None:
        return None if value is None else finite_decimal(value)

    @field_validator("reference_close_sensitivity_yen", mode="before")
    @classmethod
    def _sensitivity(cls, value: object) -> tuple[Decimal, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("reference close sensitivity must be a sequence")
        return tuple(finite_decimal(item) for item in value)

    @model_validator(mode="after")
    def _bounds(self) -> CashShareCBInput:
        if self.face_value_yen_per_bond <= 0 or self.conversion_price_yen <= 0:
            raise ValueError("face value and conversion price must be positive")
        if self.conversion_price_yen != self.conversion_price_yen.quantize(_TENTH_YEN):
            raise ValueError("conversion price must use 0.1-yen precision")
        if (
            not self.settlement_group_bond_counts
            or sum(self.settlement_group_bond_counts) != self.bond_count
        ):
            raise ValueError("settlement groups must contain every bond exactly once")
        if self.equity_value_yen_with_bond_liability < 0:
            raise ValueError("equity value must be nonnegative")
        if self.terminal_as_of < self.valuation_as_of:
            raise ValueError("terminal date precedes valuation date")
        if self.settlement_vwap_yen is not None and self.settlement_vwap_yen <= 0:
            raise ValueError("settlement VWAP must be positive")
        if (self.settlement_vwap_window_start is None) != (self.settlement_vwap_window_end is None):
            raise ValueError("settlement VWAP window requires both dates")
        if (
            self.settlement_vwap_window_start is not None
            and self.settlement_vwap_window_end is not None
            and self.settlement_vwap_window_start > self.settlement_vwap_window_end
        ):
            raise ValueError("settlement VWAP window dates are reversed")
        if (
            self.settlement_vwap_window_end
            and self.settlement_notice_on
            and self.settlement_vwap_window_end >= self.settlement_notice_on
        ):
            raise ValueError("settlement VWAP window must precede notice")
        if (
            self.settlement_notice_on
            and self.settlement_on
            and self.settlement_notice_on > self.settlement_on
        ):
            raise ValueError("settlement notice follows settlement")
        if self.settlement_on and self.settlement_on < self.valuation_as_of:
            raise ValueError("settlement date precedes valuation date")
        if any(price <= 0 for price in self.reference_close_sensitivity_yen):
            raise ValueError("reference close sensitivity prices must be positive")
        if self.settlement_on and any(
            event.effective_on and event.effective_on > self.settlement_on
            for event in self.special_dividend_adjustments
        ):
            raise ValueError("dividend adjustment takes effect after assumed settlement")
        return self


def _adjusted_price(
    initial: Decimal,
    adjustments: tuple[SpecialDividendAdjustment, ...],
    *,
    reference_override: Decimal | None = None,
) -> tuple[Decimal, list[dict[str, str | None]]]:
    price = initial
    deferred_difference = Decimal(0)
    events: list[dict[str, str | None]] = []
    for item in adjustments:
        reference = reference_override or item.reference_close_yen
        if item.special_dividend_per_share_yen >= reference:
            raise ValueError("special dividend must be below each reference close")
        basis = price - deferred_difference
        proposed = (basis * (reference - item.special_dividend_per_share_yen) / reference).quantize(
            _TENTH_YEN, rounding=ROUND_DOWN
        )
        if proposed <= 0:
            raise ValueError("dividend adjustment leaves no positive conversion price")
        if price - proposed < 1:
            deferred_difference = price - proposed
        else:
            price = proposed
            deferred_difference = Decimal(0)
        events.append(
            {
                "special_dividend_per_share_yen": str(item.special_dividend_per_share_yen),
                "reference_close_yen": str(reference),
                "conversion_price_yen": str(price),
                "deferred_difference_yen": str(deferred_difference),
                "final_record_date": item.final_record_date.isoformat()
                if item.final_record_date
                else None,
                "effective_on": item.effective_on.isoformat() if item.effective_on else None,
                "reference_window_start": item.reference_window_start.isoformat()
                if item.reference_window_start
                else None,
                "reference_window_end": item.reference_window_end.isoformat()
                if item.reference_window_end
                else None,
            }
        )
    return price, events


def _shares_for_group(face: Decimal, conversion: Decimal, vwap: Decimal) -> int:
    if vwap <= conversion:
        return 0
    excess = (face * vwap / conversion - face) / vwap
    return int(excess.to_integral_value(rounding=ROUND_FLOOR))


def _settlement_shares(
    face: Decimal,
    conversion: Decimal,
    vwap: Decimal,
    groups: tuple[int, ...],
    share_unit: int,
) -> tuple[int, int, int]:
    per_group = [_shares_for_group(face * group, conversion, vwap) for group in groups]
    gross = sum(per_group)
    delivered = sum(shares // share_unit * share_unit for shares in per_group)
    return gross, delivered, gross - delivered


def _fixed_point(
    *,
    equity: Decimal,
    existing: int,
    face: Decimal,
    groups: tuple[int, ...],
    share_unit: int,
    conversion: Decimal,
) -> dict[str, object]:
    total_face = face * sum(groups)
    undiluted = equity / existing
    continuous = (
        undiluted
        if undiluted <= conversion
        else (equity + total_face) / (existing + total_face / conversion)
    )
    price = continuous
    seen: set[int] = set()
    rounded_price: Decimal | None = None
    rounding_gap: tuple[Decimal, Decimal] | None = None
    shares = 0
    for _ in range(100):
        shares = _settlement_shares(face, conversion, price, groups, share_unit)[0] if price else 0
        new_price = equity / (existing + shares)
        if new_price == price:
            rounded_price = price
            break
        if shares in seen:
            rounding_gap = (min(price, new_price), max(price, new_price))
            break  # a share-rounding discontinuity can have no exact fixed point
        seen.add(shares)
        price = new_price
    gross, delivered, cash_settled = (
        _settlement_shares(face, conversion, continuous, groups, share_unit)
        if continuous
        else (0, 0, 0)
    )
    return {
        "continuous_price_yen": str(continuous),
        "rounded_fixed_point_price_yen": str(rounded_price) if rounded_price is not None else None,
        "rounding_gap_yen": [str(value) for value in rounding_gap] if rounding_gap else None,
        "gross_excess_shares_at_continuous_price": gross,
        "delivered_shares_at_continuous_price": delivered,
        "odd_lot_shares_cash_settled_at_continuous_price": cash_settled,
        "gross_excess_shares_at_rounded_fixed_point": shares if rounded_price is not None else None,
        "assumption": "settlement_vwap_equals_terminal_price",
    }


def calculate_cash_share_cb(inputs: CashShareCBInput) -> dict[str, object]:
    """Return exact-decimal scenario outputs; no data access or probability model."""
    with localcontext() as context:
        context.prec = 50
        conversion, events = _adjusted_price(
            inputs.conversion_price_yen, inputs.special_dividend_adjustments
        )
        face = inputs.face_value_yen_per_bond
        count = inputs.bond_count
        equity = inputs.equity_value_yen_with_bond_liability
        existing = inputs.existing_shares
        undiluted = equity / existing
        vwap = inputs.settlement_vwap_yen
        groups = inputs.settlement_group_bond_counts
        unit = inputs.share_unit
        separate_vwap: dict[str, object] | None = None
        if vwap is not None:
            gross, delivered, cash_settled = _settlement_shares(
                face, conversion, vwap, groups, unit
            )
            odd_lot_cash = cash_settled * vwap
            if odd_lot_cash > equity:
                raise ValueError("assumed odd-lot cash exceeds equity value")
            separate_vwap = {
                "settlement_vwap_yen": str(vwap),
                "gross_excess_shares": gross,
                "delivered_shares": delivered,
                "odd_lot_shares_cash_settled": cash_settled,
                "odd_lot_cash_yen": str(odd_lot_cash),
                "terminal_price_yen": str((equity - odd_lot_cash) / (existing + delivered)),
            }
        sensitivity = []
        for reference in inputs.reference_close_sensitivity_yen:
            adjusted, _ = _adjusted_price(
                inputs.conversion_price_yen,
                inputs.special_dividend_adjustments,
                reference_override=reference,
            )
            sensitivity.append(
                {
                    "reference_close_yen": str(reference),
                    "conversion_price_yen": str(adjusted),
                    "fixed_point": _fixed_point(
                        equity=equity,
                        existing=existing,
                        face=face,
                        groups=groups,
                        share_unit=unit,
                        conversion=adjusted,
                    ),
                }
            )
        return {
            "basis": "conditional_pretax_cash_face_plus_excess_shares",
            "valuation_as_of": inputs.valuation_as_of.isoformat(),
            "terminal_as_of": inputs.terminal_as_of.isoformat(),
            "settlement_on": inputs.settlement_on.isoformat() if inputs.settlement_on else None,
            "settlement_notice_on": inputs.settlement_notice_on.isoformat()
            if inputs.settlement_notice_on
            else None,
            "settlement_vwap_window_start": inputs.settlement_vwap_window_start.isoformat()
            if inputs.settlement_vwap_window_start
            else None,
            "settlement_vwap_window_end": inputs.settlement_vwap_window_end.isoformat()
            if inputs.settlement_vwap_window_end
            else None,
            "face_cash_yen": str(face * count),
            "face_cash_treatment": "extinguishes_recognized_bond_liability_no_second_deduction",
            "settlement_group_bond_counts": list(groups),
            "share_unit": unit,
            "odd_lot_cash_price_assumption": inputs.odd_lot_cash_price_basis,
            "conversion_price_yen": str(conversion),
            "dividend_adjustments": events,
            "unconverted": {"gross_excess_shares": 0, "terminal_price_yen": str(undiluted)},
            "separate_settlement_vwap": separate_vwap,
            "fixed_point": _fixed_point(
                equity=equity,
                existing=existing,
                face=face,
                groups=groups,
                share_unit=unit,
                conversion=conversion,
            ),
            "reference_close_sensitivity": sensitivity,
        }
