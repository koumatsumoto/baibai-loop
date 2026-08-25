"""Canonical Research Gate judgments for tests that consume a published shortlist.

``research prepare`` binds a workspace to a stored Shortlist, so every research test
now needs one in the application DB. The valid payload is built through the
``Shortlist`` model rather than hand-written, so a schema change breaks the helper
instead of quietly producing a judgment the publisher would have refused. The row is
inserted directly because these tests exercise the *research* boundary; publication
itself is covered by ``tests/engine/test_shortlist.py``. A raw payload can also be
seeded, which is how the version and contract fail-closes are tested at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.screening.shortlist import Shortlist
from tests.helpers.shortlist import rejected_entry, selected_entry, shortlist_payload


def research_gate_shortlist(
    *,
    shortlist_id: str,
    selection_id: str,
    run_revision_id: str,
    as_of: str,
    selected: Sequence[str],
    rejected: Sequence[str] = (),
    profile: str = "default",
    macro_context_id: str | None = None,
) -> dict[str, object]:
    """Build one canonical v5 Research Gate judgment over ``selected + rejected``."""

    entries: list[dict[str, object]] = [
        selected_entry(ticker, rank=rank, reason="一次リサーチへ進める")
        for rank, ticker in enumerate(selected, start=1)
    ]
    entries.extend(
        rejected_entry(ticker, reason="深掘りの枠を使う価値が確認できない") for ticker in rejected
    )
    return Shortlist.model_validate(
        shortlist_payload(
            shortlist_id=shortlist_id,
            selection_id=selection_id,
            run_revision_id=run_revision_id,
            as_of=as_of,
            published_at=f"{as_of}T18:00:00+09:00",
            profile=profile,
            macro_context_id=macro_context_id,
            attention_policy_parameters={"value_carry_limit": len(entries)},
            entries=entries,
        )
    ).payload()


def seed_shortlist(db_path: Path, payload: Mapping[str, object]) -> str:
    """Insert one shortlist row exactly as the publisher stores it."""

    shortlist_id = str(payload["shortlist_id"])
    initialize_database(db_path)
    with closing(connect_rw(db_path)) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO shortlist (
                shortlist_id, selection_id, run_revision_id, as_of, published_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                shortlist_id,
                str(payload["selection_id"]),
                str(payload["run_revision_id"]),
                str(payload["as_of"]),
                str(payload["published_at"]),
                canonical_json(dict(payload)),
            ),
        )
    return shortlist_id


def seed_research_gate(
    db_path: Path,
    *,
    shortlist_id: str,
    selection_id: str,
    run_revision_id: str,
    as_of: str,
    selected: Sequence[str],
    rejected: Sequence[str] = (),
) -> str:
    """Publish and store one canonical Research Gate judgment in a single call."""

    return seed_shortlist(
        db_path,
        research_gate_shortlist(
            shortlist_id=shortlist_id,
            selection_id=selection_id,
            run_revision_id=run_revision_id,
            as_of=as_of,
            selected=selected,
            rejected=rejected,
        ),
    )


__all__ = [
    "research_gate_shortlist",
    "seed_research_gate",
    "seed_shortlist",
]
