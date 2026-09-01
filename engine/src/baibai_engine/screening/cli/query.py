"""Read-only CLI commands over cached data: profiles, snapshots, and Review Sets."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TextIO
from uuid import uuid4

import yaml

from baibai_engine.appdb import database_path
from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.time import JST
from baibai_engine.market.store import latest_daily_bar_date
from baibai_engine.screening.discovery import PublishedReviewSet, build_review_set
from baibai_engine.screening.market_snapshot import build_market_snapshot
from baibai_engine.screening.rule_config import (
    DEFAULT_RULES_PATH,
    ScreeningRules,
    load_screening_rules,
)
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.screening.run_store import (
    ScreeningRunReader,
    ScreeningRunStore,
)
from baibai_engine.screening.schema import (
    normalize_ticker,
)
from baibai_engine.screening.ticker_profile import build_ticker_profile

from .common import _NoAliasDumper, _parse_iso_date


def ticker_profile_command(
    *,
    ticker: str,
    asof: str | None,
    sqlite_path: Path,
    runs_db_path: Path,
    app_db_path: Path,
    run_revision_id: str | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        normalized = normalize_ticker(ticker)
    except (ValueError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if asof is not None:
        asof_date = _parse_iso_date(asof)
    else:
        today = datetime.now(JST).date()
        resolved = latest_daily_bar_date(sqlite_path, today - timedelta(days=30), today)
        if resolved is None:
            print(
                f"no cached daily bars found to resolve --asof: {sqlite_path}",
                file=sys.stderr,
            )
            return 1
        asof_date = resolved
    try:
        payload = build_ticker_profile(
            sqlite_path=sqlite_path,
            ticker=normalized,
            asof_date=asof_date,
            runs_db_path=runs_db_path,
            app_db_path=database_path(app_db_path),
            run_revision_id=run_revision_id,
        )
    except (ValueError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    yaml.dump(payload, out, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    return 0


def market_snapshot_command(
    *,
    asof: str | None,
    weeks: int,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    if weeks < 1:
        print("--weeks must be greater than zero", file=sys.stderr)
        return 1
    if asof is not None:
        asof_date = _parse_iso_date(asof)
    else:
        today = datetime.now(JST).date()
        resolved = latest_daily_bar_date(sqlite_path, today - timedelta(days=30), today)
        if resolved is None:
            print(
                f"no cached daily bars found to resolve --asof: {sqlite_path}",
                file=sys.stderr,
            )
            return 1
        asof_date = resolved
    payload = build_market_snapshot(
        sqlite_path=sqlite_path,
        asof_date=asof_date,
        history_weeks=weeks,
    )
    yaml.dump(payload, out, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    return 0


def review_set_publish_command(
    *,
    asof_date: date,
    rules: ScreeningRules | None = None,
    output_path: Path | None = None,
    force: bool = False,
    stdout: TextIO | None = None,
    run_revision_id: str | None = None,
    runs_db_path: Path | None = None,
) -> int:
    if output_path is not None and output_path.exists() and not force:
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 1

    out = stdout if stdout is not None else sys.stdout
    rules = rules or load_screening_rules(_rules_path_from_env())
    if run_revision_id is None:
        print("run_revision_id is required", file=sys.stderr)
        return 1
    try:
        run = ScreeningRunReader(runs_db_path).get_run(run_revision_id)
        if run is None:
            raise ValueError(f"unknown run_revision_id: {run_revision_id}")
        if run.as_of_date != asof_date.isoformat():
            raise ValueError("run revision as-of does not match --asof")
        current_rules_hash = production_rules_contract_hash(rules.model_dump_json())
        if run.payload.get("screening_rules_hash") != current_rules_hash:
            raise ValueError("source run rules do not match current candidate discovery rules")
        review_set_id = f"review-set-{asof_date:%Y%m%d}-{uuid4().hex[:12]}"
        created_at = datetime.now(UTC)
        payload = PublishedReviewSet.model_validate(
            {
                "review_set_id": review_set_id,
                "run_revision_id": run_revision_id,
                "as_of": asof_date,
                "created_at": created_at,
                "screening_rules_hash": current_rules_hash,
                **build_review_set(
                    run.security_analyses,
                    rules=rules.candidate_discovery,
                    required_jpx_flags=rules.universe.required_jpx_flags,
                ),
            }
        ).model_dump(mode="json")
        publication = ScreeningRunStore(runs_db_path).publish_review_set(
            run_revision_id=run_revision_id,
            payload=payload,
            rules=rules.candidate_discovery,
            required_jpx_flags=rules.universe.required_jpx_flags,
            review_set_id=review_set_id,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"screening review-set publication failed: {exc}", file=sys.stderr)
        return 1
    if publication.publication_id != payload["review_set_id"]:  # pragma: no cover
        raise AssertionError("review set publication identity drift")
    rendered = yaml.dump(payload, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    # --output-path 指定時は同じ内容を file と stdout の両方へ出す。file は
    # local/rebuildable な保存先で、canonical 判断は thesis だけが担う。
    if output_path is not None:
        write_text_atomic(output_path, rendered)
    out.write(rendered)
    return 0


def review_set_show_command(
    *,
    review_set_id: str,
    runs_db_path: Path | None = None,
    output_path: Path | None = None,
    force: bool = False,
    stdout: TextIO | None = None,
) -> int:
    """Re-emit one immutable Review Set without publishing a replacement."""
    if output_path is not None and output_path.exists() and not force:
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 1

    out = stdout if stdout is not None else sys.stdout
    try:
        publication = ScreeningRunReader(runs_db_path).get_review_set(review_set_id)
    except (OSError, sqlite3.Error) as exc:
        print(f"screening run store is unreadable: {exc}", file=sys.stderr)
        return 1
    if publication is None:
        # No reconstruction from candidates: a Review Set that is not stored was
        # never published, and guessing one would fabricate a decision input.
        print(f"review set not found: {review_set_id}", file=sys.stderr)
        return 1

    payload = publication.payload
    rendered = yaml.dump(payload, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    if output_path is not None:
        write_text_atomic(output_path, rendered)
    out.write(rendered)
    return 0


def _rules_path_from_env() -> Path:
    return Path(os.environ.get("SCREENING_RULES_PATH") or DEFAULT_RULES_PATH)
