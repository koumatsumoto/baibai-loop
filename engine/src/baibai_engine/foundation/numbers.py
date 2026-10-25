"""Finite decimal conversion shared by judgments and confirmed price facts."""

from decimal import Decimal, InvalidOperation


def finite_decimal(value: object) -> Decimal:
    """Reject booleans and nonfinite numbers before arithmetic or comparison."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("must be a finite decimal number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("must be a finite decimal number") from error
    if not number.is_finite():
        raise ValueError("must be a finite decimal number")
    return number
