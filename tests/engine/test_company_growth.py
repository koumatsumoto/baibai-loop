"""r6の有限反例と、公開読取からResearchの固定保存・再開まで。実providerを使わない。"""

import json
import socket
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta
from hashlib import sha256

import pytest
import yaml
from tests.helpers.l1_release import publish_market_fixture
from tests.helpers.master import insert_master_rows
from tests.helpers.research_selection import draft_for, selection
from tests.helpers.screening_sqlite import insert_financial_record

from baibai_engine.company_growth.calculations import build_changes, build_period_states
from baibai_engine.market.financial_documents import normalize_financial_summary
from baibai_engine.market.financial_periods import prior_year_period_matches
from baibai_engine.read_api.company_growth import company_growth as public_company_growth
from baibai_engine.research.inputs import collect_research_inputs, project_research_inputs
from baibai_engine.research.operation.progress import selection_binding
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.selection import ResearchSelection, parse_selection, selection_sha256
from baibai_engine.research.workspace import (
    ResearchWorkspaceConflictError,
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.storage.lake.keys import release_manifest_key
from baibai_engine.storage.lake.models import (
    ReleaseManifest,
    canonical_lake_model_bytes,
)
from baibai_engine.storage.observations.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
    write_lake_store_origin,
)
from baibai_engine.storage.observations.schema import open_connection

NOW = datetime.fromisoformat("2026-10-24T18:00:00+09:00")


def document(year, quarter, sales, op=None, profit=None, **updates):
    end = (date(year, 6, 30), date(year, 9, 30), date(year, 12, 31), date(year + 1, 3, 31))[
        quarter - 1
    ]
    record = {
        "Code": "12340",
        "DiscDate": (end + timedelta(days=45)).isoformat(),
        "DiscTime": "15:00:00",
        "DiscNo": f"synthetic-{year}-{quarter}",
        "DocType": "FinancialStatements_Consolidated_JP",
        "CurPerType": ("1Q", "2Q", "3Q", "FY")[quarter - 1],
        "CurPerSt": date(year, 4, 1).isoformat(),
        "CurPerEn": end.isoformat(),
        "CurFYEn": date(year + 1, 3, 31).isoformat(),
        "Sales": sales,
        "OP": op,
        "NP": profit,
    }
    return {**record, **updates}


def states(records, sector="情報・通信業"):
    rows = [normalize_financial_summary(r) for r in records]
    assert all(row is not None for row in rows)
    return build_period_states(rows, sector=sector)


def observation(state, measurement):
    return next(o for o in state.observations if o.measurement == measurement)


def amount(state, measurement, metric):
    return observation(state, measurement).metrics[metric]


def change(result, end, metric, measurement="reported"):
    return next(
        c for c in result if c.to_end == end and c.metric == metric and c.measurement == measurement
    )


def market_store(tmp_path, records, *, release="synthetic-release-1", price_day=None):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection, connection:
        for record in records:
            insert_financial_record(connection, record)
        if price_day:
            connection.execute(
                "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
                ("1234", price_day.isoformat(), 1000),
            )
    if release:
        publish_market_fixture(path, release, created_at=NOW)
    return path


def company_growth(path, **kwargs):
    return public_company_growth(path, mirror=path.parent / "mirror", **kwargs)


def test_seasonality_and_one_time_gain_remain_one_quarter_observation():
    records = []
    for year in (2022, 2023, 2024):
        cumulative = 0
        for quarter in range(1, 5):
            cumulative += 200 if (year, quarter) == (2023, 1) else 100
            records.append(document(year, quarter, cumulative * 10, cumulative, cumulative))
    result = states(records)
    quarters = [amount(s, "quarter", "parent_profit").value for s in result]
    assert quarters[4:8] == [200, 100, 100, 100]
    ttm = [amount(s, "annual_ttm", "parent_profit").value for s in result]
    assert ttm[4:8] == [500] * 4
    assert ttm[8] == 400
    assert "sustained_growth" not in result[4].model_dump_json()


def test_cumulative_difference_and_reported_eps_never_share_arithmetic():
    result = states(
        [
            document(2024, 1, 100, 10, 100, EPS=1, AvgSh=100),
            document(2024, 2, 230, 25, 200, EPS=200 / 150, AvgSh=150),
        ]
    )
    assert amount(result[1], "quarter", "sales").value == 130
    assert amount(result[1], "quarter", "parent_profit").value == 100
    assert amount(result[1], "reported", "reported_basic_eps").value == pytest.approx(200 / 150)
    assert "reported_basic_eps" not in observation(result[1], "quarter").metrics
    assert "reported_basic_eps" not in observation(result[1], "annual_ttm").metrics
    assert observation(result[1], "quarter").period_start == date(2024, 7, 1)


@pytest.mark.parametrize(("corrected", "expected"), [(100, 130), (90, None)])
def test_later_q1_correction_only_blocks_a_known_changed_cumulative_pair(corrected, expected):
    records = [document(2024, 1, 100), document(2024, 2, 230)]
    records.append(document(2024, 1, corrected, DiscDate="2025-02-01", DiscNo="correction"))
    result = states(records)
    value = amount(result[1], "quarter", "sales")
    assert value.value == expected
    if expected is None:
        assert value.reason == "inconsistent_cumulative_revision"
    assert amount(result[0], "reported", "sales").value == corrected
    # A new H1 on the corrected basis makes the pair usable again.
    records.append(document(2024, 2, 230, DiscDate="2025-02-02", DiscNo="h1-corrected"))
    assert amount(states(records)[1], "quarter", "sales").value == 230 - corrected


def test_partial_correction_and_same_revision_conflict_are_metric_local():
    base = document(2024, 1, 100, 10, 8)
    correction = {
        **base,
        "Sales": None,
        "OP": 15,
        "NP": None,
        "DiscDate": "2024-08-20",
        "DiscNo": "op-fix",
    }
    result = states([base, correction])
    assert amount(result[0], "reported", "sales").value == 100
    assert amount(result[0], "reported", "operating_profit").value == 15
    assert amount(result[0], "reported", "sales").documents[0].disclosure_number == base["DiscNo"]
    conflicting = {**correction, "OP": 16, "DiscNo": "op-conflict"}
    result = states([base, correction, conflicting])
    assert amount(result[0], "reported", "operating_profit").value is None
    assert amount(result[0], "reported", "sales").value == 100


@pytest.mark.parametrize(
    ("before", "after", "rate", "difference"),
    [
        (10, -10, -2, -20),
        (-100, -50, None, 50),
        (0, 10, None, 10),
        (1, 2, 1, 1),
        (100, 200, 1, 100),
    ],
)
def test_growth_rates_keep_amounts_sign_and_nonpositive_base(before, after, rate, difference):
    result = build_changes(states([document(2023, 1, before), document(2024, 1, after)]))
    value = change(result, date(2024, 6, 30), "sales")
    assert (value.from_value, value.to_value) == (before, after)
    assert value.difference == difference
    assert value.rate == rate
    assert value.sign_change is not None
    if before <= 0:
        assert value.reason == "nonpositive_base"


def test_margin_points_do_not_become_a_growth_score():
    result = build_changes(states([document(2023, 1, 100, 20), document(2024, 1, 200, 30)]))
    value = change(result, date(2024, 6, 30), "operating_margin")
    assert value.margin_points == pytest.approx(-5)
    assert value.rate is None
    assert change(result, date(2024, 6, 30), "operating_profit").rate == 0.5


def test_eps_split_comparison_is_unverified_but_reported_values_survive():
    result = states(
        [
            document(2023, 4, 100, 20, 10, EPS=788.29, ShOutFY=100, TrShFY=20),
            document(2024, 4, 120, 25, 12, EPS=188.71, ShOutFY=500, TrShFY=100),
        ]
    )
    value = change(build_changes(result), date(2025, 3, 31), "reported_basic_eps")
    assert (value.from_value, value.to_value) == (788.29, 188.71)
    assert value.difference is None
    assert value.reason == "share_basis_unverified"
    assert amount(result[0], "balance", "issued_shares").value == 100
    assert amount(result[1], "balance", "treasury_shares").value == 100


def test_cf_bs_and_ordinary_profit_are_not_filled_or_relabelled():
    result = states(
        [
            document(2024, 1, 100, None, -30, OdP=20, CFO=50, TA=1000, Eq=400),
            document(2024, 2, 230, 25, -60),
        ]
    )
    assert amount(result[0], "reported", "operating_profit").value is None
    assert amount(result[0], "reported", "parent_profit").value == -30
    assert amount(result[1], "reported", "operating_cash_flow").value is None
    assert amount(result[1], "balance", "total_assets").value is None
    assert "operating_cash_flow" not in observation(result[0], "quarter").metrics
    assert amount(result[0], "balance", "net_assets").value == 400
    assert "common_equity" not in observation(result[0], "balance").metrics


@pytest.mark.parametrize(
    "sector", ["銀行業", "保険業", "証券・商品先物取引業", "その他金融業", None]
)
def test_financial_or_unknown_sector_does_not_get_nonfinancial_margin(sector):
    result = states([document(2024, 1, 100, 20)], sector=sector)
    value = amount(result[0], "reported", "operating_margin")
    assert value.value is None
    assert value.reason in {"sector_unverified", "not_applicable_to_financial_sector"}
    assert amount(result[0], "reported", "operating_profit").value == 20


def test_basis_and_irregular_year_do_not_gain_convenient_ttm():
    result = states(
        [
            document(2023, 1, 100),
            document(2023, 4, 400),
            document(2024, 1, 120, DocType="FinancialStatements_NonConsolidated_JP"),
        ]
    )
    assert amount(result[2], "annual_ttm", "sales").value is None
    assert amount(result[2], "annual_ttm", "sales").reason == "statement_basis_incomparable"
    assert change(build_changes(result), date(2024, 6, 30), "sales").difference is None
    short = document(2024, 4, 400, CurPerSt="2024-07-01")
    assert amount(states([short])[0], "annual_ttm", "sales").value is None
    first = normalize_financial_summary(document(2023, 1, 100))
    second = normalize_financial_summary(document(2024, 1, 120, CurPerEn="2024-07-05"))
    assert prior_year_period_matches(
        first, second, length_tolerance_ratio=0.1, end_tolerance_days=45
    )
    shifted = normalize_financial_summary(
        document(2024, 1, 120, CurPerSt="2024-07-01", CurPerEn="2024-09-30")
    )
    assert not prior_year_period_matches(
        first, shifted, length_tolerance_ratio=0.1, end_tolerance_days=45
    )


def test_query_retains_pre_window_operands_and_later_corrections_at_both_edges(tmp_path):
    records = [document(y, q, q * 100) for y in (2022, 2023, 2024) for q in range(1, 5)]
    records.append(document(2022, 4, 450, DiscDate="2026-10-01", DiscNo="late-old-fy"))
    path = market_store(tmp_path, records)
    before = path.read_bytes()
    narrow = company_growth(
        path, ticker="1234", as_of=NOW.date(), start=date(2023, 4, 1), end=date(2023, 12, 31)
    )
    broad = company_growth(
        path, ticker="1234", as_of=NOW.date(), start=date(2022, 4, 1), end=date(2025, 3, 31)
    )
    assert narrow.periods == tuple(
        s for s in broad.periods if date(2023, 4, 1) <= s.period_end <= date(2023, 12, 31)
    )
    assert narrow.changes == tuple(
        c for c in broad.changes if date(2023, 4, 1) <= c.to_end <= date(2023, 12, 31)
    )
    ttm = amount(narrow.periods[0], "annual_ttm", "sales")
    assert ttm.value == 450
    assert any(d.disclosure_number == "late-old-fy" for d in ttm.documents)
    assert narrow.input_release.release_id == "synthetic-release-1"
    assert narrow.input_release.receipt_cutoff is None
    assert narrow.coverage.status == "unverified"
    assert narrow.coverage.observed_start == date(2022, 6, 30)
    assert narrow.per.status == narrow.peer.status == "not_provided"
    assert path.read_bytes() == before


def test_observations_do_not_prove_continuous_acquisition_coverage(tmp_path):
    path = market_store(tmp_path, [document(2022, 1, 100), document(2024, 1, 200)])
    result = company_growth(path, ticker="1234", as_of=NOW.date())
    assert result.status == "available"
    assert result.coverage.status == "unverified"
    assert result.coverage.gaps_between_observations == ((date(2022, 6, 30), date(2024, 6, 30)),)
    value = change(result.changes, date(2024, 6, 30), "sales")
    assert value.from_value is None
    assert value.reason == "comparable_prior_period_not_observed"


def test_missing_store_unfixed_release_and_invalid_period_are_local_states(tmp_path):
    path = tmp_path / "absent.sqlite"
    assert (
        company_growth(path, ticker="1234", as_of=NOW.date()).reason == "market_store_unavailable"
    )
    assert not path.exists()
    path = market_store(tmp_path, [document(2024, 1, 100)], release=None)
    assert (
        company_growth(path, ticker="1234", as_of=NOW.date()).reason == "fixed_release_unavailable"
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        insert_financial_record(connection, document(2024, 2, 230, CurPerEn="2027-03-31"))
    publish_market_fixture(path, "fixed", created_at=NOW)
    result = company_growth(path, ticker="1234", as_of=NOW.date())
    assert len(result.periods) == 1
    assert result.coverage.excluded_document_count == 1
    with pytest.raises(ValueError, match="start <= end"):
        company_growth(path, ticker="1234", as_of=NOW.date(), start=date(2027, 1, 1))


def test_fixed_growth_result_reaches_l3_and_does_not_change_valuation_gaps(tmp_path, monkeypatch):
    records = [document(2023, 1, 100, 20, 10), document(2024, 1, 200, 30, 25)]
    path = market_store(tmp_path, records)
    selected = selection(now=NOW, tickers=("1234",))
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: pytest.fail("network used"))
    inputs = collect_research_inputs(
        snapshot=selected.inputs.analysis,
        input_gaps=selected.inputs.input_gaps,
        tickers=("1234",),
        research_as_of=NOW.date(),
        input_known_at=NOW,
        market_path=path,
        indicators_path=tmp_path / "absent-indicators",
        company_growth_mirror=tmp_path / "mirror",
        app_path=tmp_path / "absent-app",
        company_growth_start=date(2024, 4, 1),
        company_growth_end=date(2024, 6, 30),
    )
    saved = ResearchSelection.model_validate_json(
        selected.model_copy(update={"inputs": inputs}).model_dump_json()
    )
    expected = deepcopy(inputs.securities_context["1234"]["company_growth"])
    assert inputs.analysis == selected.inputs.analysis
    assert inputs.input_gaps == selected.inputs.input_gaps
    assert expected["changes"][0]["from_value"] == 100
    assert expected["changes"][0]["to_value"] == 200
    assert expected["periods"][0]["period_end"] == "2024-06-30"
    # Release, method and newly published numbers cannot replace the saved output.
    with closing(sqlite3.connect(path)) as connection, connection:
        write_lake_store_origin(connection, LakeStoreOrigin("synthetic-release-2", "c" * 64))
        insert_financial_record(
            connection, document(2024, 1, 900, DiscDate="2026-10-23", DiscNo="changed")
        )
    monkeypatch.setattr(
        "baibai_engine.research.inputs.company_growth",
        lambda *_a, **_kw: pytest.fail("growth recomputed"),
    )
    projected = project_research_inputs(saved)["securities"]["1234"]["company_growth"]
    assert projected["input_release"] == expected["input_release"]
    assert (
        projected["comparison_basis"]
        == expected["comparison_basis"]
        == "source_reported_economic_scope_unverified"
    )
    assert projected["reported"]["rows"][0]["values"][:3] == [200, 30, 25]
    assert projected["reported_yoy"]["rows"][0][5:8] == [100, 100, 1]
    assert saved.inputs.securities_context["1234"]["company_growth"] == expected
    assert selection_sha256(saved) != selection_sha256(selected)
    legacy = saved.model_dump(mode="json")
    del legacy["inputs"]["securities_context"]["1234"]["company_growth"]
    assert (
        "company_growth"
        not in project_research_inputs(parse_selection(legacy))["securities"]["1234"]
    )
    legacy["inputs"]["securities_context"]["1234"]["growth_score"] = 100
    with pytest.raises(ValueError, match="security context"):
        parse_selection(legacy)


def test_workspace_resume_restores_saved_growth_without_store_or_current_method(
    tmp_path, monkeypatch
):
    path = market_store(tmp_path, [document(2023, 1, 100), document(2024, 1, 200)])
    selected = selection(now=NOW, tickers=("1234",))
    draft = draft_for(selected).model_copy(
        update={
            "company_growth_start": date(2024, 4, 1),
            "company_growth_end": date(2024, 6, 30),
        }
    )
    db, workspace = tmp_path / "application.sqlite", tmp_path / "workspace"
    arguments = dict(db_path=db, workspace=workspace, now=NOW)
    prepared = prepare_selected_workspace(
        draft=draft,
        sqlite_path=path,
        runs_path=tmp_path / "runs",
        indicators_path=tmp_path / "absent-indicators",
        company_growth_mirror=tmp_path / "mirror",
        **arguments,
    )
    saved = selection_binding(OperationService(db).get(prepared.operation_id).payload)
    expected = project_research_inputs(saved)
    output = workspace / "domain-inputs.yaml"
    output.write_text("damaged projection", encoding="utf-8")
    monkeypatch.setattr(
        "baibai_engine.research.workspace.selected_prepare.collect_research_inputs",
        lambda **_kw: pytest.fail("inputs recollected"),
    )
    monkeypatch.setattr(
        "baibai_engine.research.inputs.company_growth",
        lambda *_a, **_kw: pytest.fail("current method used"),
    )
    resumed = resume_selected_workspace(operation_id=prepared.operation_id, force=True, **arguments)
    assert resumed.status == "resumed"
    assert yaml.safe_load(output.read_text(encoding="utf-8")) == expected
    assert (
        expected["securities"]["1234"]["company_growth"]["input_release"]["release_id"]
        == "synthetic-release-1"
    )
    with pytest.raises(ResearchWorkspaceConflictError, match="different fixed selection"):
        prepare_selected_workspace(
            draft=draft.model_copy(update={"company_growth_start": date(2023, 4, 1)}),
            sqlite_path=path,
            runs_path=tmp_path / "runs",
            indicators_path=tmp_path / "absent-indicators",
            company_growth_mirror=tmp_path / "mirror",
            **arguments,
        )


def test_same_current_state_keeps_distinct_paths_for_l3():
    improving = states(
        [
            document(2022, 4, 1000, 50, 50),
            document(2023, 4, 1000, 75, 75),
            document(2024, 4, 1000, 100, 100),
        ]
    )
    declining = states(
        [
            document(2022, 4, 1000, 200, 200),
            document(2023, 4, 1000, 150, 150),
            document(2024, 4, 1000, 100, 100),
        ]
    )
    assert amount(improving[-1], "reported", "parent_profit").value == 100
    assert amount(declining[-1], "reported", "parent_profit").value == 100
    up = change(build_changes(improving), date(2025, 3, 31), "parent_profit")
    down = change(build_changes(declining), date(2025, 3, 31), "parent_profit")
    assert (up.from_value, up.to_value, up.difference) == (75, 100, 25)
    assert (down.from_value, down.to_value, down.difference) == (150, 100, -50)
    assert up.margin_points is None
    assert "forecast" not in up.model_dump()


def test_legacy_selection_hash_keeps_its_original_serialized_inputs():
    from baibai_engine.foundation.json import canonical_json

    old = selection(now=NOW, tickers=("1234",)).model_dump(mode="json")
    expected = sha256(canonical_json(old).encode()).hexdigest()
    restored = parse_selection(old)
    assert restored.model_dump(mode="json") == old
    assert selection_sha256(restored) == expected
    assert "company_growth" not in restored.inputs.securities_context["1234"]
    assert "company_growth" not in project_research_inputs(restored)["securities"]["1234"]


@pytest.mark.parametrize("start", [None, date(2021, 6, 30)])
def test_growth_display_window_leaves_fixed_inputs_and_requested_range_unchanged(
    tmp_path, monkeypatch, capsys, start
):
    from baibai_engine.research.workspace import compute_status
    from baibai_engine.research.workspace_cli import main

    records = [
        document(year, quarter, quarter * 100, quarter * 20, quarter * 10)
        for year in range(2020, 2025)
        for quarter in range(1, 5)
    ]
    path = market_store(tmp_path, records)
    draft = draft_for(selection(now=NOW, tickers=("1234",))).model_copy(
        update={"company_growth_start": start, "company_growth_end": date(2025, 3, 31)}
    )
    arguments = dict(
        draft=draft,
        db_path=tmp_path / "application.sqlite",
        workspace=tmp_path / "workspace",
        sqlite_path=path,
        runs_path=tmp_path / "runs",
        indicators_path=tmp_path / "absent-indicators",
        company_growth_mirror=tmp_path / "mirror",
        now=NOW,
    )
    prepared = prepare_selected_workspace(**arguments)
    service = OperationService(arguments["db_path"])
    saved = selection_binding(service.get(prepared.operation_id).payload)
    original = saved.model_dump(mode="json")
    digest = selection_sha256(saved)
    growth = saved.inputs.securities_context["1234"]["company_growth"]
    assert len(growth["periods"]) == (20 if start is None else 16)
    output = arguments["workspace"] / "domain-inputs.yaml"

    def displayed():
        return yaml.safe_load(output.read_text())["securities"]["1234"]["company_growth"]

    compact = displayed()
    assert compact["saved_scope"]["requested_start"] == (start.isoformat() if start else None)
    assert compact["saved_scope"]["requested_end"] == "2025-03-31"
    assert compact["display_scope"]["period_count"] == 12
    assert len(compact["reported"]["rows"]) == 12
    assert compact["reported"]["rows"][0]["values"][:3] == [100, 20, 10]
    assert compact["reported"]["rows"][0]["missing"]["reported_basic_eps"] == "not_observed"
    assert compact["documents"]["d1"]["disclosure_number"] == "synthetic-2022-1"
    # These must be the already saved calculations, including prior operands outside the display.
    columns = compact["reported_yoy"]["columns"]
    first = dict(zip(columns, compact["reported_yoy"]["rows"][0], strict=True))
    assert first["from_end"] == "2021-06-30"
    assert first["from_value"] == 100
    assert first["rate"] == 0
    assert "periods" not in compact
    assert compact["full_result_ref"] == "/inputs/securities_context/1234/company_growth"

    monkeypatch.setattr(
        "baibai_engine.research.workspace.selected_prepare.collect_research_inputs",
        lambda **_kw: pytest.fail("inputs recollected"),
    )
    authored = arguments["workspace"] / "assessment-draft.yaml"
    authored.write_text("authored draft retained")
    resumed = prepare_selected_workspace(**arguments, company_growth_periods=16, force=True)
    assert resumed.selection_sha256 == digest
    assert displayed()["display_scope"]["period_count"] == 16
    assert (
        main(
            [
                "prepare",
                "--operation-id",
                prepared.operation_id,
                "--db",
                str(arguments["db_path"]),
                "--workspace",
                str(arguments["workspace"]),
                "--company-growth-periods",
                "all",
                "--force",
            ],
            now=NOW,
        )
        == 0
    )
    capsys.readouterr()
    assert displayed()["display_scope"]["period_count"] == len(growth["periods"])
    assert displayed()["display_scope"]["latest_period_limit"] is None
    assert compute_status(arguments["workspace"], db_path=arguments["db_path"], now=NOW)
    assert authored.read_text() == "authored draft retained"
    restored = selection_binding(service.get(prepared.operation_id).payload)
    assert restored.model_dump(mode="json") == original
    assert selection_sha256(restored) == digest


def test_old_other_dataset_watermark_cannot_hide_a_retained_financial_correction(tmp_path):
    old_price_day = date(2024, 6, 1)
    financial_day = date(2026, 10, 23)
    path = market_store(
        tmp_path,
        [
            document(2024, 1, 100),
            document(2024, 1, 150, DiscDate=financial_day.isoformat(), DiscNo="late-correction"),
        ],
        price_day=old_price_day,
    )
    origin = read_lake_store_origin(path)
    manifest = ReleaseManifest.model_validate_json(
        (path.parent / "mirror" / release_manifest_key(release_id=origin.release_id)).read_bytes()
    )
    assert manifest.data_as_of == old_price_day
    result = company_growth(path, ticker="1234", as_of=date(2024, 6, 30))
    value = amount(result.periods[0], "reported", "sales")
    assert value.value == 150
    assert value.documents[0].disclosed_at == financial_day
    assert result.input_release.manifest_sha256 == origin.release_manifest_sha256
    assert result.input_release.receipt_cutoff is None
    assert result.read_semantics == "retained_latest_in_fixed_release_not_point_in_time"


def test_optional_source_rows_leave_core_states_documents_and_changes_unchanged(tmp_path):
    path = market_store(tmp_path, [document(2023, 1, 100), document(2024, 1, 200)])
    before = company_growth(path, ticker="1234", as_of=NOW.date())
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
            ("1234", "2024-06-28", 1000),
        )
        insert_financial_record(connection, document(2024, 1, 999, Code="56780"))
    after = company_growth(path, ticker="1234", as_of=NOW.date())
    assert after.periods == before.periods
    assert after.changes == before.changes
    assert after.per.status == "not_provided"
    assert after.peer.status == "not_provided"


def test_metric_availability_cannot_change_the_shared_ttm_measurement_interval():
    records = [
        document(2023, 1, 100),
        document(2023, 1, None, 10, CurPerEn="2023-07-05", DiscNo="different-interval"),
        document(2023, 4, 400, 50),
        document(2024, 1, 200, 25),
    ]
    current = states(records)[-1]
    ttm = observation(current, "annual_ttm")
    assert ttm.period_start == date(2023, 7, 1)
    assert ttm.period_end == date(2024, 6, 30)
    assert ttm.metrics["sales"].value == 500
    assert ttm.metrics["operating_profit"].value is None
    assert ttm.metrics["operating_profit"].reason == "operand_unavailable"
    # The differently measured original OP remains observed, without relabelling it.
    assert amount(states(records)[1], "reported", "operating_profit").value == 10


def test_finite_ci_sample_reports_inputs_cost_and_thin_table_without_claiming_l3_success(tmp_path):
    from tools.company_growth_sample import Sample, SampleInput, measure_samples

    from baibai_engine.company_growth.models import InputRelease

    path = market_store(tmp_path, [document(2024, 1, 100, 10, 5)])
    spec = SampleInput(
        input_release=InputRelease(
            release_id="synthetic-release-1",
            manifest_sha256=read_lake_store_origin(path).release_manifest_sha256,
        ),
        as_of=NOW.date(),
        samples=(Sample(ticker="1234", start=date(2024, 4, 1), end=date(2024, 6, 30)),),
    )
    report = measure_samples(path, spec, mirror=path.parent / "mirror")
    sample = report["samples"][0]
    assert sample["company_growth"]["periods"][0]["period_end"] == "2024-06-30"
    assert sample["thin_reported_table"][0]["reported"]["sales"] == 100
    assert sample["query_seconds"] >= 0
    assert sample["company_growth_json_bytes"] > sample["thin_reported_table_json_bytes"]
    assert report["acceptance"] == "requires_original_value_and_l3_comparison_review"
    with pytest.raises(ValueError, match="requested fixed release"):
        measure_samples(
            path,
            spec.model_copy(
                update={
                    "input_release": InputRelease(release_id="different", manifest_sha256="b" * 64),
                }
            ),
            mirror=path.parent / "mirror",
        )


@pytest.mark.parametrize(
    ("correction_time", "h1_time", "expected", "reason"),
    [
        ("16:00:00", "15:00:00", None, "inconsistent_cumulative_revision"),
        ("14:00:00", "15:00:00", 140, None),
        (None, "15:00:00", None, "cumulative_revision_order_unresolved"),
        ("16:00:00", None, None, "cumulative_revision_order_unresolved"),
        (None, None, None, "cumulative_revision_order_unresolved"),
    ],
)
def test_same_day_cumulative_correction_respects_known_and_unknown_times(
    correction_time, h1_time, expected, reason
):
    result = states(
        [
            document(2024, 1, 100),
            document(2024, 2, 230, DiscDate="2024-11-14", DiscTime=h1_time),
            document(
                2024,
                1,
                90,
                DiscDate="2024-11-14",
                DiscTime=correction_time,
                DiscNo="same-day-q1-correction",
            ),
        ]
    )
    value = amount(result[-1], "quarter", "sales")
    assert value.value == expected
    assert value.reason == reason


def test_unknown_times_without_an_observed_change_do_not_invent_a_revision_conflict():
    result = states(
        [
            document(2024, 1, 100, DiscDate="2024-11-14", DiscTime=None),
            document(2024, 2, 230, DiscDate="2024-11-14", DiscTime=None),
        ]
    )
    assert amount(result[-1], "quarter", "sales").value == 130


def test_public_query_uses_the_normalized_security_master_sector_name(tmp_path):
    path = market_store(tmp_path, [document(2024, 1, 100, 20)])
    with closing(sqlite3.connect(path)) as connection, connection:
        insert_master_rows(
            connection,
            [
                ("2026-10-23", "1234", "合成証券", "プライム", "証券･商品先物取引業", "011"),
            ],
        )
    publish_market_fixture(path, "sector-release", created_at=NOW)
    result = company_growth(path, ticker="1234", as_of=NOW.date())
    margin = amount(result.periods[0], "reported", "operating_margin")
    assert margin.value is None
    assert margin.reason == "not_applicable_to_financial_sector"
    assert amount(result.periods[0], "reported", "sales").value == 100


def test_working_store_rows_cannot_be_mislabeled_as_the_base_release(tmp_path):
    path = market_store(tmp_path, [document(2024, 1, 100)])
    fixed = company_growth(path, ticker="1234", as_of=NOW.date())
    with closing(sqlite3.connect(path)) as connection, connection:
        insert_financial_record(
            connection,
            document(
                2024,
                1,
                900,
                DiscDate="2026-10-23",
                DiscNo="not-yet-published",
            ),
        )
    after_local_update = company_growth(path, ticker="1234", as_of=NOW.date())
    assert after_local_update.input_release == fixed.input_release
    assert after_local_update.periods == fixed.periods
    assert amount(after_local_update.periods[0], "reported", "sales").value == 100
    publish_market_fixture(path, "synthetic-release-2", created_at=NOW)
    published = company_growth(path, ticker="1234", as_of=NOW.date())
    assert published.input_release.release_id == "synthetic-release-2"
    assert amount(published.periods[0], "reported", "sales").value == 900


def test_missing_fixed_release_objects_are_local_unavailability(tmp_path):
    path = market_store(tmp_path, [document(2024, 1, 100)])
    result = public_company_growth(
        path, ticker="1234", as_of=NOW.date(), mirror=tmp_path / "absent-mirror"
    )
    assert result.reason == "fixed_release_inputs_unavailable"
    assert result.input_release is None
    assert not (tmp_path / "absent-mirror").exists()


def test_fixed_reader_os_failure_is_local_to_growth(tmp_path, monkeypatch):
    path = market_store(tmp_path, [document(2024, 1, 100)])

    def unreadable(*_args, **_kwargs):
        raise PermissionError("injected existing object failure")

    monkeypatch.setattr(
        "baibai_engine.company_growth.query.read_fixed_financial_documents", unreadable
    )
    result = company_growth(path, ticker="1234", as_of=NOW.date())
    assert result.reason == "fixed_release_inputs_unavailable"
    assert result.input_release is None


def test_private_acceptance_uses_only_financial_master_and_never_overwrites_results(
    tmp_path, monkeypatch
):
    from tests.helpers.lake_policy import narrow_release_policy
    from tests.helpers.r2_store import MemoryR2Store
    from tools.company_growth_acceptance import execute

    from baibai_batch.storage.lake_publish import LakeCASConflict
    from baibai_engine.storage.lake.keys import current_release_pointer_key
    from baibai_engine.storage.lake.release import LakeReleasePointer
    from baibai_engine.storage.observations.coverage import record_range_source_coverage

    records = [
        document(y, q, q * 42424242, q * 11, q * 7) for y in (2022, 2023, 2024) for q in range(1, 5)
    ]
    path = market_store(tmp_path / "source", records, release=None)
    with closing(sqlite3.connect(path)) as connection, connection:
        insert_master_rows(
            connection,
            [
                ("2026-10-23", "1234", "合成会社", "プライム", "情報・通信業", "011"),
            ],
        )
        record_range_source_coverage(
            connection,
            source="jquants_fin_summaries",
            operation="fixture",
            table="jquants_fin_summaries",
            date_column="disclosed_at",
            requested_start=date(2022, 8, 14),
            requested_end=date(2025, 5, 15),
            status="ok",
            error=None,
        )
    publish_market_fixture(path, "private-fixture", created_at=NOW)
    origin = read_lake_store_origin(path)
    store = MemoryR2Store()
    for file in (path.parent / "mirror" / "lake").rglob("*"):
        if file.is_file():
            store.store_object(
                file.relative_to(path.parent / "mirror").as_posix(), file.read_bytes()
            )
    pointer = LakeReleasePointer(
        release_id=origin.release_id,
        manifest_key=release_manifest_key(release_id=origin.release_id),
        manifest_sha256=origin.release_manifest_sha256,
    )
    store.store_object(current_release_pointer_key(), canonical_lake_model_bytes(pointer))
    narrow_release_policy(
        monkeypatch, datasets=("jquants.fin_summaries", "jquants.master_snapshots")
    )
    before = {key: item.body for key, item in store.values.items()}
    summary = execute(
        store,
        tmp_path / "private-runner",
        run_id="100",
        attempt="1",
        head_sha="a" * 40,
        tickers=("1234",),
    )
    assert summary["status"] == "measured"
    assert summary["samples"][0]["sample_period_count"] == 12
    assert summary["samples"][0]["window_invariance"] is True
    assert "retained_histories" not in summary
    assert "company_growth" not in summary["samples"][0]
    assert "sales" not in summary["samples"][0]
    key = summary["private_result_key"]
    assert store.put_keys == [key]
    assert key.startswith("validation/company-growth/pr-1592/100-1/")
    private = store.values[key].body
    assert b"42424242" in private
    assert all(store.values[name].body == body for name, body in before.items())
    assert all(name.startswith("lake/") or name == key for name in store.get_keys)
    with pytest.raises(LakeCASConflict):
        execute(
            store,
            tmp_path / "second-runner",
            run_id="100",
            attempt="1",
            head_sha="a" * 40,
            tickers=("1234",),
        )
    assert store.values[key].body == private
    store.get_keys.clear()
    projected = execute(
        store,
        tmp_path / "projection-runner",
        run_id="101",
        attempt="1",
        head_sha="b" * 40,
        saved_result_key=key,
    )
    new_key = projected["private_result_key"]
    assert set(store.get_keys) == {key, new_key}
    assert (
        projected["samples"][0]["l3_projection_json_bytes"]
        < summary["samples"][0]["history_json_bytes"]
    )
    assert "sales" not in projected["samples"][0]
    assert store.values[key].body == private
    result = json.loads(store.values[new_key].body)
    assert result["l3_projections"]["1234"]["display_scope"]["period_count"] == 12
    assert result["saved_result_key"] == key


def test_acceptance_cli_keeps_private_result_keys_and_digest_out_of_public_logs(
    tmp_path, monkeypatch, capsys
):
    from tools import company_growth_acceptance as acceptance

    private_key = "private-result-object"
    private_digest = "private-result-digest"
    source_key = "private-input-object"
    monkeypatch.setattr(
        "sys.argv",
        [
            "company-growth-acceptance",
            "--directory",
            str(tmp_path),
            "--run-id",
            "100",
            "--attempt",
            "1",
            "--head-sha",
            "a" * 40,
        ],
    )
    monkeypatch.setattr(acceptance, "Boto3R2Store", lambda **_: object())
    monkeypatch.setattr(
        acceptance,
        "execute",
        lambda *_a, **_kw: {
            "status": "measured",
            "sample_count": 5,
            "private_result_bytes": 73660,
            "private_result_key": private_key,
            "private_result_sha256": private_digest,
            "saved_result_key": source_key,
        },
    )
    assert acceptance.main() == 0
    logged = capsys.readouterr().out
    assert all(value not in logged for value in (private_key, private_digest, source_key))
    public = json.loads(logged)
    assert public["status"] == "measured"
    assert public["sample_count"] == 5
    assert public["private_result_bytes"] == 73660
