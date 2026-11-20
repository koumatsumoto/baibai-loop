"""#2253: replay one already verified statutory filing. No fetch, write or model override."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from baibai_engine.machine_analysis.observations.financials.edinet_metrics.csv import (
    parse_csv_metric_rows,
    read_csv_zip_members,
)

MEMBER = "XBRL_TO_CSV/jpcrp040300-ssr-001_E04246-000_2026-09-30_01_2026-11-04.csv"
GROUP_DIGEST = "8c315b7e60d7c0e555b3fddcef54b22178eabcf0fe9f91581b1ce11349b3d58e"
OPERANDS_DIGEST = "0e90dfa4467c948c84895a99762931871a928eab76bd73d2bc99a3ff9aade95b"
SIZE = 24_852
SUMMARY = "jpcrp_cor:ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults"
PL = "jppfs_cor:ProfitLossAttributableToOwnersOfParent"
GROUP = "jpcrp_cor:BusinessResultsOfGroupTextBlock"
CONTEXTS = ("Prior1InterimDuration", "InterimDuration", "Prior1YearDuration")
DEI = {
    "EDINETCodeDEI": "E04246",
    "SecurityCodeDEI": "91270",
    "AccountingStandardsDEI": "Japan GAAP",
    "WhetherConsolidatedFinancialStatementsArePreparedDEI": "true",
    "TypeOfCurrentPeriodDEI": "HY",
    "CurrentFiscalYearStartDateDEI": "2026-04-01",
    "CurrentPeriodEndDateDEI": "2026-09-30",
    "CurrentFiscalYearEndDateDEI": "2027-03-31",
    "PreviousFiscalYearStartDateDEI": "2025-04-01",
    "ComparativePeriodEndDateDEI": "2025-09-30",
    "PreviousFiscalYearEndDateDEI": "2026-03-31",
    "AmendmentFlagDEI": "false",
}


@dataclass(frozen=True)
class Identity:
    doc_id: str
    document_revision: str
    archive_sha256: str
    extraction_revision: str


FIXED = Identity(
    "S100ZB35",
    "a60152a62380291f2bd37901c47c5bc3ac74de1eabaa49b4563174c057b7c22f",
    "44a05b730472295833685b72c7eb0b6bbf76c9794cea6a1d65584f5b4a50f5f3",
    "restated-h1-study-v1",
)
NOV13 = "3d4e6b13a8b84f99bf8689c74c59f56fb6a9472118ccb72f4811f96162dd1c63"
NOV19 = "6e03fbd1560f26354ff4371f027339f848f3ccd2ee3239ffc86570bf3dbf5d0d"
VERIFIED_SELECTIONS = {
    NOV13: datetime.fromisoformat("2026-11-13T21:24:58.328947Z"),
    NOV19: datetime.fromisoformat("2026-11-19T21:59:01.545547Z"),
}
type Operands = tuple[int, int, int]  # Previous interim, current interim, previous year; JPY.


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _valid_amounts(values: object) -> bool:
    if type(values) is not tuple or len(values) != 3 or any(type(n) is not int for n in values):
        return False
    return _digest("\t".join(str(n) for n in values).encode()) == OPERANDS_DIGEST


def member_operands(
    rows: Sequence[dict[str, str]], *, group_digest: str = GROUP_DIGEST
) -> Operands | None:
    # The complete, manually verified group table includes the policy note governing both
    # comparative periods. No keyword search across other tables/filings is admissible.
    def one(tag: str, context: str) -> dict[str, str]:
        (row,) = (r for r in rows if r["要素ID"] == tag and r["コンテキストID"] == context)
        return row

    def jpy(tag: str, context: str, basis: str) -> int:
        r = one(tag, context)
        if (r["連結・個別"], r["期間・時点"], r["ユニットID"], r["単位"]) != (
            basis,
            "期間",
            "JPY",
            "円",
        ):
            raise ValueError("not the displayed yen amount")
        return int(r["値"])

    try:
        if any(one("jpdei_cor:" + tag, "FilingDateInstant")["値"] != v for tag, v in DEI.items()):
            return None
        if _digest(one(GROUP, "FilingDateInstant")["値"].encode()) != group_digest:
            return None
        prior, current, annual = (jpy(SUMMARY, c, "その他") for c in CONTEXTS)
        if (prior, current) != tuple(jpy(PL, c, "連結") for c in CONTEXTS[:2]):
            return None
        values = prior, current, annual
        return values if _valid_amounts(values) else None
    except (KeyError, ValueError):
        return None


@dataclass(frozen=True)
class Witness:
    identity: Identity
    operands: Operands
    metric_fingerprint: str | None

    @property
    def profit_yen(self) -> int:
        prior, current, annual = self.operands
        return annual + current - prior


def replay(
    key: Identity,
    read: Callable[[], bytes],
    *,
    prior: Witness | None = None,
    selection: str = NOV19,
    known_at: datetime = VERIFIED_SELECTIONS[NOV19],
) -> Witness | None:
    """Replay the fixed selections actually verified in #2253, never a rolling head.

    The caller supplies a prequalified identity, not a ticker/stage or a public-date guess.
    An old/forged seed is not evidence. No discovery, second source or fallback read.
    """
    clock = VERIFIED_SELECTIONS.get(selection)
    if key != FIXED or clock is None or known_at.tzinfo is None or known_at < clock:
        return None
    if prior is not None and prior.identity == key and _valid_amounts(prior.operands):
        # Only the operands are reusable; no ordinary metric was parsed on this path.
        return Witness(key, prior.operands, None)
    raw = read()
    if len(raw) != SIZE or _digest(raw) != key.archive_sha256:
        return None
    members = list(read_csv_zip_members(raw))
    eligible = [rows for name, rows in members if name == MEMBER]
    if (
        len(eligible) != 1
        or (values := member_operands(eligible[0], group_digest=GROUP_DIGEST)) is None
    ):
        return None
    metric = parse_csv_metric_rows(
        ticker="9127",
        doc_id=key.doc_id,
        doc_type_code="160",
        rows=[row for _, member in members for row in member],
        submit_datetime="2026-11-04 15:27",
        period_start=date(2026, 4, 1),
        period_end=date(2027, 3, 31),
    )
    payload = json.dumps(
        asdict(metric), sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )
    return Witness(key, values, _digest(payload.encode()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="existing local type-5 ZIP; no network")
    args = parser.parse_args()
    with args.archive.open("rb") as handle:
        found = replay(FIXED, lambda: handle.read(SIZE + 1))
    if found is None:
        parser.exit(1, "not the attested filing/operand set\n")
    print(json.dumps({**asdict(found), "reported_parent_profit_ttm_yen": found.profit_yen}))


if __name__ == "__main__":
    main()
