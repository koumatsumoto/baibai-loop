from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.helpers.provider_fixtures import _FakeResponse, _ForbiddenResponse, _StaticSession

from baibai_engine.data_collection.transport.context import (
    IndicatorsProviderError,
    SourceWithheldError,
)
from tools import pmi_manifest


@pytest.mark.parametrize(
    "reason", ["HTTP 429", "HTTP 404", "HTTP 503", "network failure", "too large", "not UTF-8"]
)
def test_plain_index_failure_is_not_retried_through_browser(monkeypatch, reason):
    error = IndicatorsProviderError(reason)
    monkeypatch.setattr(pmi_manifest, "fetch_text", Mock(side_effect=error))
    context = SimpleNamespace(session=object(), browser_fetcher=Mock(), discard_cached_bytes=Mock())
    with pytest.raises(IndicatorsProviderError) as caught:
        pmi_manifest.release_index_entries(context)
    assert caught.value is error
    context.browser_fetcher.assert_not_called()
    context.discard_cached_bytes.assert_not_called()


def test_forbidden_index_keeps_original_http_failure_without_browser_route():
    context = SimpleNamespace(
        session=_StaticSession(_ForbiddenResponse()),
        bytes_cache={},
        check_deadline=Mock(),
        browser_fetcher=Mock(),
        discard_cached_bytes=Mock(),
    )
    with pytest.raises(SourceWithheldError, match="source withheld") as caught:
        pmi_manifest.release_index_entries(context)
    assert caught.value.status_code == 403
    assert caught.value.__cause__ is not None
    assert str(caught.value.__cause__) == "403 Client Error: Forbidden"
    context.browser_fetcher.assert_not_called()
    context.discard_cached_bytes.assert_not_called()


def test_empty_successful_index_still_uses_rendered_html():
    response = _FakeResponse(b"", status_code=200)
    browser = SimpleNamespace(fetch_html=Mock(return_value="<html></html>"))
    context = SimpleNamespace(
        session=_StaticSession(response),
        bytes_cache={},
        check_deadline=Mock(),
        browser_fetcher=Mock(return_value=browser),
        discard_cached_bytes=Mock(),
    )
    assert pmi_manifest.release_index_entries(context) == ()
    assert response.closed
    context.discard_cached_bytes.assert_called_once_with(pmi_manifest.RELEASE_INDEX_URL, None)
    browser.fetch_html.assert_called_once_with(pmi_manifest.RELEASE_INDEX_URL)


@pytest.mark.parametrize("withheld", [False, True])
def test_rendered_index_route_remains_explicit_for_unusable_success_or_withheld(
    monkeypatch, withheld
):
    fetch = (
        Mock(side_effect=SourceWithheldError("empty response"))
        if withheld
        else Mock(return_value="<html></html>")
    )
    monkeypatch.setattr(pmi_manifest, "fetch_text", fetch)
    browser = SimpleNamespace(fetch_html=Mock(return_value="<html></html>"))
    context = SimpleNamespace(
        session=object(), browser_fetcher=Mock(return_value=browser), discard_cached_bytes=Mock()
    )
    assert pmi_manifest.release_index_entries(context) == ()
    context.discard_cached_bytes.assert_called_once_with(pmi_manifest.RELEASE_INDEX_URL, None)
    browser.fetch_html.assert_called_once_with(pmi_manifest.RELEASE_INDEX_URL)
