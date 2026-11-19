"""通常の選定で照合できた既得原本だけを固定する。データ配信網羅性の証明ではない。"""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.db_seed import seed_ledger
from tests.helpers.external_assessment_html import monex_eps_section, monex_html
from tests.helpers.external_assessment_responses import response
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target

from baibai_engine.data_collection.receipts.public_pages.receipts import insert_response
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.report_perspectives import (
    MODELS,
    estimate_missing,
)
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    parse_selection,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    prepare_selected_workspace,
)
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, ModelGeneration
from baibai_engine.machine_analysis.models.external_assessment.analysis import analyze
from baibai_engine.machine_analysis.observations.external_assessments.normalization import monex
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observations,
)
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

TICKER = "1234"
NOW = datetime(2025, 3, 4, 6, tzinfo=UTC)
OBSERVED = NOW - timedelta(minutes=20)


def native(*, html=None, at=OBSERVED, ticker=TICKER):
    return response(
        "monex",
        ticker,
        (html if html is not None else monex_html(ticker) + monex_eps_section()).encode(),
        received_at_utc=at,
        url=monex.public_url(ticker),
    )


def save(path, *rows):
    with closing(open_connection(path)) as conn, conn:
        for row in rows:
            insert_response(conn, row)


def inputs(root, *, others=()):
    path = root / "market.sqlite"
    receipt = native()
    originals = (receipt, *others)
    tickers = tuple(row.requested_ticker for row in originals)
    save_observations(
        path,
        [decode_attempt((row,), expected_parser_version="monex-theory-4") for row in originals],
    )
    with closing(open_connection(path)) as conn, conn:
        write_lake_store_origin(conn, LakeStoreOrigin("fixture", "a" * 64))
    generation = ModelGeneration(
        generation_id="synthetic-monex",
        as_of=NOW.date(),
        evaluation_time=NOW,
        inputs={"market": {"kind": "lake_release", "reference": "fixture", "sha256": "a" * 64}},
        models=tuple(
            GeneratedModel(
                model_id="external_assessment",
                subject=ticker,
                calculation_revision="test-v1",
                payload=analyze(read_observations(path, ticker, NOW), evaluated_at=NOW).model_dump(
                    mode="json"
                ),
            )
            for ticker in tickers
        ),
    )
    runs = root / "runs.sqlite"
    publish_generation(generation, path=runs)
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(
            text="個社の利益予想を検証", scope="specified_stocks", requested_tickers=tickers
        ),
        research_as_of=NOW.date(),
        input_known_at=NOW,
        generation_id=generation.generation_id,
        selection_summary="利益予想を会社の想定と独立に照合する",
        targets=tuple(selected_target(ticker) for ticker in tickers),
    )
    return receipt, dict(
        draft=draft,
        db_path=root / "app.sqlite",
        workspace=root / "research",
        runs_path=runs,
        sqlite_path=path,
        now=NOW,
    )


def test_selected_original_visible_in_research_proposal_and_authored_report_after_cache_removed(
    tmp_path,
):
    original, args = inputs(tmp_path)
    path = args["sqlite_path"]
    save(path, original, native(at=NOW + timedelta(seconds=1)), native(ticker="5678"))
    seed_ledger(
        args["db_path"],
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 3,
                "portfolio_scope": "repository_only",
                "as_of": NOW,
                "events": [
                    {
                        "type": "opening_balance",
                        "event_id": "synthetic",
                        "occurred_at": NOW,
                        "amount_yen": 1,
                    }
                ],
            }
        ),
    )
    prepared = prepare_selected_workspace(**args)
    op = OperationService(args["db_path"]).get(prepared.operation_id)
    selection = selection_binding(op.payload)
    fixed = selection.inputs.monex_fiscal_eps[TICKER]
    assert fixed.receipt_id == original.receipt_id
    assert fixed.attempt_id == original.attempt_id
    assert fixed.reference_eps.current_fy.eps_jpy_per_share == -3.5
    assert fixed.reference_eps.next_fy.eps_jpy_per_share == 0
    assert set(selection.inputs.monex_fiscal_eps) == {TICKER}
    domain = yaml.safe_load((args["workspace"] / "domain-inputs.yaml").read_text())
    assert domain["securities"][TICKER]["monex_fiscal_eps"] == fixed.model_dump(mode="json")
    assert (
        "monex_fiscal_eps"
        not in selection.inputs.generation.result("external_assessment", TICKER).payload
    )
    path.rename(tmp_path / "removed-cache.sqlite")
    assert prepare_selected_workspace(**args).selection_sha256 == prepared.selection_sha256
    assert not path.exists()
    OperationService(args["db_path"]).record_unfinished(
        op.operation_id,
        ticker=TICKER,
        reason="合成：比較を続ける",
        references=("fixed",),
        now=NOW + timedelta(minutes=1),
    )
    proposal = build_proposal_context(
        operation_id=op.operation_id,
        db_path=args["db_path"],
        sqlite_path=path,
        now=NOW + timedelta(minutes=2),
    ).payload()["saved_analysis_context"]
    assert (
        proposal["securities"][TICKER]["monex_fiscal_eps"]
        == domain["securities"][TICKER]["monex_fiscal_eps"]
    )
    raw = selection.model_dump(mode="json")

    def chart(payload):
        return estimate_missing(
            payload, ticker=TICKER, recorded=set(MODELS), selection_sha256=prepared.selection_sha256
        )

    chart_data = chart(raw)
    assert chart_data["rows"] == []  # All seven authors, including an explicit adverse view, win.
    reported = chart_data["monex_fiscal_eps"]
    assert reported["current_fy"] == fixed.reference_eps.current_fy.model_dump(mode="json")
    assert reported["next_fy"] == fixed.reference_eps.next_fy.model_dump(mode="json")
    assert reported["source_url"] == fixed.source_url
    assert reported["received_at_utc"] == fixed.received_at_utc
    assert "receipt_id" not in reported
    assert "rolling_one_year" not in reported
    # New operation admission independently re-derives values, rather than trusting authored JSON.
    forged = deepcopy(raw)
    forged["inputs"]["monex_fiscal_eps"][TICKER]["reference_eps"]["next_fy"][
        "eps_jpy_per_share"
    ] = 999
    with pytest.raises(ValueError, match="differs from retained"):
        OperationService(tmp_path / "other.sqlite", runs_path=args["runs_path"]).start(
            session_kind=op.session_kind,
            as_of=op.as_of,
            started_at=NOW,
            sqlite_path=tmp_path / "removed-cache.sqlite",
            payload=op.payload.model_copy(update={"artifacts": (forged,)}),
        )
    for key, value in [
        ("ticker", "5678"),
        ("source_url", "https://example.invalid/"),
        ("received_at_utc", (NOW + timedelta(seconds=1)).isoformat()),
    ]:
        other = deepcopy(raw)
        other["inputs"]["monex_fiscal_eps"][TICKER][key] = value
        with pytest.raises(ValidationError, match="differs"):
            parse_selection(other)
        assert chart(other) is None


@pytest.mark.parametrize(
    "failure",
    [
        "absent_store",
        "empty",
        "newer",
        "other_ticker",
        "two_attempts",
        "different_four_prices",
        "dom_identity",
        "no_fiscal_values",
        "other_release",
        "other_manifest",
        "no_origin",
    ],
)
def test_no_exact_single_fiscal_original_retains_old_selection_shape(tmp_path, failure):
    receipt, args = inputs(tmp_path)
    path = args["sqlite_path"]
    if failure == "absent_store":
        args["sqlite_path"] = tmp_path / "never-created.sqlite"
    if failure in {"other_release", "other_manifest", "no_origin"}:
        # Even an identical price table cannot authenticate different fiscal EPS in another release.
        save(
            path, native(html=(monex_html(TICKER) + monex_eps_section()).replace("-3.5円", "333円"))
        )
        with closing(open_connection(path)) as conn, conn:
            if failure == "no_origin":
                conn.execute("DELETE FROM lake_store_origin")
            else:
                write_lake_store_origin(
                    conn,
                    LakeStoreOrigin(
                        "other" if failure == "other_release" else "fixture",
                        "b" * 64 if failure == "other_manifest" else "a" * 64,
                    ),
                )
    if failure == "newer":
        save(path, native(at=NOW + timedelta(seconds=1)))
    if failure == "other_ticker":
        save(path, native(ticker="5678"))
    if failure == "two_attempts":
        save(path, receipt, native())
    if failure == "different_four_prices":
        save(path, native(html=monex_html(TICKER, value="2,345円") + monex_eps_section()))
    if failure == "dom_identity":
        save(path, replace(receipt, body=(monex_html("5678") + monex_eps_section()).encode()))
    if failure == "no_fiscal_values":
        save(path, native(html=monex_html(TICKER)))
    prepared = prepare_selected_workspace(**args)
    fixed = selection_binding(OperationService(args["db_path"]).get(prepared.operation_id).payload)
    raw = fixed.model_dump(mode="json")
    assert "monex_fiscal_eps" not in raw["inputs"]
    assert selection_sha256(parse_selection(raw)) == prepared.selection_sha256
    workspace_product = yaml.safe_load((args["workspace"] / "domain-inputs.yaml").read_text())
    assert "monex_fiscal_eps" not in workspace_product["securities"][TICKER]
    assert (
        estimate_missing(
            raw, ticker=TICKER, recorded=set(MODELS), selection_sha256=prepared.selection_sha256
        )
        is None
    )


@pytest.mark.parametrize(
    "damaged", [b"not gzip", b"\x1f\x8b\x08", b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\x07" * 12]
)
def test_damaged_single_body_never_loses_another_selected_subject(tmp_path, damaged):
    good = native(ticker="5678")
    bad, args = inputs(tmp_path, others=(good,))
    save(args["sqlite_path"], good, bad)
    with closing(open_connection(args["sqlite_path"])) as conn, conn:
        conn.execute(
            "UPDATE equity_web_source_responses SET body_gzip=? WHERE receipt_id=?",
            (damaged, bad.receipt_id),
        )
    prepared = prepare_selected_workspace(**args)
    saved = selection_binding(OperationService(args["db_path"]).get(prepared.operation_id).payload)
    assert list(saved.inputs.monex_fiscal_eps) == ["5678"]
    assert saved.inputs.monex_fiscal_eps["5678"].receipt_id == good.receipt_id
