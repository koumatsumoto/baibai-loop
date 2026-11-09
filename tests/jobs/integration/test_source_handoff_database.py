import pytest

import baibai_jobs.integration.source_handoff as source_handoff


@pytest.mark.parametrize("source", ["unknown", "jquants-typo", "edinet-typo", "article-unknown"])
def test_unknown_source_never_reaches_an_existing_owner(source, tmp_path, mocker):
    jquants = mocker.patch.object(source_handoff, "import_jquants_handoff")
    with pytest.raises(ValueError, match="unsupported"):
        source_handoff._replay(source, tmp_path / "market.sqlite", tmp_path)
    jquants.assert_not_called()
