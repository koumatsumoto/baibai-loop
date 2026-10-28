"""Production publication requirements, distinct from generic manifest integrity."""

from baibai_engine.machine_analysis.observations.external_assessments.identity import (
    ANALYST_FORECAST_DATASET,
    ANALYST_RATING_DATASET,
    MODEL_VALUATION_DATASET,
    QUANTS_SCORE_DATASET,
)
from baibai_engine.storage.snapshots.models import (
    ReleaseDatasetPolicy,
    ReleasePolicy,
    ReleaseProfile,
)

PRODUCTION_RELEASE_POLICY = ReleasePolicy(
    policy_version=1,
    profile="production",
    datasets=(
        ReleaseDatasetPolicy(
            dataset="jquants.source_pages",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset=QUANTS_SCORE_DATASET,
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset=MODEL_VALUATION_DATASET,
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset=ANALYST_FORECAST_DATASET,
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset=ANALYST_RATING_DATASET,
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="tradingview.source_receipts",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="equity_web.source_responses",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="tradingview.forecast_snapshots",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.daily_bars",
            required=True,
            carries_history=True,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.short_sale_reports",
            required=True,
            carries_history=True,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.weekly_margin",
            required=True,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.master_snapshots",
            required=True,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.fin_summaries",
            required=True,
            carries_history=True,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.market_calendar",
            required=True,
            carries_history=True,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.earnings_calendar",
            required=True,
            # Replaced by every snapshot fetch, so its earliest row is whatever the
            # exchange still publishes rather than a history this release keeps.
            carries_history=False,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.margin_alerts",
            required=True,
            carries_history=True,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.all_issues_daily_margin",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.documents",
            required=True,
            carries_history=True,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.segment_facts",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.debt_schedule",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.metrics",
            required=True,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.source_archives",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.document_lists",
            required=True,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.list_responses",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.source_bundles",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.source_responses",
            required=False,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.regulation_flags",
            required=True,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.delistings",
            required=True,
            # Each selected index and its linked pages replace the observation view.
            # The index may stop linking an older archive without retracting its events.
            carries_history=False,
            # A complete bundle proves its declared pages arrived, not full history.
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.tender_offer_exit_values",
            required=False,
            # Replaced wholesale by each derivation, so a reclassified offer moves the
            # earliest row. Pinning a start date would refuse exactly the correction the
            # derivation exists to make.
            carries_history=False,
            # Recomputed exit proxies can legitimately disappear when evidence is
            # withdrawn. No historical count is a minimum economic truth. An empty
            # result is omitted by the exporter.
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.regulation_sources",
            required=True,
            carries_history=True,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="tse.capital_policy_snapshots",
            required=False,
            carries_history=False,
            require_complete_coverage=False,
        ),
    ),
    max_manifest_bytes=16 * 1024 * 1024,
    max_objects=10_000,
)


def release_policy_for_profile(profile: ReleaseProfile) -> ReleasePolicy:
    """Return the registered release policy; an unconfigured profile fails closed.

    There is one profile because there is one set of requirements. The gates that
    matter are the declared datasets, coverage and source/manifest consistency.
    They do not become different requirements because a release is read by a
    different caller. A second profile
    carrying the same dataset entries would be two tables free to rot apart, so the
    manifest records which gate it passed and there is exactly one gate to pass.
    """

    if profile == "production":
        return PRODUCTION_RELEASE_POLICY
    raise ValueError(f"release policy is not configured for profile: {profile}")
