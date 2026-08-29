from __future__ import annotations

from copy import deepcopy

import pytest

from baibai_engine.foundation.ranked_set import (
    RankedSetResolutionError,
    resolve_ranked_set_rows,
)


def _row(ticker: object = "2331") -> dict[str, object]:
    return {"ticker": ticker, "rank": 1, "er_annual": 0.12}


def test_ranked_set_has_no_duplicate_ticker_list() -> None:
    first = _row("2331")
    second = _row("0001")

    tickers, rows = resolve_ranked_set_rows({"ranked_set": [first, second]})

    assert tickers == ("2331", "0001")
    assert rows == {"2331": first, "0001": second}


def test_empty_ranked_set_fails_closed() -> None:
    with pytest.raises(RankedSetResolutionError, match="no ranked set"):
        resolve_ranked_set_rows({"ranked_set": []})


def test_duplicate_ranked_row_fails_closed() -> None:
    row = _row()
    with pytest.raises(RankedSetResolutionError, match="duplicate ticker 2331"):
        resolve_ranked_set_rows({"ranked_set": [row, deepcopy(row)]})


@pytest.mark.parametrize("ticker", [2331, None, "233", "2331.T"])
def test_ranked_set_rejects_noncanonical_ticker(ticker: object) -> None:
    with pytest.raises(RankedSetResolutionError, match="invalid row"):
        resolve_ranked_set_rows({"ranked_set": [_row(ticker)]})
