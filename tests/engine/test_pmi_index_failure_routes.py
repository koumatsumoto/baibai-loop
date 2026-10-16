from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from baibai_engine.market_context.indicators import pmi_manifest
from baibai_engine.market_context.indicators.providers.base import (
    IndicatorsProviderError,
    SourceWithheldError,
)


@pytest.mark.parametrize("reason", ["HTTP 429", "HTTP 404", "HTTP 503", "network failure", "too large", "not UTF-8"])
def test_plain_index_failure_is_not_retried_through_browser(monkeypatch, reason):
    error = IndicatorsProviderError(reason)
    monkeypatch.setattr(pmi_manifest, "fetch_text", Mock(side_effect=error))
    context = SimpleNamespace(session=object(), browser_fetcher=Mock(), discard_cached_bytes=Mock())
    with pytest.raises(IndicatorsProviderError) as caught:
        pmi_manifest.release_index_entries(context)
    assert caught.value is error
    context.browser_fetcher.assert_not_called()
    context.discard_cached_bytes.assert_not_called()


@pytest.mark.parametrize("withheld", [False, True])
def test_rendered_index_route_remains_explicit_for_unusable_success_or_withheld(monkeypatch, withheld):
    fetch = Mock(side_effect=SourceWithheldError("empty response")) if withheld else Mock(return_value="<html></html>")
    monkeypatch.setattr(pmi_manifest, "fetch_text", fetch)
    browser = SimpleNamespace(fetch_html=Mock(return_value="<html></html>"))
    context = SimpleNamespace(session=object(), browser_fetcher=Mock(return_value=browser), discard_cached_bytes=Mock())
    assert pmi_manifest.release_index_entries(context) == ()
    context.discard_cached_bytes.assert_called_once_with()
    browser.fetch_html.assert_called_once_with(pmi_manifest.RELEASE_INDEX_URL)
