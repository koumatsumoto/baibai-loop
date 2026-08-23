"""Canonical shortlist judgment and publication service."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.foundation.reject_classification import RejectClass
from baibai_engine.screening.selection.contracts import (
    RESEARCH_GATE_CONTRACT_ID,
    VALUE_CARRY_ONLY_ATTENTION_POLICY_ID,
    ValueCarryOnlyAttentionParameters,
)

SHORTLIST_SCHEMA_VERSION = 5


class ShortlistNarrative(BaseModel):
    """Research Gate judgment for a selected candidate.

    Screening は数値の由来を機械出力するが、なぜ深掘りに値するかという判断は
    ここへ人間/AI が固定する。selected 銘柄でだけ必須にし、shortlist を
    ephemeral な HTML narrative ではなく application DB の一次記録にする。

    リスクリワードの判断 ``upside`` / ``downside`` / ``rr``、dated catalyst、
    macro context ヒントの消化を必須にするのは、この段階で「割安に見える」だけの
    候補と「非対称が買いに値する」候補を分けるためである。ここで書けない候補は
    一次リサーチの枠を使う価値が確認できていない。
    """

    model_config = ConfigDict(extra="forbid")
    ploss: Literal["低", "中低", "中", "要精査", "高"]
    why: str = Field(min_length=1)
    temporary: str = Field(min_length=1)
    structural: str = Field(min_length=1)
    survive: str = Field(min_length=1)
    unlock: str = Field(min_length=1)
    counter: str = Field(min_length=1)
    research: str = Field(min_length=1)
    value: str = Field(min_length=1)
    prov: str = Field(min_length=1)
    upside: str = Field(min_length=1)
    downside: str = Field(min_length=1)
    rr: str = Field(min_length=1)
    catalyst: str = Field(min_length=1)
    catalyst_date: date | None = None
    macro: str = Field(min_length=1)
    sector_label: str | None = None


class ShortlistFvConvergence(BaseModel):
    """Judgment-time FV convergence warning, exactly as the machine reported it."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["warning", "clear", "not_evaluable"]
    warning_code: str | None = None
    market_price_yen: float | None = None
    anchors_yen: dict[str, float] = Field(default_factory=dict)
    er_reversion_annual: float | None = None


class ShortlistMachineSnapshot(BaseModel):
    """The machine coordinates this judgment was made against.

    The run store keeps three generations, and the selection that ranked these
    tickers is deleted with the run it belongs to. Everything the review surface
    shows beside the narrative — the machine's own ordering, the FV anchor, the
    reference price, the warnings — lives only there. A shortlist is the canonical
    record of a cycle (the only one when nothing is selected), so the coordinates
    have to travel with the judgment rather than be joined back to a store that
    outlives it by three runs.

    The publisher fills this from the source selection's longlist row; a draft does
    not carry it. It is a display record: nothing recomputes or overwrites it.
    """

    model_config = ConfigDict(extra="forbid")
    rank: int | None = None
    name: str | None = None
    opportunity_lane_id: str = Field(min_length=1)
    selection_policy_id: str = Field(min_length=1)
    selection_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lane_rank: int = Field(ge=1)
    lane_native_value: float | None
    lane_native_unit: str = Field(min_length=1)
    baseline_er_rank: int | None = Field(default=None, ge=1)
    primary_evidence_pattern_id: str | None = None
    policy_diagnostic_ids: tuple[str, ...]
    expected_return_pct: float | None = None
    fair_value_anchor_yen: float | None = None
    market_price_yen: float | None = None
    liquidity_status: str | None = None
    durability_warnings: tuple[str, ...] = ()
    event_warnings: tuple[str, ...] = ()
    selection_reasons: tuple[str, ...] = ()
    fv_convergence: ShortlistFvConvergence | None = None


class ShortlistEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    decision: Literal["selected", "rejected"]
    reason: str = Field(min_length=1)
    # reason が判断の正本。class は頻度集計と改善候補の列挙にだけ使い、
    # 自動除外や順位変更には使わない。
    reject_class: RejectClass | None = None
    rank: int | None = Field(default=None, ge=1)
    narrative: ShortlistNarrative | None = None
    # The machine E[r] this judgment was made against, in annual ratio. The publisher
    # fills it from the bound run; a draft does not carry it. The run store keeps only
    # a few generations, so a comparison of the judgment against the ranking it
    # started from has to hold the ranking here or lose it before the horizon matures.
    er_annual: float | None = None
    # The rest of the machine row the judgment read. Same reason as er_annual, and
    # the same discipline: written once at publish, never recomputed.
    machine_snapshot: ShortlistMachineSnapshot | None = None

    @model_validator(mode="after")
    def validate_narrative_matches_decision(self) -> Self:
        if self.decision == "selected" and self.narrative is None:
            raise ValueError("selected shortlist entry must include a Research Gate narrative")
        if self.decision == "rejected" and self.narrative is not None:
            raise ValueError("rejected shortlist entry must not include a narrative")
        if self.decision == "selected" and self.rank is None:
            raise ValueError("selected shortlist entry must carry a provisional rank")
        if self.decision == "rejected" and self.rank is not None:
            raise ValueError("rejected shortlist entry must not carry a provisional rank")
        if self.decision == "rejected" and self.reject_class is None:
            raise ValueError("rejected shortlist entry must include a reject_class")
        if self.decision == "selected" and self.reject_class is not None:
            raise ValueError("selected shortlist entry must not include a reject_class")
        return self


CATALYST_HORIZON_DAYS = 550
"""dated catalyst として書ける将来の幅。as_of から約 18 か月。

これより先の日付は「いつか起きる」であって着手順位を決める catalyst にならず、
as_of より前の日付は既に判明した事実なので、どちらも再評価 trigger にならない。
"""


class Shortlist(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[5]
    kind: Literal["shortlist"]
    shortlist_id: str
    selection_id: str = Field(min_length=1)
    run_revision_id: str = Field(min_length=1)
    as_of: date
    published_at: datetime
    profile: str = Field(min_length=1)
    macro_context_id: str | None = None
    attention_policy_id: Literal["value-carry-only-v1"]
    attention_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    attention_policy_parameters: ValueCarryOnlyAttentionParameters
    review_basis_shortlist_id: str | None
    research_gate_contract_id: Literal["research-gate-v1"]
    entries: tuple[ShortlistEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_publication(self) -> Self:
        if not re.fullmatch(r"shortlist-\d{8}-[a-z0-9-]+", self.shortlist_id):
            raise ValueError("shortlist_id has an invalid format")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        if self.attention_policy_id == VALUE_CARRY_ONLY_ATTENTION_POLICY_ID and not isinstance(
            self.attention_policy_parameters, ValueCarryOnlyAttentionParameters
        ):
            raise ValueError("Core-only Attention Policy parameters do not match policy ID")
        if self.research_gate_contract_id != RESEARCH_GATE_CONTRACT_ID:
            raise ValueError("unknown Research Gate Contract ID")
        tickers = [entry.ticker for entry in self.entries]
        if len(tickers) != len(set(tickers)):
            raise ValueError("shortlist ticker must be unique")
        # selected 0 件は「longlist をResearch Gateで見たが、一次リサーチの枠を使う価値のある
        # 候補が無かった」という正常な判断であり、その見送り理由は rejected entry の
        # reason にしか書けない。ここで publish を拒むと、そのサイクルの判断が
        # 記録の外へ落ちる。
        selected = [entry for entry in self.entries if entry.decision == "selected"]
        ranks = sorted(entry.rank for entry in selected if entry.rank is not None)
        if ranks != list(range(1, len(selected) + 1)):
            raise ValueError("selected provisional ranks must be 1..N without gaps or duplicates")
        for entry in selected:
            narrative = entry.narrative
            if narrative is None or narrative.catalyst_date is None:
                continue
            delta = (narrative.catalyst_date - self.as_of).days
            if not 0 <= delta <= CATALYST_HORIZON_DAYS:
                raise ValueError(
                    f"{entry.ticker} catalyst_date must fall between as_of and "
                    f"as_of + {CATALYST_HORIZON_DAYS} days"
                )
        return self

    def selected_by_rank(self) -> tuple[ShortlistEntry, ...]:
        """暫定順位の昇順で selected を返す。順位は validator が 1..N を保証する。"""
        selected = [entry for entry in self.entries if entry.decision == "selected"]
        return tuple(sorted(selected, key=lambda entry: entry.rank or 0))

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class ShortlistConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SelectionBinding:
    selection_id: str
    run_revision_id: str
    as_of: date
    profile: str
    macro_context_id: str | None
    review_tickers: tuple[str, ...]
    candidate_er: Mapping[str, float]
    # ticker -> the selection's longlist row. Empty when the selection was published
    # without a longlist, in which case nothing is burned in and the review surface
    # degrades once the bound run is evicted.
    candidate_machine_rows: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    attention_policy_id: str = VALUE_CARRY_ONLY_ATTENTION_POLICY_ID
    attention_policy_hash: str = ""
    attention_policy_parameters: Mapping[str, object] = field(default_factory=dict)
    review_basis_shortlist_id: str | None = None


def _machine_snapshot(row: Mapping[str, object] | None) -> ShortlistMachineSnapshot | None:
    """Read the selection's longlist row into the judgment's own record.

    Unknown keys are dropped rather than rejected: the longlist row also carries the
    research hand-off block, and a judgment record does not need to grow every time
    that view does.
    """

    if row is None:
        return None
    fields = set(ShortlistMachineSnapshot.model_fields)
    payload = {key: value for key, value in row.items() if key in fields}
    return ShortlistMachineSnapshot.model_validate(payload)


class ShortlistService:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish(
        self,
        shortlist: Shortlist,
        *,
        selection: SelectionBinding,
    ) -> Shortlist:
        expected = (
            selection.selection_id,
            selection.run_revision_id,
            selection.as_of,
            selection.profile,
            selection.macro_context_id,
        )
        actual = (
            shortlist.selection_id,
            shortlist.run_revision_id,
            shortlist.as_of,
            shortlist.profile,
            shortlist.macro_context_id,
        )
        if actual != expected:
            raise ShortlistConflictError("shortlist source selection metadata does not match")
        contract_expected = (
            selection.attention_policy_id,
            selection.attention_policy_hash,
            dict(selection.attention_policy_parameters),
            selection.review_basis_shortlist_id,
        )
        contract_actual = (
            shortlist.attention_policy_id,
            shortlist.attention_policy_hash,
            shortlist.attention_policy_parameters.model_dump(mode="json"),
            shortlist.review_basis_shortlist_id,
        )
        if contract_actual != contract_expected:
            raise ShortlistConflictError("shortlist Attention Policy binding does not match")
        entry_tickers = tuple(entry.ticker for entry in shortlist.entries)
        if set(entry_tickers) != set(selection.review_tickers):
            missing = sorted(set(selection.review_tickers) - set(entry_tickers))
            extra = sorted(set(entry_tickers) - set(selection.review_tickers))
            raise ShortlistConflictError(
                f"shortlist entries must equal the Review Set; missing={missing}, extra={extra}"
            )
        missing_rows = sorted(
            set(selection.review_tickers) - selection.candidate_machine_rows.keys()
        )
        if missing_rows:
            raise ShortlistConflictError(
                f"Review Set source rows are missing: {', '.join(missing_rows)}"
            )
        # Burn the machine estimate into the judgment before it is persisted, so the
        # later comparison reads what the judgment saw rather than whatever run is
        # still in the store.
        shortlist = shortlist.model_copy(
            update={
                "entries": tuple(
                    entry.model_copy(
                        update={
                            "er_annual": selection.candidate_er.get(entry.ticker),
                            "machine_snapshot": _machine_snapshot(
                                selection.candidate_machine_rows.get(entry.ticker)
                            ),
                        }
                    )
                    for entry in shortlist.entries
                )
            }
        )
        initialize_database(self._db_path)
        payload = canonical_json(shortlist.payload())
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload FROM shortlist WHERE shortlist_id = ?",
                    (shortlist.shortlist_id,),
                ).fetchone()
                if row is not None:
                    if str(row[0]) == payload:
                        connection.rollback()
                        return shortlist
                    raise ShortlistConflictError(
                        f"shortlist differs from existing publication: {shortlist.shortlist_id}"
                    )
                latest = connection.execute(
                    """
                    SELECT shortlist_id FROM shortlist
                    ORDER BY as_of DESC, published_at DESC, shortlist_id DESC
                    LIMIT 1
                    """
                ).fetchone()
                latest_id = str(latest[0]) if latest is not None else None
                if shortlist.review_basis_shortlist_id != latest_id:
                    raise ShortlistConflictError(
                        "Review Basis is stale; source selection must be rebuilt against "
                        f"the latest canonical shortlist: {latest_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO shortlist (
                        shortlist_id, selection_id, run_revision_id, as_of, published_at, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        shortlist.shortlist_id,
                        shortlist.selection_id,
                        shortlist.run_revision_id,
                        shortlist.as_of.isoformat(),
                        shortlist.published_at.isoformat(),
                        payload,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return shortlist


__all__ = [
    "CATALYST_HORIZON_DAYS",
    "SHORTLIST_SCHEMA_VERSION",
    "SelectionBinding",
    "Shortlist",
    "ShortlistConflictError",
    "ShortlistEntry",
    "ShortlistNarrative",
    "ShortlistService",
]
