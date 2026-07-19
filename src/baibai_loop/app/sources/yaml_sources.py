"""YAML-backed implementations of the application source contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

from baibai_loop.app.sources.types import (
    CandidatesRun,
    PacketDetail,
    ResearchRevision,
    ScenarioSummary,
    TaskRecord,
)
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.position.ledger import (
    PortfolioSnapshot,
    load_portfolio_ledger,
    reconcile_portfolio,
)
from baibai_loop.thesis.decision_packet import DecisionPacketError, load_decision_packet

_CANDIDATES_CACHE: dict[tuple[Path, int], Mapping[str, object]] = {}


class YamlLedgerSource:
    def __init__(self, root: Path) -> None:
        self._path = root.resolve() / "records/04-position/portfolio-ledger.yaml"

    def exists(self) -> bool:
        return self._path.is_file()

    def snapshot(self) -> PortfolioSnapshot:
        return reconcile_portfolio(load_portfolio_ledger(self._path))


class YamlResearchSource:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._records_root = self._root / "records/03-thesis"
        self._load_errors: list[str] = []

    def revisions(self) -> list[ResearchRevision]:
        revisions: list[ResearchRevision] = []
        errors: list[str] = []
        if not self._records_root.is_dir():
            self._load_errors = []
            return revisions
        for path in self._records_root.rglob("*-decision.yaml"):
            if "_archive" in path.parts:
                continue
            try:
                revisions.append(self._revision(path))
            except (DecisionPacketError, ValueError):
                errors.append(self._relative(path))
        revisions.sort(key=lambda item: (item.as_of, item.packet_path), reverse=True)
        self._load_errors = sorted(errors)
        return revisions

    def packet_detail(self, packet_path: str) -> PacketDetail:
        path = self._resolve_packet(packet_path)
        packet = load_decision_packet(path)
        return PacketDetail(
            revision=self._revision(path),
            entry_price_basis_yen=float(packet.estimates.entry_price_basis_yen),
            required_5y_base_cagr_pct=packet.estimates.required_5y_base_cagr_pct,
            permanent_loss_risk_count=len(packet.permanent_loss_risks),
            scenarios=tuple(
                ScenarioSummary(name=item.name, horizon_years=item.horizon_years)
                for item in packet.estimates.scenarios
            ),
            permanent_loss_conclusion=packet.judgment.permanent_loss_conclusion,
            strongest_countercase=packet.judgment.strongest_countercase,
            sizing_action=packet.judgment.sizing_action,
        )

    def load_errors(self) -> list[str]:
        return list(self._load_errors)

    def _revision(self, path: Path) -> ResearchRevision:
        packet = load_decision_packet(path)
        review_name = path.name.removesuffix("-decision.yaml") + "-decision-review.yaml"
        review_path = path.with_name(review_name)
        return ResearchRevision(
            ticker=packet.input_snapshot.ticker,
            company_name=packet.input_snapshot.company_name,
            sector=packet.input_snapshot.sector,
            as_of=packet.input_snapshot.as_of,
            packet_path=self._relative(path),
            recommendation=packet.judgment.recommendation,
            confidence=packet.judgment.confidence,
            current_fair_value_yen=float(packet.estimates.current_fair_value_yen),
            model_version=packet.estimates.model_version,
            review_path=self._relative(review_path) if review_path.is_file() else None,
        )

    def _resolve_packet(self, packet_path: str) -> Path:
        relative = Path(packet_path)
        if relative.is_absolute():
            raise ValueError("packet_path must be relative to the repository root")
        path = (self._root / relative).resolve()
        try:
            path.relative_to(self._records_root)
        except ValueError as error:
            raise ValueError("packet_path must identify a decision packet") from error
        return path

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self._root).as_posix()


class YamlTaskSource:
    def __init__(self, root: Path) -> None:
        self._path = root.resolve() / "records/05-task/tasks.yaml"

    def exists(self) -> bool:
        return self._path.is_file()

    def list_tasks(self) -> list[TaskRecord]:
        if not self.exists():
            return []
        raw = safe_load(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("task list root must be a mapping")
        tasks = raw.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("task list tasks must be an array")
        return [self._parse_task(item) for item in tasks]

    @staticmethod
    def _parse_task(raw: object) -> TaskRecord:
        if not isinstance(raw, Mapping):
            raise ValueError("task must be a mapping")
        related_refs = raw.get("related_refs", [])
        if not isinstance(related_refs, list):
            raise ValueError("task related_refs must be an array")
        return TaskRecord(
            task_id=str(raw["task_id"]),
            title=str(raw["title"]),
            kind=str(raw["kind"]),
            status=str(raw["status"]),
            ticker=_optional_text(raw.get("ticker")),
            due_date=date.fromisoformat(str(raw["due_date"])),
            event_label=_optional_text(raw.get("event_label")),
            event_date=_optional_date(raw.get("event_date")),
            body_md=_optional_text(raw.get("body_md")),
            related_refs=tuple(str(item) for item in related_refs),
            created_at=date.fromisoformat(str(raw["created_at"])),
            closed_at=_optional_date(raw.get("closed_at")),
        )


class YamlCandidatesSource:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._records_root = self._root / "records/02-candidates"

    def latest_run(self) -> CandidatesRun | None:
        if not self._records_root.is_dir():
            return None
        paths = list(self._records_root.rglob("????-??-??.yaml"))
        if not paths:
            return None
        path = max(paths, key=lambda item: (item.name, item.as_posix()))
        raw = _load_candidates(path)
        candidates = raw.get("candidates")
        if not isinstance(candidates, list) or not all(
            isinstance(item, dict) for item in candidates
        ):
            raise ValueError("candidates must be an array of mappings")
        return CandidatesRun(
            run_id=str(raw["run_id"]),
            run_date=date.fromisoformat(str(raw["run_date"])),
            asof_date=date.fromisoformat(str(raw["asof_date"])),
            universe_size=int(str(raw["universe_size"])),
            source_path=path.relative_to(self._root).as_posix(),
            rows=tuple(candidates),
        )


def _load_candidates(path: Path) -> Mapping[str, object]:
    resolved = path.resolve()
    key = (resolved, resolved.stat().st_mtime_ns)
    cached = _CANDIDATES_CACHE.get(key)
    if cached is not None:
        return cached
    raw = safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("candidates root must be a mapping")
    _CANDIDATES_CACHE.clear()
    _CANDIDATES_CACHE[key] = raw
    return raw


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_date(value: object) -> date | None:
    return None if value is None else date.fromisoformat(str(value))
