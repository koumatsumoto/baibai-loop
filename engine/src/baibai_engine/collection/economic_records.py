"""Native economic source responses retained before L2 interpretation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

# A complete source response grouped by its native observation dates.
type NativeRecords = Mapping[date, Sequence[Mapping[str, object]]]
