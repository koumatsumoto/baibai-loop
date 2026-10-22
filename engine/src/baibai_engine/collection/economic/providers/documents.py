"""Original-document acquisition, without extracting indicator values."""

from collections.abc import Iterator, Mapping
from datetime import date

from baibai_engine.collection.economic.documents import SourceDocument
from baibai_engine.collection.economic.sources import SourceDefinition

from .base import FetchContext, IndicatorsProviderError
from .matsui_margin import MatsuiMarginSource
from .spglobal_pmi import SpGlobalPmiSource
from .tsr_bankruptcies import TsrBankruptciesSource


def fetch_source_documents(
    source: SourceDefinition,
    *,
    start: date,
    end: date,
    context: FetchContext,
    existing: Mapping[str, SourceDocument],
    force: bool,
) -> Iterator[SourceDocument]:
    if source.provider == "spglobal_pmi":
        yield from SpGlobalPmiSource().fetch_documents(
            source,
            start=start,
            end=end,
            session=context.session,
            context=context,
            existing=existing,
            force=force,
        )
    elif source.provider == "tsr_bankruptcies":
        yield TsrBankruptciesSource().fetch_document(
            source, session=context.session, context=context
        )
    elif source.provider == "matsui_margin":
        # This is a current page, never a day-by-day history traversal.
        yield MatsuiMarginSource().fetch_document(
            source, session=context.session, context=context
        )
    else:
        raise IndicatorsProviderError(f"unsupported document source provider: {source.provider}")
