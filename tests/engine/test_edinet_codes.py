from __future__ import annotations

import pytest

from baibai_engine.market.edinet_decode import parse_sec_code
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.market.providers.edinet import parse_doc_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7203", "7203"), ("72030", "7203"), ("130a", "130A"), ("130A0", "130A")],
)
def test_edinet_code_normalizes_ordinary_share_codes(raw: str, expected: str) -> None:
    assert parse_sec_code(raw) == expected


@pytest.mark.parametrize("raw", ["１２３４", "123!", "123é", "12ß", "12ß0", "", None])
def test_malformed_edinet_code_uses_provider_error(raw: object) -> None:
    with pytest.raises(EDINETProviderError, match="invalid EDINET secCode"):
        parse_sec_code(raw)


def test_noncommon_issue_is_not_folded_into_ordinary_stock() -> None:
    with pytest.raises(EDINETProviderError, match="unsupported EDINET secCode suffix"):
        parse_sec_code("72031")


def test_doc_id_rejects_non_ascii_before_case_conversion() -> None:
    assert parse_doc_id("s100abcd") == "S100ABCD"
    with pytest.raises(EDINETProviderError, match="invalid EDINET docID"):
        parse_doc_id("S100abß")
