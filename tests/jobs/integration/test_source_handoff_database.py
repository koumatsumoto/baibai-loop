import pytest

import baibai_jobs.integration.source_handoff as source_handoff


@pytest.mark.parametrize("source", ["unknown", "jquants-typo", "edinet-typo", "article-unknown"])
def test_unknown_source_is_rejected_before_opening_a_packet(source, tmp_path):
    with pytest.raises(ValueError, match="unsupported"):
        source_handoff._replay(source, tmp_path / "market.sqlite", tmp_path)
    assert not list(tmp_path.iterdir())
