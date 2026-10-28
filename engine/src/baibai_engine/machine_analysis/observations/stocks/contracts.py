"""株式masterの固定観測契約。読取機構を読み込まない。"""
from dataclasses import dataclass
from datetime import date
from .models import SecurityMaster


@dataclass(frozen=True, slots=True)
class MasterSnapshotRead:
    """A point-in-time security-master read with its source snapshot status."""

    masters: tuple[SecurityMaster, ...]
    snapshot_date: date | None
    status: str


@dataclass(frozen=True, slots=True)
class MasterSnapshot:
    as_of: date
    securities: dict[str, dict[str, object]]
