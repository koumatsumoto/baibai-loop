import pytest

from baibai_engine.market import valuation_catalysts as catalysts


def test_workbook_is_closed_when_monthly_rows_are_missing(mocker):
    workbook = mocker.Mock(worksheets=[])
    mocker.patch.object(catalysts.openpyxl, "load_workbook", return_value=workbook)
    with pytest.raises(catalysts.ValuationCatalystError, match="no monthly rows"):
        catalysts.parse_tse_capital_policy_workbook(b"workbook")
    workbook.close.assert_called_once_with()


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("outcome", ["ok", "http_error", "parse_error"])
def test_refresh_closes_response_and_only_owned_session(tmp_path, mocker, owned, outcome):
    response = mocker.Mock(status_code=500 if outcome == "http_error" else 200, content=b"book")
    client = mocker.Mock()
    client.get.return_value = response
    mocker.patch.object(catalysts.requests, "Session", return_value=client)
    parser = mocker.patch.object(catalysts, "parse_tse_capital_policy_workbook", return_value=())
    writer = mocker.patch.object(catalysts, "store_tse_capital_policy_rows")
    if outcome == "parse_error":
        parser.side_effect = catalysts.ValuationCatalystError("invalid workbook")
    if outcome == "ok":
        summary = catalysts.refresh_tse_capital_policy(
            tmp_path / "market.sqlite", session=None if owned else client
        )
        assert summary.row_count == 0
        writer.assert_called_once()
    else:
        with pytest.raises(catalysts.ValuationCatalystError):
            catalysts.refresh_tse_capital_policy(
                tmp_path / "market.sqlite", session=None if owned else client
            )
        writer.assert_not_called()
    response.close.assert_called_once_with()
    if owned:
        client.close.assert_called_once_with()
    else:
        client.close.assert_not_called()


def test_request_failure_still_closes_owned_session(tmp_path, mocker):
    client = mocker.Mock()
    client.get.side_effect = catalysts.requests.ConnectionError("unavailable")
    mocker.patch.object(catalysts.requests, "Session", return_value=client)
    with pytest.raises(catalysts.requests.ConnectionError):
        catalysts.refresh_tse_capital_policy(tmp_path / "market.sqlite")
    client.close.assert_called_once_with()
