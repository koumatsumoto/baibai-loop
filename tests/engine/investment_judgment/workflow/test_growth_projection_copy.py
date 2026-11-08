"""成長表示の一時copy削減は、保存入力と全投影の意味を変えない。"""

from copy import deepcopy
from datetime import UTC, date, datetime
from hashlib import sha256

import pytest
from tests.engine.machine_analysis.models.company_growth.test_company_growth import document
from tests.engine.machine_analysis.observations.financials.test_pdf_jp_restatement import (
    ASOF,
    docs,
    pdf,
)

from baibai_engine.investment_judgment.workflow import generated_inputs
from baibai_engine.machine_analysis.generation.contracts import ModelInputSlice
from baibai_engine.machine_analysis.models.company_growth.generation import generate_company_growth
from baibai_engine.machine_analysis.models.company_growth.models import CompanyGrowth, InputRelease
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    normalize_financial_summary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_selection import (
    prepare_pdf_financials,
)
from baibai_engine.primitives.json import canonical_json


def growth_payload(kind):
    release = InputRelease(release_id="fixture", manifest_sha256="a" * 64)
    if kind == "pdf":
        return generate_company_growth(
            docs(),
            ticker="9127",
            sector=None,
            as_of=ASOF,
            input_release=release,
            pdf_input=prepare_pdf_financials(docs(), (pdf(), pdf(True)), as_of=ASOF),
        ).model_dump(mode="json")
    rows = [
        normalize_financial_summary(document(year, quarter, 1000 * quarter, op=None, profit=0))
        for year in range(2020, 2024)
        for quarter in range(1, 5)
    ]
    payload = generate_company_growth(
        [] if kind == "unavailable" else rows,
        ticker="1234",
        sector=None,
        as_of=date(2026, 10, 24),
        input_release=release,
    ).model_dump(mode="json")
    if kind == "legacy":
        for period in payload["periods"]:
            for observation in period["observations"]:
                observation.pop("accounting_standard", None)
                observation.pop("period_status", None)
                for value in observation["metrics"].values():
                    if value["value"] is not None:
                        value["value"] = int(value["value"])
        for change in payload["changes"]:
            change.pop("comparison")
    return payload


def containers(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from containers(child)
    elif isinstance(value, list):
        yield value
        for child in value:
            yield from containers(child)


@pytest.mark.parametrize("kind", ["current", "legacy", "pdf", "unavailable"])
@pytest.mark.parametrize("periods", [12, None])
def test_projection_matches_full_dump_and_copy_without_aliasing(monkeypatch, kind, periods):
    payload = growth_payload(kind)
    ticker = payload["ticker"]
    saved = ModelInputSlice(
        generation_id="fixture",
        as_of=date(2026, 10, 24),
        evaluation_time=datetime(2026, 10, 24, tzinfo=UTC),
        inputs={"fixed": {"kind": "lake_release", "reference": "fixture", "sha256": "a" * 64}},
        subjects=(ticker,),
        models=(
            {
                "model_id": "company_growth",
                "subject": ticker,
                "calculation_revision": payload["calculation_method"],
                "payload": payload,
            },
        ),
    )
    before = canonical_json(saved.model_dump(mode="json"))
    digest = sha256(before.encode()).hexdigest()
    project = generated_inputs.project_company_growth
    dump = CompanyGrowth.model_dump

    # Reproduce both removed operations, without duplicating the projection policy.
    def full_dump(self, **kwargs):
        kwargs.pop("include", None)
        return dump(self, **kwargs)

    def copied_projection(source, **kwargs):
        return project(deepcopy(source), **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(CompanyGrowth, "model_dump", full_dump)
        patch.setattr(generated_inputs, "project_company_growth", copied_projection)
        expected = generated_inputs.project_generated_inputs(saved, company_growth_periods=periods)
    actual = generated_inputs.project_generated_inputs(saved, company_growth_periods=periods)
    assert canonical_json(actual) == canonical_json(expected)
    growth = actual["securities"][ticker]["company_growth"]
    assert growth["full_result_ref"] == "/inputs/generation/models/0/payload"
    assert growth["display_scope"]["period_count"] == min(periods or 999, len(payload["periods"]))
    source_ids = {id(value) for value in containers(saved.models[0].payload)}
    output = list(containers(actual))
    assert not source_ids.intersection(id(value) for value in output)
    for value in output:
        if isinstance(value, dict):
            value["mutation_probe"] = True
        else:
            value.append("mutation_probe")
    after = canonical_json(saved.model_dump(mode="json"))
    assert after == before
    assert sha256(after.encode()).hexdigest() == digest
