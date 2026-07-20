"""Query-only screening publication views."""

from __future__ import annotations

from pathlib import Path

from baibai_engine.screening.run_store import ScreeningRunReader


def screening_run_payload(
    path: Path,
    *,
    run_revision_id: str | None = None,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    reader = ScreeningRunReader(path)
    run = reader.latest_run() if run_revision_id is None else reader.get_run(run_revision_id)
    return None if run is None else _run_payload(run)


def screening_selection_payloads(
    path: Path,
    *,
    run_revision_id: str | None = None,
) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return [
        {
            "selection_id": item.selection_id,
            "run_revision_id": item.run_revision_id,
            "as_of_date": item.as_of_date,
            "profile": item.profile,
            "macro_context_id": item.macro_context_id,
            "publication_kind": item.publication_kind,
            "created_at": item.created_at,
            "source_selection_id": item.source_selection_id,
            "payload": item.payload,
            "entries": list(item.entries),
        }
        for item in ScreeningRunReader(path).list_selections(run_revision_id=run_revision_id)
    ]


def _run_payload(run: object) -> dict[str, object]:
    from baibai_engine.screening.run_store import RunPublication

    if not isinstance(run, RunPublication):  # pragma: no cover - internal contract
        raise TypeError("expected RunPublication")
    return {
        "run_revision_id": run.run_revision_id,
        "public_run_id": run.public_run_id,
        "run_date": run.run_date,
        "as_of_date": run.as_of_date,
        "run_at": run.run_at,
        "universe_size": run.universe_size,
        "rules_ref": run.rules_ref,
        "application_git_commit": run.application_git_commit,
        "payload": run.payload,
        "candidates": list(run.candidates),
    }


__all__ = [
    "screening_run_payload",
    "screening_selection_payloads",
]
