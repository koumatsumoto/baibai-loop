"""公開実原本と有限反例。外部取得・本番storeの更新は行わない。"""

import hashlib
import json
import zipfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.lake_release import publish_market_fixture

from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot, ModelInputSlice
from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.generation import generate_company_growth
from baibai_engine.machine_analysis.models.company_growth.models import CompanyGrowth, InputRelease
from baibai_engine.machine_analysis.observations.financials.edinet_comparison_reader import (
    read_comparison_population,
)
from baibai_engine.machine_analysis.observations.financials.edinet_comparisons import (
    extract_comparisons,
)
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    normalize_financial_summary,
)
from baibai_engine.machine_analysis.observations.financials.fixed_financial_reader import (
    read_fixed_financial_documents,
)
from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_fin_summaries
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

FIXTURES = Path(__file__).parents[4] / "fixtures" / "edinet" / "comparisons"
ASOF = date(2026, 10, 30)
RELEASE = InputRelease(release_id="synthetic-comparisons", manifest_sha256="a" * 64)


def archive(doc="S100YRSR", replace=None):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        for suffix in [".xbrl", "_pre.xml"]:
            content = (FIXTURES / (doc + suffix)).read_bytes()
            for before, after in (replace or {}).items():
                content = content.replace(before, after)
            entry = zipfile.ZipInfo(
                "XBRL/PublicDoc/" + doc + suffix, date_time=(2000, 1, 1, 0, 0, 0)
            )
            bundle.writestr(entry, content)
    return stream.getvalue()


def metadata(doc="S100YRSR"):
    return json.loads((FIXTURES / (doc + ".json")).read_text())


def extract(doc="S100YRSR", **kwargs):
    row = metadata(doc)
    return extract_comparisons(
        content=archive(doc, **kwargs),
        ticker=row["secCode"][:-1],
        doc_id=doc,
        submitted=row["submitDateTime"],
        document_type=row["docTypeCode"],
    )


def seed(path, doc="S100YRSR"):
    row = metadata(doc)
    day = date.fromisoformat(row["submitDateTime"][:10])
    store_edinet_documents(path, day, [row], response_json=json.dumps({"results": [row]}))
    with closing(open_connection(path)) as connection:
        retain_source_archive(connection, doc, 1, archive(doc), stored_on=ASOF)
    return row


@pytest.mark.parametrize(
    ("doc", "metric", "prior", "current", "start", "end", "precision"),
    [
        ("S100YRSR", "sales", "3126231000", "3525761000", "2024-05-01", "2025-04-30", "-3"),
        (
            "S100YRSR",
            "operating_profit",
            "1232568000",
            "1500178000",
            "2024-05-01",
            "2025-04-30",
            "-3",
        ),
        ("S100YV6Y", "sales", "1048867000000", "1000495000000", "2025-01-01", "2025-06-30", "-6"),
        (
            "S100YV6Y",
            "operating_profit",
            "616882000000",
            "618713000000",
            "2025-01-01",
            "2025-06-30",
            "-6",
        ),
        (
            "S100YV6Y",
            "parent_profit",
            "223527000000",
            "263147000000",
            "2025-01-01",
            "2025-06-30",
            "-6",
        ),
    ],
)
def test_native_pairs_keep_precision_period_scope_and_locator(
    doc, metric, prior, current, start, end, precision
):
    result = extract(doc)
    assert result.reason is None
    pair = next(p for p in result.pairs if p.metric == metric)
    assert pair.reason is None
    assert pair.comparative.amount == prior
    assert pair.current.amount == current
    assert (pair.comparative.period_start.isoformat(), pair.comparative.period_end.isoformat()) == (
        start,
        end,
    )
    assert pair.current.decimals == pair.comparative.decimals == precision
    assert pair.comparative.currency == "JPY"
    assert pair.comparative.presentation_role.endswith("StatementOfProfitOrLossIFRS")
    assert pair.comparative.locators
    assert result.source_sha256 == hashlib.sha256(archive(doc)).hexdigest()
    assert result.source_submit_datetime.utcoffset() == timedelta(hours=9)
    if doc == "S100YRSR":
        assert result.statement_basis == "nonconsolidated"
        assert not any(p.metric == "parent_profit" for p in result.pairs)
    else:
        assert result.statement_basis == "consolidated"
        assert pair.current.period_end == date(2026, 6, 30)
        assert metadata(doc)["periodEnd"] == "2026-12-31"


@pytest.mark.parametrize(
    ("replace", "reason"),
    [
        ({b">IFRS<": b">JP<"}, "comparison_accounting_standard_unsupported"),
        ({b">IFRS<": b">unknown<"}, "comparison_accounting_standard_unsupported"),
        (
            {b"<xbrli:endDate>2025-04-30": b"<xbrli:endDate>2025-03-31"},
            "comparison_periods_incomparable",
        ),
    ],
)
def test_unknown_or_changed_native_contract_is_not_inferred(replace, reason):
    assert extract(replace=replace).reason == reason


def test_q1_is_not_inferred_from_supported_annual_source():
    row = metadata()
    result = extract_comparisons(
        content=archive(),
        ticker="436A",
        doc_id="S100YRSR",
        submitted=row["submitDateTime"],
        document_type="140",
    )
    assert result.reason == "comparison_document_type_unsupported"
    assert not result.pairs


def test_same_basis_partial_conflict_does_not_discard_independent_sales():
    original = (FIXTURES / "S100YRSR.xbrl").read_text()
    start = original.index("<jpigp_cor:OperatingProfitLossIFRS")
    stop = original.index("</jpigp_cor:OperatingProfitLossIFRS>", start) + len(
        "</jpigp_cor:OperatingProfitLossIFRS>"
    )
    fact = original[start:stop]
    changed = {fact.encode(): (fact + fact.replace("1232568000", "1232568001")).encode()}
    result = extract(replace=changed)
    pairs = {p.metric: p for p in result.pairs}
    assert pairs["operating_profit"].reason == "comparison_fact_conflict"
    assert pairs["sales"].current.amount == "3525761000"
    assert pairs["sales"].comparative.amount == "3126231000"


def test_receipt_reader_obeys_publication_clock_and_preserves_bytes(tmp_path):
    path = tmp_path / "market.sqlite"
    row = seed(path)
    published = datetime.fromisoformat(row["submitDateTime"]).replace(
        tzinfo=extract().source_submit_datetime.tzinfo
    )
    with closing(open_connection(path)) as connection:
        assert (
            read_comparison_population(connection, as_of=published.date() - timedelta(days=1)) == {}
        )
        assert (
            read_comparison_population(
                connection, as_of=ASOF, known_at=published - timedelta(seconds=1)
            )
            == {}
        )
        result = read_comparison_population(connection, as_of=ASOF, known_at=published)["436A"]
        assert result[0].pairs[0].comparative.amount == "3126231000"
        assert (
            bytes(
                connection.execute("SELECT zip_content FROM edinet_source_archives").fetchone()[0]
            )
            == archive()
        )


def test_unrelated_orphan_event_does_not_remove_annual_comparison(tmp_path):
    path = tmp_path / "market.sqlite"
    seed(path)
    event = {
        "seqNumber": 1,
        "docID": "S100OTHER",
        "secCode": "436A0",
        "docTypeCode": "350",
        "docInfoEditStatus": "1",
    }
    store_edinet_documents(
        path, date(2026, 7, 25), [event], response_json=json.dumps({"results": [event]})
    )
    with closing(open_connection(path)) as connection:
        assert read_comparison_population(connection, as_of=ASOF)["436A"][0].reason is None


@pytest.mark.parametrize("clear_code", [False, True])
def test_native_sparse_edit_preserves_omission_and_explicit_null(tmp_path, clear_code):
    path = tmp_path / "market.sqlite"
    seed(path)
    event = {"seqNumber": 1, "docID": "S100YRSR", "docInfoEditStatus": "1"}
    if clear_code:
        event["secCode"] = None
    store_edinet_documents(
        path,
        date(2026, 7, 25),
        [event],
        response_json=json.dumps({"results": [event]}),
    )
    with closing(open_connection(path)) as connection:
        result = read_comparison_population(connection, as_of=ASOF)
    if clear_code:
        assert result == {}
    else:
        assert result["436A"][0].pairs[0].comparative.amount == "3126231000"


def test_unreceived_correction_remains_a_separate_vintage(tmp_path):
    path = tmp_path / "market.sqlite"
    row = seed(path)
    correction = {
        **row,
        "docID": "S100FIX",
        "docTypeCode": "130",
        "parentDocID": row["docID"],
        "submitDateTime": "2026-07-25 15:31",
    }
    store_edinet_documents(path, date(2026, 7, 25), [correction])
    with closing(open_connection(path)) as connection:
        result = read_comparison_population(connection, as_of=ASOF)["436A"]
    assert [r.source_doc_id for r in result] == ["S100YRSR", "S100FIX"]
    assert result[0].reason is None
    assert result[1].reason == "comparison_archive_not_received"
    assert not result[1].pairs


def test_models_share_native_comparison_without_rewriting_jquants_or_ttm():
    native = extract()
    raw = {
        "Code": "436A0",
        "DiscDate": "2025-07-24",
        "DiscNo": "synthetic-jp",
        "DocType": "FYFinancialStatements_NonConsolidated_JP",
        "CurPerType": "FY",
        "CurPerSt": "2024-05-01",
        "CurPerEn": "2025-04-30",
        "CurFYEn": "2025-04-30",
        "Sales": "3000000000",
        "OP": "1000000000",
    }
    document = normalize_financial_summary(raw)
    old = document.document_payload
    kwargs = dict(
        ticker="436A",
        documents=[document],
        adjusted_bars=(),
        adjustment_events=(),
        trading_dates=(),
        expected_price_date=None,
        sector=None,
        as_of=ASOF,
        input_release=RELEASE,
        ttm_rules=DEFAULT_TTM_RULES,
    )
    baseline = generate_financial_and_price_models(**kwargs)
    changed = generate_financial_and_price_models(**kwargs, comparisons=[native])
    comparison = changed[1].payload["reported_comparisons"][0]
    assert comparison["changes"][1]["difference"] == 267610000
    assert comparison["read_semantics"] == "each_document_vintage_not_latest_or_ttm"
    assert changed[0].payload["financial"]["reported_comparisons"] == [comparison]
    assert changed[0].payload["earnings_price"] == baseline[0].payload["earnings_price"]
    assert changed[1].payload["periods"] == baseline[1].payload["periods"]
    assert changed[1].payload["changes"] == baseline[1].payload["changes"]
    assert document.document_payload == old
    legacy = baseline[1].payload
    assert "reported_comparisons" not in legacy
    assert canonical_json(
        CompanyGrowth.model_validate(legacy).model_dump(mode="json")
    ) == canonical_json(legacy)
    filtered = generate_financial_and_price_models(
        **kwargs, comparisons=[native], growth_start=date(2026, 8, 1)
    )
    assert filtered[0].payload == changed[0].payload


def test_pure_growth_does_not_use_a_future_document():
    result = generate_company_growth(
        [],
        ticker="436A",
        sector=None,
        as_of=date(2026, 7, 23),
        input_release=RELEASE,
        comparisons=[extract()],
    )
    assert not result.reported_comparisons


def test_fixed_release_matches_sealed_reader_and_ignores_mutable_zip(tmp_path):
    path = tmp_path / "market.sqlite"
    seed(path)
    raw = {
        "Code": "436A0",
        "DiscDate": "2026-07-24",
        "DiscNo": "synthetic-current",
        "DocType": "FYFinancialStatements_NonConsolidated_IFRS",
        "CurPerType": "FY",
        "CurPerSt": "2025-05-01",
        "CurPerEn": "2026-04-30",
        "CurFYEn": "2026-04-30",
        "Sales": "3525761000",
    }
    store_jquants_fin_summaries(
        path, [raw], requested_start=date(2026, 7, 24), requested_end=date(2026, 7, 24)
    )
    with closing(open_connection(path)) as connection:
        expected = read_comparison_population(connection, as_of=ASOF)["436A"]
    publish_market_fixture(path, "comparisons-fixed", created_at=datetime(2026, 10, 31, tzinfo=UTC))
    origin = read_lake_store_origin(path)
    fixed = read_fixed_financial_documents(
        tmp_path / "mirror", origin=origin, ticker="436A", as_of=ASOF
    )
    assert fixed.comparisons == expected
    with closing(open_connection(path)) as connection, connection:
        connection.execute(
            "UPDATE edinet_source_archives SET zip_content = ?", (b"broken mutable copy",)
        )
    again = read_fixed_financial_documents(
        tmp_path / "mirror", origin=origin, ticker="436A", as_of=ASOF
    )
    assert again.comparisons == expected
    native_objects = list((tmp_path / "mirror" / "lake").rglob("*.parquet"))
    native_path = next(p for p in native_objects if "edinet.source_archives" in str(p))
    preserved = native_path.read_bytes()
    native_path.unlink()
    partial = read_fixed_financial_documents(
        tmp_path / "mirror", origin=origin, ticker="436A", as_of=ASOF
    )
    assert partial.documents == fixed.documents
    assert partial.comparisons == ()
    assert partial.comparison_input_reason == "fixed_comparison_objects_not_materialized"
    native_path.write_bytes(preserved[:-1] + bytes([preserved[-1] ^ 1]))
    from baibai_engine.storage.snapshots.objects import LakeObjectError

    with pytest.raises(LakeObjectError, match="differs"):
        read_fixed_financial_documents(
            tmp_path / "mirror", origin=origin, ticker="436A", as_of=ASOF
        )


@pytest.mark.parametrize(
    ("doc", "amounts", "precision"),
    [
        (
            "S100XU9L",
            [
                ("2265837000000", "2011351000000"),
                ("1271789000000", "1135440000000"),
                ("427344000000", "393836000000"),
            ],
            "-6",
        ),
        (
            "S100YG7Z",
            [("15668000000", "15943000000"), ("340000000", "676000000"), ("81000000", "578000000")],
            "-6",
        ),
        (
            "S100Z3FV",
            [None, ("-1155922000", "1100296000"), ("-1711953000", "465710000")],
            "-3",
        ),
    ],
)
def test_consolidated_annual_native_pairs_keep_period_scope_and_partial_gap(
    doc, amounts, precision
):
    result = extract(doc)
    assert result.reason is None
    assert result.statement_basis == "consolidated"
    assert result.extractor_revision == "edinet-ifrs-statement-comparisons-v3-known-non-ifrs"
    assert result.current_period_start.isoformat() == metadata(doc)["periodStart"]
    assert result.current_period_end.isoformat() == metadata(doc)["periodEnd"]
    assert [pair.metric for pair in result.pairs] == ["sales", "operating_profit", "parent_profit"]
    for pair, expected in zip(result.pairs, amounts, strict=True):
        if expected is None:
            # Revenue2IFRS is retained in the source fixture, not aliased by this role fix.
            assert pair.reason == "comparison_operand_not_observed"
            assert pair.current is pair.comparative is None
            continue
        assert pair.reason is None
        assert (pair.comparative.amount, pair.current.amount) == expected
        for amount in (pair.comparative, pair.current):
            assert amount.decimals == precision
            assert amount.currency == "JPY"
            assert amount.presentation_role.endswith("/rol_ConsolidatedStatementOfProfitOrLossIFRS")
            assert amount.locators
        assert pair.comparative.context_id == "Prior1YearDuration"
        assert pair.current.context_id == "CurrentYearDuration"
        assert pair.current.period_start == result.current_period_start
        assert pair.current.period_end == result.current_period_end
        assert pair.comparative.period_end.year == pair.current.period_end.year - 1


@pytest.mark.parametrize(
    ("doc", "before", "after"),
    [
        (
            "S100YG7Z",
            b"rol_ConsolidatedStatementOfProfitOrLossIFRS",
            b"rol_StatementOfProfitOrLossIFRS",
        ),
        (
            "S100YRSR",
            b"rol_StatementOfProfitOrLossIFRS",
            b"rol_ConsolidatedStatementOfProfitOrLossIFRS",
        ),
        (
            "S100YG7Z",
            b"rol_ConsolidatedStatementOfProfitOrLossIFRS",
            b"rol_ConsolidatedStatementOfComprehensiveIncomeIFRS",
        ),
        ("S100YG7Z", b"/role/jpigp/", b"/role/issuer/"),
    ],
)
def test_main_statement_role_must_match_dei_scope_and_exact_standard_role(doc, before, after):
    result = extract(doc, replace={before: after})
    assert result.reason == "comparison_main_statement_unknown"
    assert not result.pairs


@pytest.mark.parametrize("same_role", [False, True])
def test_consolidated_role_does_not_pool_a_second_presentation_link(same_role):
    original = (FIXTURES / "S100YG7Z_pre.xml").read_bytes()
    start = original.index(b"<link:presentationLink ")
    end = original.index(b"</link:presentationLink>") + len(b"</link:presentationLink>")
    second = original[start:end]
    if not same_role:
        second = second.replace(
            b"rol_ConsolidatedStatementOfProfitOrLossIFRS", b"rol_StatementOfProfitOrLossIFRS"
        )
    result = extract("S100YG7Z", replace={b"</link:linkbase>": second + b"</link:linkbase>"})
    assert result.reason == ("comparison_main_statement_unknown" if same_role else None)
    if not same_role:
        assert result.pairs[1].current.amount == "676000000"


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        (b"iso4217:JPY", b"iso4217:USD", "comparison_currency_unsupported"),
        (
            b"#jpigp_cor_OperatingProfitLossIFRS",
            b"#issuer_OperatingProfitLossIFRS",
            "comparison_operand_not_observed",
        ),
    ],
)
def test_consolidated_role_does_not_bypass_currency_or_main_statement_membership(
    before, after, reason
):
    result = extract("S100YG7Z", replace={before: after})
    assert result.reason is None
    assert result.pairs[1].reason == reason
    assert result.pairs[1].current is None


@pytest.mark.parametrize("doc", ["S100XU9L", "S100YG7Z", "S100Z3FV"])
def test_consolidated_comparisons_reach_l3_without_replacing_series_or_ttm(doc):
    native = extract(doc)
    assert native.reason is None
    row = metadata(doc)
    document = normalize_financial_summary(
        {
            "Code": row["secCode"],
            "DiscDate": row["submitDateTime"][:10],
            "DiscNo": "synthetic-series-not-native",
            "DocType": "FYFinancialStatements_Consolidated_IFRS",
            "CurPerType": "FY",
            "CurPerSt": row["periodStart"],
            "CurPerEn": row["periodEnd"],
            "CurFYEn": row["periodEnd"],
            "Sales": "3000000000",
            "OP": "1000000000",
        }
    )
    kwargs = dict(
        ticker=native.ticker,
        documents=[document],
        adjusted_bars=(),
        adjustment_events=(),
        trading_dates=(),
        expected_price_date=None,
        sector=None,
        as_of=ASOF,
        input_release=RELEASE,
        ttm_rules=DEFAULT_TTM_RULES,
    )
    baseline = generate_financial_and_price_models(**kwargs)
    changed = generate_financial_and_price_models(**kwargs, comparisons=[native])
    comparison = changed[1].payload["reported_comparisons"][0]
    assert comparison["read_semantics"] == "each_document_vintage_not_latest_or_ttm"
    assert changed[0].payload["financial"]["reported_comparisons"] == [comparison]
    assert changed[0].payload["earnings_price"] == baseline[0].payload["earnings_price"]
    assert changed[1].payload["periods"] == baseline[1].payload["periods"]
    assert changed[1].payload["changes"] == baseline[1].payload["changes"]
    saved = ModelInputSlice(
        generation_id="synthetic-role-comparison",
        as_of=ASOF,
        evaluation_time=datetime(2026, 10, 31, tzinfo=UTC),
        inputs={"market": InputSnapshot(kind="lake_release", reference="fixed", sha256="a" * 64)},
        subjects=(native.ticker,),
        models=tuple(changed),
    )
    before = saved.model_dump_json()
    displayed = project_generated_inputs(saved)
    reported = displayed["securities"][native.ticker]["company_financials"]["financial"][
        "reported_comparisons"
    ]
    assert reported == [comparison]
    reported[0]["observation"]["pairs"][1]["current"]["amount"] = "display-only-change"
    assert saved.model_dump_json() == before
