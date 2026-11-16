"""合成IFRS本文の組替注記と列役割を、実際の測定期へ限定する。"""

from copy import deepcopy

import pytest
from tests.engine.machine_analysis.observations.financials.test_pdf_selection import (
    document,
    models,
)
from tests.helpers.pdf_statements import base, pages, statement

from baibai_engine.machine_analysis.models.company_growth.reading import (
    CompanyGrowthReader,
    metric_value,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    PdfStatement,
    extract_pages,
)


def period_label(annual):
    return "連結会計年度" if annual else "第1四半期連結累計期間"


@pytest.mark.parametrize("annual", [False, True])
@pytest.mark.parametrize("other", ["opposite", "中間連結会計期間"])
def test_restatement_note_cannot_be_borrowed_from_another_period(annual, other):
    text = pages(annual)
    other = period_label(not annual) if other == "opposite" else other
    text[1] = f"前{other}の数値についても、IFRSベースに組み替えて比較分析"
    original = deepcopy(text)
    result = extract_pages(base(annual), text)
    assert result.reason == "pdf_comparative_basis_not_explicit"
    assert not result.pairs
    assert result.source_sha256 == base(annual).source_sha256
    assert result.received_at == base(annual).received_at
    assert text == original


@pytest.mark.parametrize("annual", [False, True])
@pytest.mark.parametrize("mode", ["prior", "current", "both", "reversed", "extra"])
def test_column_roles_must_match_the_measured_period_in_order(annual, mode):
    text = pages(annual)
    expected, other = period_label(annual), period_label(not annual)
    prior, current = f"前{expected}", f"当{expected}"
    if mode in {"prior", "both"}:
        prior = f"前{other}"
    if mode in {"current", "both"}:
        current = f"当{other}"
    if mode == "reversed":
        prior, current = current, prior
    if mode == "extra":
        prior += f"及び前{other}"
    text[2] = text[2].replace(f"前{expected}", "PRIOR_ROLE")
    text[2] = text[2].replace(f"当{expected}", current).replace("PRIOR_ROLE", prior)
    result = extract_pages(base(annual), text)
    assert result.reason == "pdf_column_roles_unsupported"
    assert not result.pairs


@pytest.mark.parametrize("annual", [False, True])
def test_matching_period_keeps_values_precision_and_source_with_other_notes_present(annual):
    text = pages(annual)
    expected = statement(annual)
    # A note for another period elsewhere does not invalidate the matching note.
    text.append(pages(not annual)[1])
    assert extract_pages(base(annual), text) == expected
    assert expected.reason is None
    assert len(expected.pairs) == 3
    assert expected.rounding == "truncate_below_unit"
    assert expected.current_start == document(annual).period_start
    assert expected.current_end == document(annual).period_end


@pytest.mark.parametrize("annual", [False, True])
@pytest.mark.parametrize("mode", ["note", "columns"])
def test_invalid_period_authority_cannot_create_restated_ttm_or_growth(annual, mode):
    text = pages(annual)
    if mode == "note":
        text[1] = pages(not annual)[1]
    else:
        text[2] = text[2].replace(period_label(annual), period_label(not annual))
    invalid = extract_pages(base(annual), text)
    assert invalid.reason is not None
    documents = [document(True)] if annual else [document(True), document()]
    originals = deepcopy(documents)
    pdfs = [invalid] if annual else [statement(True), invalid]
    before = [item.model_dump_json() for item in pdfs]
    financial, growth, technical = models(documents=documents, pdfs=pdfs)
    native = models(documents=documents, pdfs=[])
    assert financial.payload["financial"]["pdf_selection"]["current"] is None
    reader = CompanyGrowthReader(growth.payload)
    observed = reader.observation(reader.latest_period, "reported" if annual else "quarter")
    assert metric_value(observed, "parent_profit") == document(annual).profit
    assert not any(
        item.get("source_sha256") == invalid.source_sha256 for item in growth.payload["changes"]
    )
    assert financial.payload["earnings_price"] == native[0].payload["earnings_price"]
    assert technical == native[2]
    assert documents == originals
    assert [item.model_dump_json() for item in pdfs] == before
    if not annual:
        assert financial.payload["earnings_price"]["profit_ttm_yen"] is None


@pytest.mark.parametrize("annual", [False, True])
def test_old_typed_pdf_is_read_as_saved_without_reextracting(annual):
    saved = statement(annual).model_dump(mode="json")
    saved["extractor_revision"] = "pdf-restated-display-statements-v4-ifrs-interim"
    saved["comparative_basis_text"] = pages(not annual)[1]
    original = deepcopy(saved)
    # Stored old observations stay historical evidence, not silently re-extracted.
    assert PdfStatement.model_validate(saved).model_dump(mode="json") == original
    assert saved == original
