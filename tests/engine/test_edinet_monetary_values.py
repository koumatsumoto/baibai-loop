"""Currency and missingness are part of a financial fact, not display metadata."""

import zipfile
from io import BytesIO

import pytest

from baibai_engine.market.providers.edinet_csv import parse_csv_zip_metric_record


def _record(rows):
    header = "要素ID\tコンテキストID\t連結・個別\tユニットID\t単位\t値\n"
    text = header + "\n".join(
        f"jppfs_cor:CashAndDeposits\t{context}\t連結\t{unit}\t円\t{value}"
        for context, unit, value in rows
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/facts.csv", text.encode("utf-16-le"))
    return parse_csv_zip_metric_record(
        ticker="0000", doc_id="S100TEST", doc_type_code="120", content=buffer.getvalue()
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_unit_id_not_display_label_selects_yen(reverse):
    rows = [("CurrentYearInstant", "USD", "900"), ("CurrentYearInstant", "JPY", "100")]
    record = _record(list(reversed(rows)) if reverse else rows)
    assert record.cash == 100


@pytest.mark.parametrize("unit", ["USD", "JPYPerShares", "-", ""])
def test_non_yen_current_fact_does_not_restore_prior_yen(unit):
    record = _record(
        [
            ("Prior1YearInstant", "JPY", "900"),
            ("CurrentYearInstant", unit, "100"),
        ]
    )
    assert record.cash is None
    assert "metric_unavailable:cash" in record.failure_reasons


@pytest.mark.parametrize("value", ["", "－", "―", "–"])
def test_missing_or_lookalike_bars_are_not_zero_or_prior_value(value):
    record = _record(
        [
            ("Prior1YearInstant", "JPY", "900"),
            ("CurrentYearInstant", "JPY", value),
        ]
    )
    assert record.cash is None


@pytest.mark.parametrize("value", ["0", "-"])
def test_explicit_csv_zero_remains_zero(value):
    assert _record([("CurrentYearInstant", "JPY", value)]).cash == 0


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["100", "100"], 100),
        (["100", "200"], None),
        (["200", "100"], None),
        (["100", ""], None),
        (["", "100"], None),
    ],
)
def test_conflicting_or_missing_duplicate_facts_are_not_first_row_wins(values, expected):
    assert _record([("CurrentYearInstant", "JPY", value) for value in values]).cash == expected
