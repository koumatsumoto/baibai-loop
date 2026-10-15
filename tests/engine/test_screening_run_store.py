from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml
from tests.engine.test_candidate_discovery import GOLDEN, _analysis

from baibai_engine.read_api.screening import previous_run_revision_id
from baibai_engine.screening.discovery.contracts import APPROACH_IDS
from baibai_engine.screening.discovery.projection import candidate_rows
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.screening.run_store import (
    RunStoreAmbiguousError,
    RunStoreConflictError,
    ScreeningRunReader,
    ScreeningRunStore,
    connect_read_only,
    initialize_run_store,
)
from baibai_engine.screening.run_store import store as run_store
from baibai_engine.screening.run_store.read import stored_run_page
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION
from baibai_engine.screening.schema import ScreeningRunDocument, StockAnalysis
from baibai_engine.screening.screening_run_serializer import (
    published_run_payload,
    screening_run_payload,
    serialize_published_run_yaml,
    stock_analysis_payload,
)

RULES = load_screening_rules()
RULES_HASH = production_rules_contract_hash(RULES.model_dump_json())


def _run(*, rows=None, asof="2026-10-14", at=None):
    return {
        "schema_version": 1,
        "kind": "stock_analysis_run",
        "run_id": f"screening-{asof.replace('-', '')}",
        "run_date": asof,
        "asof_date": asof,
        "run_at": at or f"{asof}T18:00:00+09:00",
        "universe_size": len(rows) if rows is not None else 1,
        "screening_rules_hash": RULES_HASH,
        "er_model_version": "expected-return-v1",
        "stock_analyses": [_analysis("1111")] if rows is None else rows,
    }


def _publish(path, payload=None, *, revision=None):
    return ScreeningRunStore(path).publish_run(
        _run() if payload is None else payload, rules=RULES, run_revision_id=revision
    )


def test_schema6_contains_single_header_and_stock_rows(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    assert initialize_run_store(path) == RUN_STORE_SCHEMA_VERSION == 6
    assert initialize_run_store(path) == 6
    result = _publish(path)
    assert result.inserted
    assert result.publication_id.startswith("run-revision-")
    with closing(sqlite3.connect(path)) as connection, connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert tables == {"screening_run", "stock_analysis"}
        header = json.loads(connection.execute("SELECT payload FROM screening_run").fetchone()[0])
        assert "stock_analyses" not in header
        assert "candidate_discovery" in header
    run = ScreeningRunReader(path).get_run(result.publication_id)
    assert run is not None
    assert run.stock_analyses[0]["candidate"]["common_eligible"] is True
    assert len(run.stock_analyses[0]["candidate"]["reasons"]) == 4
    assert not hasattr(result, "review_set_id")
    assert not hasattr(ScreeningRunStore(path), "publish_review_set")
    assert not hasattr(ScreeningRunReader(path), "get_review_set")


@pytest.mark.parametrize("rows", [[], [{**_analysis("1111"), "market_cap_oku": None}]])
def test_zero_analyses_and_zero_candidates_are_valid_completed_runs(tmp_path: Path, rows) -> None:
    path = tmp_path / "runs.sqlite"
    result = _publish(path, _run(rows=rows))
    run = ScreeningRunReader(path).get_run(result.publication_id)
    assert run is not None
    assert len(run.stock_analyses) == len(rows)
    assert candidate_rows(run.stock_analyses) == ()
    assert run.payload["candidate_discovery"]["diagnostics"] == {
        "common_eligible_count": 0,
        "candidate_count": 0,
        "candidate_counts_by_approach": dict.fromkeys(APPROACH_IDS, 0),
    }


def test_immutable_idempotency_includes_header_all_rows_and_candidate_context(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.sqlite"
    result = _publish(path, revision="run-a")
    repeated = _publish(path)
    assert repeated.publication_id == result.publication_id
    assert repeated.inserted is False
    changed = _run()
    changed["stock_analyses"][0]["metrics"]["er_annual"] = 0.15
    with pytest.raises(RunStoreConflictError, match="differs"):
        _publish(path, changed)
    with pytest.raises(RunStoreConflictError, match="different run_revision_id"):
        _publish(path, revision="run-b")
    assert len(ScreeningRunReader(path).list_runs()) == 1


def test_child_insert_failure_rolls_back_header_and_all_children(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    initialize_run_store(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_second BEFORE INSERT ON stock_analysis "
            "WHEN NEW.ticker='2222' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        _publish(path, _run(rows=[_analysis("1111"), _analysis("2222")]))
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("SELECT count(*) FROM screening_run").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM stock_analysis").fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ["rules", "candidate", "summary", "duplicate", "legacy_key"])
def test_writer_rejects_source_rule_mismatch_and_caller_candidate_context(
    tmp_path: Path, mutation
) -> None:
    path = tmp_path / "runs.sqlite"
    payload = _run()
    if mutation == "rules":
        payload["screening_rules_hash"] = "different"
    elif mutation == "candidate":
        payload["stock_analyses"][0]["candidate"] = {"common_eligible": False}
    elif mutation == "summary":
        payload["candidate_discovery"] = {}
    elif mutation == "duplicate":
        payload["stock_analyses"] *= 2
    else:
        payload["security_analyses"] = payload.pop("stock_analyses")
    with pytest.raises(
        ValueError,
        match=r"rules|owned|duplicate|stock_analyses",
    ):
        _publish(path, payload)
    assert not path.exists()


def test_read_export_preserves_saved_raw_values_candidate_and_nulls(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    raw = _run()
    raw["stock_analyses"][0]["metrics"].update(custom_known_value=0.123456789, missing=None)
    result = _publish(path, raw)
    run = ScreeningRunReader(path).get_run(result.publication_id)
    exported = published_run_payload(run)
    assert yaml.safe_load(serialize_published_run_yaml(run)) == exported
    assert exported["run_revision_id"] == result.publication_id
    assert exported["stock_analyses"][0]["metrics"]["custom_known_value"] == 0.123456789
    assert exported["stock_analyses"] == list(run.stock_analyses)
    assert "security_analyses" not in exported
    with pytest.raises(ValueError, match="owned"):
        _publish(path, exported)


def test_latest_same_day_cutoff_and_unknown_exact_do_not_fallback(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    old = _publish(path, _run(asof="2026-10-13"), revision="old")
    early = _publish(path, _run(at="2026-10-14T09:00:00+09:00"), revision="early")
    empty = _publish(path, _run(rows=[], at="2026-10-14T12:00:00+09:00"), revision="empty")
    future = _publish(path, _run(asof="2026-10-15"), revision="future")
    reader = ScreeningRunReader(path)
    assert reader.latest_run().run_revision_id == future.publication_id
    assert reader.latest_run_on(as_of=date(2026, 10, 14)).run_revision_id == empty.publication_id
    assert candidate_rows(reader.latest_run_on(as_of=date(2026, 10, 14)).stock_analyses) == ()
    with pytest.raises(RunStoreAmbiguousError):
        reader.resolve_run(as_of_date="2026-10-14")
    cutoff = datetime.fromisoformat("2026-10-14T01:00:00+00:00")
    assert (
        reader.analysis_run(as_of=date(2026, 10, 14), known_at=cutoff).run_revision_id
        == early.publication_id
    )
    assert (
        reader.analysis_run(as_of=date(2026, 10, 13), known_at=cutoff).run_revision_id
        == old.publication_id
    )
    for identifier in ("unknown", "future", "empty"):
        with pytest.raises(
            ValueError,
            match=r"unknown|cutoff",
        ):
            reader.analysis_run(
                as_of=date(2026, 10, 14), known_at=cutoff, run_revision_id=identifier
            )
    with pytest.raises(ValueError, match="timezone"):
        reader.analysis_run(
            as_of=date(2026, 10, 14), known_at=datetime.fromisoformat("2026-10-14T00:00:00")
        )
    assert reader.latest_run_on(as_of=date(2026, 10, 12)) is None
    assert reader.get_run("unknown") is None


@pytest.mark.parametrize(
    "candidate",
    [None, {}, {"common_eligible": False, "reasons": [], "normalized_per_3fy_sector_gap": "0"}],
)
@pytest.mark.parametrize("damaged_ticker", ["1111", "2222"])
@pytest.mark.parametrize("candidates_only", [False, True])
@pytest.mark.parametrize("ticker", [None, "1111", "9999"])
def test_paged_read_checks_whole_run_before_filters(
    tmp_path: Path, candidate, candidates_only, ticker, damaged_ticker
) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(
        path,
        _run(rows=[_analysis("1111"), {**_analysis("2222"), "market_cap_oku": None}]),
        revision="run-a",
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE stock_analysis SET payload=json_set(payload,'$.candidate',json(?)) WHERE ticker=?",
            (json.dumps(candidate), damaged_ticker),
        )
    with closing(connect_read_only(path)) as connection, connection:
        connection.execute("BEGIN")
        filters = {"run_revision_id": "run-a", "candidates_only": candidates_only}
        if ticker is not None:
            filters["ticker"] = ticker
        with pytest.raises(
            ValueError,
            match=r"invalid|StockCandidateContext|CandidateDiscoverySummary",
        ):
            stored_run_page(
                connection, kind="stock_analysis", filters=filters, after=None, limit=1, full=True
            )
    with pytest.raises(
        ValueError,
        match=r"invalid|StockCandidateContext|CandidateDiscoverySummary",
    ):
        ScreeningRunReader(path).get_run("run-a")


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "boolean",
        "reasons",
        "gap",
        "ineligible",
        "reason_approach",
        "reason_method",
        "reason_rank",
        "header",
    ],
)
def test_saved_candidate_shape_drift_is_invalid_not_empty(tmp_path: Path, mutation) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, revision="run-a")
    with closing(sqlite3.connect(path)) as connection, connection:
        raw = json.loads(connection.execute("SELECT payload FROM stock_analysis").fetchone()[0])
        candidate = raw["candidate"]
        if mutation == "missing":
            raw.pop("candidate")
        elif mutation == "boolean":
            candidate["common_eligible"] = "true"
        elif mutation == "reasons":
            candidate["reasons"] = {}
        elif mutation == "gap":
            candidate.pop("normalized_per_3fy_sector_gap")
        elif mutation == "ineligible":
            candidate["common_eligible"] = False
        elif mutation == "reason_approach":
            candidate["reasons"][0]["valuation_approach_id"] = "unknown"
        elif mutation == "reason_method":
            candidate["reasons"][0]["valuation_method_id"] = ""
        elif mutation == "reason_rank":
            candidate["reasons"][0]["rank"] = False
        else:
            connection.execute(
                "UPDATE screening_run SET payload=json_remove(payload,'$.candidate_discovery')"
            )
        connection.execute("UPDATE stock_analysis SET payload=?", (json.dumps(raw),))
    with closing(connect_read_only(path)) as connection, connection:
        connection.execute("BEGIN")
        with pytest.raises(
            ValueError,
            match=r"invalid|StockCandidateContext|CandidateDiscoverySummary",
        ):
            stored_run_page(
                connection,
                kind="stock_analysis",
                filters={"run_revision_id": "run-a", "candidates_only": True},
                after=None,
                limit=1,
            )


def test_pagination_preserves_ordinal_with_revision_and_candidates_filter(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(
        path,
        _run(
            rows=[
                _analysis("2222"),
                {**_analysis("1111"), "market_cap_oku": None},
                _analysis("3333"),
            ]
        ),
        revision="run-a",
    )
    with closing(connect_read_only(path)) as connection, connection:
        connection.execute("BEGIN")
        filters = {"run_revision_id": "run-a", "candidates_only": True}
        first = stored_run_page(
            connection, kind="stock_analysis", filters=filters, after=None, limit=1, full=True
        )
        second = stored_run_page(
            connection,
            kind="stock_analysis",
            filters=filters,
            after=[first[0]["ordinal"]],
            limit=1,
            full=True,
        )
    assert first[0]["ticker"] == "2222"
    assert second[0]["ticker"] == "3333"
    assert "candidate" in first[0]["payload"]


def test_prune_and_old_schema_require_explicit_cutover(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    for day in (12, 13, 14):
        _publish(path, _run(asof=f"2026-10-{day}"))
    pruned = ScreeningRunStore(path).prune(keep=1)
    assert pruned.deleted_runs == pruned.deleted_stock_analyses == 2
    assert not hasattr(pruned, "deleted_review_sets")
    assert len(ScreeningRunReader(path).list_runs()) == 1
    legacy = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(legacy)) as connection, connection:
        connection.execute("CREATE TABLE old_cache(value TEXT)")
        connection.execute("PRAGMA user_version=5")
    with pytest.raises(RuntimeError, match="rebuild"):
        initialize_run_store(legacy)
    with pytest.raises(RuntimeError, match="rebuild"):
        ScreeningRunReader(legacy).latest_run()


def test_stock_analysis_schema_and_serializer_keep_original_numeric_rounding() -> None:
    analysis = StockAnalysis(
        ticker="1111",
        name="合成会社",
        sector_33="合成業種",
        per_forward=10.005,
        per_trailing=11.999,
        pbr=0.855,
        ev_ebitda=5.15,
        p_s=1.234,
        pcfr=9.99,
        ttm_quality={},
        market_cap_oku=99,
        avg_turnover_oku=1.25,
        listing_span_days=20,
        price_change_1d=0.012345,
        metrics={
            "er_annual": 0.123456,
            "normalized_per_3fy": 10.123456,
            "forecast_special_gain_flag": False,
            "unknown": None,
        },
    )
    assert stock_analysis_payload(analysis) == {**GOLDEN["rounded_analysis"], "candidate": None}
    document = ScreeningRunDocument(
        run_date=date(2026, 10, 14),
        asof_date=date(2026, 10, 14),
        universe_size=1,
        filters={},
        stock_analyses=[analysis],
        run_at=datetime.fromisoformat("2026-10-14T18:00:00+09:00"),
        run_id="screening-20261014",
        screening_rules_hash=RULES_HASH,
        er_model_version="expected-return-v1",
    )
    payload = screening_run_payload(document)
    assert payload["schema_version"] == 1
    assert payload["kind"] == "stock_analysis_run"
    assert payload["stock_analyses"][0]["candidate"] is None
    assert analysis.candidate is None
    with pytest.raises(
        ValueError,
        match="finite",
    ):
        StockAnalysis(**{**asdict(analysis), "per_forward": float("inf")})


@pytest.mark.parametrize(
    ("cutoff", "expected"),
    [("000100", "zz-early"), ("000500", "aa-late"), ("000800", "00-latest")],
)
def test_research_cutoff_and_latest_selection_preserve_sub_millisecond_clock(
    tmp_path: Path, cutoff: str, expected: str
) -> None:
    path = tmp_path / "runs.sqlite"
    for fraction, revision in (
        ("000000", "zz-early"),
        ("000400", "aa-late"),
        ("000700", "00-latest"),
    ):
        _publish(path, _run(at=f"2026-10-14T12:00:00.{fraction}+09:00"), revision=revision)
    reader = ScreeningRunReader(path)
    known_at = datetime.fromisoformat(f"2026-10-14T12:00:00.{cutoff}+09:00")
    run = reader.analysis_run(as_of=date(2026, 10, 14), known_at=known_at)
    assert run is not None
    assert run.run_revision_id == expected
    for fraction, revision in (
        ("000000", "zz-early"),
        ("000400", "aa-late"),
        ("000700", "00-latest"),
    ):
        if fraction > cutoff:
            with pytest.raises(ValueError, match="after the Research cutoff"):
                reader.analysis_run(
                    as_of=date(2026, 10, 14), known_at=known_at, run_revision_id=revision
                )
        else:
            exact = reader.analysis_run(
                as_of=date(2026, 10, 14), known_at=known_at, run_revision_id=revision
            )
            assert exact is not None
            assert exact.run_revision_id == revision


def test_research_cutoff_compares_offset_equivalent_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, _run(at="2026-10-14T12:00:00.000400+09:00"), revision="aa-jst")
    _publish(path, _run(at="2026-10-14T03:00:00.000400+00:00"), revision="zz-utc")
    reader = ScreeningRunReader(path)
    known_at = datetime.fromisoformat("2026-10-14T03:00:00.000400+00:00")
    run = reader.analysis_run(as_of=date(2026, 10, 14), known_at=known_at)
    assert run is not None
    assert run.run_revision_id == "zz-utc"


@pytest.fixture
def precise_clock_runs(tmp_path):
    path = tmp_path / "runs.sqlite"
    revisions = [
        "run-revision-ffffffff-ffff-4fff-bfff-ffffffffffff",
        "run-revision-00000000-0000-4000-8000-000000000001",
        "run-revision-00000000-0000-4000-8000-000000000000",
    ]
    clocks = [
        "2026-10-14T12:00:00.000400+09:00",
        "2026-10-14T03:00:00.000401+00:00",
        "2026-10-14T12:00:00.000402+09:00",
    ]
    for revision, clock in zip(revisions, clocks, strict=True):
        _publish(path, _run(at=clock), revision=revision)
    return path, revisions, clocks


@pytest.mark.parametrize(
    "query", ["latest_run", "latest_run_on", "valuation_run", "analysis_run", "list_runs"]
)
def test_run_readers_use_the_same_exact_instant_order(precise_clock_runs, query):
    path, revisions, clocks = precise_clock_runs
    reader = ScreeningRunReader(path)
    before = path.read_bytes()
    arguments = {}
    if query in {"latest_run_on", "valuation_run", "analysis_run"}:
        arguments["as_of"] = date(2026, 10, 14)
    if query == "analysis_run":
        arguments["known_at"] = datetime.fromisoformat(clocks[-1])
    result = getattr(reader, query)(**arguments)
    if query == "list_runs":
        assert [run.run_revision_id for run in result] == list(reversed(revisions))
        assert [run.run_at for run in result] == list(reversed(clocks))
    else:
        assert result.run_revision_id == revisions[-1]
        assert result.run_at == clocks[-1]
    assert path.read_bytes() == before


@pytest.mark.parametrize("full", [False, True])
def test_run_cursor_pages_preserve_exact_instant_order(precise_clock_runs, full):
    path, revisions, clocks = precise_clock_runs
    before = path.read_bytes()
    rows, cursor = [], None
    with closing(connect_read_only(path)) as connection, connection:
        for _ in range(4):
            page = stored_run_page(
                connection, kind="screening_run", filters={}, after=cursor, limit=1, full=full
            )
            if not page:
                break
            rows.extend(page)
            last = page[-1]
            cursor = [last["asof_date"], last["page_time"], last["run_revision_id"]]
    assert [row["run_revision_id"] for row in rows] == revisions
    assert [row["run_at"] for row in rows] == clocks
    assert path.read_bytes() == before


def test_prune_keeps_the_exact_latest_instants_without_rewriting_clocks(precise_clock_runs):
    path, revisions, clocks = precise_clock_runs
    result = ScreeningRunStore(path).prune(keep=2)
    assert result.deleted_runs == result.deleted_stock_analyses == 1
    with closing(sqlite3.connect(path)) as connection, connection:
        retained = dict(connection.execute("SELECT run_revision_id, run_at FROM screening_run"))
    assert retained == dict(zip(revisions[1:], clocks[1:], strict=True))


def test_previous_run_facade_preserves_exact_instant_order(precise_clock_runs):
    path, revisions, _ = precise_clock_runs
    before = path.read_bytes()
    assert previous_run_revision_id(path, date(2026, 10, 15)) == revisions[-1]
    assert path.read_bytes() == before


def test_initialization_failure_rolls_back_run_schema_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    with monkeypatch.context() as patch:
        patch.setattr(run_store, "SCHEMA_SQL", run_store.SCHEMA_SQL + "\nINVALID SQL;")
        with pytest.raises(sqlite3.OperationalError):
            initialize_run_store(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT name FROM sqlite_schema").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert initialize_run_store(path) == RUN_STORE_SCHEMA_VERSION
