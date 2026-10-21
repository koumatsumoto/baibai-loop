"""Workbook parser resource ownership."""

import pytest

from baibai_engine.market import valuation_catalysts as catalysts


def test_workbook_is_closed_when_monthly_rows_are_missing(mocker):
    workbook = mocker.Mock(worksheets=[])
    mocker.patch.object(catalysts.openpyxl, "load_workbook", return_value=workbook)
    with pytest.raises(catalysts.ValuationCatalystError, match="no monthly rows"):
        catalysts.parse_tse_capital_policy_workbook(b"workbook")
    workbook.close.assert_called_once_with()
