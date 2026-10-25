from datetime import UTC, date, datetime

from tests.helpers.l1_release import market_store

from baibai_engine.machine_analysis import generation_reader
from baibai_engine.machine_analysis.company_growth.models import InputRelease
from baibai_engine.machine_analysis.company_valuation.metrics.models import TTMRules
from baibai_engine.storage.lake.writer import sealed_sqlite_snapshot


def test_generation_reads_shared_snapshots_and_uses_new_release_not_old_origin(tmp_path, mocker):
    path = market_store(tmp_path / "market.sqlite", daily_bars=(("1234", "2026-01-05", 10.0, 1.0),))
    spies = [
        mocker.spy(generation_reader, name)
        for name in ("read_price_bars", "read_financial_documents", "read_observation_population")
    ]
    mocker.patch(
        "baibai_engine.machine_analysis.market_context.security.read_price_bars",
        side_effect=AssertionError("must reuse shared prices"),
    )
    mocker.patch(
        "baibai_engine.machine_analysis.market_context.snapshot.read_price_bars",
        side_effect=AssertionError("must reuse shared prices"),
    )
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=tmp_path / "lake") as snapshot:
        result = generation_reader.read_and_generate_models(
            generation_id="generated-from-new-release",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="new-release", manifest_sha256="a" * 64),
            economic_path=None,
            economic_identity=None,
            as_of=date(2026, 1, 5),
            evaluation_time=datetime(2026, 1, 6, tzinfo=UTC),
            ttm_rules=TTMRules(
                full_year_min_days=330,
                full_year_max_days=370,
                period_length_tolerance_ratio=0.1,
                period_end_tolerance_days=45,
            ),
            tickers=("1234", "5678"),
        )
    assert len(result.models) == 11
    assert result.inputs["market"].reference == "new-release"
    assert (
        result.result("company_growth", "1234").payload["input_release"]["release_id"]
        == "new-release"
    )
    assert result.result("market_context", "market").payload["reading"] is None
    assert [spy.call_count for spy in spies] == [1, 1, 1]
