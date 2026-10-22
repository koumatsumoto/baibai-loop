"""Public HTML attempts with canonical IDs for fixed-release publication tests."""

from __future__ import annotations

from datetime import datetime

from baibai_batch.jobs.external_assessment_cohorts import RunPlan
from baibai_batch.storage.external_assessment_raw import RawBundle
from baibai_batch.storage.lake_publish import ObjectStore
from baibai_engine.collection.equity_web.models import EquityWebSourceResponse, Source
from baibai_engine.external_assessment.normalization import kabumap, minkabu, monex, rakuten
from tests.batch.external_assessment_response_fixtures import response
from tests.engine.test_external_assessment_sources import monex_html, official_html, rakuten_html
from tests.engine.test_kabumap import html as kabumap_html
from tests.engine.test_minkabu import html as minkabu_html


def pair_responses(
    source: Source,
    ticker: str,
    *,
    received_at_utc: datetime,
    value: str | None = None,
    malformed: bool = False,
) -> tuple[EquityWebSourceResponse, ...]:
    if source == "monex":
        body = (
            "<html>invalid public page</html>"
            if malformed
            else monex_html(ticker, value=value or "1,234円")
        )
        return (
            response(
                source,
                ticker,
                body.encode(),
                url=monex.public_url(ticker),
                received_at_utc=received_at_utc,
            ),
        )
    if source == "minkabu":
        body = "<html>invalid public page</html>" if malformed else minkabu_html(ticker)
        return (
            response(
                source,
                ticker,
                body.encode(),
                url=minkabu.public_url(ticker),
                received_at_utc=received_at_utc,
            ),
        )
    if source == "kabumap":
        body = "<html>invalid public page</html>" if malformed else kabumap_html(ticker)
        return (
            response(
                source,
                ticker,
                body.encode("shift_jis"),
                url=kabumap.public_url(ticker),
                received_at_utc=received_at_utc,
                encoding="shift_jis",
            ),
        )
    outer = official_html(ticker)
    page = response(
        source,
        ticker,
        outer.encode(),
        url=rakuten.public_url(ticker),
        received_at_utc=received_at_utc,
    )
    iframe = response(
        source,
        ticker,
        (
            "<html>unconfirmed rating page</html>"
            if malformed
            else rakuten_html(ticker, value=value or "3.25")
        ).encode(),
        attempt_id=page.attempt_id,
        role="iframe",
        parent_receipt_id=page.receipt_id,
        url=rakuten.iframe_url(outer, ticker=ticker, source_url=page.requested_url),
        received_at_utc=received_at_utc,
    )
    return page, iframe


def upload_bundle(
    store: ObjectStore,
    plan: RunPlan,
    index: int,
    responses: tuple[EquityWebSourceResponse, ...],
) -> str:
    with RawBundle(plan.run_id, index) as bundle:
        for item in responses:
            bundle.append(item)
        key = bundle.finish()
        assert key is not None
        bundle.upload(store)
    return key
