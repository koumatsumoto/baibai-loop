from __future__ import annotations

import pytest

from baibai_engine.foundation.redaction import redact_credentials


@pytest.mark.parametrize(
    "text",
    [
        "403 Client Error for url: https://api.e-stat.go.jp/rest/getStatsData?appId=SECRET&statsDataId=1",
        "Max retries exceeded with url: /rest/getStatsData?appId=SECRET&x=1",
        "failed: https://example.com/v1?api_key=SECRET",
        "failed: https://example.com/v1?token=SECRET&next=2",
        "failed: https://example.com/v1?access_token=SECRET",
    ],
)
def test_credential_query_values_are_masked(text: str) -> None:
    masked = redact_credentials(text)

    assert "SECRET" not in masked
    assert "<redacted>" in masked


def test_non_credential_text_is_left_intact() -> None:
    text = (
        "failed to fetch https://www.jpx.co.jp/markets/statistics-equities/margin/index.html: 503"
    )

    assert redact_credentials(text) == text
