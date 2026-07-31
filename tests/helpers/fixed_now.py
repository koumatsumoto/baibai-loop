"""The instant the seeded research fixtures are judged against.

`tests/fixtures/thesis/2331-decision.yaml` carries a human evidence override with
a hard-coded `expires_at`. Judged against the wall clock it lapses, and every test
that publishes the fixture starts failing at that minute — the suite reports a
clock reading as a defect. Passing this instant keeps the fixture describing the
situation it was written for, so a failure means something changed.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

FIXED_NOW = datetime(2026, 7, 14, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
