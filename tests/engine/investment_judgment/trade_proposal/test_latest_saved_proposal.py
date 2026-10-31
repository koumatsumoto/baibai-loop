"""最新基準日の保存提案を選び、時刻精度や破損を古い提案で隠さない。"""

import sqlite3
from contextlib import closing

import pytest

from baibai_engine.investment_judgment.trade_proposal import query
from baibai_engine.investment_judgment.trade_proposal.contracts import ProposalConflictError
from baibai_engine.storage.sqlite.application.schema import PROPOSAL_RECORDS_SCHEMA_SQL


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (
            [
                ("old", "2026-10-13", "2026-10-15T10:00:00+09:00", "discover_new"),
                ("new", "2026-10-14", "2026-10-14T10:00:00+09:00", "review_positions"),
            ],
            "new",
        ),
        (
            [
                ("a", "2026-10-14", "2026-10-14T11:00:00+09:00", "specified_stocks"),
                ("b", "2026-10-14", "2026-10-14T03:00:00+00:00", "review_positions"),
            ],
            "b",
        ),
        (
            [
                ("a", "2026-10-14", "2026-10-14T11:00:00+09:00", "discover_new"),
                ("b", "2026-10-14", "2026-10-14T02:00:00+00:00", "specified_stocks"),
            ],
            "b",
        ),
        (
            [
                ("z", "2026-10-14", "2026-10-14T11:00:00.000001+09:00", "discover_new"),
                ("a", "2026-10-14", "2026-10-14T11:00:00.000002+09:00", "specified_stocks"),
            ],
            "a",
        ),
    ],
)
def test_latest_uses_as_of_aware_instant_and_id_across_all_scopes(headers, expected, mocker):
    exact = mocker.patch.object(query, "load_investment_proposal", return_value=object())
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            "CREATE TABLE investment_proposal "
            "(proposal_id TEXT,as_of TEXT,published_at TEXT,request_scope TEXT)"
        )
        connection.executescript(PROPOSAL_RECORDS_SCHEMA_SQL)
        connection.executemany("INSERT INTO investment_proposal VALUES (?,?,?,?)", headers)
        assert query.load_latest_investment_proposal(connection) is exact.return_value
        exact.assert_called_once_with(connection, expected)


@pytest.mark.parametrize("published_at", ["invalid", "2026-10-14T11:00:00"])
def test_invalid_latest_day_header_is_not_skipped(published_at, mocker):
    exact = mocker.patch.object(query, "load_investment_proposal")
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            "CREATE TABLE investment_proposal (proposal_id TEXT,as_of TEXT,published_at TEXT)"
        )
        connection.executescript(PROPOSAL_RECORDS_SCHEMA_SQL)
        connection.executemany(
            "INSERT INTO investment_proposal VALUES (?,?,?)",
            [
                ("valid", "2026-10-14", "2026-10-14T10:00:00+09:00"),
                ("invalid", "2026-10-14", published_at),
            ],
        )
        with pytest.raises(ProposalConflictError, match="publication header"):
            query.load_latest_investment_proposal(connection)
    exact.assert_not_called()


def test_corrupt_selected_proposal_does_not_fall_back(mocker):
    exact = mocker.patch.object(
        query, "load_investment_proposal", side_effect=ProposalConflictError("broken latest")
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            "CREATE TABLE investment_proposal (proposal_id TEXT,as_of TEXT,published_at TEXT)"
        )
        connection.executescript(PROPOSAL_RECORDS_SCHEMA_SQL)
        assert query.load_latest_investment_proposal(connection) is None
        connection.executemany(
            "INSERT INTO investment_proposal VALUES (?,?,?)",
            [
                ("old-buy", "2026-10-13", "2026-10-13T10:00:00+09:00"),
                ("new-hold", "2026-10-14", "2026-10-14T10:00:00+09:00"),
            ],
        )
        with pytest.raises(ProposalConflictError, match="broken latest"):
            query.load_latest_investment_proposal(connection)
        exact.assert_called_once_with(connection, "new-hold")
