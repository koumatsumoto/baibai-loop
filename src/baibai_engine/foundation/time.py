from __future__ import annotations

from datetime import timedelta, timezone

# Japan Standard Time (+09:00). Records and rendered artefacts stamp run_at in JST.
JST = timezone(timedelta(hours=9))
