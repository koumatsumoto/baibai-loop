"""J-Quants観測coverageの永続識別子とkey。"""

from datetime import date

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
