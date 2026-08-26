"""Read-only decision boundary before a shortlist cycle creates a screening run."""

from __future__ import annotations

import subprocess  # nosec B404
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from baibai_engine.read_api.shortlist import list_shortlist_payloads
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import (
    RunPublication,
    ScreeningRunReader,
    SelectionPublication,
)

PreflightDecision = Literal[
    "reuse",
    "resume-current-code",
    "rerun-current-code",
    "blocked",
]


@dataclass(frozen=True, slots=True)
class GitState:
    commit: str
    clean: bool


def shortlist_preflight(
    *,
    as_of: date,
    runs_db_path: Path,
    app_db_path: Path,
    repo_root: Path,
    previous_run_revision_id: str | None = None,
    git_state: GitState | None = None,
) -> dict[str, object]:
    """Choose reuse or one current-code rerun without mutating retention state.

    The pulled run store is the only input. A run for ``as_of`` whose application
    commit is the checked-out HEAD, together with the machine selection bound to
    it, is what the cloud batch (or an earlier local rerun on this commit)
    published; nothing else can be reused. The previous publication ``select``
    compares against is resolved here as well, from the canonical shortlist first
    so a pruned run store never substitutes a non-canonical same-day run.
    """

    git = git_state or _git_state(repo_root)
    reader = ScreeningRunReader(runs_db_path)
    default_profile = load_screening_rules().selection.default_profile
    current_code = _resolve_current_code(
        reader,
        as_of=as_of,
        commit=git.commit,
        default_profile=default_profile,
    )
    previous = _resolve_previous(
        reader,
        app_db_path=app_db_path,
        before=as_of,
        explicit_run_revision_id=previous_run_revision_id,
    )
    reasons: list[str] = []
    if not git.clean:
        reasons.append("checked-out worktree is dirty")
    if current_code["status"] != "resolved" and previous["status"] in {
        "ambiguous",
        "canonical-unavailable",
        "invalid-explicit",
    }:
        reasons.append("previous publication must be resolved before run or select")

    reusable: dict[str, object] | None = None
    if reasons:
        decision: PreflightDecision = "blocked"
    elif current_code["status"] == "resolved":
        decision = "reuse"
        reusable = {
            "run_revision_id": current_code["run_revision_id"],
            "selection_id": current_code["selection_id"],
            "application_git_commit": git.commit,
        }
    elif current_code["status"] == "run-only":
        decision = "resume-current-code"
        reusable = {
            "run_revision_id": current_code["run_revision_id"],
            "selection_id": None,
            "application_git_commit": git.commit,
        }
    else:
        decision = "rerun-current-code"

    return {
        "kind": "shortlist-preflight",
        "as_of": as_of.isoformat(),
        "decision": decision,
        "reasons": reasons,
        "checked_out": {"commit": git.commit, "clean": git.clean},
        "reusable": reusable,
        "current_code": current_code,
        "previous": previous,
    }


def _git_state(repo_root: Path) -> GitState:
    commit_before = _git(repo_root, "rev-parse", "HEAD")
    status = _git(repo_root, "status", "--porcelain")
    commit_after = _git(repo_root, "rev-parse", "HEAD")
    return GitState(
        commit=commit_after,
        clean=not status and commit_before == commit_after,
    )


def _git(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(  # nosec B603
        ("git", "-C", str(repo_root), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError(f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def _resolve_current_code(
    reader: ScreeningRunReader,
    *,
    as_of: date,
    commit: str,
    default_profile: str,
) -> dict[str, object]:
    """Find what this commit already published for ``as_of``.

    Several publications on the same commit and day are the same code over the
    same data, so the newest one is reused rather than the choice being pushed
    back to the operator; every candidate is still listed.
    """

    as_of_text = as_of.isoformat()
    runs = sorted(
        (
            item
            for item in reader.list_runs()
            if item.as_of_date == as_of_text and item.application_git_commit == commit
        ),
        key=lambda item: item.run_at,
    )
    run_by_id = {item.run_revision_id: item for item in runs}
    selections = sorted(
        (
            item
            for item in reader.list_selections(as_of_date=as_of_text)
            if item.run_revision_id in run_by_id
            and item.application_git_commit == commit
            and item.publication_kind == "machine"
            and item.profile == default_profile
        ),
        key=lambda item: item.created_at,
    )
    if selections:
        newest = selections[-1]
        return {
            "status": "resolved",
            "run_revision_id": newest.run_revision_id,
            "selection_id": newest.selection_id,
            "candidates": [_selection_ref(item) for item in selections],
        }
    if runs:
        return {
            "status": "run-only",
            "run_revision_id": runs[-1].run_revision_id,
            "selection_id": None,
            "candidates": [_run_ref(item) for item in runs],
        }
    return {"status": "missing", "run_revision_id": None, "selection_id": None, "candidates": []}


def _run_ref(item: RunPublication) -> dict[str, object]:
    return {
        "run_revision_id": item.run_revision_id,
        "selection_id": None,
        "created_at": item.run_at,
    }


def _resolve_previous(
    reader: ScreeningRunReader,
    *,
    app_db_path: Path,
    before: date,
    explicit_run_revision_id: str | None,
) -> dict[str, object]:
    before_text = before.isoformat()
    runs = [item for item in reader.list_runs() if item.as_of_date < before_text]
    selections = [item for item in reader.list_selections() if item.as_of_date < before_text]
    canonical_by_as_of: dict[str, dict[str, object]] = {}
    for item in list_shortlist_payloads(app_db_path):
        item_as_of = item.get("as_of")
        if isinstance(item_as_of, str) and item_as_of < before_text:
            # The read API is newest-first, so the first publication is the one
            # shown by the application when a day has multiple revisions.
            canonical_by_as_of.setdefault(item_as_of, item)
    prior_dates = {item.as_of_date for item in runs} | set(canonical_by_as_of)
    if not prior_dates:
        if explicit_run_revision_id is not None:
            return {
                "status": "invalid-explicit",
                "as_of": None,
                "run_revision_id": explicit_run_revision_id,
                "selection_rule": (
                    "the explicit previous run must belong to the greatest prior as-of"
                ),
                "candidates": [],
            }
        return {"status": "missing", "as_of": None, "candidates": []}
    prior_as_of = max(prior_dates)
    candidate_runs = [item for item in runs if item.as_of_date == prior_as_of]
    candidate_selections = [item for item in selections if item.as_of_date == prior_as_of]
    canonical = canonical_by_as_of.get(prior_as_of)
    if canonical is not None:
        canonical_run = canonical.get("run_revision_id")
        if explicit_run_revision_id is not None and explicit_run_revision_id != canonical_run:
            return {
                "status": "invalid-explicit",
                "as_of": prior_as_of,
                "run_revision_id": explicit_run_revision_id,
                "selection_rule": "the explicit previous run must match the canonical shortlist",
                "candidates": [_run_ref(item) for item in candidate_runs],
            }
        resolved_run = next(
            (
                item
                for item in candidate_runs
                if item.run_revision_id == canonical.get("run_revision_id")
            ),
            None,
        )
        if resolved_run is not None:
            return _resolved_previous_run(
                resolved_run,
                source="canonical-shortlist",
                selection_id=_optional_string(canonical.get("selection_id")),
            )
        retained_tickers = _canonical_entry_tickers(canonical)
        shortlist_id = _optional_string(canonical.get("shortlist_id"))
        if retained_tickers and shortlist_id is not None:
            return {
                "status": "resolved-shortlist",
                "as_of": prior_as_of,
                "source": "canonical-shortlist-retained",
                "shortlist_id": shortlist_id,
                "run_revision_id": canonical.get("run_revision_id"),
                "selection_id": canonical.get("selection_id"),
                "candidate_count": len(retained_tickers),
                "selection_arguments": [
                    "--previous-shortlist-id",
                    shortlist_id,
                ],
                "candidates": [_run_ref(item) for item in candidate_runs],
            }
        return {
            "status": "canonical-unavailable",
            "as_of": prior_as_of,
            "run_revision_id": canonical.get("run_revision_id"),
            "selection_id": canonical.get("selection_id"),
            "selection_rule": (
                "do not substitute a non-canonical same-day publication; recover the "
                "canonical selection or use the canonical shortlist's retained longlist"
            ),
            "candidates": [_run_ref(item) for item in candidate_runs],
        }

    if explicit_run_revision_id is not None:
        explicit = next(
            (item for item in candidate_runs if item.run_revision_id == explicit_run_revision_id),
            None,
        )
        if explicit is not None:
            return _resolved_previous_run(explicit, source="explicit-run")
        return {
            "status": "invalid-explicit",
            "as_of": prior_as_of,
            "run_revision_id": explicit_run_revision_id,
            "selection_rule": "the explicit previous run must belong to the greatest prior as-of",
            "candidates": [_run_ref(item) for item in candidate_runs],
        }

    if len(candidate_runs) == 1:
        selection_id = next(
            (
                item.selection_id
                for item in candidate_selections
                if item.run_revision_id == candidate_runs[0].run_revision_id
            ),
            None,
        )
        return _resolved_previous_run(
            candidate_runs[0],
            source="unique-prior-run",
            selection_id=selection_id,
        )
    return {
        "status": "ambiguous",
        "as_of": prior_as_of,
        "selection_rule": (
            "choose the run bound by the application DB canonical shortlist; "
            "otherwise pass --previous-run-revision-id explicitly"
        ),
        "candidates": [_run_ref(item) for item in candidate_runs],
    }


def _resolved_previous_run(
    item: RunPublication,
    *,
    source: str,
    selection_id: str | None = None,
) -> dict[str, object]:
    return {
        "status": "resolved",
        "as_of": item.as_of_date,
        "source": source,
        "run_revision_id": item.run_revision_id,
        "selection_id": selection_id,
        "selection_arguments": ["--previous-run-revision-id", item.run_revision_id],
        "candidates": [_run_ref(item)],
    }


def _canonical_entry_tickers(payload: dict[str, object]) -> tuple[str, ...]:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return ()
    tickers: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("ticker"), str):
            return ()
        tickers.append(entry["ticker"])
    return tuple(tickers)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _selection_ref(item: SelectionPublication) -> dict[str, object]:
    return {
        "selection_id": item.selection_id,
        "run_revision_id": item.run_revision_id,
        "created_at": item.created_at,
    }


__all__ = ["GitState", "shortlist_preflight"]
