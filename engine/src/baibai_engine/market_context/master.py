"""共通L1 masterのcutoff以下の最新snapshotを、集合を変えずに読む。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_engine.foundation.sqlite_read import read_rows


@dataclass(frozen=True, slots=True)
class MasterSnapshot:
    as_of: date
    securities: dict[str, dict[str, object]]


def read_master_snapshot(path: Path, *, as_of: date) -> MasterSnapshot | None:
    rows = read_rows(
        path,
        "SELECT snapshot_date, ticker, name, market, sector_33, is_common_stock "
        "FROM jquants_master_snapshots WHERE snapshot_date = ("
        "SELECT MAX(snapshot_date) FROM jquants_master_snapshots "
        "WHERE snapshot_date != 'unknown' AND snapshot_date <= ?) ORDER BY ticker",
        (as_of.isoformat(),),
    )
    if not rows:
        return None
    return MasterSnapshot(
        date.fromisoformat(str(rows[0]["snapshot_date"])),
        {
            str(row["ticker"]): {
                "name": str(row["name"] or ""),
                "market_segment": str(row["market"] or ""),
                "sector_33": str(row["sector_33"] or ""),
                "is_common_stock": bool(row["is_common_stock"]),
                "snapshot_date": str(row["snapshot_date"]),
            }
            for row in rows
        },
    )
