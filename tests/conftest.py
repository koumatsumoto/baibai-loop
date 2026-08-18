from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.indicators.db import initialize_database as initialize_indicators_db
from baibai_engine.macro.reading.rules import DEFAULT_RULES_PATH as MACRO_READING_RULES_PATH
from baibai_engine.position.ledger import load_portfolio_ledger
from baibai_engine.research.store import ResearchStoreService
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_engine.tasks.models import Task
from tests.helpers.db_seed import seed_ledger, seed_tasks
from tests.helpers.fixed_now import FIXED_NOW

FIXTURES = Path(__file__).parent / "fixtures"

# Real operational stores that a test must never open. App DB and run store resolve
# their default from these env vars; the indicators and market stores have no env
# override and are guarded by fingerprint detection alone. The market store is the one
# a shell test can reach without opening a database at all: `r2_transfer.sh` resolves
# every store path from its own location, so a pull run from the checkout replaces all
# four files whatever the fake CLI hands back.
_REAL_DB_DEFAULTS = (
    Path("stores/application/baibai.sqlite"),
    Path("stores/screening/runs.sqlite"),
    Path("stores/macro/macro.sqlite"),
    Path("stores/market/market.sqlite"),
)


@pytest.fixture(autouse=True)
def _isolate_databases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point env-resolved DB paths at a per-test tmp location.

    A test that omits an explicit path resolves the default operational DB. Forcing
    ``BAIBAI_DB`` / ``BAIBAI_RUNS_DB`` to tmp guarantees such a test opens an isolated
    file instead. Explicit path arguments still take precedence, so fixtures that pass
    tmp paths are unaffected.
    """
    db_root = tmp_path / "isolated-db"
    db_root.mkdir()
    monkeypatch.setenv("BAIBAI_DB", str(db_root / "app.sqlite"))
    monkeypatch.setenv("BAIBAI_RUNS_DB", str(db_root / "runs.sqlite"))


def _database_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


@pytest.fixture(scope="session", autouse=True)
def _guard_real_databases() -> Iterator[None]:
    """Fail the session closed if any test mutates a real operational database.

    A safety net for isolation gaps the env redirect cannot cover: a hard-coded real
    path or the indicators store, which has no env override. Detection rather than
    prevention, but it turns silent corruption into a loud failure.
    """
    before = {path: _database_fingerprint(path) for path in _REAL_DB_DEFAULTS}
    yield
    mutated = [
        str(path) for path in _REAL_DB_DEFAULTS if _database_fingerprint(path) != before[path]
    ]
    if mutated:
        raise AssertionError("tests mutated real operational database(s): " + ", ".join(mutated))


def _seed_app_method_root(root: Path) -> None:
    config_dir = root / "web/config"
    config_dir.mkdir(parents=True)
    (config_dir / "macro-panel.yaml").write_text(
        Path("web/config/macro-panel.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    reading_rules = root / MACRO_READING_RULES_PATH
    reading_rules.parent.mkdir(parents=True, exist_ok=True)
    reading_rules.write_text(MACRO_READING_RULES_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    indicators_dir = root / "stores/macro"
    indicators_dir.mkdir(parents=True, exist_ok=True)
    initialize_indicators_db(indicators_dir / "macro.sqlite").close()
    db_path = root / "stores/application/baibai.sqlite"
    ledger = load_portfolio_ledger(FIXTURES / "portfolio-ledger/representative.yaml")
    seed_ledger(
        db_path,
        ledger.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in ledger.market_prices
                )
            }
        ),
    )
    thesis = safe_load((FIXTURES / "thesis/2331-decision.yaml").read_text(encoding="utf-8"))
    review = safe_load((FIXTURES / "thesis/2331-decision-review.yaml").read_text(encoding="utf-8"))
    assert isinstance(thesis, dict)
    assert isinstance(review, dict)
    ResearchStoreService(db_path, clock=lambda: FIXED_NOW).publish_thesis_with_review(
        "thesis-20260714-2331-r1", thesis, review
    )
    tasks = yaml.safe_load(_TASKS)["tasks"]
    seed_tasks(db_path, (Task.model_validate(item) for item in tasks))
    run_store = ScreeningRunStore(root / "stores/screening/runs.sqlite")
    for text in (
        _CANDIDATES.replace("screening-20260708", "screening-20260701").replace(
            "2026-07-08", "2026-07-01"
        ),
        _CANDIDATES,
    ):
        payload = yaml.safe_load(text)
        assert isinstance(payload, dict)
        run_store.publish_run(payload)


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture(scope="session")
def _app_method_root_template(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Build the immutable application-store seed once for per-test clones."""
    root = tmp_path_factory.mktemp("app-method-root-template") / "repo"
    _seed_app_method_root(root)
    fingerprint = _tree_fingerprint(root)
    yield root
    assert _tree_fingerprint(root) == fingerprint, "app_method_root template was mutated"


@pytest.fixture
def app_method_root(tmp_path: Path, _app_method_root_template: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(_app_method_root_template, root)
    template_db = _app_method_root_template / "stores/application/baibai.sqlite"
    assert root != _app_method_root_template
    assert not (root / "stores/application/baibai.sqlite").samefile(template_db)
    return root


_TASKS = """\
schema_version: 1
tasks:
  - task_id: task-20260730-2331-review
    title: 2331 review
    kind: earnings-review
    status: open
    ticker: "2331"
    due_date: "2026-07-30"
    event_label: Q1 earnings
    event_date: "2026-07-30"
    related_refs: []
    created_at: "2026-07-18"
  - task_id: task-20260801-ops
    title: ops check
    kind: ops
    status: open
    due_date: "2026-08-01"
    related_refs: []
    created_at: "2026-07-18"
  - task_id: task-20260701-done
    title: completed
    kind: other
    status: done
    due_date: "2026-07-01"
    related_refs: []
    created_at: "2026-06-01"
    closed_at: "2026-07-01"
"""

_CANDIDATES = """\
run_id: screening-20260708
run_date: "2026-07-08"
asof_date: "2026-07-08"
universe_size: 3
run_at: "2026-07-08T12:00:00+09:00"
candidates:
  - ticker: "2331"
    name: ALSOK
    sector_33: サービス業
    per_trailing: 12.0
    metrics:
      er_annual: 0.12
    evidence_hits: []
  - ticker: "0001"
    name: Sample One
    sector_33: 情報・通信業
    metrics: {}
    evidence_hits: []
  - ticker: "0002"
    name: Sample Two
    sector_33: 小売業
    evidence_hits: []
"""
