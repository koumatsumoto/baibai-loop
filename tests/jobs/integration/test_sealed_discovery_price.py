"""実native投影→有限の探索→実queryが返した個社をCaseへ。外部store/実口座は使わない。"""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.engine.machine_analysis.screening.test_full_fy_fallback import documents
from tests.helpers.lake_release import market_store
from tests.helpers.master import insert_master_rows
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.screening_sqlite import insert_financial_record

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.price_native import PriceAttempt, PriceL1
from baibai_engine.investment_judgment.workflow.case_screening import read_case_screening
from baibai_engine.investment_judgment.workflow.selection import ResearchRequest
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.machine_analysis.generation.inputs import (
    read_and_generate_models,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DISCOVERY_V2,
    discovery_publication_id,
)
from baibai_engine.machine_analysis.screening.discovery_index import read_discovery_index
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_jobs.integration.models import _screening_stream
from baibai_jobs.integration.price_native import model_input


@pytest.mark.parametrize(
    ("held", "receipt_hour_utc", "expected_status"),
    [(False, 10, "ok"), (True, 10, "partial"), (False, 0, "unconfirmed")],
)
def test_real_native_close_only_and_failed_recheck_reach_nonfirst_value_case(
    tmp_path, held, receipt_hour_utc, expected_status
):
    day = date(2025, 8, 29)
    at = datetime(2025, 8, 29, receipt_hour_utc, tzinfo=UTC)
    data = {
        "data": [
            {"Code": "10010", "Date": day.isoformat(), "C": 510},
            {"Code": "10020", "Date": day.isoformat(), "C": 710},
        ]
    }
    store = MemoryR2Store()
    native = PriceL1(store, tmp_path / "native")
    native.initialize()
    native.commit(
        [
            PriceAttempt(
                "adopted",
                day,
                at,
                (
                    JQuantsPage(
                        "daily_bars",
                        {"date": day.isoformat()},
                        at,
                        json.dumps(data).encode(),
                    ),
                ),
            )
        ]
    )
    attempted = at + timedelta(minutes=1)
    if held:
        native.commit(
            [
                PriceAttempt(
                    "incomplete",
                    day,
                    attempted,
                    (
                        JQuantsPage(
                            "daily_bars",
                            {"date": day.isoformat()},
                            attempted,
                            json.dumps({**data, "pagination_key": "never_received"}).encode(),
                        ),
                    ),
                )
            ]
        )
    market = market_store(tmp_path / "market", daily_bars=())
    with sqlite3.connect(market) as c:
        c.execute("INSERT INTO jquants_market_calendar VALUES (?,?)", (day.isoformat(), "1"))
        insert_master_rows(
            c,
            [
                (day.isoformat(), t, "合成企業", "Prime", "情報・通信業", "011")
                for t in ("1000", "1001", "1002")
            ],
        )
        for doc in documents():
            insert_financial_record(
                c,
                {
                    "Code": doc.ticker + "0",
                    "DiscDate": doc.disclosed_at.isoformat(),
                    "DiscTime": "15:00:00",
                    "DiscNo": doc.disclosure_number,
                    "DocType": doc.document_type,
                    "CurPerType": doc.fiscal_period,
                    "CurFYEn": doc.fiscal_year_end.isoformat(),
                    "CurPerSt": doc.period_start.isoformat(),
                    "CurPerEn": doc.period_end.isoformat(),
                    "Sales": doc.sales,
                    "OP": doc.operating_profit,
                    "NP": doc.profit,
                    "EPS": doc.cumulative_eps,
                    "BPS": doc.bps,
                    "ShOutFY": doc.shares_outstanding,
                    "TrShFY": doc.treasury_shares,
                },
            )
    fixed = model_input(store, native.fixed(), as_of=day, evaluated_at=attempted, market=market)
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "seal") as seal:
        stream = read_and_generate_models(
            generation_id="native-finite",
            include_session_prices=True,
            market_snapshot=seal,
            price_identity=fixed,
            market_release=InputRelease(release_id="fixture", manifest_sha256="2" * 64),
            economic_input=None,
            as_of=day,
            evaluation_time=attempted,
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("1000", "1001", "1002"),
        )
    # A planned third stock whose models were not produced still owns an explicit row.
    source = stream.models
    stream = replace(stream, models=(m for m in source if m.subject != "1002"))
    models, select = _screening_stream(stream, DISCOVERY_V2)
    runs = tmp_path / "runs"
    ModelGenerationStore(runs).publish_stream(replace(stream, models=models))
    result = select()
    key = discovery_publication_id(stream.metadata.generation_id, DISCOVERY_V2)
    output = ScreeningSelectionStore(runs)
    # The model-less company cannot be removed from both self-reported collections.
    with pytest.raises(ValueError, match="saved stock plan"):
        output.publish(
            key,
            replace(
                result,
                universe_tickers=result.universe_tickers[:-1],
                result=replace(result.result, rows=result.result.rows[:-1]),
            ),
        )
    output.publish(key, result)
    index = read_discovery_index(path=runs, selection_id=key)
    assert index.universe_tickers == ("1000", "1001", "1002")
    assert index.rows[-1]["models_recorded"] is False
    assert index.rows[-1]["execution_price"] == {"day_state": "mixed", "raw_close_yen": 710.0}
    assert index.price_session.status == expected_status
    assert index.price_session.recorded_at == at
    assert result.result.price_coverage()["valid_count"] == (2 if expected_status == "ok" else 0)
    assert result.price_inputs is None  # Not a parallel persistent TA price summary.
    actual = load_generated_selection_context(
        request=ResearchRequest(scope="discover_new", text="当時の報告利益を比べる"),
        generation_id=None,
        screening_selection_id=key,
        research_as_of=day,
        known_at=index.published_at,
        runs_path=runs,
        db_path=tmp_path / "app",
        discovery_question="earning_power",
    )
    (positive,) = actual["discovery"]["rows"]
    ticker = positive["ticker"]
    assert ticker == "1001"
    assert ticker != index.universe_tickers[0]
    row = positive["observations"]
    assert row["execution_price"] == {"day_state": "mixed", "raw_close_yen": 510.0}
    assert row["technical_state"] == "unavailable"
    assert "earning_power" in row["matched_paths"]["financial"]
    assert actual["price_session"]["status"] == index.price_session.status
    _, case = read_case_screening(key, subjects=(ticker,), known_at=index.published_at, path=runs)
    assert case.result.rows == (row,)
    assert case.result.price_session == index.price_session
    assert case.universe_tickers == index.universe_tickers
