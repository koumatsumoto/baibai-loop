from pathlib import Path

from tests.engine.test_candidate_discovery import _analysis
from tests.engine.test_screening_run_store import _publish, _run
from tests.web.test_screening_judgment_binding import _dependencies

from baibai_engine.read_api import candidate_rows
from baibai_web.readmodel.stocks import build_screening, build_security_detail
from baibai_web.sources.db_sources import DbScreeningSource


def test_zero_candidate_latest_run_never_falls_back(tmp_path: Path, mocker):
    path = tmp_path / "runs.sqlite"
    assert DbScreeningSource(path).latest_run() is None
    _publish(path, _run(rows=[_analysis("2331")], asof="2026-10-13"), revision="old")
    row = _analysis("2331")
    row["market_cap_oku"] = None
    row["metrics"].update(market_price_yen=1000.0, fv_sector_median_yen=1700.0)
    _publish(path, _run(rows=[row]), revision="new")
    source = DbScreeningSource(path)
    run = source.latest_run()
    assert run.run_revision_id == "new"
    assert candidate_rows(run.rows) == ()
    ledger, research = _dependencies(mocker)
    research.proposals.return_value = []
    market = mocker.Mock()
    market.security_market_context.return_value = None
    listing = build_screening(source, ledger, research)
    detail = build_security_detail("2331", ledger, research, source, market)
    assert listing.run.run_revision_id == detail.screening_run.run_revision_id == "new"
    assert listing.candidate_discovery.candidate_count == 0
    assert listing.stock_analyses[0].fair_value_anchor_yen == 1700.0
    assert detail.stock_analysis.fair_value_anchor_yen == 1700.0


def test_canonical_candidate_order_is_ticker_and_keeps_all_rows(tmp_path: Path):
    path = tmp_path / "runs.sqlite"
    _publish(
        path, _run(rows=[_analysis("9999"), _analysis("1111"), _analysis("5555")]), revision="fixed"
    )
    run = DbScreeningSource(path).run("fixed")
    assert [r["ticker"] for r in run.rows] == ["9999", "1111", "5555"]
    rows = candidate_rows(run.rows)
    assert [r["ticker"] for r in rows] == ["1111", "5555", "9999"]
    assert all(any(r is saved for saved in run.rows) for r in rows)
