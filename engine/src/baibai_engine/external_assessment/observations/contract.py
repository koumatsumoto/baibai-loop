"""Physical dataset contracts; safe for schema/lake to import without services."""

from baibai_engine.external_assessment.observations.models import QUANTS_METRICS, VALUATION_METRICS

from .identity import ANALYST_FORECAST_TABLE as ANALYST_FORECAST_TABLE
from .identity import ANALYST_RATING_TABLE as ANALYST_RATING_TABLE
from .identity import MODEL_VALUATION_TABLE as MODEL_VALUATION_TABLE
from .identity import QUANTS_SCORE_TABLE as QUANTS_SCORE_TABLE

type Column = tuple[str, str, bool, int]

_COMMON_COLUMNS: tuple[Column, ...] = (
    ("ticker", "TEXT", False, 1),
    ("fetched_at_utc", "TEXT", False, 2),
    ("snapshot_date", "TEXT", False, 0),
    ("distributor", "TEXT", False, 0),
    ("origin", "TEXT", True, 0),
    ("source_url", "TEXT", False, 0),
    ("parser_version", "TEXT", False, 0),
    ("contract_version", "TEXT", False, 0),
)
MODEL_VALUATION_COLUMNS: tuple[Column, ...] = (
    *_COMMON_COLUMNS,
    *((name, "REAL", True, 0) for name in VALUATION_METRICS),
    *((name, "TEXT", True, 0) for name in ("currency", "unit", "share_basis")),
    *(
        (f"{metric}_{suffix}", kind, True, 0)
        for metric in VALUATION_METRICS
        for suffix, kind in (
            ("origin", "TEXT"),
            ("period", "TEXT"),
            ("count", "INTEGER"),
            ("updated_at_utc", "TEXT"),
            ("updated_label", "TEXT"),
        )
    ),
)
ANALYST_RATING_COLUMNS: tuple[Column, ...] = (
    *_COMMON_COLUMNS,
    ("rating", "REAL", True, 0),
    ("rating_scale_min", "REAL", True, 0),
    ("rating_scale_max", "REAL", True, 0),
    ("rating_scale_direction", "TEXT", True, 0),
    ("rating_count", "INTEGER", True, 0),
    ("rating_updated_at_utc", "TEXT", True, 0),
    ("rating_updated_label", "TEXT", True, 0),
)
QUANTS_SCORE_COLUMNS: tuple[Column, ...] = (
    *_COMMON_COLUMNS,
    ("score_as_of_date", "TEXT", False, 0),
    ("score_scale_max", "INTEGER", False, 0),
    ("composite_scale_max", "INTEGER", False, 0),
    *(
        (name, "REAL", True, 0)
        for name in (*QUANTS_METRICS, "composite_score", "industry_average_score")
    ),
)


def _ddl(table: str, columns: tuple[Column, ...]) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {table}("
        + ",".join(
            f"{name} {kind}" + (" NOT NULL" if not nullable else "")
            for name, kind, nullable, _ in columns
        )
        + ",PRIMARY KEY(ticker,fetched_at_utc));"
        + f"CREATE INDEX IF NOT EXISTS idx_{table}_snapshot ON {table}(snapshot_date);"
    )


MODEL_VALUATION_DDL = _ddl(MODEL_VALUATION_TABLE, MODEL_VALUATION_COLUMNS)
ANALYST_RATING_DDL = _ddl(ANALYST_RATING_TABLE, ANALYST_RATING_COLUMNS)
QUANTS_SCORE_DDL = _ddl(QUANTS_SCORE_TABLE, QUANTS_SCORE_COLUMNS)


ANALYST_FORECAST_COLUMNS: tuple[Column, ...] = (
    *_COMMON_COLUMNS,
    ("availability", "TEXT", False, 0),
    ("display_as_of_date", "TEXT", True, 0),
    ("rating_label", "TEXT", True, 0),
    *(
        (name, "INTEGER", True, 0)
        for name in (
            "strong_buy_count",
            "buy_count",
            "neutral_count",
            "sell_count",
            "strong_sell_count",
            "target_horizon_months",
        )
    ),
    ("target_price_yen", "REAL", True, 0),
    *(
        (name, "TEXT", True, 0)
        for name in (
            "forecast_period_label",
            "analyst_forecast_as_of_date",
            "company_forecast_as_of_date",
            "share_basis",
        )
    ),
    *(
        (f"{subject}_{metric}", "REAL", True, 0)
        for subject in ("analyst", "company")
        for metric in ("revenue_million_yen", "net_income_million_yen", "eps_yen")
    ),
)
ANALYST_FORECAST_DDL = _ddl(ANALYST_FORECAST_TABLE, ANALYST_FORECAST_COLUMNS)
