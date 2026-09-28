"""表示bundleの生成時刻とsource状態を示す。"""

from __future__ import annotations

from datetime import datetime

from baibai_web.readmodel.builders.presentation import _JST
from baibai_web.readmodel.models import (
    MetaBatch,
    MetaView,
)
from baibai_web.sources.db_sources import DbMetaSource


def build_meta(source: DbMetaSource, *, batch: MetaBatch | None = None) -> MetaView:
    """Report per-store as-of freshness so a consumer can judge view staleness."""

    return MetaView(
        generated_at=datetime.now(_JST),
        data_updated_at=source.data_updated_at(),
        screening_as_of=source.screening_as_of(),
        macro_as_of=source.macro_as_of(),
        app_db_updated_at=source.app_db_updated_at(),
        batch=batch,
    )
