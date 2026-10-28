from __future__ import annotations

import pytest

from baibai_engine.primitives.stocks import (
    SecurityCodeError,
    normalize_ticker,
    parse_security_code_parts,
)
from baibai_engine.market.normalization import code_quality, normalize_ticker_or_none


@pytest.mark.parametrize("code", ["12!4", "１２３４", "株式銘柄", "12ß", "130A!", "１２３４0"])
def test_invalid_codes_are_rejected_without_escaping_normalization(code: str) -> None:
    with pytest.raises(SecurityCodeError):
        parse_security_code_parts(code)
    assert code_quality(code) == (None, "rejected")
    assert normalize_ticker_or_none(code) is None


@pytest.mark.parametrize("code", ["１２３４", "株式銘柄", "12ß", "12!4"])
def test_ticker_normalization_uses_the_model_ascii_contract(code: str) -> None:
    with pytest.raises(SecurityCodeError):
        normalize_ticker(code)


def test_ascii_ticker_normalization_preserves_common_and_other_issue_identity() -> None:
    assert normalize_ticker(" 130a ") == "130A"
    assert parse_security_code_parts(" 130a0 ") == ("130A", True)
    assert code_quality("130a0") == ("130A", "ok")
    assert parse_security_code_parts("130a1") == ("130A", False)
    assert code_quality("130a1") == (None, "excluded")
    assert normalize_ticker_or_none("130a1") is None
