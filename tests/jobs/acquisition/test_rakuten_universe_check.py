from __future__ import annotations

import json
from pathlib import Path

import pytest

from baibai_jobs.cli import main

_CATALOG = "ticker,exchange\nBRK B,NYSE\nKO,NYSE\nNVDA,NASDAQ\nHYP,NYSE American\n"
_RESTRICTIONS = (
    "ticker,exchange,restriction\nBRK.B,NYSE,buy_suspended\nBW,NYSE,sell_only\nSYNQ,OTC,sell_only\n"
)


def _args(
    root: Path,
    *,
    catalog: str = _CATALOG,
    restrictions: str = _RESTRICTIONS,
    counts: tuple[str, ...] = ("NASDAQ=1", "NYSE=2", "NYSE American=1", "NYSE Arca=0"),
    catalog_count: int = 4,
    restrictions_count: int = 3,
    restrictions_at: str = "2026-11-27T16:10:00Z",
) -> list[str]:
    catalog_file = root / "catalog.csv"
    restrictions_file = root / "restrictions.csv"
    catalog_file.write_text(catalog, encoding="utf-8")
    restrictions_file.write_text(restrictions, encoding="utf-8")
    argv = [
        "rakuten-universe-check",
        "--session",
        "2026-11-27",
        "--catalog",
        str(catalog_file),
        "--catalog-count",
        str(catalog_count),
        "--catalog-received-at",
        "2026-11-27T11:00:00-05:00",
        "--restrictions",
        str(restrictions_file),
        "--restrictions-count",
        str(restrictions_count),
        "--restrictions-received-at",
        restrictions_at,
    ]
    for count in counts:
        argv.extend(["--venue-count", count])
    return argv


def test_reports_missing_and_known_empty_separately_without_identifiers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_args(tmp_path)) == 0
    output = capsys.readouterr()
    assert not output.err
    assert all(ticker not in output.out for ticker in ("BRK B", "BRK.B", "NVDA", "SYNQ"))
    result = json.loads(output.out)
    assert result["status"] == "supported_intake_validated"
    assert result["effective_session"] == "2026-11-27"
    assert result["catalog_received_at"] == "2026-11-27T16:00:00+00:00"
    assert result["restrictions_received_at"] == "2026-11-27T16:10:00+00:00"
    assert result["catalog_count"] == 4
    assert result["restrictions_count"] == 3
    assert result["missing_requested_venue_counts"] == ["Cboe"]
    assert result["unsupported_catalog_targets"] == 1
    venues = result["venues"]
    assert venues["NYSE"] == {
        "requested": True,
        "listing_supported": True,
        "catalog_count": 2,
        "catalog_targets": 1,
        "sell_only": 1,
        "buy_suspended": 1,
        "restriction_only": 1,
    }
    assert venues["Cboe"]["catalog_targets"] is None
    assert venues["NYSE Arca"]["catalog_targets"] == 0
    assert not venues["NYSE American"]["listing_supported"]
    assert not venues["OTC"]["requested"]
    assert venues["OTC"]["catalog_count"] is None
    assert venues["OTC"]["restriction_only"] == 1
    assert {p.name for p in tmp_path.iterdir()} == {"catalog.csv", "restrictions.csv"}


def test_unverified_supported_venue_cannot_be_replaced_by_a_restriction_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _args(
        tmp_path,
        catalog="ticker,exchange\nNVDA,NASDAQ\n",
        catalog_count=1,
        counts=("NASDAQ=1",),
    )
    assert main(argv) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "incomplete_supported_intake"
    assert "NYSE" in result["missing_requested_venue_counts"]
    assert result["venues"]["NYSE"]["restriction_only"] == 2
    assert result["venues"]["NYSE"]["catalog_targets"] is None


def test_confirmed_zero_restrictions_and_zero_ordinary_in_two_unlisted_venues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _args(
        tmp_path,
        restrictions="ticker,exchange,restriction\n",
        restrictions_count=0,
        counts=("NASDAQ=1", "NYSE=2", "NYSE American=1", "NYSE Arca=0", "Cboe=0"),
    )
    assert main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["missing_requested_venue_counts"] == []
    assert result["unsupported_catalog_targets"] == 1
    assert result["restrictions_count"] == 0
    assert result["venues"]["NYSE"]["catalog_targets"] == 2


def test_no_valid_supported_targets_even_with_all_venue_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _args(
        tmp_path,
        catalog="ticker,exchange\nBW,NYSE\nHYP,NYSE American\n",
        catalog_count=2,
        counts=("NASDAQ=0", "NYSE=1", "NYSE American=1", "NYSE Arca=0", "Cboe=0"),
    )
    assert main(argv) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "no_supported_catalog_targets"
    assert result["missing_requested_venue_counts"] == []
    assert result["unsupported_catalog_targets"] == 1


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"catalog_count": 2900}, "incomplete Rakuten snapshot CSV"),
        ({"counts": ("NASDAQ=0", "NYSE=3", "NYSE American=1")}, "venue totals"),
        ({"counts": ("NASDAQ=1", "NYSE=2")}, "venue totals"),
        ({"counts": ("NASDAQ=1", "NASDAQ=1", "NYSE=2", "NYSE American=1")}, "repeated"),
        (
            {
                "catalog": "ticker,exchange\nBRK B,NYSE\nBRK.B,NYSE\nNVDA,NASDAQ\nHYP,NYSE American\n"
            },
            "duplicate",
        ),
        ({"restrictions_count": 4}, "incomplete"),
        ({"restrictions_at": "2026-11-28T05:01:00Z"}, "outside its target NY session"),
        ({"restrictions_at": "2026-11-27T12:13:00"}, "must include a UTC offset"),
    ],
)
def test_rejects_invalid_or_truncated_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], changes: dict, fragment: str
) -> None:
    assert main(_args(tmp_path, **changes)) == 2
    result = capsys.readouterr()
    assert not result.out
    assert fragment in result.err


def test_missing_restriction_file_is_not_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _args(tmp_path)
    (tmp_path / "restrictions.csv").unlink()
    assert main(argv) == 2
    result = capsys.readouterr()
    assert not result.out
    assert "cannot read the normalized UTF-8" in result.err
