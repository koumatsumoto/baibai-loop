"""Shared current Screening selection; retained raw runs remain independently readable."""

from __future__ import annotations

from baibai_web.sources.protocols import ScreeningSource
from baibai_web.sources.types import ScreeningRunRecord


def operative_screening_run(
    screening: ScreeningSource,
) -> tuple[ScreeningRunRecord | None, list[dict[str, object]]]:
    """Resolve the run every screening surface reads, with its own review_sets.

    The L2 machine Review Set binds to a specific run revision; the L3 Research
    Triage refers to that published set.
    A newer revision of the same as-of — a determinism re-run, say — carries no
    review_set of its own, so presenting it would blank the machine-review_set view.
    Falling back to the newest review_set's run
    keeps Security Analyses, their review_sets, and the research_triage join on one run.

    Every surface that shows a candidate resolves the run here, so the list and the
    security page cannot end up describing the same ticker from different runs.
    """

    run = screening.latest_run()
    if run is None:
        return None, []
    # Ask for this run's review_sets first: the whole published history is only needed to
    # find a fallback, which is the uncommon case, and a security page reads this on
    # every request.
    run_review_sets = screening.review_sets(run_revision_id=run.run_revision_id)
    if not run_review_sets and (all_review_sets := screening.review_sets()):
        newest = all_review_sets[0]
        fallback_run = screening.run(str(newest["run_revision_id"]))
        if fallback_run is not None:
            run = fallback_run
            run_review_sets = [
                item
                for item in all_review_sets
                if str(item["run_revision_id"]) == run.run_revision_id
            ]
    return run, run_review_sets
