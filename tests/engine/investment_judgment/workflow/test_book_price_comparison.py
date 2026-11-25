"""簿価比較を黒字銘柄・閾値通過銘柄の比較にすり替えない。"""

import gzip
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from shutil import copyfile

import pytest
from tests.engine.investment_judgment.workflow.test_discovery_questions import observation
from tests.helpers.model_generation import generation, publish_generation

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.workflow.book_prices import BookPriceComparison, _observed
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.screening import discovery_index
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoveryPolicy,
    DiscoverySelection,
    discovery_publication_id,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json

_DAY = "2026-10-23"


def _payload(pbr, *, measured="2026-06-30", earnings=-0.02):
    cap = 120
    return {
        "expected_price_date": _DAY,
        "financial": {
            "pbr": pbr,
            "market_cap": cap,
            "book_equity": {
                "amount_yen": cap / pbr,
                "measurement_date": measured,
                "disclosed_at": "2026-09-25",
                "method": "assets_times_equity_ratio",
                "statement_basis": "consolidated",
                "accounting_standard": "JP",
            },
        },
        "earnings_price": {
            "price_date": _DAY,
            "price_yen": 12,
            "shares_ex_treasury": 10,
            "market_cap_yen": cap,
            "earnings_yield": earnings,
        },
    }


@pytest.mark.parametrize(
    ("key", "change"),
    [
        ("financial", {"pbr": True}),
        ("financial", {"pbr": 0.0}),
        ("financial", {"pbr": -0.5}),
        ("financial", {"pbr": float("inf")}),
        ("financial", {"market_cap": 121}),
        ("earnings_price", {"price_date": "2026-10-21"}),
        ("earnings_price", {"shares_ex_treasury": None}),
        ("earnings_price", {"price_yen": 6}),
        ("book_equity", {"amount_yen": -120}),
        ("book_equity", {"measurement_date": "2027-02-01"}),
        ("book_equity", {"disclosed_at": "2026-10-26"}),
    ],
)
def test_unavailable_multiple_is_not_a_cheap_stock(key, change):
    f = _payload(0.5)
    (f["financial"][key] if key == "book_equity" else f[key]).update(change)
    assert _observed(f, _DAY, date.fromisoformat(_DAY)) is None


def test_unknown_book_standard_or_sector_does_not_form_a_peer_group():
    source = _payload(0.5)
    del source["financial"]["book_equity"]["accounting_standard"]
    observed = _observed(source, _DAY, date.fromisoformat(_DAY))
    assert observed is not None
    comparison = BookPriceComparison(
        1, {"1001": None, "1002": None}, {"1001": observed, "1002": observed}
    )
    result = comparison.detail("1001")
    assert result["market"]["same_price_date"] == {"count": 1, "median_pbr": 0.5}
    assert result["sector"] is None
    assert result["market"]["same_book_basis"] is None


def test_assets_cli_uses_all_unfiltered_peers_and_same_book_period(tmp_path, capsys, monkeypatch):
    import baibai_engine.investment_judgment.workflow.book_prices as owner

    runs = tmp_path / "runs"
    gen = generation()
    cases = {
        "1001": ("s", _payload(0.5)),  # loss, no positive-income route
        "1002": ("s", _payload(1, measured="2026-03-31")),
        "1003": ("s", _payload(3, earnings=0.05)),
        "1004": ("t", _payload(6)),
        "1005": ("s", _payload(0.12)),  # stale price, not a cheap comparator
        "1006": ("s", None),  # financial model not recorded
    }
    gen = gen.model_copy(
        update={
            "models": tuple(
                GeneratedModel(
                    model_id="company_financials",
                    calculation_revision="test",
                    subject=t,
                    payload=fin,
                )
                for t, (_, fin) in cases.items()
                if fin is not None
            ),
        }
    )
    publish_generation(gen, path=runs)
    identity = discovery_publication_id(gen.generation_id)
    stocks = tuple(cases)
    rows = tuple(
        observation(
            t,
            sector=sec,
            price_date="2026-10-22" if t == "1005" else None if t == "1001" else _DAY,
            expected_price_date=None if t == "1001" else _DAY,
            price_ready=t not in {"1001", "1005"},
            book_equity={
                "amount_yen": 240 if fin is None else fin["financial"]["book_equity"]["amount_yen"]
            },
        )
        for t, (sec, fin) in cases.items()
    )
    ScreeningSelectionStore(runs).publish(
        identity,
        ScreeningSelection(
            gen.generation_id, stocks, DiscoverySelection(DiscoveryPolicy(), rows, {})
        ),
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    base = [
        "selection-context",
        "--scope",
        "discover_new",
        "--request",
        "資産",
        "--runs-path",
        str(runs),
        "--db",
        str(tmp_path / "app"),
        "--screening-selection-id",
        identity,
    ]
    before = runs.read_bytes()
    calls = []
    original = owner.stream_generation_subjects_from_connection

    def limited(connection, generation_id, **kwargs):
        assert connection.in_transaction
        assert connection.execute("PRAGMA database_list").fetchone()[2] == str(runs)
        calls.append((generation_id, kwargs))
        return original(connection, generation_id, **kwargs)

    monkeypatch.setattr(owner, "stream_generation_subjects_from_connection", limited)
    assert main([*base, "--discovery-question", "reported_assets"], now=now) == 0
    capsys.readouterr()
    assert calls == []
    assert (
        main([*base, "--discovery-question", "reported_assets", "--reported-pbr-max", "1"], now=now)
        == 0
    )
    result = json.loads(capsys.readouterr().out)["discovery"]
    assert len(calls) == 1
    assert calls[0][0] == gen.generation_id
    assert calls[0][1]["model_ids"] == ("company_financials",)
    assert set(calls[0][1]["subjects"]) == set(stocks)
    meta = result["reported_book_comparison"]
    assert "推計簿価" in result["question"]
    assert "分母は推計簿価" in meta["meaning"]
    assert [meta[k] for k in ("within_question", "at_or_below", "above", "unavailable")] == [
        6,
        2,
        2,
        2,
    ]
    assert [row["ticker"] for row in result["rows"]] == ["1001", "1002"]
    first, second = [r["book_price_comparison"] for r in result["rows"]]
    assert (first["reported_pbr"], first["book_method"]) == (0.5, "assets_times_equity_ratio")
    assert first["market"] == {
        "other_companies": 5,
        "same_price_date": {"count": 3, "median_pbr": 3},
        "same_book_basis": {"count": 2, "median_pbr": 4.5},
    }
    assert first["sector"] == {
        "other_companies": 4,
        "same_price_date": {"count": 2, "median_pbr": 2},
        "same_book_basis": {"count": 1, "median_pbr": 3},
    }
    assert second["sector"]["same_book_basis"] == {"count": 0, "median_pbr": None}
    assert result["rows"][0]["detail_args"] == ["--compare-ticker", "1001"]
    assert runs.read_bytes() == before
    assert not (tmp_path / "app").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--discovery-question", "reinvestment", "--reported-pbr-max", "1"],
        ["--discovery-question", "reported_assets", "--reported-pbr-max", "nan"],
        ["--discovery-question", "reported_assets", "--reported-pbr-max", "0"],
        [
            "--discovery-question",
            "reported_assets",
            "--reported-pbr-max",
            "1",
            "--price-coordinate-min",
            "a",
            "b",
            "c",
            "1",
        ],
    ],
)
def test_invalid_book_price_request_is_not_an_empty_result(tmp_path, capsys, args):
    assert (
        main(
            [
                "selection-context",
                "--scope",
                "discover_new",
                "--request",
                "資産",
                "--runs-path",
                str(tmp_path / "absent"),
                *args,
            ]
        )
        == 3
    )
    assert "reported PBR" in json.loads(capsys.readouterr().out)["reason"]


@pytest.mark.parametrize(
    "route",
    [
        (
            "--scope",
            "discover_new",
            "--discovery-question",
            "reported_assets",
            "--reported-pbr-max",
            "0.6",
        ),
        ("--scope", "specified_stocks", "--ticker", "1001"),
    ],
)
def test_discovery_response_uses_one_runs_copy_during_replacement(
    tmp_path, capsys, monkeypatch, route
):
    runs, replacement, app = (tmp_path / name for name in ("runs", "replacement", "app"))

    def financial(price):
        payload = _payload(price / 1000)
        cap = price * 10
        payload["financial"].update(market_cap=cap)
        payload["financial"]["book_equity"]["amount_yen"] = 10_000
        payload["earnings_price"].update(price_yen=price, market_cap_yen=cap)
        return payload

    original = generation().model_copy(
        update={
            "models": (
                GeneratedModel(
                    model_id="company_financials",
                    subject="1001",
                    calculation_revision="test",
                    payload=financial(900),
                ),
            ),
        },
    )
    publish_generation(original, path=runs)
    identity = discovery_publication_id(original.generation_id)
    row = observation(
        "1001",
        price=900,
        price_date=_DAY,
        expected_price_date=_DAY,
        basis="last_close_unadjusted",
        price_ready=True,
        book_equity={"amount_yen": 10_000},
    )
    ScreeningSelectionStore(runs).publish(
        identity,
        ScreeningSelection(
            original.generation_id, ("1001",), DiscoverySelection(DiscoveryPolicy(), (row,), {})
        ),
    )
    copyfile(runs, replacement)
    # 同ID異本文は通常のpublisherが拒む。異なる復元元として試験内だけで合成する。
    with closing(sqlite3.connect(replacement)) as db, db:
        (blob,) = db.execute(
            "SELECT payload_gzip FROM screening_selection WHERE selection_id=?", (identity,)
        ).fetchone()
        raw = json.loads(gzip.decompress(blob))
        raw["result"]["rows"][0]["price"] = 500
        db.execute(
            "UPDATE screening_selection SET payload_gzip=? WHERE selection_id=?",
            (gzip.compress(encode_json(raw).encode(), mtime=0), identity),
        )
        db.execute(
            "UPDATE generated_model SET payload_gzip=? WHERE generation_id=? AND subject=?",
            (
                gzip.compress(encode_json(financial(500)).encode(), mtime=0),
                original.generation_id,
                "1001",
            ),
        )
    now = datetime.now(UTC) + timedelta(seconds=1)

    def read():
        assert (
            main(
                [
                    "selection-context",
                    "--request",
                    "資産",
                    "--runs-path",
                    str(runs),
                    "--db",
                    str(app),
                    "--screening-selection-id",
                    identity,
                    *route,
                ],
                now=now,
            )
            == 0
        )
        return capsys.readouterr().out

    expected = read()
    source_validation = discovery_index.validate_selection_source

    def replace_after_index(*args, **kwargs):
        source_validation(*args, **kwargs)
        replacement.replace(runs)

    monkeypatch.setattr(discovery_index, "validate_selection_source", replace_after_index)
    observed = read()
    assert not replacement.exists()
    assert not app.exists()
    assert observed == expected
