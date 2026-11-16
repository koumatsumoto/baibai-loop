"""出典階層と同refの別IDだけを示し、locatorや独立性を追加認定しない。"""

import json

import pytest
from tests.engine.investment_judgment.company_research.test_brief_evidence import (
    _hypothesis,
    _save,
    _source,
)

from baibai_engine.investment_judgment.company_research.context import company_context_brief


@pytest.mark.parametrize("kind", ["research", "assessment"])
def test_same_external_ref_is_bounded_without_locator_or_source_clock_collapse(tmp_path, kind):
    sources = [
        _source(f"copy-{index}", day="2026-08-31" if index else "2026-10-14") for index in range(6)
    ]
    # Never repeat an authenticated/private locator in the first-view projection.
    for item in sources:
        item["ref"] = "https://user:synthetic-secret@example.test/document?token=synthetic-token"
    sources[1]["source_tier"] = "secondary"
    local = _source("local", local=True)
    second_local = _source("another-local", local=True)
    sources.extend([local, second_local])
    sources.append(_source("different-ref"))
    context = _save(
        tmp_path / "app",
        kind,
        sources,
        [
            _hypothesis("出典の違いを確認する", ["copy-0", "copy-1", "local"]),
            _hypothesis("別のURL", ["different-ref"]),
        ],
    )
    original = context.model_dump_json()
    brief = company_context_brief(context)[kind]
    first, second, local_brief = brief["hypotheses"][0]["sources"]
    assert first["source_tier"] == "primary"
    assert second["source_tier"] == "secondary"
    assert first["same_external_ref_source_ids"] == ["copy-1", "copy-2", "copy-3"]
    assert second["same_external_ref_source_ids"] == ["copy-0", "copy-2", "copy-3"]
    assert first["same_external_ref_sources_omitted"] == 2
    assert first["as_of"] == "2026-10-14"
    assert second["as_of"] == "2026-08-31"
    assert local_brief["source_tier"] == "local_data"
    assert "same_external_ref_source_ids" not in local_brief
    assert "same_external_ref_source_ids" not in brief["hypotheses"][1]["sources"][0]
    rendered = json.dumps(brief)
    assert "synthetic-secret" not in rendered
    assert "synthetic-token" not in rendered
    assert "https://" not in rendered
    assert '"provider"' not in rendered
    assert '"dataset"' not in rendered
    first["same_external_ref_source_ids"].clear()
    assert context.model_dump_json() == original
