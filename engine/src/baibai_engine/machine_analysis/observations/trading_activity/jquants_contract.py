"""J-Quants観測の永続識別子と週次・日次信用残の対象期間。"""

from datetime import date

from baibai_engine.data_collection.providers.jquants.market_rules import (
    ALL_ISSUES_DAILY_FIRST_BALANCE_DATE,
    WEEKLY_SERIES_LAST_BALANCE_DATE,
)

WEEKLY_MARGIN_SOURCE = "jquants_weekly_margin"
WEEKLY_MARGIN_OPERATION = "get_mkt_margin_interest"
MARGIN_ALERT_SOURCE = "jquants_margin_alerts"
MARGIN_ALERT_OPERATION = "get_mkt_margin_alert_range"
ALL_ISSUES_DAILY_MARGIN_SOURCE = "jquants_all_issues_daily_margin"
ALL_ISSUES_DAILY_MARGIN_OPERATION = "get_mkt_margin_interest"

SHORT_SALE_REPORT_SOURCE = "jquants_short_sale_reports"
SHORT_SALE_REPORT_OPERATION = "get_mkt_short_sale_report"


def weekly_margin_coverage_key(week_end: date) -> str:
    iso = week_end.isoformat()
    return f"{WEEKLY_MARGIN_OPERATION}:{iso}..{iso}"


def all_issues_daily_margin_coverage_key(balance_date: date) -> str:
    iso = balance_date.isoformat()
    return f"{ALL_ISSUES_DAILY_MARGIN_OPERATION}:{iso}..{iso}"


def require_weekly_series_balance_date(balance_date: date) -> None:
    if balance_date > WEEKLY_SERIES_LAST_BALANCE_DATE:
        raise ValueError(
            "jquants_weekly_margin accepts only the legacy weekly series through "
            f"{WEEKLY_SERIES_LAST_BALANCE_DATE.isoformat()}; got {balance_date.isoformat()}"
        )


def require_all_issues_daily_balance_date(balance_date: date) -> None:
    if balance_date < ALL_ISSUES_DAILY_FIRST_BALANCE_DATE:
        raise ValueError(
            "jquants_all_issues_daily_margin accepts only the daily all-issues series from "
            f"{ALL_ISSUES_DAILY_FIRST_BALANCE_DATE.isoformat()}; got {balance_date.isoformat()}"
        )
