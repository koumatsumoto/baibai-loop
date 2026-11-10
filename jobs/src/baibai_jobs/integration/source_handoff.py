"""Private original-receipt handoffs; publishers replay onto freshly hydrated stores."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakePublishError,
)
from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_jobs.acquisition.economic_staging import load_handoff, save_handoff
from baibai_jobs.integration.jpx_handoff import export_handoff as export_jpx_handoff
from baibai_jobs.integration.jpx_handoff import import_handoff as import_jpx_handoff
from baibai_jobs.integration.jquants_handoff import MARKET_SOURCES as JQUANTS_MARKET_SOURCES
from baibai_jobs.integration.jquants_handoff import export_handoff as export_jquants_handoff
from baibai_jobs.integration.jquants_handoff import import_handoff as import_jquants_handoff
from baibai_jobs.integration.replay_result import ReplayResult
from baibai_jobs.integration.tradingview_handoff import TradingViewHandoff
from baibai_jobs.integration.tradingview_handoff import export_handoff as export_tradingview_handoff
from baibai_jobs.integration.tradingview_handoff import import_handoff as import_tradingview_handoff

SOURCES = (
    "tradingview",
    "jquants-market",
    "jquants-financial",
    "jpx-earnings",
    "jpx-regulations",
    "edinet-list",
    "edinet-metrics",
    "edinet-facts",
    "jpx-financial-pdfs",
    "jpx-capital-actions",
)
_EDINET_SOURCES = {"edinet-list", "edinet-metrics", "edinet-facts"}
_JPX_COLLECTION = {
    "jpx-earnings": "earnings_calendar",
    "jpx-regulations": "regulations",
    "jpx-financial-pdfs": "financial_statements",
    "jpx-capital-actions": "capital_actions",
}


def _export(
    source: str,
    asof: date,
    since: datetime,
    database: Path,
    directory: Path,
    master_input: InputSnapshot | None = None,
    calendar_input: InputSnapshot | None = None,
) -> None:
    if source not in SOURCES:
        raise ValueError("unsupported market handoff source")
    if source in _EDINET_SOURCES:
        raise ValueError("EDINET native mode does not export legacy stages")
    if source == "jquants-financial":
        raise ValueError("legacy financial handoff is disabled after native cutover")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "receipts.sqlite"
    report: dict[str, object] = {
        "source": source,
        "asof": asof.isoformat(),
        "started_at": since.isoformat(),
    }
    if source == "tradingview":
        handoff = (
            export_tradingview_handoff(database, path, day=asof, since=since)
            if master_input is None and calendar_input is None
            else export_tradingview_handoff(
                database,
                path,
                day=asof,
                since=since,
                master_input=master_input,
                calendar_input=calendar_input,
            )
        )
        report.update(handoff.to_json())
    elif source == "jquants-market":
        report["request_ids"] = export_jquants_handoff(
            database, path, since=since, sources=JQUANTS_MARKET_SOURCES
        )
    elif source in _JPX_COLLECTION:
        report["bundle_ids"] = export_jpx_handoff(
            database, path, since=since, collection=_JPX_COLLECTION[source]
        )

    write_json_atomic(directory / "result.json", report)


def _handoff_ids(report: dict[str, object], field: str) -> tuple[str, ...]:
    values = report.get(field)
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"invalid handoff {field}")
    return tuple(values)


def _replay(
    source: str,
    database: Path,
    directory: Path,
    *,
    store: Boto3R2Store | None = None,
    master_input: InputSnapshot | None = None,
    master_fingerprints: Mapping[str, str] | None = None,
    calendar_input: InputSnapshot | None = None,
    calendar_fingerprints: Mapping[str, str] | None = None,
    price_fingerprints: Mapping[str, str] | None = None,
    margin_alerts_fingerprints: Mapping[str, str] | None = None,
    short_sale_reports_fingerprints: Mapping[str, str] | None = None,
    margin_interest_fingerprints: Mapping[str, str] | None = None,
) -> ReplayResult:
    from baibai_jobs.acquisition.article_planning import ARTICLE_SOURCES
    from baibai_jobs.integration.article_handoff import import_handoff as import_article_handoff

    if source in _EDINET_SOURCES:
        raise ValueError("EDINET stages cannot be replayed into a native execution copy")
    if source == "jquants-financial":
        raise ValueError("legacy financial handoff is disabled after native cutover")
    if source in ARTICLE_SOURCES:
        return import_article_handoff(source, database, directory)
    if source not in SOURCES:
        raise ValueError("unsupported market handoff source")
    report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("source") != source:
        raise ValueError("handoff source differs from the publication plan")
    path = directory / "receipts.sqlite"
    if source == "tradingview":
        handoff = TradingViewHandoff.from_json(report)

        if calendar_input is None:
            raise ValueError("native TradingView replay requires its fixed calendar context")
        if store is None:
            raise ValueError("native calendar replay requires its object store")
        from baibai_engine.data_collection.providers.jquants.calendar_native import interval
        from baibai_jobs.integration.calendar_native import (
            materialize_saved_input as materialize_calendar,
        )

        materialize_calendar(
            store,
            calendar_input,
            market=database,
            days=interval(handoff.master_asof, handoff.snapshot_date),
            replace_all=False,
        )
        if master_input is None:
            raise ValueError("native TradingView replay requires its fixed master context")
        from baibai_engine.machine_analysis.generation.master_inputs import read_master_input
        from baibai_jobs.integration.master_native import materialize_saved_input

        if master_input.descriptor is None:
            raise ValueError("native master replay requires a fixed descriptor")
        selected_master = read_master_input(
            store, master_input.descriptor, days=[handoff.master_asof]
        )
        scope = selected_master["scopes"].get(handoff.master_asof.isoformat(), {})
        if not scope.get("adopted") or scope.get("failure") or scope.get("held"):
            raise ValueError("native master replay acquisition is unresolved")
        materialize_saved_input(store, master_input, market=database, days=[handoff.master_asof])
        return import_tradingview_handoff(
            path,
            database,
            handoff,
            store=store,
            master_fingerprints=master_fingerprints,
        )
    if source == "jquants-market":
        if price_fingerprints is None:
            raise ValueError("native price replay requires retained cutover fingerprints")

        if master_fingerprints is None:
            raise ValueError("native master replay requires retained cutover fingerprints")
        if calendar_fingerprints is None:
            raise ValueError("native calendar replay requires retained cutover fingerprints")
        if margin_alerts_fingerprints is None:
            raise ValueError("native margin alerts replay requires retained cutover fingerprints")
        if short_sale_reports_fingerprints is None:
            raise ValueError(
                "native short-sale reports replay requires retained cutover fingerprints"
            )
        if margin_interest_fingerprints is None:
            raise ValueError("native margin interest replay requires retained cutover fingerprints")
        return import_jquants_handoff(
            path,
            database,
            _handoff_ids(report, "request_ids"),
            sources=JQUANTS_MARKET_SOURCES,
            master_fingerprints=master_fingerprints,
            calendar_fingerprints=calendar_fingerprints,
            price_fingerprints=price_fingerprints,
            margin_alerts_fingerprints=margin_alerts_fingerprints,
            short_sale_reports_fingerprints=short_sale_reports_fingerprints,
            margin_interest_fingerprints=margin_interest_fingerprints,
        )
    if source in _JPX_COLLECTION:
        return import_jpx_handoff(
            path, database, _handoff_ids(report, "bundle_ids"), collection=_JPX_COLLECTION[source]
        )
    raise ValueError("unsupported market handoff source")


def _acquire_edinet_lists(asof: date, directory: Path) -> bool:
    from baibai_engine.data_collection.providers.edinet.http import EDINETHTTP
    from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1
    from baibai_jobs.acquisition.edinet_list_native import acquire_lists

    with (
        closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store,
        closing(EDINETHTTP(os.environ.get("EDINET_API_KEY", ""))) as client,
    ):
        owner = EdinetListL1(store, directory)
        started = datetime.now(UTC)
        try:
            report = acquire_lists(owner, client, start=asof - timedelta(days=540), end=asof)
            owner.require_clean_work()
        except Exception:
            from baibai_jobs.integration.edinet_list_native import export_pending_handoff

            if export_pending_handoff(owner, asof=asof, started_at=started) and (
                output := os.environ.get("GITHUB_OUTPUT")
            ):
                with Path(output).open("a", encoding="utf-8") as stream:
                    print("stage_ready=true", file=stream)
            raise
    print(json.dumps(report, sort_keys=True))
    return not bool(report["failed"])


def _acquire_edinet_archives(source: str, asof: date, directory: Path) -> bool:
    from baibai_engine.data_collection.providers.edinet.archive_native import EdinetArchiveL1
    from baibai_engine.data_collection.providers.edinet.http import EDINETHTTP
    from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1
    from baibai_jobs.acquisition.edinet_archive_native import acquire_archives
    from baibai_jobs.integration.edinet_archive_native import export_pending_handoff

    if source not in {"edinet-metrics", "edinet-facts"}:
        raise ValueError("unsupported native EDINET archive group")
    with (
        closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store,
        closing(EDINETHTTP(os.environ.get("EDINET_API_KEY", ""))) as client,
    ):
        owner = EdinetArchiveL1(store, directory)
        lists = EdinetListL1(store, directory / "lists").freeze()
        started = datetime.now(UTC)
        try:
            report = acquire_archives(
                owner,
                client,
                lists,
                group="edinet-metrics" if source == "edinet-metrics" else "edinet-facts",
                asof=asof,
            )
            owner.require_clean_work()
        except Exception:
            if export_pending_handoff(owner, source=source, asof=asof, started_at=started) and (
                output := os.environ.get("GITHUB_OUTPUT")
            ):
                with Path(output).open("a", encoding="utf-8") as stream:
                    print("stage_ready=true", file=stream)
            raise
    print(json.dumps(report, sort_keys=True))
    return report["status"] != "failed"


def _acquire_financial_l1(asof: date, directory: Path) -> bool:
    """Acquire independent native originals, without a lake or copied L2 input."""
    from baibai_engine.data_collection.providers.jquants.financial_native import FinancialL1
    from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
    from baibai_engine.primitives.market_windows import (
        FINANCIAL_HISTORY_DAYS,
        FINANCIAL_REVISION_OVERLAP_DAYS,
    )
    from baibai_jobs.acquisition.jquants_financial_native import acquire_financial_native

    with (
        closing(
            Boto3R2Store(
                bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores",
                verify_put_with_head=False,
            )
        ) as store,
        closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as client,
    ):
        result = acquire_financial_native(
            FinancialL1(store, directory),
            start=asof - timedelta(days=FINANCIAL_HISTORY_DAYS),
            end=asof,
            revision_overlap_days=FINANCIAL_REVISION_OVERLAP_DAYS,
            client=client,
        )
    return not bool(result["failed"] or result["held"])


def _acquire_market_l1(asof: date, directory: Path) -> bool:
    """Acquire each market source using retained originals, not L2 copies."""
    from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP

    bucket = os.environ.get("R2_STORES_BUCKET") or "baibai-stores"
    from baibai_engine.data_collection.providers.jquants.calendar_native import CalendarL1
    from baibai_jobs.acquisition.jquants_calendar_native import (
        acquire_calendar_native,
        calendar_window,
    )

    with (
        closing(Boto3R2Store(bucket=bucket, verify_put_with_head=False)) as calendar_store,
        closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as calendar_client,
    ):
        owner = CalendarL1(calendar_store, directory / "calendar-native")
        start, end = calendar_window(owner.fixed(), asof)
        result = acquire_calendar_native(owner, start=start, end=end, client=calendar_client)
        calendar_fixed = owner.fixed()
    calendar_result = dict(result)
    calendar_ok = not bool(result["failed"] or result["held"] or result["incomplete"])
    provider_halted = bool(result.get("halted"))
    write_json_atomic(directory / "calendar-native-result.json", calendar_result)
    if provider_halted:
        price_ok = False
        native_result = {"halted": 1, "status": "provider_halted"}
    else:
        from baibai_engine.data_collection.providers.jquants.market_rules import (
            MARKET_REVISION_OVERLAP_DAYS,
        )
        from baibai_engine.data_collection.providers.jquants.price_native import PriceL1
        from baibai_engine.primitives.market_windows import FINANCIAL_HISTORY_DAYS
        from baibai_jobs.acquisition.jquants_price_native import acquire_price_native

        # Commit price originals before independent sources and L2 work.
        try:
            with (
                closing(Boto3R2Store(bucket=bucket, verify_put_with_head=False)) as price_store,
                closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as price_client,
            ):
                price_owner = PriceL1(price_store, directory / "price-native")
                result = acquire_price_native(
                    price_owner,
                    start=asof - timedelta(days=FINANCIAL_HISTORY_DAYS),
                    end=asof,
                    revision_overlap_days=MARKET_REVISION_OVERLAP_DAYS,
                    client=price_client,
                )
            provider_halted = bool(result.get("halted", 0))
            native_result = dict(result)
            price_ok = not bool(result["failed"] or result["held"])
        except LakePublishError:
            raise
        except Exception as error:
            price_ok = False
            native_result = {"error": type(error).__name__}
        directory.mkdir(parents=True, exist_ok=True)
        write_json_atomic(directory / "price-native-result.json", native_result)
    from baibai_engine.data_collection.providers.jquants.master_native import MasterL1
    from baibai_jobs.acquisition.jquants_master_native import acquire_master_native

    if provider_halted:
        master_result = {"halted": 1, "status": "provider_halted"}
        master_ok = False
    else:
        try:
            with (
                closing(Boto3R2Store(bucket=bucket, verify_put_with_head=False)) as master_store,
                closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as master_client,
            ):
                master_owner = MasterL1(master_store, directory / "master-native")
                acquired_master = acquire_master_native(
                    master_owner,
                    start=asof,
                    end=asof,
                    client=master_client,
                )
            master_result = dict(acquired_master)
            provider_halted = bool(acquired_master.get("halted", 0))
            master_ok = not bool(
                acquired_master["failed"]
                or acquired_master["held"]
                or acquired_master["incomplete"]
            )
        except LakePublishError:
            raise
        except Exception as error:
            master_ok = False
            master_result = {"error": type(error).__name__}
    directory.mkdir(parents=True, exist_ok=True)
    write_json_atomic(directory / "master-native-result.json", master_result)
    from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
        MarginAlertsL1,
    )
    from baibai_jobs.acquisition.jquants_margin_alerts_native import (
        acquire_margin_alerts_native,
    )

    if provider_halted:
        alerts_result = {"halted": 1, "status": "provider_halted"}
        alerts_ok = False
    else:
        # Independent source commits survive later acquisition or L2 failures.
        with (
            closing(Boto3R2Store(bucket=bucket, verify_put_with_head=False)) as alerts_store,
            closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as alerts_client,
        ):
            acquired_alerts = acquire_margin_alerts_native(
                MarginAlertsL1(alerts_store, directory / "margin-alerts-native"),
                asof=asof,
                client=alerts_client,
            )
        alerts_result = dict(acquired_alerts)
        provider_halted = bool(acquired_alerts.get("halted"))
        alerts_ok = not bool(acquired_alerts["failed"] or acquired_alerts["held"])
    write_json_atomic(directory / "margin-alerts-native-result.json", alerts_result)
    from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
        ShortSaleReportsL1,
    )
    from baibai_jobs.acquisition.jquants_short_sale_reports_native import (
        acquire_short_sale_reports_native,
    )

    if provider_halted:
        shorts_result = {"halted": 1, "status": "provider_halted"}
        shorts_ok = False
    else:
        # Independent source commits survive later acquisition or L2 failures.
        with (
            closing(Boto3R2Store(bucket=bucket, verify_put_with_head=False)) as shorts_store,
            closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as shorts_client,
        ):
            acquired_shorts = acquire_short_sale_reports_native(
                ShortSaleReportsL1(shorts_store, directory / "short-sale-reports-native"),
                asof=asof,
                client=shorts_client,
            )
        shorts_result = dict(acquired_shorts)
        provider_halted = bool(acquired_shorts.get("halted"))
        shorts_ok = not bool(acquired_shorts["failed"] or acquired_shorts["held"])
    write_json_atomic(directory / "short-sale-reports-native-result.json", shorts_result)
    from baibai_engine.data_collection.providers.jquants.margin_interest_native import (
        MarginInterestL1,
    )
    from baibai_jobs.acquisition.jquants_margin_interest_native import (
        acquire_margin_interest_native,
    )

    if provider_halted:
        interest_result = {"halted": 1, "status": "provider_halted"}
        interest_ok = False
    else:
        with (
            closing(Boto3R2Store(bucket=bucket, verify_put_with_head=False)) as interest_store,
            closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as interest_client,
        ):
            acquired_interest = acquire_margin_interest_native(
                MarginInterestL1(interest_store, directory / "margin-interest-native"),
                calendar_fixed,
                asof=asof,
                client=interest_client,
            )
        interest_result = dict(acquired_interest)
        provider_halted = bool(acquired_interest.get("halted"))
        interest_ok = not bool(
            acquired_interest["failed"]
            or acquired_interest["held"]
            or acquired_interest["incomplete"]
            or acquired_interest["remaining"]["unknown_calendar_days"]
            or acquired_interest["remaining"]["unknown_incomplete_requests"]
        )
    write_json_atomic(directory / "margin-interest-native-result.json", interest_result)
    # With the last source migrated there is no legacy release or handoff to open.
    from baibai_engine.data_collection.providers.jquants.native_index import calendar_sessions

    sessions, unknown = calendar_sessions(calendar_fixed.index(), start=asof, end=asof)
    skipped = (
        os.environ.get("GITHUB_EVENT_NAME") == "schedule"
        and sessions[asof] is False
        and not unknown
    )
    write_json_atomic(
        directory / "result.json",
        {
            "source": "jquants-market",
            "asof": asof.isoformat(),
            "native_l1": True,
            "acquisition": {"status": "skipped_non_business_day" if skipped else "native_l1"},
        },
    )

    path = directory / "result.json"
    report = json.loads(path.read_bytes())
    if (
        master_result.get("incomplete")
        and not (master_result.get("failed") or master_result.get("held"))
        and report.get("acquisition", {}).get("status") == "skipped_non_business_day"
    ):
        # Raw empty stays unfulfilled, but a proven closed cycle does not
        # require that date's nonempty universe.
        from baibai_engine.data_collection.providers.jquants.native_index import (
            calendar_sessions,
        )

        calendar = calendar_fixed.index()
        sessions, unknown = calendar_sessions(calendar, start=asof, end=asof)
        receipts = [
            item
            for item in calendar.receipts
            if any(day == asof for day, _ in item.calendar_divisions)
        ]
        latest = max((item.received_at for item in receipts), default=None)
        divisions = {
            division
            for item in receipts
            if item.received_at == latest
            for day, division in item.calendar_divisions
            if day == asof
        }
        if sessions[asof] is False and not unknown and divisions and divisions <= {"0", "3"}:
            master_ok = True
            master_result = {**master_result, "status": "not_required_non_business_day"}
    report["independent_l1"] = {
        "daily_bars": native_result,
        "master": master_result,
        "market_calendar": calendar_result,
        "margin_alerts": alerts_result,
        "short_sale_reports": shorts_result,
        "margin_interest": interest_result,
    }
    write_json_atomic(path, report)
    return price_ok and master_ok and calendar_ok and alerts_ok and shorts_ok and interest_ok


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("input", "resume", "acquire", "export", "stage", "download", "replay-copy"),
    )
    parser.add_argument("--source", choices=SOURCES, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--since", type=datetime.fromisoformat)
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--stage-id")
    parser.add_argument("--input-directory", type=Path)
    return parser


class _ReplayContext(TypedDict, total=False):
    master_input: InputSnapshot
    calendar_input: InputSnapshot
    master_fingerprints: Mapping[str, str]
    calendar_fingerprints: Mapping[str, str]
    price_fingerprints: Mapping[str, str]
    margin_alerts_fingerprints: Mapping[str, str]
    short_sale_reports_fingerprints: Mapping[str, str]
    margin_interest_fingerprints: Mapping[str, str]


def _replay_context(
    store: Boto3R2Store,
    source: str,
    directory: Path,
    packet: dict[str, object],
    *,
    master_input: InputSnapshot | None = None,
    calendar_input: InputSnapshot | None = None,
    price_fingerprints: Mapping[str, str] | None = None,
    margin_alerts_fingerprints: Mapping[str, str] | None = None,
    short_sale_reports_fingerprints: Mapping[str, str] | None = None,
    margin_interest_fingerprints: Mapping[str, str] | None = None,
) -> _ReplayContext:
    """Prefer saved native inputs; latest heads are only for legacy replay migration."""

    context: _ReplayContext = {}
    if source == "jquants-market" and price_fingerprints is None:
        from baibai_engine.data_collection.providers.jquants.price_native import PriceL1

        price_fingerprints = {
            identity: receipt["fingerprint"]
            for identity, receipt in PriceL1(store, directory / "price-native")
            .fixed()
            .value()["receipts"]
            .items()
        }
    if price_fingerprints is not None:
        context["price_fingerprints"] = price_fingerprints
    if source == "jquants-market":
        from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
            MarginAlertsL1,
        )

        if margin_alerts_fingerprints is None:
            margin_alerts_fingerprints = {
                identity: receipt["fingerprint"]
                for identity, receipt in MarginAlertsL1(store, directory / "margin-alerts-native")
                .fixed()
                .value()["receipts"]
                .items()
            }
        context["margin_alerts_fingerprints"] = margin_alerts_fingerprints
    if source == "jquants-market":
        from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
            ShortSaleReportsL1,
        )

        if short_sale_reports_fingerprints is None:
            short_sale_reports_fingerprints = {
                identity: receipt["fingerprint"]
                for identity, receipt in ShortSaleReportsL1(
                    store, directory / "short-sale-reports-native"
                )
                .fixed()
                .value()["receipts"]
                .items()
            }
        context["short_sale_reports_fingerprints"] = short_sale_reports_fingerprints
    if source == "jquants-market":
        from baibai_engine.data_collection.providers.jquants.margin_interest_native import (
            MarginInterestL1,
        )

        if margin_interest_fingerprints is None:
            margin_interest_fingerprints = {
                identity: receipt["fingerprint"]
                for identity, receipt in MarginInterestL1(
                    store, directory / "margin-interest-native"
                )
                .fixed()
                .value()["receipts"]
                .items()
            }
        context["margin_interest_fingerprints"] = margin_interest_fingerprints
    day = date.fromisoformat(str(packet["asof"]))
    if source == "tradingview":
        if master_input is None and packet.get("master_native_input") is not None:
            master_input = InputSnapshot.model_validate(packet["master_native_input"])
        if calendar_input is None and packet.get("calendar_native_input") is not None:
            calendar_input = InputSnapshot.model_validate(packet["calendar_native_input"])
    if master_input is None or source == "jquants-market" or packet.get("master_request_ids"):
        from baibai_engine.data_collection.providers.jquants.master_native import MasterL1
        from baibai_jobs.integration.master_native import fixed_input as fixed_master_input

        fixed_master = MasterL1(store, directory / "master-native").fixed()
        context["master_fingerprints"] = {
            identity: receipt["fingerprint"]
            for identity, receipt in fixed_master.value()["receipts"].items()
        }
        if source == "tradingview" and master_input is None:
            master_input = fixed_master_input(
                store, fixed_master, as_of=day, evaluated_at=datetime.now(UTC)
            )
    if (
        calendar_input is None
        or source == "jquants-market"
        or packet.get("calendar_native_input") is None
    ):
        from baibai_engine.data_collection.providers.jquants.calendar_native import CalendarL1
        from baibai_jobs.integration.calendar_native import fixed_input as fixed_calendar_input

        fixed_calendar = CalendarL1(store, directory / "calendar-native").fixed()
        context["calendar_fingerprints"] = {
            identity: receipt["fingerprint"]
            for identity, receipt in fixed_calendar.value()["receipts"].items()
        }
        if source == "tradingview" and calendar_input is None:
            calendar_input = fixed_calendar_input(
                store, fixed_calendar, as_of=day, evaluated_at=datetime.now(UTC)
            )
    if master_input is not None:
        context["master_input"] = master_input
    if calendar_input is not None:
        context["calendar_input"] = calendar_input
    return context


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    directory = args.directory
    try:
        if args.source == "jquants-financial" and args.command in {
            "input",
            "resume",
            "export",
            "stage",
            "replay-copy",
        }:
            raise ValueError("legacy financial handoff is disabled after native cutover")
        if args.command in {"input", "resume"} and args.source in _EDINET_SOURCES:
            raise ValueError(
                "native EDINET acquisitions use fixed L1 heads, not market source inputs"
            )
        if args.command == "replay-copy":
            packet = json.loads((directory / "result.json").read_bytes())

            native = args.source in {"tradingview", "jquants-market"}
            if native:
                with closing(
                    Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
                ) as store:
                    context = _replay_context(store, args.source, directory, packet)
                    replay = _replay(args.source, args.database, directory, store=store, **context)
            else:
                replay = _replay(args.source, args.database, directory)
            print(json.dumps(replay.report, sort_keys=True))
            return int(not replay.complete)
        if args.command == "input":
            from baibai_jobs.acquisition.source_inputs import load_input

            with closing(
                Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            ) as store:
                load_input(store, args.source, args.database, directory)
        elif args.command == "resume":
            from baibai_jobs.acquisition.receipts import available_handoffs, download_fixed

            if args.asof is None:
                raise ValueError("--asof is required")
            store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            saved_input = json.loads((directory / "result.json").read_text())
            master_input = (
                InputSnapshot.model_validate(saved_input["master_native_input"])
                if args.source == "tradingview"
                and saved_input.get("master_native_input") is not None
                else None
            )

            if args.source == "tradingview" and master_input is None:
                return 0  # Acquire the fixed master, then resume these receipts.
            calendar_input = (
                InputSnapshot.model_validate(saved_input["calendar_native_input"])
                if args.source == "tradingview"
                and saved_input.get("calendar_native_input") is not None
                else None
            )
            if args.source == "tradingview" and calendar_input is None:
                raise ValueError("native TradingView resume requires its fixed calendar input")

            price_fingerprints = None
            if args.source == "jquants-market":
                from baibai_engine.data_collection.providers.jquants.price_native import PriceL1

                price_fingerprints = {
                    identity: receipt["fingerprint"]
                    for identity, receipt in PriceL1(store, directory / "price-native")
                    .fixed()
                    .value()["receipts"]
                    .items()
                }
            source = args.source
            for index, entry in enumerate(
                available_handoffs(store, source=source, include_integrated=True)
            ):
                if entry.get("invalid") or entry.get("late_before_integrated"):
                    continue
                integrated = entry.get("integration")
                if isinstance(integrated, dict) and (
                    integrated["status"] == "needs_explicit_recovery"
                    or integrated["integrated_at"] <= saved_input["created_at"]
                ):
                    continue
                if args.source == "tradingview" and entry["asof"] != args.asof.isoformat():
                    continue
                packet = directory / f"resume-{source}-{index}"
                download_fixed(store, entry, packet)
                if source not in {"tradingview", "jquants-market"}:
                    _replay(source, args.database, packet)
                else:
                    report = json.loads((packet / "result.json").read_bytes())
                    context = _replay_context(
                        store,
                        source,
                        directory,
                        report,
                        master_input=master_input,
                        calendar_input=calendar_input,
                        price_fingerprints=price_fingerprints,
                    )
                    from baibai_jobs.acquisition.calendar_dependencies import (
                        verify_stage_calendar_dependencies,
                    )

                    # Native packets carry fixed references, not legacy dependencies.
                    # Old stage dependency evidence is verified against the cutover.
                    fingerprints = context.get("calendar_fingerprints")
                    if fingerprints is not None:
                        verify_stage_calendar_dependencies(store, entry, fingerprints)
                    _replay(source, args.database, packet, store=store, **context)
        elif args.command in {"stage", "download"}:
            if args.stage_id is None:
                raise ValueError("--stage-id is required")
            store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            if args.command == "stage":
                save_handoff(store, args.stage_id, directory, market=True)
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print(f"handoff={args.stage_id}", file=stream)
            else:
                load_handoff(store, args.stage_id, directory, market=True)
        elif args.command in {"acquire", "export"}:
            if args.asof is None or (args.command == "export" and args.since is None):
                raise ValueError("--asof and export --since are required")
            if args.command == "acquire" and args.source in _EDINET_SOURCES:
                ok = (
                    _acquire_edinet_lists(args.asof, directory)
                    if args.source == "edinet-list"
                    else _acquire_edinet_archives(args.source, args.asof, directory)
                )
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print("stage_ready=false\nnative_l1=true", file=stream)
                return int(not ok)
            if args.command == "acquire" and args.source in {"jquants-financial", "jquants-market"}:
                acquire = (
                    _acquire_financial_l1
                    if args.source == "jquants-financial"
                    else _acquire_market_l1
                )
                ok = acquire(args.asof, directory)
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print("stage_ready=false\nnative_l1=true", file=stream)
                return int(not ok)
            if args.command == "acquire" and args.source in {"jpx-earnings", "jpx-regulations"}:
                from baibai_engine.data_collection.providers.jpx.http import JPXHTTP
                from baibai_jobs.acquisition.jpx_native import acquire as acquire_jpx

                with closing(JPXHTTP()) as client:
                    ok = acquire_jpx(args.source, args.asof, directory, client)
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print("stage_ready=true", file=stream)
                return int(not ok)
            since = args.since or datetime.now(UTC)
            status = 0
            if args.command == "acquire":
                raise ValueError(
                    "Use the source's explicit original receipt import; no live client here"
                )
            master_input = None
            calendar_input = None
            if args.source == "tradingview":
                if args.input_directory is None:
                    raise ValueError("native TradingView export requires its fixed source input")
                saved = json.loads((args.input_directory / "result.json").read_bytes())
                master_input = InputSnapshot.model_validate(saved["master_native_input"])
                calendar_input = InputSnapshot.model_validate(saved["calendar_native_input"])
            if master_input is None and calendar_input is None:
                _export(args.source, args.asof, since, args.database, directory)
            else:
                _export(
                    args.source,
                    args.asof,
                    since,
                    args.database,
                    directory,
                    master_input,
                    calendar_input,
                )
            report = json.loads((directory / "result.json").read_text())
            report["acquisition"] = {"status": "failed" if status else "received"}
            if args.input_directory is not None:
                report["input_generation"] = json.loads(
                    (args.input_directory / "result.json").read_text()
                )["canonical_generations"]
            write_json_atomic(directory / "result.json", report)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a", encoding="utf-8") as stream:
                    print("stage_ready=true", file=stream)
            return status
    except Exception as error:
        # Raw provider payload and credentials must not enter cross-job output.
        print(f"source handoff failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
