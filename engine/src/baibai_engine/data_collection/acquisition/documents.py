"""Original-document acquisition, without extracting indicator values."""

from collections.abc import Iterator, Mapping
from datetime import date

from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.providers.matsui.margin import MatsuiMarginSource
from baibai_engine.data_collection.providers.spglobal.pmi import SpGlobalPmiSource
from baibai_engine.data_collection.providers.tradingeconomics.pmi import TradingEconomicsPmiSource
from baibai_engine.data_collection.providers.tsr.bankruptcies import TsrBankruptciesSource
from baibai_engine.data_collection.receipts.native_sources.documents import SourceDocument
from baibai_engine.data_collection.transport.context import FetchContext, IndicatorsProviderError


def fetch_source_documents(
    source: SourceDefinition,
    *,
    start: date,
    end: date,
    context: FetchContext,
    existing: Mapping[str, SourceDocument],
    force: bool,
    recheck_existing: bool = True,
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
            recheck_existing=recheck_existing,
        )
    elif source.provider == "tradingeconomics_pmi":
        yield from TradingEconomicsPmiSource().fetch_documents(
            source,
            start=start,
            end=end,
            session=context.session,
            context=context,
            existing=existing,
            force=force,
            recheck_existing=recheck_existing,
        )
    elif source.provider == "tsr_bankruptcies":
        yield TsrBankruptciesSource().fetch_document(
            source, session=context.session, context=context
        )
    elif source.provider == "matsui_margin":
        # This is a current page, never a day-by-day history traversal.
        yield MatsuiMarginSource().fetch_document(source, session=context.session, context=context)
    else:
        raise IndicatorsProviderError(f"unsupported document source provider: {source.provider}")
