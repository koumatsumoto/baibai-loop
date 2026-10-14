"""Minimal constructed document fixtures; no provider response bodies."""

from __future__ import annotations

import json
import sqlite3
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
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.sqlite_coverage.required_fields import required_field_coverage
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
    # A full-market acquisition creates coverage; an issuer repair cannot claim it.
    store_jquants_fin_summaries(db, records, requested_start=date(2026, 8, 10), requested_end=ASOF)
    for _ in range(2):
        store_jquants_fin_summaries(
            db,
            records,
            requested_start=date(2026, 8, 10),
            requested_end=ASOF,
            requested_ticker="9999",
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
    with sqlite3.connect(db) as conn:
        values = conn.execute(
            "SELECT forecast_eps, forecast_profit, forecast_ordinary_profit, dps_forecast_annual FROM jquants_fin_summaries"
        ).fetchall()
    assert values == [(None, None, None, None)] * 4


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


def test_nonconsolidated_supplement_does_not_override_consolidated_basis() -> None:
    records = documents()
    records[0]["DocType"] = "FYFinancialStatements_Consolidated_IFRS"
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
    assert result.earnings_forecast_source["status"] == "missing"


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


def test_legacy_stays_unconfirmed_and_cannot_override_confirmed_period() -> None:
    legacy = JQuantsFinancialSummary(
        ticker="9999", disclosed_at=ASOF, forecast_eps=99, dps_forecast_annual=99
    )
    assert snapshot([legacy]).per_forward == 1074 / 99
    assert snapshot([legacy]).earnings_forecast_source == {
        "status": "legacy_unknown",
        "disclosure_number": None,
        "disclosed_at": ASOF.isoformat(),
        "disclosed_time": None,
        "document_type": None,
        "period_start": None,
        "period_end": None,
        "basis": None,
    }
    assert snapshot([*decode(documents()), legacy]).per_forward == 1074 / 31.9


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
    assert build_normalized_profit_signals(rows, (), ASOF, close=1074).normalized_per_3fy is None


def test_same_time_different_docs_cannot_supply_capital_pair(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    records = [document("issued", ShOutFY="1000"), document("treasury", TrShFY="100")]
    store_jquants_fin_summaries(db, records, requested_start=date(2026, 9, 14), requested_end=ASOF)
    conn = open_connection(db)
    try:
        conn.execute(
            "INSERT INTO jquants_master_snapshots(snapshot_date,ticker,name,market,sector_33,is_common_stock) VALUES (?,?,?,?,?,?)",
            (ASOF.isoformat(), "9999", "constructed", "STANDARD", "情報・通信業", 1),
        )
        rows = read_financial_documents(conn, start=date(2026, 9, 14), end=ASOF)
        assert _resolve_capital_basis(rows).shares_ex_treasury is None
        coverage = required_field_coverage(conn, start=date(2026, 9, 14), asof=ASOF)
        assert coverage.population_tickers == 1
        assert coverage.market_cap_required_fields_tickers == 0
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
    from baibai_engine.storage.lake.release import create_l1_release
    from baibai_engine.storage.lake.writer import (
        export_legacy_sqlite,
        sealed_sqlite_snapshot,
        validate_legacy_parity,
    )

    narrow_release_policy(monkeypatch, datasets=("jquants.fin_summaries",))
    db = tmp_path / "source.sqlite"
    mirror = tmp_path / "mirror"
    store_jquants_fin_summaries(
        db, documents(), requested_start=date(2026, 8, 10), requested_end=ASOF
    )
    with sealed_sqlite_snapshot(sqlite_path=db, mirror_root=mirror) as sealed:
        exported = export_legacy_sqlite(
            dataset_name="jquants.fin_summaries",
            mirror_root=mirror,
            source_snapshot=sealed,
            producer_git_commit="a" * 40,
            created_at=datetime(2026, 10, 14, tzinfo=UTC),
        )
    assert exported.manifest.contract_version == 2
    validate_legacy_parity(sqlite_path=db, mirror_root=mirror, manifest=exported.manifest)
    release_path, manifest = create_l1_release(
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        release_id="financial-documents",
        created_at=datetime(2026, 10, 14, tzinfo=UTC),
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source, manifest.release_id, manifest_sha256=sha256(release_path.read_bytes()).hexdigest()
    )
    destination = tmp_path / "arrived.sqlite"
    # Hydration fills typed rows; source coverage belongs to the store skeleton.
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
