from __future__ import annotations

import pytest

from baibai_engine.market.jpx_decode import parse_jpx_code
from baibai_engine.market.jpx_models import JPXProviderError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7203", "7203"), ("72030", "7203"), ("130a", "130A"), ("130A0", "130A")],
)
def test_jpx_code_normalizes_ordinary_share_codes(raw: str, expected: str) -> None:
    assert parse_jpx_code(raw) == expected


@pytest.mark.parametrize("raw", ["１２３４", "123!", "123é", "12ß", "12ß0", "72031", "", None])
def test_malformed_jpx_code_uses_provider_error(raw: object) -> None:
    with pytest.raises(JPXProviderError, match="invalid JPX code"):
        parse_jpx_code(raw)
