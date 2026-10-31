"""外部値の出所別selector、PIT、受領/予測の区別と副作用の境界。"""

import hashlib
import socket
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tests.engine.test_external_assessment_core import RECEIVED, rating, valuation

from baibai_engine.cli.external_assessment import main
from baibai_engine.machine_analysis.generation.runners.external_assessment import (
    external_assessment,
)
from baibai_engine.machine_analysis.models.external_assessment.analysis import FORECAST_METRICS
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    QuantsScoreObservation,
)
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observation_population,
    read_observations,
)
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.contract import (
    COLUMNS,
    TABLE,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def tv_row(path, fetched, *, status="ok", ticker="1234", **metrics):
    values = dict(
        snapshot_date=fetched.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat(),
        ticker=ticker,
        provider_symbol=f"TSE:{ticker}",
        fetched_at_utc=fetched.isoformat(),
        fetch_status=status,
        **metrics,
    )
    names = tuple(item[0] for item in COLUMNS)
    connection = open_connection(path)
    try:
        connection.execute(
            f"INSERT INTO {TABLE} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
            tuple(values.get(name) for name in names),
        )
        connection.commit()
    finally:
        connection.close()


def read(path, cutoff=RECEIVED + timedelta(days=2)):
    return external_assessment(path, ticker="1234", cutoff=cutoff, evaluated_at=cutoff)


def quants(fetched, score_date, **changes):
    return QuantsScoreObservation(
        ticker="1234",
        fetched_at_utc=fetched,
        score_as_of_date=score_date,
        source_url="https://example.invalid/quants",
        parser_version="synthetic-1",
        **changes,
    )


def test_latest_two_are_selected_per_distributor_including_nulls(tmp_path):
    path = tmp_path / "market.sqlite"
    first = valuation(RECEIVED, distributor="monex", consensus_target=1000)
    latest = valuation(RECEIVED + timedelta(minutes=2), distributor="monex", consensus_target=1200)
    other_first = valuation(
        RECEIVED + timedelta(minutes=1), distributor="other", consensus_target=900
    )
    other_null = valuation(
        RECEIVED + timedelta(minutes=3), distributor="other", consensus_target=None
    )
    save_observations(path, [first, latest, other_first, other_null])
    sources = {source.inputs.latest.distributor: source for source in read(path).model_valuations}
    assert sources["monex"].inputs.previous == first
    assert sources["monex"].comparisons[0].delta == 200
    assert sources["other"].inputs.previous == other_first
    assert sources["other"].comparisons[0].state == "missing"
    assert sources["other"].comparisons[0].delta is None


def test_population_read_keeps_each_company_history_separate_and_matches_single_reads(tmp_path):
    path = tmp_path / "market.sqlite"
    rows = [
        valuation(RECEIVED, consensus_target=1000),
        valuation(RECEIVED + timedelta(minutes=1), consensus_target=1100),
        valuation(RECEIVED + timedelta(minutes=2), consensus_target=1200),
        valuation(RECEIVED + timedelta(minutes=3), consensus_target=2100).model_copy(
            update={"ticker": "5678"}
        ),
        valuation(RECEIVED + timedelta(minutes=4), consensus_target=2200).model_copy(
            update={"ticker": "5678"}
        ),
    ]
    save_observations(path, rows)
    tv_row(path, RECEIVED, ticker="1234", price_target_average=1100)
    tv_row(path, RECEIVED + timedelta(minutes=1), ticker="5678", price_target_average=2200)
    cutoff = RECEIVED + timedelta(days=1)
    population = read_observation_population(path, tickers=("1234", "5678", "9999"), cutoff=cutoff)
    for ticker, history in population.items():
        assert history == read_observations(path, ticker, cutoff)
    assert population["1234"].model_valuations[0].latest.consensus_target == 1200
    assert population["1234"].model_valuations[0].previous.consensus_target == 1100
    assert population["5678"].model_valuations[0].previous.consensus_target == 2100
    assert population["9999"].model_valuations == ()
    assert population["9999"].tradingview.latest is None


def test_original_origin_groups_do_not_merge_different_models_into_votes(tmp_path):
    path = tmp_path / "market.sqlite"
    save_observations(
        path,
        [
            valuation(
                consensus_target_origin="IFIS",
                per_company_forecast=1100,
                per_company_forecast_origin="Monex Scouter model",
            ),
            rating(origin="IFIS"),
        ],
    )
    result = read(path)
    groups = {group.origin: group.references for group in result.origin_groups}
    assert {(ref.distributor, ref.metric) for ref in groups["IFIS"]} == {
        ("monex", "consensus_target"),
        ("rakuten", "rating"),
    }
    assert groups["Monex Scouter model"][0].metric == "per_company_forecast"
    assert result.model_valuations[0].inputs.latest.consensus_target == 1200
    assert result.analyst_ratings[0].inputs.latest.rating == 3.5
    assert not hasattr(result, "composite_score")


def test_quants_native_scores_keep_separate_scale_and_basis_dates(tmp_path):
    path = tmp_path / "market.sqlite"
    first = quants(RECEIVED, "2026-10-09", growth_score=6, composite_score=60)
    latest = quants(RECEIVED + timedelta(days=1), "2026-10-10", growth_score=8, composite_score=65)
    save_observations(path, [first, latest])
    result = read(path).quants_scores[0]
    assert result.inputs.latest.score_as_of_date.isoformat() == "2026-10-10"
    assert result.inputs.previous.score_as_of_date.isoformat() == "2026-10-09"
    assert result.inputs.latest.score_scale_max == 10
    assert result.inputs.latest.composite_scale_max == 100
    assert result.comparisons[0].delta == 2
    assert result.comparisons[6].delta == 5
    assert result.comparisons[1].state == "missing"


def test_tradingview_receipt_forecast_null_and_unresolved_are_distinct(tmp_path):
    path = tmp_path / "market.sqlite"
    first = datetime(2026, 10, 9, 7, 0, tzinfo=UTC)
    second = first + timedelta(days=1)
    third = second + timedelta(days=1)
    tv_row(path, first, eps_forecast_next_fy=0, quote_currency="JPY", quote_close=100)
    tv_row(path, second, quote_currency="JPY", quote_close=101)
    tv_row(path, third, status="unresolved")
    received = read(path, first).tradingview
    assert received.availability == "forecast_fields_present"
    assert received.forecast_fields == ("eps_forecast_next_fy",)
    assert received.inputs.latest.estimate_currency is None
    assert received.inputs.latest.quote_currency == "JPY"
    missing = read(path, second).tradingview
    assert missing.availability == "all_null"
    assert missing.inputs.previous.eps_forecast_next_fy == 0
    assert (
        next(item for item in missing.comparisons if item.metric == "eps_forecast_next_fy").state
        == "missing"
    )
    assert read(path, third).tradingview.availability == "unresolved"
    assert read(path, first - timedelta(microseconds=1)).tradingview.availability == "not_observed"


def test_exact_received_cutoff_does_not_include_later_same_day_microsecond(tmp_path):
    path = tmp_path / "market.sqlite"
    cutoff = datetime(2026, 10, 10, 7, tzinfo=UTC)
    # TradingView stores ISO strings with optional fractional seconds.
    tv_row(path, cutoff, eps_forecast_next_fy=10)
    # One snapshot/day is immutable; use a distinct ticker for a future receipt.
    tv_row(path, cutoff + timedelta(microseconds=1), ticker="5678", eps_forecast_next_fy=99)
    result = external_assessment(path, ticker="5678", cutoff=cutoff, evaluated_at=cutoff)
    assert result.tradingview.availability == "not_observed"
    assert read(path, cutoff).tradingview.inputs.latest.eps_forecast_next_fy == 10


def test_query_and_public_show_have_no_network_or_writes(tmp_path, monkeypatch, capsys):
    path = tmp_path / "market.sqlite"
    save_observations(path, [valuation(consensus_target_period=None, share_basis=None)])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    original = sqlite3.connect

    def connect(database, *args, **kwargs):
        # The canonical validator creates a private schema template in memory.
        if database == ":memory:":
            return original(database, *args, **kwargs)
        assert "mode=ro" in database
        connection = original(database, *args, **kwargs)

        def authorize(action, _a, _b, _db, _trigger):
            assert action not in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_DROP_TABLE,
            }
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    def forbidden(*_args, **_kwargs):
        pytest.fail("external assessment read attempted network I/O")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert read(path).model_valuations[0].inputs.latest.share_basis is None
    assert (
        main(["show", "--db", str(path), "--ticker", "1234", "--cutoff", RECEIVED.isoformat()]) == 0
    )
    assert '"consensus_target"' in capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    missing = tmp_path / "absent.sqlite"
    assert read(missing).model_valuations == ()
    assert not missing.exists()


def test_domain_import_does_not_start_composition_or_acquisition():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """\nimport sys
from baibai_engine.machine_analysis.generation.runners.external_assessment import external_assessment
assert not any(name.startswith(("baibai_engine.investment_judgment", "baibai_engine.machine_analysis.screening", "baibai_engine.machine_analysis.models.company_financials", "baibai_engine.machine_analysis.models.market_context")) for name in sys.modules)
assert "baibai_jobs.acquisition.external_assessments.collection" not in sys.modules
assert not any(name.startswith("baibai_jobs.maintenance.tradingview") for name in sys.modules)
""",
        ],
        check=True,
    )


@pytest.mark.parametrize("metric", FORECAST_METRICS)
def test_tradingview_row_year_metadata_does_not_establish_each_metric_period(tmp_path, metric):
    path = tmp_path / "market.sqlite"
    first = datetime(2026, 10, 9, 7, tzinfo=UTC)
    metadata = dict(
        target_period_label="FY2027",
        target_period_end="2027-03-31",
        estimate_currency="JPY",
        estimate_unit="currency",
    )
    tv_row(path, first, **{metric: 100}, **metadata)
    tv_row(path, first + timedelta(days=1), **{metric: 120}, **metadata)
    result = read(path).tradingview
    comparison = next(item for item in result.comparisons if item.metric == metric)
    assert comparison.state == "not_comparable"
    assert comparison.reason == "metric_period_mapping_unconfirmed"
    assert not comparison.comparable
    assert comparison.delta is None
    assert comparison.latest_value == 120
    assert comparison.previous_value == 100
    assert result.inputs.latest.target_period_label == "FY2027"
    assert result.inputs.latest.target_period_end == "2027-03-31"
    assert result.availability == "forecast_fields_present"


@pytest.mark.parametrize("stored_offset", [-7, 0, 9])
@pytest.mark.parametrize("cutoff_offset", [-7, 0, 9])
@pytest.mark.parametrize("fraction", [0, 123456])
def test_tradingview_population_and_single_reads_agree_at_subsecond_and_offset_boundaries(
    tmp_path, stored_offset, cutoff_offset, fraction
):
    from datetime import timezone

    path = tmp_path / "market.sqlite"
    # Includes the JST day rollover and ISO strings with/without fractional seconds.
    cutoff = datetime(2026, 10, 10, 15, microsecond=fraction, tzinfo=UTC)
    stored_zone = timezone(timedelta(hours=stored_offset))
    requested_cutoff = cutoff.astimezone(timezone(timedelta(hours=cutoff_offset)))
    for ticker, shift in (("1001", -1), ("1002", 0), ("1003", 1)):
        received = (cutoff + timedelta(microseconds=shift)).astimezone(stored_zone)
        tv_row(path, received, ticker=ticker, eps_forecast_next_fy=1)
    population = read_observation_population(
        path, tickers=("1001", "1002", "1003"), cutoff=requested_cutoff
    )
    assert {ticker for ticker, row in population.items() if row.tradingview.latest is not None} == {
        "1001",
        "1002",
    }
    for ticker in ("1001", "1002", "1003"):
        assessment = external_assessment(
            path, ticker=ticker, cutoff=requested_cutoff, evaluated_at=requested_cutoff
        )
        assert assessment.tradingview.inputs == population[ticker].tradingview
        assert (assessment.tradingview.inputs.latest is not None) == (ticker in {"1001", "1002"})


def test_show_parser_does_not_import_acquisition_or_store_implementations():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from baibai_engine.cli.external_assessment import build_parser
args = build_parser().parse_args([
    "show", "--ticker", "1234", "--cutoff", "2026-10-15T12:00:00+09:00"
])
assert args.command == "show"
assert "baibai_jobs.acquisition.external_assessments.collection" not in sys.modules
assert "baibai_engine.machine_analysis.observations.external_assessments.store" not in sys.modules
""",
        ],
        check=True,
    )


def test_new_generation_retains_tradingview_inputs_without_rewriting_saved_history(tmp_path):
    from baibai_engine.machine_analysis.generation.runners.external_assessment import (
        generate_external_assessment,
    )
    from baibai_engine.machine_analysis.models.external_assessment.models import ExternalAssessment

    path = tmp_path / "market.sqlite"
    tv_row(path, RECEIVED, eps_forecast_next_fy=10, quote_close=100)
    save_observations(path, [valuation()])
    before = path.read_bytes()
    cutoff = RECEIVED + timedelta(days=30)
    history = read_observations(path, "1234", cutoff)
    personal = external_assessment(path, ticker="1234", cutoff=cutoff, evaluated_at=cutoff)
    assert personal.tradingview.inputs.latest.fetched_at_utc == RECEIVED
    assert (
        personal.tradingview.inputs.latest.snapshot_date
        == RECEIVED.astimezone(ZoneInfo("Asia/Tokyo")).date()
    )
    assert personal.tradingview.inputs.latest.eps_forecast_next_fy == 10
    fixed_payload = personal.model_dump_json()
    generated = generate_external_assessment(history, evaluated_at=cutoff)
    assert generated.method_version == "same-series-v6"
    assert generated.tradingview == personal.tradingview
    assert generated.tradingview.availability == "forecast_fields_present"
    assert "eps_forecast_next_fy" in generated.tradingview.forecast_fields
    assert generated.model_valuations == personal.model_valuations
    assert ExternalAssessment.model_validate_json(fixed_payload).model_dump_json() == fixed_payload
    assert path.read_bytes() == before

    from baibai_engine.machine_analysis.observations.external_assessments.history import (
        TradingViewHistory,
    )

    historical = generated.model_copy(
        update={
            "method_version": "same-series-v5-no-tradingview",
            "tradingview": generated.tradingview.model_copy(
                update={
                    "inputs": TradingViewHistory(),
                    "availability": "excluded_by_source_terms",
                    "forecast_fields": (),
                    "comparisons": (),
                }
            ),
        }
    ).model_dump_json()
    assert ExternalAssessment.model_validate_json(historical).model_dump_json() == historical


def test_optional_population_read_can_skip_tradingview_rows(tmp_path):
    path = tmp_path / "market.sqlite"
    tv_row(path, RECEIVED, eps_forecast_next_fy=10)
    save_observations(path, [valuation()])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE tradingview_forecast_snapshots SET eps_forecast_next_fy='invalid'")
    before = path.read_bytes()
    result = read_observation_population(
        path, tickers=("1234",), cutoff=RECEIVED, include_tradingview=False
    )
    assert result["1234"].tradingview.latest is None
    assert result["1234"].model_valuations[0].latest.consensus_target == 1200
    assert path.read_bytes() == before
