"""Physical dataset contracts; safe for schema/lake to import without services."""

from .models import QUANTS_METRICS, VALUATION_METRICS

MODEL_VALUATION_TABLE = "retail_model_valuation_observations"
ANALYST_RATING_TABLE = "retail_analyst_rating_observations"
QUANTS_SCORE_TABLE = "retail_quants_score_observations"
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
