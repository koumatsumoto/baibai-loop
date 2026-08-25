from __future__ import annotations

import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from tests.helpers.screening_run import screening_candidate, screening_run_payload
from tests.helpers.screening_selection import value_carry_selection_payload

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.screening.cli.app import main as screening_main
from baibai_engine.screening.run_store import (
    RunStoreAmbiguousError,
    RunStoreConflictError,
    RunStoreNotFoundError,
    ScreeningRunReader,
    ScreeningRunStore,
    initialize_run_store,
)
from baibai_engine.screening.run_store import read as run_store_read
from baibai_engine.screening.run_store.migrations import MIGRATIONS
from baibai_engine.screening.run_store.store import (
    _application_git_commit,
    unchanged_application_git_commit,
)
from baibai_engine.screening.selection.contracts import (
    ValueCarryOnlyAttentionParameters,
    value_carry_only_attention_policy_hash,
)

_RULES_HASH = "rules-fixture"
_MODEL_ID = "expected-return-v1"


def _run(
    *,
    as_of: str = "2026-07-08",
    run_at: str = "2026-07-09T01:59:42+09:00",
    ticker: str = "1301",
) -> dict[str, object]:
    return screening_run_payload(
        as_of=as_of,
        run_at=run_at,
        rules_hash=_RULES_HASH,
        model_id=_MODEL_ID,
        candidates=[
            screening_candidate(
                ticker,
                per_forward=7.43,
                per_trailing=7.82,
                pbr=0.69,
                metrics={"dividend_yield": 0.021, "er_annual": 0.13},
            )
        ],
        filters={"scope": "all-common-stocks"},
        generated_by="screening-cli-v1",
        data_sources=["j-quants-light"],
        provider_status_lines=[],
        fallback_lines=[],
    )


def _selection(
    ticker: str = "1301",
    *,
    asof: str = "2026-07-08",
    candidates_ref: str = "run-revision-fixture",
    macro_context_ref: str | None = None,
) -> dict[str, object]:
    source_run = _run(as_of=asof, ticker=ticker)
    return value_carry_selection_payload(
        ticker=ticker,
        er_annual=0.13,
        rules_hash=_RULES_HASH,
        model_id=_MODEL_ID,
        asof=asof,
        candidates_ref=candidates_ref,
        macro_context_ref=macro_context_ref,
        source_candidates=source_run["candidates"],  # type: ignore[arg-type]
        recommendations=[{"ticker": ticker, "rank": 1, "reason_tags": ["cheap"]}],
    )


def test_run_store_has_independent_forward_schema(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"

    assert initialize_run_store(database) == 3
    assert initialize_run_store(database) == 3

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "screening_run",
            "screening_candidate",
            "screening_selection",
            "selection_entry",
        } <= tables
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(screening_run)").fetchall()
        }
        assert "application_git_commit" in columns
        selection_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(screening_selection)").fetchall()
        }
        assert "application_git_commit" in selection_columns


def test_v1_migration_verifies_candidate_rows_before_compacting_payload(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    payload = _run()
    _write_v1_run(database, payload, run_revision_id="run-v1")

    assert initialize_run_store(database) == 3

    with sqlite3.connect(database) as connection:
        stored = json.loads(connection.execute("SELECT payload FROM screening_run").fetchone()[0])
        assert stored == {key: value for key, value in payload.items() if key != "candidates"}
        assert len(connection.execute("SELECT payload FROM screening_candidate").fetchall()) == 1


def test_read_only_reader_accepts_v1_store_until_writer_migrates(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    payload = _run()
    _write_v1_run(database, payload, run_revision_id="run-v1")

    run = ScreeningRunReader(database).latest_run()

    assert run is not None
    assert run.application_git_commit is None
    assert list(run.candidates) == payload["candidates"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_v1_migration_rolls_back_when_candidate_rows_differ(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    payload = _run()
    _write_v1_run(database, payload, run_revision_id="run-v1")
    with sqlite3.connect(database) as connection:
        changed = dict(payload["candidates"][0])  # type: ignore[index]
        changed["name"] = "不一致"
        connection.execute(
            "UPDATE screening_candidate SET payload = ?",
            (canonical_json(changed),),
        )

    with pytest.raises(sqlite3.IntegrityError, match="differ from candidate rows"):
        initialize_run_store(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(screening_run)").fetchall()
        }
        assert "application_git_commit" not in columns


def test_v1_migration_is_safe_under_concurrent_initialization(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    _write_v1_run(database, _run(), run_revision_id="run-v1")

    with ThreadPoolExecutor(max_workers=4) as executor:
        versions = list(executor.map(lambda _: initialize_run_store(database), range(4)))

    assert versions == [3, 3, 3, 3]
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert len(connection.execute("PRAGMA table_info(screening_run)").fetchall()) == 10


def test_run_publication_is_immutable_and_same_asof_keeps_revisions(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    first_payload = _run()

    first = store.publish_run(first_payload, run_revision_id="run-revision-a")
    retry = store.publish_run(first_payload)
    second = store.publish_run(
        _run(run_at="2026-07-09T02:30:00+09:00"),
        run_revision_id="run-revision-b",
    )

    assert first.inserted is True
    assert retry == type(retry)("run-revision-a", inserted=False)
    assert second.inserted is True
    changed = dict(first_payload)
    changed["universe_size"] = 999
    with pytest.raises(RunStoreConflictError):
        store.publish_run(changed)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM screening_run").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM screening_candidate").fetchone()[0] == 2


def test_run_read_keeps_one_snapshot_while_prune_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    published = store.publish_run(_run(), run_revision_id="run-snapshot")
    parent_loaded = Event()
    continue_read = Event()
    prune_started = Event()
    original = run_store_read._run_from_row

    def delayed_run_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> object:
        parent_loaded.set()
        assert continue_read.wait(timeout=2)
        return original(connection, row)

    monkeypatch.setattr(run_store_read, "_run_from_row", delayed_run_from_row)

    def prune() -> object:
        prune_started.set()
        return store.prune(keep=0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        read_future = executor.submit(
            ScreeningRunReader(database).get_run,
            published.publication_id,
        )
        assert parent_loaded.wait(timeout=2)
        prune_future = executor.submit(prune)
        assert prune_started.wait(timeout=2)
        continue_read.set()
        run = read_future.result(timeout=2)
        prune_future.result(timeout=2)

    assert run is not None
    assert len(run.candidates) == 1
    assert ScreeningRunReader(database).get_run(published.publication_id) is None


def test_run_payload_is_metadata_only_and_exposes_application_git_commit(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    payload = _run()
    commit = "a" * 40
    store = ScreeningRunStore(database, git_commit_factory=lambda: commit)

    publication = store.publish_run(payload, run_revision_id="run-revision-metadata")

    assert publication.inserted is True
    with sqlite3.connect(database) as connection:
        raw_payload, stored_commit = connection.execute(
            "SELECT payload, application_git_commit FROM screening_run"
        ).fetchone()
    metadata = json.loads(raw_payload)
    assert metadata == {key: value for key, value in payload.items() if key != "candidates"}
    assert "candidates" not in metadata
    assert len(raw_payload) < 100_000
    assert stored_commit == commit
    run = ScreeningRunReader(database).get_run("run-revision-metadata")
    assert run is not None
    assert run.application_git_commit == commit
    assert list(run.candidates) == payload["candidates"]


@pytest.mark.parametrize("git_commit", [None, "not-a-git-hash"])
def test_run_publish_allows_unavailable_git_commit(
    tmp_path: Path,
    git_commit: str | None,
) -> None:
    database = tmp_path / "runs.sqlite"
    ScreeningRunStore(database, git_commit_factory=lambda: git_commit).publish_run(_run())
    run = ScreeningRunReader(database).latest_run()
    assert run is not None
    assert run.application_git_commit is None


def test_application_git_commit_is_bound_to_application_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    expected = "a" * 40
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        output = "" if "status" in arguments else f"{expected}\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)

    assert _application_git_commit() == expected
    assert len(calls) == 2
    assert all(call[1:3] == ["-C", str(repository)] for call in calls)


def test_application_git_commit_is_unavailable_for_a_dirty_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=" M engine/source.py\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _application_git_commit() is None
    assert len(calls) == 1
    assert "status" in calls[0]


def test_starting_commit_is_invalidated_when_clean_head_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "baibai_engine.screening.run_store.store.application_git_commit",
        lambda: "b" * 40,
    )

    assert unchanged_application_git_commit("a" * 40) is None


def test_identical_run_metadata_does_not_hide_candidate_drift(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    payload = _run()
    store.publish_run(payload)
    changed = dict(payload)
    candidate = dict(payload["candidates"][0])  # type: ignore[index]
    candidate["name"] = "別名"
    changed["candidates"] = [candidate]

    with pytest.raises(RunStoreConflictError, match="differs"):
        store.publish_run(changed)


def test_run_parent_and_candidates_are_one_transaction(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    initialize_run_store(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_candidate_failure
            BEFORE INSERT ON screening_candidate
            WHEN NEW.ticker = '1332'
            BEGIN
                SELECT RAISE(ABORT, 'injected candidate failure');
            END
            """
        )
    payload = _run()
    second_candidate = dict(payload["candidates"][0])  # type: ignore[index]
    second_candidate["ticker"] = "1332"
    payload["candidates"] = [
        payload["candidates"][0],  # type: ignore[index]
        second_candidate,
    ]

    with pytest.raises(sqlite3.IntegrityError, match="injected candidate failure"):
        ScreeningRunStore(database).publish_run(payload)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM screening_run").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM screening_candidate").fetchone()[0] == 0


def test_selection_binds_explicit_run_and_read_facade_exposes_metadata(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    commit = "a" * 40
    store = ScreeningRunStore(database, git_commit_factory=lambda: commit)
    store.publish_run(_run(), run_revision_id="run-revision-fixture")
    payload = _selection(macro_context_ref="macro-context-2026-07-08-base")

    result = store.publish_selection(
        run_revision_id="run-revision-fixture",
        profile="default",
        macro_context_id="macro-context-2026-07-08-base",
        payload=payload,
        selection_id="selection-fixture",
    )
    retry = store.publish_selection(
        run_revision_id="run-revision-fixture",
        profile="default",
        macro_context_id="macro-context-2026-07-08-base",
        payload=payload,
        selection_id="selection-fixture",
    )

    assert result.inserted is True
    assert retry.inserted is False
    publication = ScreeningRunReader(database).get_selection("selection-fixture")
    assert publication is not None
    assert publication.selection_id == "selection-fixture"
    assert publication.run_revision_id == "run-revision-fixture"
    assert publication.as_of_date == "2026-07-08"
    assert publication.as_of == "2026-07-08"
    assert publication.profile == "default"
    assert publication.macro_context_id == "macro-context-2026-07-08-base"
    assert publication.application_git_commit == commit
    assert publication.payload == payload
    assert publication.entries == tuple(payload["recommendations"])  # type: ignore[arg-type]
    with sqlite3.connect(database) as connection:
        stored = json.loads(
            connection.execute(
                "SELECT payload FROM screening_selection WHERE selection_id = 'selection-fixture'"
            ).fetchone()[0]
        )
    assert stored == payload


def test_selection_rejects_fabricated_exact_provenance(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-revision-fixture")
    payload = _selection()
    payload["attention_policy_hash"] = "f" * 64

    with pytest.raises(ValueError, match="Attention Policy hash does not match"):
        store.publish_selection(
            run_revision_id="run-revision-fixture",
            profile="default",
            macro_context_id=None,
            payload=payload,
        )


def test_selection_rejects_self_consistent_but_fabricated_policy_hash(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-revision-fixture")
    payload = _selection()
    fabricated = "f" * 64
    origin = payload["longlist_origin"]
    assert isinstance(origin, dict)
    origin["selection_policy_hash"] = fabricated
    longlist = payload["longlist"]
    assert isinstance(longlist, list)
    assert isinstance(longlist[0], dict)
    longlist[0]["selection_policy_hash"] = fabricated
    parameters = ValueCarryOnlyAttentionParameters.model_validate(
        payload["attention_policy_parameters"]
    )
    payload["attention_policy_hash"] = value_carry_only_attention_policy_hash(
        selection_policy_hash=fabricated,
        parameters=parameters,
    )

    with pytest.raises(ValueError, match="Selection Policy hash does not match its inputs"):
        store.publish_selection(
            run_revision_id="run-revision-fixture",
            profile="default",
            macro_context_id=None,
            payload=payload,
        )


def test_selection_rejects_longlist_provenance_that_differs_from_origin(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-revision-fixture")
    payload = _selection()
    longlist = payload["longlist"]
    assert isinstance(longlist, list)
    row = longlist[0]
    assert isinstance(row, dict)
    row["selection_policy_hash"] = "f" * 64

    with pytest.raises(ValueError, match="row Policy hash does not match"):
        store.publish_selection(
            run_revision_id="run-revision-fixture",
            profile="default",
            macro_context_id=None,
            payload=payload,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rank", 99, "longlist ranks must be contiguous and aligned"),
        ("expected_return_pct", -999.0, r"displayed E\[r\] does not match"),
        ("estimate_snapshot", {}, "estimate_snapshot does not match the source candidate"),
        ("fair_value_anchor_yen", 1.0, "fair_value_anchor_yen does not match"),
        ("market_price_yen", 1.0, "market_price_yen does not match"),
        ("name", "改ざん名", "longlist name does not match"),
        (
            "fv_convergence",
            {
                "status": "clear",
                "warning_code": None,
                "market_price_yen": None,
                "anchors_yen": {},
                "er_reversion_annual": None,
            },
            "longlist fv_convergence does not match",
        ),
        ("durability_warnings", [], "longlist durability_warnings does not match"),
        ("event_warnings", ["freshness_warning"], "longlist event_warnings does not match"),
        (
            "selection_reasons",
            ["durability_high"],
            "longlist selection_reasons does not match",
        ),
    ],
)
def test_selection_rejects_display_coordinates_that_differ_from_exact_rank(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-revision-fixture")
    payload = _selection()
    longlist = payload["longlist"]
    assert isinstance(longlist, list)
    assert isinstance(longlist[0], dict)
    longlist[0][field] = value

    with pytest.raises(ValueError, match=message):
        store.publish_selection(
            run_revision_id="run-revision-fixture",
            profile="default",
            macro_context_id=None,
            payload=payload,
        )


@pytest.mark.parametrize("parameter_kind", ["selection", "attention"])
def test_selection_rejects_coerced_wire_parameters(
    tmp_path: Path,
    parameter_kind: str,
) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-revision-fixture")
    payload = _selection()
    if parameter_kind == "selection":
        parameters = payload["selection_policy_parameters"]
        assert isinstance(parameters, dict)
        parameters["required_jpx_flags"] = ""
        message = "invalid Selection Policy parameters"
    else:
        parameters = payload["attention_policy_parameters"]
        assert isinstance(parameters, dict)
        parameters["value_carry_limit"] = "1"
        message = "invalid Attention Policy parameters"

    with pytest.raises(ValueError, match=message):
        store.publish_selection(
            run_revision_id="run-revision-fixture",
            profile="default",
            macro_context_id=None,
            payload=payload,
        )


def test_selection_rejects_model_identity_that_differs_from_source_run(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-revision-fixture")
    payload = _selection()
    selection = payload["selection"]
    assert isinstance(selection, dict)
    selection["er_model_version"] = "expected-return-v2"

    with pytest.raises(ValueError, match="Model ID does not match selection metadata"):
        store.publish_selection(
            run_revision_id="run-revision-fixture",
            profile="default",
            macro_context_id=None,
            payload=payload,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("asof", "2026-07-09", "asof does not match the source run"),
        ("profile", "balanced", "profile does not match the publication"),
        ("candidates_ref", "run-foreign", "candidates_ref does not match the source run"),
        ("macro_context_ref", "macro-foreign", "macro_context_ref does not match"),
    ],
)
def test_selection_rejects_metadata_that_is_not_bound_to_publication(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-revision-fixture")
    payload = _selection()
    selection = payload["selection"]
    assert isinstance(selection, dict)
    if field in {"candidates_ref", "macro_context_ref"}:
        input_refs = selection["input_refs"]
        assert isinstance(input_refs, dict)
        input_refs[field] = value
    else:
        selection[field] = value

    with pytest.raises(ValueError, match=message):
        store.publish_selection(
            run_revision_id="run-revision-fixture",
            profile="default",
            macro_context_id=None,
            payload=payload,
        )


def test_selection_rejects_longlist_order_that_differs_from_policy(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    run = _run()
    second = dict(run["candidates"][0])  # type: ignore[index]
    second["ticker"] = "9999"
    second["metrics"] = {"er_annual": 0.2}
    run["candidates"] = [run["candidates"][0], second]  # type: ignore[index]
    store.publish_run(run, run_revision_id="run-revision-fixture")
    payload = value_carry_selection_payload(
        ticker="1301",
        er_annual=0.13,
        rules_hash=_RULES_HASH,
        model_id=_MODEL_ID,
        longlist=(("1301", 0.13), ("9999", 0.2)),
        source_candidates=run["candidates"],  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="longlist order or Evidence Pattern"):
        store.publish_selection(
            run_revision_id="run-revision-fixture",
            profile="default",
            macro_context_id=None,
            payload=payload,
        )


@pytest.mark.parametrize(
    ("ticker", "native_value", "message"),
    [
        ("9999", 0.13, "ticker does not belong to the source run"),
        ("1301", 0.14, r"native value does not match the source run E\[r\]"),
    ],
)
def test_selection_rejects_longlist_that_does_not_match_source_candidates(
    tmp_path: Path,
    ticker: str,
    native_value: float,
    message: str,
) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-revision-fixture")
    payload = _selection(ticker)
    longlist = payload["longlist"]
    assert isinstance(longlist, list)
    row = longlist[0]
    assert isinstance(row, dict)
    row["lane_native_value"] = native_value
    if ticker == "1301":
        row["expected_return_pct"] = round(native_value * 100, 4)

    with pytest.raises(ValueError, match=message):
        store.publish_selection(
            run_revision_id="run-revision-fixture",
            profile="default",
            macro_context_id=None,
            payload=payload,
        )


def test_selection_rejects_unknown_or_cross_run_source(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    with pytest.raises(RunStoreNotFoundError):
        store.publish_selection(
            run_revision_id="missing",
            profile="default",
            macro_context_id=None,
            payload=_selection(),
        )
    store.publish_run(_run(), run_revision_id="run-a")
    store.publish_run(
        _run(as_of="2026-07-09", run_at="2026-07-10T01:00:00+09:00"),
        run_revision_id="run-b",
    )
    store.publish_selection(
        run_revision_id="run-a",
        profile="default",
        macro_context_id=None,
        payload=_selection(candidates_ref="run-a"),
        selection_id="selection-a",
    )
    with pytest.raises(RunStoreConflictError, match="same run revision"):
        store.publish_selection(
            run_revision_id="run-b",
            profile="default",
            macro_context_id=None,
            payload=_selection(asof="2026-07-09", candidates_ref="run-b"),
            source_selection_id="selection-a",
        )


def test_asof_queries_fail_on_ambiguous_revision_and_latest_is_deterministic(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(
        _run(as_of="2026-07-07", run_at="2026-07-08T01:00:00+09:00"),
        run_revision_id="previous-a",
    )
    store.publish_run(
        _run(as_of="2026-07-07", run_at="2026-07-08T02:00:00+09:00"),
        run_revision_id="previous-b",
    )
    store.publish_run(_run(), run_revision_id="current")
    reader = ScreeningRunReader(database)

    with pytest.raises(RunStoreAmbiguousError):
        reader.resolve_run(as_of_date="2026-07-07")
    with pytest.raises(RunStoreAmbiguousError):
        reader.previous_run(before_as_of_date="2026-07-08")
    assert reader.resolve_run(run_revision_id="previous-a") is not None
    latest = reader.latest_run()
    assert latest is not None
    assert latest.run_revision_id == "current"
    assert [run.run_revision_id for run in reader.list_runs()] == [
        "current",
        "previous-b",
        "previous-a",
    ]

    store.publish_run(
        _run(run_at="2026-07-09T03:00:00+09:00"),
        run_revision_id="current-b",
    )
    latest = reader.latest_run()
    assert latest is not None
    assert latest.run_revision_id == "current-b"


def test_prune_keeps_newest_generations_and_removes_dependent_cache_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    for day in range(1, 6):
        as_of = f"2026-07-{day:02d}"
        payload = _run(as_of=as_of, run_at=f"{as_of}T15:00:00+09:00")
        candidate = dict(payload["candidates"][0])  # type: ignore[index]
        candidate["padding"] = str(day) * 200_000
        payload["candidates"] = [candidate]
        store.publish_run(payload, run_revision_id=f"run-{day}")
    store.publish_selection(
        run_revision_id="run-1",
        profile="default",
        macro_context_id=None,
        payload=_selection(asof="2026-07-01", candidates_ref="run-1"),
        selection_id="selection-parent",
    )
    store.publish_selection(
        run_revision_id="run-1",
        profile="default",
        macro_context_id=None,
        payload=_selection(asof="2026-07-01", candidates_ref="run-1"),
        selection_id="selection-child",
        source_selection_id="selection-parent",
    )
    store.publish_selection(
        run_revision_id="run-5",
        profile="default",
        macro_context_id=None,
        payload=_selection(asof="2026-07-05", candidates_ref="run-5"),
        selection_id="selection-kept",
    )

    result = store.prune(keep=3)

    assert result.kept_runs == 3
    assert result.deleted_runs == 2
    assert result.deleted_candidates == 2
    assert result.deleted_selections == 2
    assert result.bytes_after < result.bytes_before
    reader = ScreeningRunReader(database)
    assert [run.run_revision_id for run in reader.list_runs()] == ["run-5", "run-4", "run-3"]
    assert reader.get_selection("selection-parent") is None
    assert reader.get_selection("selection-child") is None
    assert reader.get_selection("selection-kept") is not None
    store.publish_run(
        _run(as_of="2026-07-06", run_at="2026-07-06T15:00:00+09:00"),
        run_revision_id="run-6",
    )
    assert reader.get_run("run-6") is not None


def test_prune_rejects_negative_keep(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="zero or greater"):
        ScreeningRunStore(tmp_path / "runs.sqlite").prune(keep=-1)


def test_prune_handles_more_runs_than_sqlite_variable_limit(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    initialize_run_store(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """
            INSERT INTO screening_run (
                run_revision_id, public_run_id, run_date, asof_date, run_at,
                universe_size, rules_ref, created_at, payload, application_git_commit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    f"run-{index:04d}",
                    "screening-20260708",
                    "2026-07-08",
                    "2026-07-08",
                    f"2026-07-08T12:00:00.{index:04d}+09:00",
                    0,
                    None,
                    "2026-07-20T00:00:00+00:00",
                    "{}",
                    None,
                )
                for index in range(1_005)
            ),
        )

    result = ScreeningRunStore(database).prune(keep=3)

    assert result.deleted_runs == 1_002
    assert len(ScreeningRunReader(database).list_runs()) == 3


def test_prune_cli_keeps_three_generations_by_default(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    for day in range(1, 5):
        as_of = f"2026-07-{day:02d}"
        store.publish_run(
            _run(as_of=as_of, run_at=f"{as_of}T15:00:00+09:00"),
            run_revision_id=f"run-{day}",
        )

    assert screening_main(["prune", "--runs-db", str(database)]) == 0

    output = safe_load(capsys.readouterr().out)
    assert output["kept_runs"] == 3
    assert output["deleted_runs"] == 1
    assert [run.run_revision_id for run in ScreeningRunReader(database).list_runs()] == [
        "run-4",
        "run-3",
        "run-2",
    ]


def test_reader_connection_is_query_only(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    ScreeningRunStore(database).publish_run(_run())
    reader = ScreeningRunReader(database)

    connection = reader._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM screening_run")
    finally:
        connection.close()


def test_reader_returns_none_for_missing_publications(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    initialize_run_store(database)
    reader = ScreeningRunReader(database)

    assert reader.latest_run() is None
    assert reader.get_run("missing") is None
    assert reader.get_selection("missing") is None
    assert reader.resolve_run(as_of_date="2026-07-08") is None


@pytest.mark.parametrize(
    ("evidence_hit", "message"),
    [
        (
            {
                "name": "x",
                "evidence_pattern_id": "p",
                "source_status": "invalid",
                "sizing_eligible": False,
            },
            "source_status",
        ),
        (
            {
                "name": "x",
                "evidence_pattern_id": "p",
                "source_status": "warning",
                "sizing_eligible": True,
            },
            "non-ok evidence",
        ),
        (
            {
                "name": "",
                "evidence_pattern_id": "p",
                "source_status": "ok",
                "sizing_eligible": True,
            },
            "name must",
        ),
        (
            {
                "name": "x",
                "evidence_pattern_id": "  ",
                "source_status": "ok",
                "sizing_eligible": True,
            },
            "evidence pattern ID",
        ),
    ],
)
def test_run_rejects_invalid_evidence_contract(
    tmp_path: Path,
    evidence_hit: dict[str, object],
    message: str,
) -> None:
    payload = _run()
    candidate = dict(payload["candidates"][0])  # type: ignore[index]
    candidate["evidence_hits"] = [evidence_hit]
    payload["candidates"] = [candidate]

    with pytest.raises(ValueError, match=message):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_run(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("ticker", "123", "invalid format"),
        ("name", "", "name must"),
        ("evidence_hits", None, "evidence_hits must"),
    ],
)
def test_run_rejects_invalid_candidate_shape(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _run()
    candidate = dict(payload["candidates"][0])  # type: ignore[index]
    candidate[field] = value
    payload["candidates"] = [candidate]
    with pytest.raises(ValueError, match=message):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_run(payload)


def test_run_rejects_invalid_evidence_summary(tmp_path: Path) -> None:
    payload = _run()
    payload["evidence_hits_summary"] = {"cheap": -1}
    with pytest.raises(ValueError, match="non-negative integers"):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_run(payload)


@pytest.mark.parametrize("field", ["run_date", "asof_date", "run_at", "run_id", "candidates"])
def test_run_rejects_missing_retired_schema_root_fields(tmp_path: Path, field: str) -> None:
    payload = _run()
    del payload[field]

    with pytest.raises(ValueError, match=field):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_run(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("evidence_pattern_id", None, "evidence pattern ID"),
        ("sizing_eligible", 1, "sizing_eligible must be boolean"),
    ],
)
def test_run_rejects_missing_or_mistyped_evidence_fields(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _run()
    candidate = dict(payload["candidates"][0])  # type: ignore[index]
    evidence_hit: dict[str, object] = {
        "name": "evidence",
        "evidence_pattern_id": "pattern",
        "source_status": "ok",
        "sizing_eligible": True,
    }
    if value is None:
        del evidence_hit[field]
    else:
        evidence_hit[field] = value
    candidate["evidence_hits"] = [evidence_hit]
    payload["candidates"] = [candidate]

    with pytest.raises(ValueError, match=message):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_run(payload)


def _write_v1_run(
    database: Path,
    payload: dict[str, object],
    *,
    run_revision_id: str,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in MIGRATIONS[0].statements:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            """
            INSERT INTO screening_run (
                run_revision_id, public_run_id, run_date, asof_date, run_at,
                universe_size, rules_ref, created_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_revision_id,
                payload["run_id"],
                payload["run_date"],
                payload["asof_date"],
                payload["run_at"],
                payload["universe_size"],
                None,
                "2026-07-20T00:00:00+00:00",
                canonical_json(payload),
            ),
        )
        candidate = payload["candidates"][0]  # type: ignore[index]
        assert isinstance(candidate, dict)
        connection.execute(
            """
            INSERT INTO screening_candidate (
                run_revision_id, ordinal, ticker, sector_33, per_forward,
                per_trailing, pbr, dividend_yield, er_annual, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_revision_id,
                0,
                candidate["ticker"],
                candidate["sector_33"],
                candidate["per_forward"],
                candidate["per_trailing"],
                candidate["pbr"],
                candidate["metrics"]["dividend_yield"],  # type: ignore[index]
                candidate["metrics"]["er_annual"],  # type: ignore[index]
                canonical_json(candidate),
            ),
        )
