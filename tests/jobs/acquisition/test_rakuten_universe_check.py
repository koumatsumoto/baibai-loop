from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx2 import MockTransport, Request, Response
from tests.jobs.integration.test_us_daily_handoff import _SATURDAY_SLOT, _whole
from tests.jobs.integration.test_us_daily_prices import _CALENDAR_ROWS, _payload
from tests.jobs.integration.test_us_listing_refresh import _NASDAQ, _NYSE

from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    read_local_listing_master,
)
from baibai_jobs.acquisition import rakuten_universe_check
from baibai_jobs.cli import main
from baibai_jobs.integration import source_handoff, us_daily_handoff

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


def _assemble_args(
    root: Path, *, output: Path, otherlisted_received_at: str = "2026-11-27T13:49:00Z"
) -> list[str]:
    directories = root / "verified-nasdaq"
    directories.mkdir(exist_ok=True)
    (directories / "nasdaqlisted.txt").write_bytes(_NASDAQ)
    (directories / "otherlisted.txt").write_bytes(_NYSE)
    return [
        *_args(
            root,
            catalog="ticker,exchange\nGOOG,NASDAQ\nBRK B,NYSE\nQQEX,Cboe\n",
            restrictions="ticker,exchange,restriction\nGOOG,NASDAQ,buy_suspended\n",
            counts=("NASDAQ=1", "NYSE=1", "Cboe=1"),
            catalog_count=3,
            restrictions_count=1,
        ),
        "--listing-directory",
        str(directories),
        "--nasdaqlisted-received-at",
        "2026-11-27T13:48:00Z",
        "--otherlisted-received-at",
        otherlisted_received_at,
        "--output-master",
        str(output),
    ]


def test_operator_assembles_a_master_the_raw_sip_entrypoint_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 11, 27, 19, tzinfo=UTC)
    monkeypatch.setattr(rakuten_universe_check, "_now", lambda: now)
    output = tmp_path / "current.json"
    args = _assemble_args(tmp_path, output=output)
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["listing_sip_targets"] == 1
    assert report["unsupported_catalog_targets"] == 1
    assert not output.stat().st_mode & 0o077
    before = output.read_bytes()
    current = read_local_listing_master(output)["US"]
    assert current.rakuten is not None
    assert {row.listing_key: row.state for row in current.rakuten.rows} == {
        ("XNAS", "GOOG"): "buy_suspended",
        ("XNYS", "BRK.B"): "catalog_target",
    }

    # Reuse an existing pin without changing its identity or copying raw data to a store.
    next_output = tmp_path / "repeat.json"
    with_previous = [*args[:-1], str(next_output), "--previous-master", str(output)]
    assert main(with_previous) == 0
    assert next_output.read_bytes() == before
    capsys.readouterr()
    assert main(args) == 2
    assert "FileExistsError" in capsys.readouterr().err
    assert output.read_bytes() == before

    for env in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.setenv(env, "test-only")
    monkeypatch.setattr(us_daily_handoff, "_now", lambda: _SATURDAY_SLOT)
    calls: list[Request] = []

    def get(request: Request) -> Response:
        calls.append(request)
        url = urlsplit(str(request.url))
        params = parse_qs(url.query)
        if url.path == "/v2/calendar":
            assert params["end"] == ["2026-11-28"]
            return _whole(request, _CALENDAR_ROWS[:1])
        assert url.path == "/v2/stocks/bars"
        assert params["symbols"] == ["BRK.B"]
        assert params["start"] == params["end"] == ["2026-11-27"]
        assert (params["feed"], params["adjustment"]) == (["sip"], ["raw"])
        return _whole(request, _payload(("BRK.B",)))

    monkeypatch.setattr(us_daily_handoff, "HTTPTransport", lambda **_: MockTransport(get))
    assert (
        source_handoff.main(
            [
                "acquire",
                "--source",
                "alpaca-us-daily",
                "--asof",
                "2026-11-27",
                "--current-listing-master",
                str(output),
                "--directory",
                str(tmp_path / "sip"),
            ]
        )
        == 0
    )
    assert len(calls) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["expected"] == result["received"] == 1


@pytest.mark.parametrize(
    ("make_invalid", "reason"),
    [
        ("missing_native", "FileNotFoundError"),
        ("other_native_day", "native creation date"),
        ("late_clock", "after the current refresh"),
        ("past_ny_session", "not current"),
        ("unverified_nyse_count", "independently reported venue totals"),
        ("no_positive_join", "no SIP-eligible Rakuten catalog targets"),
        ("only_flagged_fund", "no SIP-eligible Rakuten catalog targets"),
    ],
)
def test_mismatched_inputs_create_no_private_master(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    make_invalid: str,
    reason: str,
) -> None:
    output = tmp_path / "never-created.json"
    now = datetime(2026, 11, 27, 19, tzinfo=UTC)
    if make_invalid == "past_ny_session":
        now = _SATURDAY_SLOT
    monkeypatch.setattr(rakuten_universe_check, "_now", lambda: now)
    kwargs = (
        {"otherlisted_received_at": "2026-11-27T21:00:00Z"} if make_invalid == "late_clock" else {}
    )
    args = _assemble_args(tmp_path, output=output, **kwargs)
    other = tmp_path / "verified-nasdaq" / "otherlisted.txt"
    if make_invalid == "missing_native":
        other.unlink()
    elif make_invalid == "other_native_day":
        other.write_bytes(_NYSE.replace(b"11272026", b"11252026"))
    elif make_invalid == "unverified_nyse_count":
        index = args.index("NYSE=1")
        del args[index - 1 : index + 1]
    elif make_invalid == "no_positive_join":
        catalog = tmp_path / "catalog.csv"
        catalog.write_text(
            catalog.read_text().replace("BRK B,NYSE", "OTHER,NYSE"), encoding="utf-8"
        )
    elif make_invalid == "only_flagged_fund":
        other.write_bytes(_NYSE.replace(b"|BRK.B|N|100|", b"|BRK.B|Y|100|"))
    assert main(args) == 2
    assert reason in capsys.readouterr().err
    assert not output.exists()
