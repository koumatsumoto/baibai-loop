"""簿価比較を黒字銘柄・閾値通過銘柄の比較にすり替えない。"""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.engine.investment_judgment.workflow.test_discovery_questions import observation
from tests.helpers.model_generation import generation, publish_generation

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.workflow.book_prices import BookPriceComparison, _observed
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoveryPolicy,
    DiscoverySelection,
    discovery_publication_id,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore

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
            price_date="2026-10-22" if t == "1005" else _DAY,
            expected_price_date=_DAY,
            price_ready=t != "1005",
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
    original = owner.stream_generation_subjects

    def limited(generation_id, **kwargs):
        calls.append((generation_id, kwargs))
        return original(generation_id, **kwargs)

    monkeypatch.setattr(owner, "stream_generation_subjects", limited)
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
    assert calls[0][1]["path"] == runs
    assert "1005" not in calls[0][1]["subjects"]
    meta = result["reported_book_comparison"]
    assert [meta[k] for k in ("within_question", "at_or_below", "above", "unavailable")] == [
        6,
        2,
        2,
        2,
    ]
    assert [row["ticker"] for row in result["rows"]] == ["1001", "1002"]
    first, second = [r["book_price_comparison"] for r in result["rows"]]
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
