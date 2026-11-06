"""PDF観測を一度固定し、財務が成長モデルの表示結果へ依存しない。"""

from datetime import timedelta

import pytest
from tests.engine.machine_analysis.observations.financials.test_pdf_jp_restatement import (
    ASOF,
    docs,
    pdf,
)

from baibai_engine.machine_analysis.generation import financial_models
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.generation import generate_company_growth
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials import pdf_selection
from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    build_pdf_supplement,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

RELEASE = InputRelease(release_id="synthetic", manifest_sha256="a" * 64)


def generate(statements):
    return {
        model.model_id: model
        for model in financial_models.generate_financial_and_price_models(
            ticker="9127",
            documents=docs(),
            adjusted_bars=(PriceBar(ASOF, 1000.0, None),),
            adjustment_events=(),
            trading_dates=(ASOF,),
            expected_price_date=ASOF,
            sector=None,
            as_of=ASOF,
            input_release=RELEASE,
            ttm_rules=DEFAULT_TTM_RULES,
            pdf_statements=statements,
        )
    }


def test_pdf_selection_and_supplement_are_shared_once_not_read_from_growth(mocker):
    statements = (pdf(), pdf(True))
    original = generate(statements)
    choose = mocker.spy(pdf_selection, "select_pdf_financials")
    supplement = mocker.spy(pdf_selection, "build_pdf_supplement")
    real_growth = financial_models.generate_company_growth
    received = []

    def growth_without_display_supplement(*args, **kwargs):
        fixed = kwargs["pdf_input"]
        received.append(fixed)
        before = (fixed.selection.model_dump_json(), fixed.supplement.model_dump_json())
        result = real_growth(*args, **kwargs)
        assert before == (fixed.selection.model_dump_json(), fixed.supplement.model_dump_json())
        return result.model_copy(update={"pdf_supplement": None})

    mocker.patch.object(
        financial_models, "generate_company_growth", side_effect=growth_without_display_supplement
    )
    changed = generate(statements)
    assert choose.call_count == supplement.call_count == 1
    assert received[0].supplement is supplement.spy_return
    assert changed["company_growth"].payload.get("pdf_supplement") is None
    assert original["company_growth"].payload["pdf_supplement"] is not None
    # Includes the exact financial payload, source evidence and calculation_revision.
    assert changed["company_financials"] == original["company_financials"]
    assert changed["technical_analysis"] == original["technical_analysis"]


@pytest.mark.parametrize("kind", ["absent", "future", "unsupported", "duplicate"])
def test_common_input_preserves_existing_missing_date_precision_and_receipts(kind):
    current = pdf()
    statements = {
        "absent": (),
        "future": (current.model_copy(update={"published_on": ASOF + timedelta(days=1)}),),
        "unsupported": (current.model_copy(update={"reason": "pdf_precision_unknown"}),),
        "duplicate": (current, current, pdf(True)),
    }[kind]
    before = tuple(item.model_dump_json() for item in statements)
    fixed = pdf_selection.prepare_pdf_financials(docs(), statements, as_of=ASOF)
    models = generate(statements)
    expected = build_pdf_supplement(statements, as_of=ASOF)
    expected_payload = None if expected is None else expected.model_dump(mode="json")
    assert (None if fixed is None else fixed.supplement) == expected
    assert models["company_growth"].payload.get("pdf_supplement") == expected_payload
    financial = models["company_financials"].payload["financial"]
    assert financial.get("pdf_supplement") == expected_payload
    assert ("pdf_supplement" in financial) == (expected is not None)
    assert models["company_growth"].payload.get("pdf_selection") == (
        None
        if fixed is None or fixed.selection is None
        else fixed.selection.model_dump(mode="json")
    )
    assert before == tuple(item.model_dump_json() for item in statements)


@pytest.mark.parametrize("mismatch", ["ticker", "as_of"])
def test_growth_cannot_adopt_pdf_observations_from_another_scope(mismatch):
    fixed = pdf_selection.prepare_pdf_financials(docs(), (pdf(),), as_of=ASOF)
    with pytest.raises(ValueError, match="different"):
        generate_company_growth(
            [],
            ticker="1001" if mismatch == "ticker" else "9127",
            sector=None,
            as_of=ASOF - timedelta(days=1) if mismatch == "as_of" else ASOF,
            input_release=RELEASE,
            pdf_input=fixed,
        )
