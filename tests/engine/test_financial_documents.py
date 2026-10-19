"""Minimal constructed document fixtures; no provider response bodies."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from baibai_engine.market.financial_documents import (
    latest_forecast_source,
    normalize_financial_summary,
    project_financial_documents,
)
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.storage.observations.ingest.jquants import store_jquants_fin_summaries
from baibai_engine.storage.observations.reader import read_fin_summaries, read_financial_documents
from baibai_engine.storage.observations.schema import open_connection
from baibai_engine.valuation.metrics.capital import _resolve_capital_basis
from baibai_engine.valuation.metrics.financial_snapshot import build_financial_snapshot
from baibai_engine.valuation.metrics.periods import _latest_actual_row
from baibai_engine.valuation.metrics.profit import build_normalized_profit_signals

ASOF = date(2026, 10, 13)


def document(
    number: str, *, day: str = "2026-09-14", time: str = "16:00:00", **fields: Any
) -> dict[str, Any]:
    return {
        "Code": "99990",
        "DiscDate": day,
        "DiscTime": time,
        "DiscNo": number,
        "DocType": "EarnForecastRevision",
        "CurPerType": "FY",
        "CurFYSt": "2026-07-01",
        "CurFYEn": "2027-06-30",
        "CurPerSt": "2026-07-01",
        "CurPerEn": "2027-06-30",
        **fields,
    }


def documents() -> list[dict[str, Any]]:
    return [
        document(
            "statement",
            day="2026-08-10",
            DocType="FYFinancialStatements_NonConsolidated_JP",
            CurFYSt="2025-07-01",
            CurFYEn="2026-06-30",
            CurPerSt="2025-07-01",
            CurPerEn="2026-06-30",
            NxtFYSt="2026-07-01",
            NxtFYEn="2027-06-30",
            EPS="33.3",
            NP="86000000",
            DivAnn="7",
            NxFEPS="39.69",
            NxFNp="101000000",
            NxFOdP="164000000",
            NxFDivAnn="7",
            FNCEPS="",
            NxFNCEPS="",
        ),
        document("income-first", FNCEPS="31.9", FNCNP="81000000", FNCOdP="143000000"),
        document("dividend", DocType="DividendForecastRevision", FDivAnn="0.0"),
        document(
            "income-correction",
            time="19:00:00",
            FNCEPS="31.9",
            FNCNP="81000000",
            FNCOdP="144000000",
            FDivAnn="",
        ),
    ]


def decode(records: list[dict[str, Any]]) -> list[JQuantsFinancialSummary]:
    rows = [normalize_financial_summary(record) for record in records]
    assert all(row is not None for row in rows)
    return [row for row in rows if row is not None]


def snapshot(rows: list[JQuantsFinancialSummary]):
    rules = load_screening_rules()
    return build_financial_snapshot(
        1074, rows, None, rules.ttm, adjustment_events=(), asof_date=ASOF
    )


def test_document_streams_keep_zero_and_income_source(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    records = documents()
    # Repeated complete-range replacement preserves documents and coverage.
    store_jquants_fin_summaries(db, records, requested_start=date(2026, 8, 10), requested_end=ASOF)
    for _ in range(2):
        store_jquants_fin_summaries(
            db,
            records,
            requested_start=date(2026, 8, 10),
            requested_end=ASOF,
        )
    before = db.read_bytes()
    stored = read_fin_summaries(db, date(2026, 8, 10), ASOF)
    assert stored is not None
    assert len(stored) == 4
    assert snapshot(stored) == snapshot(decode(records))
    result = snapshot(stored)
    assert (result.dps_forecast_annual, result.dividend_yield) == (0, 0)
    assert result.per_forward == 1074 / 31.9
    assert result.earnings_forecast_source
    assert result.earnings_forecast_source["disclosure_number"] == "income-correction"
    assert result.dividend_forecast_source
    assert result.dividend_forecast_source["disclosure_number"] == "dividend"
    assert stored[0].eps_ttm == 33.3
    assert db.read_bytes() == before
    with closing(sqlite3.connect(db)) as conn, conn:
        values = conn.execute("SELECT document_payload FROM jquants_fin_summaries").fetchall()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jquants_fin_summaries)")}
    assert columns == {"ticker", "disclosed_at", "disclosure_number", "document_payload"}
    assert {json.loads(row[0])["DiscNo"]: json.loads(row[0]) for row in values} == {
        record["DiscNo"]: record for record in records
    }


def test_asof_excludes_later_revision() -> None:
    rows = project_financial_documents(decode(documents()), date(2026, 8, 31))
    income = latest_forecast_source(rows)
    dividend = latest_forecast_source(rows, dividend=True)
    assert income
    assert income.forecast_eps == 39.69
    assert dividend
    assert dividend.dps_forecast_annual == 7


@pytest.mark.parametrize("fields", [{"FEPS": "20"}, {"FEPS": "20", "FNP": "100"}])
def test_eps_does_not_require_ordinary_profit(fields: dict[str, str]) -> None:
    rows = decode([document("eps-only", **fields)])
    assert snapshot(rows).per_forward == 1074 / 20
    assert not snapshot(rows).forecast_special_gain_flag


@pytest.mark.parametrize("standard", ["JP", "IFRS", "US"])
def test_nonconsolidated_supplement_does_not_override_consolidated_basis(standard) -> None:
    records = documents()
    records[0]["DocType"] = f"FYFinancialStatements_Consolidated_{standard}"
    result = snapshot(decode(records))
    assert result.per_forward == 1074 / 39.69
    assert result.earnings_forecast_source
    assert result.earnings_forecast_source["basis"] == "consolidated"


def test_target_period_changes_before_numeric_availability() -> None:
    old = document(
        "old", day="2026-08-01", CurFYSt="2026-04-01", CurFYEn="2027-03-31", FEPS="20", FDivAnn="7"
    )
    new = document("short-fy", CurFYSt="2026-04-01", CurFYEn="2026-12-31", FEPS="", FDivAnn="")
    result = snapshot(decode([old, new]))
    assert result.per_forward is None
    assert result.dps_forecast_annual is None
    assert result.earnings_forecast_source
    assert result.earnings_forecast_source["period_end"] == "2026-12-31"
    assert result.earnings_forecast_source["status"] == "unresolved"


def test_unknown_present_slot_does_not_revive_old_forecast() -> None:
    old = document("old", day="2026-08-01", FEPS="20", FDivAnn="7")
    new = document("unknown", FEPS="NaN", FDivAnn="NaN")
    result = snapshot(decode([old, new]))
    assert result.per_forward is None
    assert result.dps_forecast_annual is None


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_same_time_forecasts_are_unresolved(reverse: bool) -> None:
    records = [
        document("later-id", FEPS="20", FDivAnn="7"),
        document("earlier-id", FEPS="30", FDivAnn="0"),
    ]
    if reverse:
        records.reverse()
    result = snapshot(decode(records))
    assert result.per_forward is None
    assert result.dps_forecast_annual is None
    assert result.earnings_forecast_source
    assert result.earnings_forecast_source["status"] == "unresolved"
    assert result.dividend_forecast_source
    assert result.dividend_forecast_source["status"] == "unresolved"


@pytest.mark.parametrize(
    "period",
    [{"CurFYSt": "", "CurFYEn": ""}, {"CurFYSt": "2026-10-01", "CurFYEn": "2026-03-31"}],
)
def test_missing_or_reversed_period_does_not_supply_forecasts(period: dict[str, str]) -> None:
    invalid = document("invalid-period", FEPS="99", FDivAnn="99", **period)
    result = snapshot(decode([invalid]))
    assert result.per_forward is None
    assert result.dps_forecast_annual is None
    assert snapshot([*decode(documents()), *decode([invalid])]).per_forward == 1074 / 31.9


def test_f_and_next_slots_are_retained_without_mixing() -> None:
    record = document(
        "both",
        FEPS="20",
        FNP="100",
        FOdP="90",
        NxFEPS="30",
        NxFNp="400",
        NxFOdP="500",
        NxtFYSt="2027-07-01",
        NxtFYEn="2028-06-30",
        FDivAnn="0",
        NxFDivAnn="7",
    )
    row = decode([record])[0]
    assert row.document_payload
    assert json.loads(row.document_payload)["NxFNp"] == "400"
    selected = latest_forecast_source(project_financial_documents([row], ASOF))
    assert selected
    assert (
        selected.forecast_eps,
        selected.forecast_profit,
        selected.forecast_ordinary_profit,
    ) == (20, 100, 90)
    assert snapshot([row]).forecast_special_gain_flag


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_actual_eps_is_unresolved_in_normalized_fy(reverse: bool) -> None:
    rows = decode(
        [
            document(
                "a",
                DocType="FYFinancialStatements_NonConsolidated_JP",
                CurFYSt="2025-07-01",
                CurFYEn="2026-06-30",
                EPS="20",
            ),
            document(
                "b",
                DocType="FYFinancialStatements_NonConsolidated_JP",
                CurFYSt="2025-07-01",
                CurFYEn="2026-06-30",
                EPS="30",
            ),
        ]
    )
    rows = [
        replace(
            row,
            fiscal_year_end=date(2026, 6, 30),
            period_start=date(2025, 7, 1),
            period_end=date(2026, 6, 30),
        )
        for row in rows
    ]
    if reverse:
        rows.reverse()
    assert _latest_actual_row(rows, field_names=("eps_ttm",)) is None
    assert (
        build_normalized_profit_signals(
            rows, (), ASOF, close=1074, ttm_rules=load_screening_rules().ttm
        ).normalized_per_3fy
        is None
    )


def test_same_time_different_docs_cannot_supply_capital_pair(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    records = [document("issued", ShOutFY="1000"), document("treasury", TrShFY="100")]
    store_jquants_fin_summaries(db, records, requested_start=date(2026, 9, 14), requested_end=ASOF)
    conn = open_connection(db)
    try:
        rows = read_financial_documents(conn, start=date(2026, 9, 14), end=ASOF)
        assert _resolve_capital_basis(rows).shares_ex_treasury is None
    finally:
        conn.close()


def test_financial_lake_contract_roundtrip_preserves_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    from hashlib import sha256

    from tests.helpers.lake_policy import narrow_release_policy

    from baibai_engine.storage.lake.duck import lake_session
    from baibai_engine.storage.lake.hydrate import hydrate_market_store
    from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
    from baibai_engine.storage.lake.reader import resolve_release
    from baibai_engine.storage.lake.release import create_release
    from baibai_engine.storage.lake.writer import (
        export_dataset_snapshot,
        sealed_sqlite_snapshot,
        validate_snapshot_parity,
    )

    narrow_release_policy(monkeypatch, datasets=("jquants.fin_summaries",))
    db = tmp_path / "source.sqlite"
    mirror = tmp_path / "mirror"
    store_jquants_fin_summaries(
        db, documents(), requested_start=date(2026, 8, 10), requested_end=ASOF
    )
    with sealed_sqlite_snapshot(sqlite_path=db, mirror_root=mirror) as sealed:
        exported = export_dataset_snapshot(
            dataset_name="jquants.fin_summaries",
            mirror_root=mirror,
            source_snapshot=sealed,
            producer_git_commit="a" * 40,
            created_at=datetime(2026, 10, 14, tzinfo=UTC),
        )
    assert exported.manifest.contract_version == 5
    assert exported.manifest.layer == "l2_processed"
    assert all(
        item.key.startswith("lake/l2/processed/")
        for partition in exported.manifest.partitions
        for item in partition.objects
    )
    validate_snapshot_parity(sqlite_path=db, mirror_root=mirror, manifest=exported.manifest)
    release_path, manifest = create_release(
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        release_id="financial-documents",
        created_at=datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert manifest.datasets["jquants.fin_summaries"].layer == "l2_processed"
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source, manifest.release_id, manifest_sha256=sha256(release_path.read_bytes()).hexdigest()
    )
    destination = tmp_path / "arrived.sqlite"
    # Hydration preserves adopted financial records; coverage belongs to the store skeleton.
    source_conn = sqlite3.connect(db)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
        destination_conn.execute("DELETE FROM jquants_fin_summaries")
        destination_conn.commit()
    finally:
        source_conn.close()
        destination_conn.close()
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=release,
            cache=LakeObjectCache(root=mirror, source=source),
            store=destination,
            dataset_names=("jquants.fin_summaries",),
        )
    original = read_fin_summaries(db, date(2026, 8, 10), ASOF)
    arrived = read_fin_summaries(destination, date(2026, 8, 10), ASOF)
    assert original == arrived
    assert arrived is not None
    assert len(arrived) == 4
    assert snapshot(arrived).dps_forecast_annual == 0


@pytest.mark.parametrize(
    ("old_basis", "new_basis"),
    [("NonConsolidated", "Consolidated"), ("Consolidated", "NonConsolidated")],
)
def test_basis_transition_does_not_relabel_old_primary_guidance(
    old_basis: str, new_basis: str
) -> None:
    old = document(
        "old-basis-fy",
        day="2026-08-10",
        DocType=f"FYFinancialStatements_{old_basis}_JP",
        CurFYSt="2025-07-01",
        CurFYEn="2026-06-30",
        CurPerSt="2025-07-01",
        CurPerEn="2026-06-30",
        NxtFYSt="2026-07-01",
        NxtFYEn="2027-06-30",
        NxFEPS="20",
        NxFNp="100",
        NxFOdP="90",
        NxFDivAnn="7",
    )
    new = document(
        "new-basis-q1",
        DocType=f"1QFinancialStatements_{new_basis}_JP",
        CurPerType="1Q",
        CurPerEn="2026-09-30",
        FEPS="",
        FNP="",
        FOdP="",
    )
    result = snapshot(decode([old, new]))
    assert result.per_forward is None
    assert not result.forecast_special_gain_flag
    # Issuer DPS does not acquire a consolidated/nonconsolidated accounting basis.
    assert result.dps_forecast_annual == 7
    projected = project_financial_documents(decode([old, new]), ASOF)
    old_source = next(row for row in projected if row.disclosure_number == "old-basis-fy")
    expected_old = "nonconsolidated" if old_basis == "NonConsolidated" else "consolidated"
    expected_new = "nonconsolidated" if new_basis == "NonConsolidated" else "consolidated"
    assert old_source.forecast_basis == expected_old
    assert old_source.forecast_target_basis == expected_new
    assert old_source.forecast_status == "basis_mismatch"


@pytest.mark.parametrize("reverse", [False, True])
def test_simultaneous_statement_basis_conflict_does_not_select_by_order(reverse: bool) -> None:
    records = [
        document("nc", DocType="1QFinancialStatements_NonConsolidated_JP", FEPS="20", FNCEPS="30"),
        document(
            "consolidated", DocType="1QFinancialStatements_Consolidated_JP", FEPS="20", FNCEPS="30"
        ),
    ]
    if reverse:
        records.reverse()
    assert snapshot(decode(records)).per_forward is None
    projected = project_financial_documents(decode(records), ASOF)
    assert {row.forecast_target_basis for row in projected} == {"basis_unresolved"}
    assert {row.forecast_basis for row in projected} == {"consolidated", "nonconsolidated"}


def actual_document(number: str, **fields: Any) -> dict[str, Any]:
    return document(
        number,
        day="2026-08-10",
        DocType="FYFinancialStatements_NonConsolidated_JP",
        CurFYSt="2025-07-01",
        CurFYEn="2026-06-30",
        CurPerSt="2025-07-01",
        CurPerEn="2026-06-30",
        EPS="20",
        NP="100",
        Sales="1000",
        **fields,
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_actual_dps_does_not_hide_agreed_profit_and_sales(reverse: bool) -> None:
    from baibai_engine.valuation.metrics.dividends import _resolve_dividend_carry
    from baibai_engine.valuation.metrics.periods import _ttm_value

    rows = decode([actual_document("paid", DivAnn="7"), actual_document("zero", DivAnn="0")])
    if reverse:
        rows.reverse()
    carry = _resolve_dividend_carry(rows, (), 100, ASOF)
    assert carry.dividend_yield is None
    assert carry.dps_actual_annual is None
    assert carry.basis == "unresolved_actual_dividend"
    rules = load_screening_rules().ttm
    assert _ttm_value(rows, "profit", rules)[0] == 100
    assert _ttm_value(rows, "sales", rules)[0] == 1000
    assert snapshot(rows).sales_ttm == 1000
    assert snapshot(rows).dps_actual_annual is None


@pytest.mark.parametrize("reverse", [False, True])
def test_unknown_time_actual_agreement_is_kept_and_conflict_is_unresolved(reverse: bool) -> None:
    records = [
        actual_document("unknown-time", DiscTime="", ShOutFY="1000", TrShFY="0"),
        actual_document("known-time", ShOutFY="1000", TrShFY="0"),
    ]
    unknown, known = decode(records)
    rows = [unknown, known]
    if reverse:
        rows.reverse()
    selected = _latest_actual_row(rows, field_names=("eps_ttm",))
    assert selected is not None
    assert selected.eps_ttm == 20
    assert _resolve_capital_basis(rows).shares_ex_treasury == 1000
    unknown = replace(unknown, eps_ttm=30, shares_outstanding=2000)
    contradictory = [unknown, known] if not reverse else [known, unknown]
    assert _latest_actual_row(contradictory, field_names=("eps_ttm",)) is None
    assert _resolve_capital_basis(contradictory).shares_ex_treasury is None


@pytest.mark.parametrize("reverse", [False, True])
def test_new_raw_unknown_time_forecasts_compare_each_field(reverse: bool) -> None:
    records = [
        document("unknown", time="", FEPS="20", FNP="100", FOdP="90", FDivAnn="7"),
        document("known", time="16:00:00", FEPS="20", FNP="110", FOdP="90", FDivAnn="0"),
    ]
    if reverse:
        records.reverse()
    result = snapshot(decode(records))
    assert result.per_forward == 1074 / 20
    assert result.dps_forecast_annual is None
    assert result.earnings_forecast_source
    assert result.earnings_forecast_source["status"] == "partially_unresolved"
    assert result.earnings_forecast_source["disclosed_time"] is None


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("with_older_reference", [False, True])
def test_conflicting_pre_split_reference_cannot_choose_share_basis(
    reverse: bool, with_older_reference: bool
) -> None:
    from baibai_engine.market.price_models import JQuantsAdjustmentFactorEvent
    from baibai_engine.valuation.metrics.capital import _normalize_summaries_with_status

    # Keep the conflicting reference period distinct from the later FY correction.
    # Otherwise that correction resolves the same cohort before reference selection.
    rows = decode(
        [actual_document("before-a", ShOutFY="1000"), actual_document("before-b", ShOutFY="500")]
    )
    rows = [
        replace(
            row,
            disclosed_at=date(2026, 7, 10),
            fiscal_period="3Q",
            period_end=date(2026, 3, 31),
        )
        for row in rows
    ]
    if with_older_reference:
        older = decode([actual_document("older", ShOutFY="1000")])[0]
        rows.insert(
            0,
            replace(
                older,
                disclosed_at=date(2026, 7, 1),
                fiscal_period="3Q",
                period_end=date(2026, 3, 31),
            ),
        )
    if reverse:
        rows.reverse()
    rows.extend(decode([actual_document("correction", ShOutFY="1000", TrShFY="0")]))
    events = [
        JQuantsAdjustmentFactorEvent(
            ticker="9999", traded_at=date(2026, 8, 1), adjustment_factor=0.5, ex_right_type="1"
        )
    ]
    result = _normalize_summaries_with_status(rows, events, ASOF)
    correction = result.summaries[-1]
    assert correction.shares_outstanding is None
    assert correction.eps_ttm is None
    capital = _resolve_capital_basis(
        result.summaries, capital_basis_barrier=result.capital_basis_barrier
    )
    assert capital.shares_ex_treasury is None
    assert capital.failure_reason == "indeterminate_share_basis"


def test_unknown_time_agreed_raw_guidance_is_order_invariant() -> None:
    records = [
        document("unknown", time="", FEPS="20", FNP="100", FOdP="90", FDivAnn="7"),
        document("known", FEPS="20", FNP="100", FOdP="90", FDivAnn="7"),
    ]
    result = snapshot(decode(records))
    assert result == snapshot(decode(list(reversed(records))))
    assert result.per_forward == 1074 / 20
    assert result.dps_forecast_annual == 7
    assert result.earnings_forecast_source
    assert result.earnings_forecast_source["status"] == "confirmed"
    assert result.earnings_forecast_source["disclosed_time"] is None


def test_later_partial_actual_does_not_revive_conflicting_old_dps() -> None:
    from baibai_engine.valuation.metrics.dividends import _resolve_dividend_carry

    rows = decode([actual_document("paid", DivAnn="7"), actual_document("zero", DivAnn="0")])
    rows = [replace(row, disclosed_at=date(2026, 7, 10)) for row in rows]
    rows.extend(decode([actual_document("later-earnings-only")]))
    assert _resolve_dividend_carry(rows, (), 100, ASOF).dps_actual_annual is None
    assert snapshot(rows).sales_ttm == 1000


def test_latest_actual_missing_fields_do_not_revive_older_revision() -> None:
    from baibai_engine.valuation.metrics.periods import _ttm_value

    old = actual_document("old-full")
    old["DiscDate"] = "2026-07-10"
    latest = actual_document("latest-partial", ShOutFY="1000")
    latest["NP"] = latest["Sales"] = ""
    rows = decode([old, latest])
    rules = load_screening_rules().ttm
    assert _ttm_value(rows, "profit", rules)[0] is None
    assert _ttm_value(rows, "sales", rules)[0] is None


@pytest.mark.parametrize("missing_time", [None, "", float("nan")])
def test_missing_sdk_time_cannot_become_a_known_revision(missing_time: Any) -> None:
    rows = decode([document("unknown", time=missing_time, FEPS="30"), document("known", FEPS="20")])
    assert rows[0].disclosed_time is None
    assert snapshot(rows).per_forward is None


@pytest.mark.parametrize("reverse", [False, True])
def test_complementary_earnings_documents_keep_independent_fields_and_sources(
    reverse: bool,
) -> None:
    records = [document("eps", FEPS="20"), document("profit", FNP="100", FOdP="90")]
    if reverse:
        records.reverse()
    result = snapshot(decode(records))
    assert result.per_forward == 1074 / 20
    assert result.forecast_special_gain_flag
    assert result.earnings_forecast_source
    assert result.earnings_forecast_source["forecast_eps_source_records"] == "eps"
    assert result.earnings_forecast_source["forecast_profit_source_records"] == "profit"
    assert result.earnings_forecast_source["forecast_ordinary_profit_source_records"] == "profit"


@pytest.mark.parametrize("reverse", [False, True])
def test_complementary_fy_eps_and_dps_keep_both_without_joining_capital_pair(reverse: bool) -> None:
    from baibai_engine.valuation.metrics.dividends import _latest_fy_revisions

    a, b = (
        actual_document("eps", ShOutFY="1000"),
        actual_document("payout", DivAnn="7", TrShFY="100"),
    )
    a["NP"] = a["Sales"] = ""
    b["EPS"] = b["NP"] = b["Sales"] = ""
    rows = decode([a, b] if not reverse else [b, a])
    fy = _latest_fy_revisions(rows)[0]
    assert fy.eps_ttm == 20
    assert fy.dps_actual_annual == 7
    assert fy.shares_outstanding is None or fy.treasury_shares is None
    assert _resolve_capital_basis(rows).shares_ex_treasury is None


@pytest.mark.parametrize("reverse", [False, True])
def test_unresolved_only_latest_period_cannot_revive_old_profit(reverse: bool) -> None:
    from baibai_engine.valuation.metrics.capital import _normalize_summaries_with_status
    from baibai_engine.valuation.metrics.periods import _ttm_value

    old = document(
        "old-fy",
        day="2025-08-10",
        DocType="FYFinancialStatements_NonConsolidated_JP",
        CurFYSt="2024-07-01",
        CurFYEn="2025-06-30",
        CurPerSt="2024-07-01",
        CurPerEn="2025-06-30",
        EPS="10",
        NP="100",
    )
    latest = [
        document(
            "latest-a",
            day="2026-08-10",
            DocType="FYFinancialStatements_NonConsolidated_JP",
            CurFYSt="2025-07-01",
            CurFYEn="2026-06-30",
            CurPerSt="2025-07-01",
            CurPerEn="2026-06-30",
            EPS="20",
            NP="200",
        ),
        document(
            "latest-b",
            day="2026-08-10",
            DocType="FYFinancialStatements_NonConsolidated_JP",
            CurFYSt="2025-07-01",
            CurFYEn="2026-06-30",
            CurPerSt="2025-07-01",
            CurPerEn="2026-06-30",
            EPS="30",
            NP="300",
        ),
    ]
    if reverse:
        latest.reverse()
    rows = decode([old, *latest])
    rules = load_screening_rules().ttm
    assert _ttm_value(rows, "profit", rules)[0] is None
    normalized = _normalize_summaries_with_status(rows, (), ASOF).summaries
    assert normalized[-1].unresolved_actual_fields
    assert _ttm_value(normalized, "profit", rules)[0] is None
    assert snapshot(rows).operating_profit is None


def test_explicit_unknown_forecast_slot_is_not_an_absent_complement() -> None:
    rows = decode(
        [
            document("unknown", time="", FEPS="NaN", FNP="100"),
            document("known", FEPS="20", FOdP="90"),
        ]
    )
    result = snapshot(rows)
    assert result.per_forward is None
    assert result.earnings_forecast_source
    assert result.earnings_forecast_source["forecast_eps_source_records"] == "known,unknown"


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_payment_allocations_cannot_select_split_adjustment_by_order(
    reverse: bool,
) -> None:
    from baibai_engine.market.price_models import JQuantsAdjustmentFactorEvent
    from baibai_engine.valuation.metrics.dividends import (
        _asof_basis_fy_dps,
        _latest_fy_revisions,
        _resolve_dividend_carry,
    )

    eps = actual_document("eps-only")
    eps["NP"] = eps["Sales"] = ""
    a, b = (
        actual_document("interim", DivAnn="7", Div2Q="7"),
        actual_document("year-end", DivAnn="7", DivFY="7"),
    )
    for record in (a, b):
        record["EPS"] = record["NP"] = record["Sales"] = ""
    records = [eps, *([b, a] if reverse else [a, b])]
    rows = decode(records)
    events = [
        JQuantsAdjustmentFactorEvent(
            ticker="9999", traded_at=date(2026, 1, 15), adjustment_factor=0.5, ex_right_type="1"
        )
    ]
    fy_rows = _latest_fy_revisions(rows)
    assert fy_rows[0].eps_ttm == 20
    assert fy_rows[0].dps_actual_annual == 7
    assert _asof_basis_fy_dps(fy_rows, events, ASOF) == {}
    assert _resolve_dividend_carry(rows, events, 100, ASOF).dividend_yield is None
    assert _resolve_dividend_carry(rows, (), 100, ASOF).dividend_yield == 0.07


@pytest.mark.parametrize("outside_day", ["2026-08-09", "2026-10-14"])
def test_outside_financial_payload_preserves_rows_and_coverage(
    tmp_path: Path, outside_day: str
) -> None:
    db = tmp_path / "market.sqlite"
    start = date(2026, 8, 10)
    store_jquants_fin_summaries(db, documents(), requested_start=start, requested_end=ASOF)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="outside the request"):
        store_jquants_fin_summaries(
            db,
            [document("unexpected", day=outside_day)],
            requested_start=start,
            requested_end=ASOF,
        )
    assert db.read_bytes() == before


def test_reversed_financial_range_does_not_create_store(tmp_path: Path) -> None:
    db = tmp_path / "absent.sqlite"
    with pytest.raises(ValueError, match="start must not follow end"):
        store_jquants_fin_summaries(db, [], requested_start=ASOF, requested_end=date(2026, 8, 10))
    assert not db.exists()


def test_financial_store_keeps_uninterpreted_provider_fields(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    record = document(
        "all-fields",
        EPS="not-a-number",
        ShOutFY="",
        FEPS="0",
        NxFEPS="20",
        ProviderExtraField="uninterpreted",
    )
    store_jquants_fin_summaries(db, [record], requested_start=date(2026, 9, 14), requested_end=ASOF)
    with closing(sqlite3.connect(db)) as conn:
        payload = conn.execute("SELECT document_payload FROM jquants_fin_summaries").fetchone()[0]
    assert json.loads(payload) == record
    rows = read_fin_summaries(db, date(2026, 9, 14), ASOF)
    assert rows is not None
    assert rows[0].eps_ttm is None
    assert rows[0].shares_outstanding is None
    assert rows[0].forecast_eps == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_same_document_identity_preserves_store(tmp_path, reverse):
    db = tmp_path / "market.sqlite"
    start = date(2026, 9, 14)
    store_jquants_fin_summaries(
        db, [document("held", FEPS="10")], requested_start=start, requested_end=ASOF
    )
    before = db.read_bytes()
    records = [document("duplicate", FEPS="20"), document("duplicate", FEPS="30")]
    if reverse:
        records.reverse()
    with pytest.raises(ValueError, match="conflicting financial document identity"):
        store_jquants_fin_summaries(db, records, requested_start=start, requested_end=ASOF)
    assert db.read_bytes() == before


@pytest.mark.parametrize("late_old_correction", [False, True])
def test_expired_dividend_guidance_cannot_replace_current_zero_actual(
    late_old_correction: bool,
) -> None:
    records = [
        document(
            "old-guidance",
            day="2024-09-18",
            CurFYSt="2023-10-01",
            CurFYEn="2024-09-30",
            FDivAnn="17.5",
        ),
        document(
            "zero-actual",
            day="2025-11-27",
            DocType="FYFinancialStatements_Consolidated_JP",
            CurFYSt="2024-10-01",
            CurFYEn="2025-09-30",
            CurPerSt="2024-10-01",
            CurPerEn="2025-09-30",
            NxtFYSt="2025-10-01",
            NxtFYEn="2026-09-30",
            DivAnn="0",
            NxFDivAnn="",
        ),
        document(
            "current-quarter",
            day="2026-02-13",
            DocType="1QFinancialStatements_Consolidated_JP",
            CurFYSt="2025-10-01",
            CurFYEn="2026-09-30",
            CurPerSt="2025-10-01",
            CurPerEn="2025-12-31",
            FDivAnn="",
        ),
    ]
    if late_old_correction:
        records.append(
            document(
                "late-old-actual-correction",
                day="2026-08-10",
                DocType="FYFinancialStatements_Consolidated_JP",
                CurFYSt="2023-10-01",
                CurFYEn="2024-09-30",
                CurPerSt="2023-10-01",
                CurPerEn="2024-09-30",
                DivAnn="30",
            )
        )
    rules = load_screening_rules()
    result = build_financial_snapshot(
        130, decode(records), None, rules.ttm, adjustment_events=(), asof_date=date(2026, 8, 11)
    )
    assert result.dps_forecast_annual is None
    assert result.dps_actual_annual == 0
    assert result.dividend_yield == 0


def test_actual_correction_does_not_withdraw_a_different_period_forecast() -> None:
    records = [
        document(
            "current-guidance",
            day="2026-05-15",
            CurFYSt="2026-04-01",
            CurFYEn="2027-03-31",
            FDivAnn="40",
        ),
        document(
            "prior-actual-correction",
            day="2026-05-20",
            DocType="FYFinancialStatements_Consolidated_JP",
            CurFYSt="2025-04-01",
            CurFYEn="2026-03-31",
            CurPerSt="2025-04-01",
            CurPerEn="2026-03-31",
            NxtFYSt="2026-04-01",
            NxtFYEn="2027-03-31",
            DivAnn="0",
            NxFDivAnn="",
        ),
    ]
    rules = load_screening_rules()
    result = build_financial_snapshot(
        1000, decode(records), None, rules.ttm, adjustment_events=(), asof_date=date(2026, 5, 20)
    )
    assert result.dps_actual_annual == 0
    assert result.dps_forecast_annual == 40
    assert result.dividend_yield == 0.04


@pytest.mark.parametrize("standard", ["JP", "IFRS", "US"])
def test_native_primary_financial_values_reach_l2_without_nc_substitution(tmp_path, standard):
    record = documents()[0]
    record.update(
        DocType=f"FYFinancialStatements_Consolidated_{standard}",
        Sales="1000",
        OP="100",
        CFO="120",
        TA="2000",
        Eq="800",
        CashEq="300",
        NCSales="9999",
        NCOP="999",
        NCTA="9998",
        NCEq="9997",
    )
    path = tmp_path / "market.sqlite"
    start = date(2026, 8, 10)
    store_jquants_fin_summaries(path, [record], requested_start=start, requested_end=ASOF)
    rows = read_fin_summaries(path, start, ASOF)
    assert rows is not None
    assert len(rows) == 1
    row = rows[0]
    assert row.document_type == record["DocType"]
    assert (
        row.sales,
        row.operating_profit,
        row.cfo,
        row.total_assets,
        row.equity,
        row.cash_eq,
    ) == (
        1000,
        100,
        120,
        2000,
        800,
        300,
    )
    assert json.loads(row.document_payload) == record
    result = snapshot(rows)
    assert result.sales_ttm == 1000
    assert result.operating_profit_ttm == 100
    assert result.ocf_ttm == 120
    assert result.total_assets == 2000
    assert result.cash_eq == 300
    # A missing primary fact is not replaced by a supplemental individual basis.
    record.update(Sales="", OP="")
    missing = snapshot(decode([record]))
    assert missing.sales_ttm is None
    assert missing.operating_profit_ttm is None


@pytest.mark.parametrize("number", [None, "", "   "])
def test_native_identity_is_required_before_replacing_history(tmp_path, number):
    db = tmp_path / "market.sqlite"
    store_jquants_fin_summaries(
        db, documents(), requested_start=date(2026, 8, 10), requested_end=ASOF
    )
    before = db.read_bytes()
    record = document("unused", FNCEPS="30")
    if number is None:
        record.pop("DiscNo")
    else:
        record["DiscNo"] = number
    with pytest.raises(JQuantsProviderError, match="native DiscNo"):
        normalize_financial_summary(record)
    with pytest.raises(JQuantsProviderError, match="native DiscNo"):
        store_jquants_fin_summaries(
            db, [record], requested_start=date(2026, 8, 10), requested_end=ASOF
        )
    assert db.read_bytes() == before


@pytest.mark.parametrize("missing_field", ["CurPerSt", "CurPerEn"])
def test_prior_year_comparison_requires_actual_period_dates(missing_field):
    from baibai_engine.valuation.metrics.periods import _prior_year_summary

    old = actual_document("prior")
    old.update(
        DiscDate="2025-08-10",
        CurFYSt="2024-07-01",
        CurFYEn="2025-06-30",
        CurPerSt="2024-07-01",
        CurPerEn="2025-06-30",
    )
    current = actual_document("current")
    rules = load_screening_rules().ttm
    assert _prior_year_summary(decode([old, current]), rules, field_name="eps_ttm") is not None
    current[missing_field] = ""
    assert _prior_year_summary(decode([old, current]), rules, field_name="eps_ttm") is None


@pytest.mark.parametrize("dps", [0, 7])
def test_actual_dividend_does_not_invent_missing_accrual_period(dps):
    from baibai_engine.valuation.metrics.dividends import _resolve_dividend_carry

    record = actual_document("missing-period", DivAnn=str(dps))
    record["CurPerSt"] = ""
    result = _resolve_dividend_carry(decode([record]), (), 100, ASOF)
    assert result.dps_actual_annual == (0 if dps == 0 else None)
    assert result.dividend_yield == (0 if dps == 0 else None)
    assert result.basis == ("actual_reported" if dps == 0 else "unresolved_actual_dividend")


@pytest.mark.parametrize("fields", [{}, {"FSales": "1000", "FOP": "100"}])
def test_annual_revision_without_point_estimate_does_not_revive_old_earnings(fields):
    old = document("old", day="2026-08-01", FEPS="20", FDivAnn="7")
    revised = document("range-or-undecided", **fields)
    result = snapshot(decode([old, revised]))
    assert result.per_forward is None
    assert result.dps_forecast_annual == 7
    assert result.earnings_forecast_source["status"] == "unresolved"
    assert result.earnings_forecast_source["disclosure_number"] == "range-or-undecided"


@pytest.mark.parametrize("period_type", ["2Q", "FY"])
@pytest.mark.parametrize("prefix", ["F", "FNC"])
def test_half_year_only_revision_keeps_annual_earnings(period_type, prefix):
    old = document("old", day="2026-08-01", FEPS="20")
    half_year = document("half", CurPerType=period_type, **{prefix + "EPS2Q": "5"})
    assert snapshot(decode([old, half_year])).per_forward == 1074 / 20


@pytest.mark.parametrize("fields", [{}, {"FDiv2Q": "3"}, {"FDiv2Q": "3", "FDivFY": "4"}])
def test_dividend_revision_without_annual_value_does_not_invent_or_revive_it(fields):
    old = document("old", day="2026-08-01", FEPS="20", FDivAnn="7")
    revised = document("undecided-dividend", DocType="DividendForecastRevision", **fields)
    result = snapshot(decode([old, revised]))
    assert result.dps_forecast_annual is None
    assert result.per_forward == 1074 / 20
    assert result.dividend_forecast_source["status"] == "unresolved"


@pytest.mark.parametrize("reverse", [False, True])
def test_same_time_unknown_revision_cannot_be_hidden_by_numeric_document(reverse):
    records = [document("numeric", FEPS="20"), document("unresolved")]
    if reverse:
        records.reverse()
    assert snapshot(decode(records)).per_forward is None


def test_new_numeric_guidance_resolves_previous_unknown_revision():
    records = [
        document("old", day="2026-08-01", FEPS="20"),
        document("unresolved"),
        document("new", day="2026-10-01", FEPS="30"),
    ]
    assert snapshot(decode(records)).per_forward == 1074 / 30


@pytest.mark.parametrize(
    "field,value",  # noqa: PT006 - preserve the existing test parameter spelling
    [("CurPerSt", ""), ("CurPerEn", ""), ("CurPerSt", "2027-01-01")],
)
@pytest.mark.parametrize("dps", [0, 7])
def test_actual_dividend_period_validity_agrees_with_forward_measurement(
    tmp_path, field, value, dps
):
    from baibai_engine.screening.calibration.forward import _asof_basis_dividend, _load_fy_dividends
    from baibai_engine.valuation.metrics.dividends import _resolve_dividend_carry

    record = actual_document("invalid-period", DivAnn=str(dps))
    record[field] = value
    path = tmp_path / "market.sqlite"
    store_jquants_fin_summaries(
        path, [record], requested_start=date(2026, 8, 10), requested_end=ASOF
    )
    with closing(sqlite3.connect(path)) as connection:
        observations = _load_fy_dividends(connection, "9999", cutoff=ASOF)
    assert len(observations) == 1
    expected = 0 if dps == 0 else None
    assert _asof_basis_dividend(observations[0], (), basis_date=ASOF) == expected
    assert _resolve_dividend_carry(decode([record]), (), 100, ASOF).dps_actual_annual == expected


@pytest.mark.parametrize("new_basis", ["Consolidated", "NonConsolidated"])
@pytest.mark.parametrize("late_old_correction", [False, True])
@pytest.mark.parametrize("revision", [False, True])
def test_short_fiscal_year_uses_current_statement_basis(new_basis, late_old_correction, revision):
    old_basis = "NonConsolidated" if new_basis == "Consolidated" else "Consolidated"
    old = document(
        "old-quarter",
        day="2026-08-10",
        DocType=f"1QFinancialStatements_{old_basis}_JP",
        CurPerType="1Q",
        CurFYSt="2026-04-01",
        CurFYEn="2027-03-31",
        CurPerSt="2026-04-01",
        CurPerEn="2026-06-30",
    )
    current = document(
        "short-year",
        day="2027-02-10",
        DocType=f"FYFinancialStatements_{new_basis}_JP",
        CurFYSt="2026-04-01",
        CurFYEn="2026-12-31",
        CurPerSt="2026-04-01",
        CurPerEn="2026-12-31",
        NxtFYSt="2027-01-01",
        NxtFYEn="2027-12-31",
        NxFEPS="20",
    )
    records = [old, current]
    if late_old_correction:
        records.append({**old, "DiscNo": "old-correction", "DiscDate": "2027-02-12"})
    if revision:
        records.append(
            document(
                "new-revision",
                day="2027-02-13",
                CurFYSt="2027-01-01",
                CurFYEn="2027-12-31",
                CurPerSt="2027-01-01",
                CurPerEn="2027-12-31",
                FEPS="25",
            )
        )
    for ordered in (records, list(reversed(records))):
        projected = project_financial_documents(decode(ordered), date(2027, 2, 15))
        source = latest_forecast_source(projected)
        assert source is not None
        assert source.forecast_eps == (25 if revision else 20)
        assert source.forecast_target_basis == new_basis.lower()
        assert source.disclosure_number == ("new-revision" if revision else "short-year")
        result = build_financial_snapshot(
            1000,
            decode(ordered),
            None,
            load_screening_rules().ttm,
            adjustment_events=(),
            asof_date=date(2027, 2, 15),
        )
        assert result.per_forward == 1000 / (25 if revision else 20)


@pytest.mark.parametrize("different_operand", [None, 0, 1])
@pytest.mark.parametrize("current_basis", ["Consolidated", "NonConsolidated"])
def test_ttm_does_not_add_different_statement_bases(different_operand, current_basis):
    from baibai_engine.market.metric_quality import TTMQuality
    from baibai_engine.valuation.metrics.periods import _ttm_value

    periods = [
        ("prior-quarter", "2025-08-10", "2025-04-01", "2025-06-30", "2026-03-31", "1Q", "20"),
        ("prior-year", "2026-05-10", "2025-04-01", "2026-03-31", "2026-03-31", "FY", "100"),
        ("current-quarter", "2026-08-10", "2026-04-01", "2026-06-30", "2027-03-31", "1Q", "30"),
    ]
    records = []
    for index, (number, day, start, end, fy_end, period, value) in enumerate(periods):
        basis = current_basis
        if index == different_operand:
            basis = "NonConsolidated" if current_basis == "Consolidated" else "Consolidated"
        records.append(
            document(
                number,
                day=day,
                DocType=f"{period}FinancialStatements_{basis}_JP",
                CurFYSt=start,
                CurFYEn=fy_end,
                CurPerSt=start,
                CurPerEn=end,
                CurPerType=period,
                Sales=value,
                OP=value,
                NP=value,
                CFO=value,
            )
        )
    expected = (
        (110.0, TTMQuality.EXACT) if different_operand is None else (None, TTMQuality.UNAVAILABLE)
    )
    for rows in (records, list(reversed(records))):
        decoded = decode(rows)
        for field in ("sales", "operating_profit", "profit", "cfo"):
            assert _ttm_value(decoded, field, load_screening_rules().ttm) == expected
        # Single-period reported facts remain usable; no raw value is rewritten.
        assert {row.sales for row in decoded} == {20, 100, 30}


@pytest.mark.parametrize("new_basis", ["Consolidated", "NonConsolidated"])
@pytest.mark.parametrize("late_correction", [False, True])
def test_financial_yoy_does_not_compare_different_scopes_but_keeps_issuer_shares(
    new_basis, late_correction
):
    old_basis = "NonConsolidated" if new_basis == "Consolidated" else "Consolidated"
    old = document(
        "prior-quarter",
        day="2025-08-10",
        DocType=f"1QFinancialStatements_{old_basis}_JP",
        CurPerType="1Q",
        CurFYSt="2025-04-01",
        CurFYEn="2026-03-31",
        CurPerSt="2025-04-01",
        CurPerEn="2025-06-30",
        EPS="10",
        Sales="100",
        CFO="20",
        OP="30",
        ShOutFY="1000",
        TrShFY="0",
    )
    current = document(
        "current-quarter",
        day="2026-08-10",
        DocType=f"1QFinancialStatements_{new_basis}_JP",
        CurPerType="1Q",
        CurFYSt="2026-04-01",
        CurFYEn="2027-03-31",
        CurPerSt="2026-04-01",
        CurPerEn="2026-06-30",
        EPS="20",
        Sales="200",
        CFO="40",
        OP="60",
        ShOutFY="900",
        TrShFY="0",
    )
    records = [old, current]
    if late_correction:
        # Do not search backwards past a newer contrary basis for a comparable row.
        earlier = {**old, "DocType": f"1QFinancialStatements_{new_basis}_JP"}
        correction = {**old, "DiscNo": "prior-correction", "DiscDate": "2026-08-12"}
        records = [earlier, current, correction]
    for ordered in (records, list(reversed(records))):
        result = build_financial_snapshot(
            1000,
            decode(ordered),
            None,
            load_screening_rules().ttm,
            adjustment_events=(),
            asof_date=date(2026, 8, 15),
        )
        assert result.eps_yoy is None
        assert result.sales_yoy is None
        assert result.cfo_yoy is None
        assert result.operating_profit_yoy is None
        assert result.operating_profit_loss_narrowing is None
        assert result.net_share_change_yoy == pytest.approx(-0.1)
        assert result.sales == 200


@pytest.mark.parametrize("dps", [0, 7])
def test_tied_fy_period_conflict_does_not_select_by_input_order(dps):
    from baibai_engine.valuation.metrics.dividends import _latest_fy_revisions

    first = actual_document("full-year", DivAnn=str(dps), ShOutFY="1000", TrShFY="0")
    short = {**first, "DiscNo": "short-year", "CurPerSt": "2025-10-01", "EPS": "40"}
    for records in ([first, short], [short, first]):
        rows = decode(records)
        selected = _latest_fy_revisions(rows)
        assert len(selected) == 1
        assert selected[0].eps_ttm is None
        assert selected[0].dps_actual_annual == (0 if dps == 0 else None)
        assert selected[0].shares_outstanding is None
        assert {row.eps_ttm for row in rows} == {20, 40}


def test_tied_fy_basis_conflict_preserves_independent_issuer_dividend():
    from baibai_engine.valuation.metrics.dividends import _latest_fy_revisions

    individual = actual_document("individual", DivAnn="7")
    consolidated = {
        **individual,
        "DiscNo": "group",
        "DocType": "FYFinancialStatements_Consolidated_JP",
    }
    for records in ([individual, consolidated], [consolidated, individual]):
        selected = _latest_fy_revisions(decode(records))
        assert selected[0].eps_ttm is None
        assert selected[0].dps_actual_annual == 7


@pytest.mark.parametrize("problem", ["missing", "short", "inverted", "future", "basis"])
@pytest.mark.parametrize("affected_index", [0, 2])
def test_normalized_earnings_requires_comparable_completed_years(problem, affected_index):
    records = [
        document(
            f"year-{year}",
            day=f"{year}-05-15",
            DocType="FYFinancialStatements_Consolidated_JP",
            CurPerType="FY",
            CurFYSt=f"{year - 1}-04-01",
            CurFYEn=f"{year}-03-31",
            CurPerSt=f"{year - 1}-04-01",
            CurPerEn=f"{year}-03-31",
            EPS="20",
        )
        for year in range(2022, 2027)
    ]
    rules = load_screening_rules().ttm
    baseline = build_normalized_profit_signals(
        decode(records), (), date(2026, 6, 1), close=1000, ttm_rules=rules
    )
    assert baseline.normalized_per_3fy == 50
    assert baseline.normalized_per_5fy == 50
    affected = records[affected_index]
    year = 2022 + affected_index
    if problem == "missing":
        affected["CurPerSt"] = ""
    elif problem == "short":
        affected["CurPerSt"] = f"{year - 1}-10-01"
    elif problem == "inverted":
        affected["CurPerSt"] = f"{year}-04-01"
    elif problem == "future":
        affected["CurPerEn"] = f"{year + 1}-03-31"
    else:
        affected["DocType"] = "FYFinancialStatements_NonConsolidated_JP"
    for ordered in (records, list(reversed(records))):
        result = build_normalized_profit_signals(
            decode(ordered), (), date(2026, 6, 1), close=1000, ttm_rules=rules
        )
        assert result.normalized_per_3fy == (50 if affected_index == 0 else None)
        assert result.normalized_per_5fy is None
