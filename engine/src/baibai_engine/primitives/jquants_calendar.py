"""Meaning of J-Quants HolDiv for cash equities, not session timing or coverage."""

from typing import Final

CASH_EQUITY_OPEN_DIVISIONS: Final = ("1", "2")
_CASH_EQUITY_CLOSED_DIVISIONS: Final = ("0", "3")
CASH_EQUITY_KNOWN_DIVISIONS: Final = (*CASH_EQUITY_OPEN_DIVISIONS, *_CASH_EQUITY_CLOSED_DIVISIONS)


def cash_equity_open(holiday_division: object) -> bool | None:
    """Half sessions are open; futures-only sessions are closed for cash equities."""
    if holiday_division in CASH_EQUITY_OPEN_DIVISIONS:
        return True
    if holiday_division in _CASH_EQUITY_CLOSED_DIVISIONS:
        return False
    return None
