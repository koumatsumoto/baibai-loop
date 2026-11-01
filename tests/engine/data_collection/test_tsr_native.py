"""TSRの原受領は形状に依存した値抽出より先に保持する。"""

import pytest
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _FakeResponse, _StaticSession

from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.providers.tsr.bankruptcies import TsrBankruptciesSource
from baibai_engine.data_collection.transport.context import IndicatorsProviderError


def test_tsr_acquires_original_bytes_without_parsing_values():
    source = require_source("tsr-bankruptcies")
    body = b'{"unrecognized": "published body"}'
    result = TsrBankruptciesSource().fetch_document(
        source, session=_StaticSession(_FakeResponse(body))
    )
    assert result.payload == body
    assert result.source_id == source.source_id


def test_tsr_rejects_non_json_response():
    source = require_source("tsr-bankruptcies")
    with pytest.raises(IndicatorsProviderError, match="not UTF-8 JSON"):
        TsrBankruptciesSource().fetch_document(
            source, session=_StaticSession(_FakeResponse(b"<html>not JSON</html>"))
        )
