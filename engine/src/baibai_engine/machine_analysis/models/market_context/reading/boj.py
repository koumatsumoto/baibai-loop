"""Interpret retained BOJ workbook cells at the L2 boundary."""

from collections.abc import Mapping, Sequence

from baibai_engine.machine_analysis.models.market_context.indicators.definitions import SeriesDefinition


def boj_value(series: SeriesDefinition, record: Mapping[str, object]) -> float | None:
    value_col, expected_header, metadata_col, expected_metadata = _value_column_spec(
        series.reading_key
    )
    preamble = record["preamble"]
    cells = record["row"]
    if not isinstance(preamble, list) or not isinstance(cells, list):
        raise ValueError("BOJ native record must contain workbook rows")
    headers = _column_text(preamble, value_col)
    if _normalize_header(expected_header) not in headers:
        raise ValueError(
            f"BOJ value column {value_col} header mismatch: expected {expected_header!r}"
        )
    if _normalize_header(expected_metadata) not in _column_text(preamble, metadata_col):
        raise ValueError(
            f"BOJ metadata column {metadata_col} mismatch: expected {expected_metadata!r}"
        )
    value = cells[value_col - 1] if value_col <= len(cells) else None
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("BOJ selected workbook cell must be numeric or null")
    return float(value)


def _column_text(rows: Sequence[object], column: int) -> set[str]:
    values = set()
    for row in rows:
        if not isinstance(row, list):
            raise ValueError("BOJ preamble must contain workbook rows")
        value = row[column - 1] if column <= len(row) else None
        if isinstance(value, str) and value.strip():
            values.add(_normalize_header(value))
    return values


def _value_column_spec(reading_key: str) -> tuple[int, str, int, str]:
    parts = reading_key.split("|")
    if len(parts) != 4 or not parts[1].strip() or not parts[3].strip():
        raise ValueError(
            "BOJ reading_key must be "
            "'<value column>|<expected header>|<metadata column>|<expected metadata>': "
            f"{reading_key!r}"
        )
    raw_index, expected_header, raw_metadata_index, expected_metadata = parts
    try:
        index = int(raw_index)
    except ValueError:
        raise ValueError(
            f"BOJ reading_key must start with a 1-based column index: {reading_key!r}"
        ) from None
    if index < 2:
        raise ValueError(f"BOJ value column index must be a 1-based value column (>= 2): {index}")
    try:
        metadata_index = int(raw_metadata_index)
    except ValueError:
        raise ValueError(
            f"BOJ reading_key metadata column must be a 1-based column index: {reading_key!r}"
        ) from None
    if metadata_index < 1:
        raise ValueError(f"BOJ metadata column index must be 1-based (>= 1): {metadata_index}")
    return (
        index,
        expected_header.strip(),
        metadata_index,
        expected_metadata.strip(),
    )


def _normalize_header(value: str) -> str:
    return " ".join(value.split()).casefold()
