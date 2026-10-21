"""Stock views must not publish an invalid ledger as an unheld portfolio."""

from datetime import date
from functools import partial

import pytest

from baibai_engine.read_api import PortfolioLedgerError
from baibai_web.readmodel.stocks.detail import prepare_security_inputs
from baibai_web.readmodel.stocks.screening import build_screening, build_screening_history_run


@pytest.mark.parametrize("surface", ["latest", "history", "security"])
def test_stock_views_propagate_invalid_ledger_instead_of_empty_holdings(mocker, surface):
    ledger = mocker.Mock()
    ledger.exists.return_value = True
    failure = PortfolioLedgerError("invalid ledger")
    ledger.snapshot.side_effect = failure
    research = mocker.Mock()
    research.revisions.return_value = []
    research.proposals.return_value = []
    screening = mocker.Mock()
    screening.latest_run.return_value = None

    build = {
        "latest": partial(build_screening, screening, ledger, research),
        "history": partial(
            build_screening_history_run, screening, ledger, research, as_of=date(2026, 10, 15)
        ),
        "security": partial(prepare_security_inputs, ledger, research, screening),
    }[surface]
    with pytest.raises(PortfolioLedgerError) as caught:
        build()
    assert caught.value is failure


def test_security_inputs_allow_an_uninitialized_ledger_without_reading_it(mocker):
    ledger = mocker.Mock()
    ledger.exists.return_value = False
    research = mocker.Mock()
    research.revisions.return_value = []
    research.proposals.return_value = []
    screening = mocker.Mock()
    screening.latest_run.return_value = None

    prepared = prepare_security_inputs(ledger, research, screening)
    assert prepared.holdings == {}
    assert prepared.reserved == set()
    ledger.snapshot.assert_not_called()
