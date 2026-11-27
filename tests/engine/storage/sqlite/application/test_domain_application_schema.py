"""Current application schema and immutable publication constraints."""

import sqlite3
from contextlib import closing

import pytest

from baibai_engine.storage.sqlite.application import write as application_write
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.schema import (
    APPLICATION_SCHEMA_VERSION,
    SCHEMA_SQL,
    US_EQUITY_PROPOSAL_SCHEMA_STATEMENTS,
)
from baibai_engine.storage.sqlite.application.write import initialize_database

EXPECTED_TABLES = (
    "selection_identity_migration",
    "proposal_correction",
    "proposal_human_report",
    "company_research",
    "judgment_retrospective",
    "market_context_assessment",
    "market_context_assessment_head",
    "ledger_event",
    "ledger_meta",
    "portfolio_outcome",
    "stock_assessment",
    "stock_assessment_review",
    "investment_proposal",
    "investment_proposal_item",
    "position_decision_reference",
    "operation_session",
    "us_equity_proposal",
)


@pytest.fixture
def connection():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA_SQL)
        yield connection


def assessment(connection, identifier="stock-assessment-synthetic"):
    connection.execute(
        "INSERT INTO stock_assessment VALUES (?,?,?,?,?,?,?,?)",
        (
            identifier,
            "1234",
            "2026-10-14",
            "supported",
            "2026-10-14T16:00:00+09:00",
            None,
            "{}",
            "0" * 64,
        ),
    )
    return identifier


def proposal(connection):
    identifier = "investment-proposal-synthetic"
    connection.execute(
        "INSERT INTO investment_proposal VALUES (?,?,?,?,?,?,?)",
        (
            identifier,
            "2026-10-14",
            "2026-10-14T16:00:00+09:00",
            "2026-10-14T16:10:00+09:00",
            "specified_stocks",
            "{}",
            "0" * 64,
        ),
    )
    return identifier


def test_schema_has_only_current_research_and_fact_tables(tmp_path):
    path = tmp_path / "app.sqlite"
    assert initialize_database(path) == APPLICATION_SCHEMA_VERSION
    with closing(sqlite3.connect(path)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert tables == set(EXPECTED_TABLES)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == APPLICATION_SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_isolated_v30_cutover_adds_only_us_table_and_leaves_existing_jp_rows(tmp_path):
    path = tmp_path / "isolated.sqlite"

    def snapshot(connection):
        return (
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE tbl_name <> 'us_equity_proposal' ORDER BY name"
            ).fetchall(),
            {
                name: connection.execute(f'SELECT * FROM "{name}"').fetchall()
                for name in EXPECTED_TABLES
                if name != "us_equity_proposal"
            },
        )

    with closing(sqlite3.connect(path, autocommit=True)) as original:
        original.execute("PRAGMA foreign_keys=ON")
        original.executescript(
            SCHEMA_SQL.removesuffix("".join(US_EQUITY_PROPOSAL_SCHEMA_STATEMENTS))
        )
        original.execute("PRAGMA user_version=30")
        case_id, proposal_id = assessment(original), proposal(original)
        original.execute(
            "INSERT INTO investment_proposal_item VALUES (?,?,?,?,?)",
            (proposal_id, 0, "1234", "defer", case_id),
        )
        original.execute(
            "INSERT INTO operation_session VALUES (?,?,?,?,?,?,?,?)",
            (
                "synthetic-operation",
                "position-review",
                "active",
                "2026-10-14",
                "1234",
                "2026-10-14T09:00:00+09:00",
                None,
                "{}",
            ),
        )
        original.execute(
            "INSERT INTO ledger_event VALUES (?,?,?,?,?,?,?,?)",
            (
                1,
                "synthetic-jpy",
                "2026-10-14T09:00:00+09:00",
                0,
                "opening_balance",
                None,
                None,
                '{"currency":"JPY","synthetic":true}',
            ),
        )
        before = snapshot(original)
        before_bytes = path.read_bytes()
        for opener in (connect_read_only, application_write.connect_existing, initialize_database):
            with pytest.raises(RuntimeError, match="user_version=30"):
                opener(path)
        assert path.read_bytes() == before_bytes

        original.execute("BEGIN IMMEDIATE")
        for statement in US_EQUITY_PROPOSAL_SCHEMA_STATEMENTS:
            original.execute(statement)
        original.execute(f"PRAGMA user_version={APPLICATION_SCHEMA_VERSION}")
        original.execute("ROLLBACK")
        assert original.execute("PRAGMA user_version").fetchone() == (30,)
        assert snapshot(original) == before
        assert (
            original.execute(
                "SELECT 1 FROM sqlite_schema WHERE tbl_name='us_equity_proposal'"
            ).fetchone()
            is None
        )

        original.execute("BEGIN IMMEDIATE")
        for statement in US_EQUITY_PROPOSAL_SCHEMA_STATEMENTS:
            original.execute(statement)
        original.execute(f"PRAGMA user_version={APPLICATION_SCHEMA_VERSION}")
        original.execute("COMMIT")
        assert snapshot(original) == before
        assert original.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert original.execute("PRAGMA foreign_key_check").fetchall() == []

    with closing(connect_read_only(path)) as upgraded:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 31
        assert upgraded.execute("SELECT * FROM us_equity_proposal").fetchall() == []
        assert [
            tuple(row)
            for row in upgraded.execute(
                "SELECT type,name FROM sqlite_schema WHERE tbl_name='us_equity_proposal' "
                "AND sql IS NOT NULL ORDER BY type"
            )
        ] == [("table", "us_equity_proposal"), ("trigger", "us_equity_proposal_immutable")]


def test_ledger_keeps_fact_reference_separate_from_proposal_identity(connection):
    ledger_columns = {row[1] for row in connection.execute("PRAGMA table_info(ledger_event)")}
    assert "decision_reference" in ledger_columns
    assert "proposal_id" not in ledger_columns


def test_company_research_is_immutable_and_prior_is_an_existing_other_record(connection):
    values = ("research-one", "1234", "2026-10-29", "2026-10-29T16:00:00+09:00")
    connection.execute(
        "INSERT INTO company_research VALUES (?,?,?,?,?,?,?)", (*values, None, "{}", "0" * 64)
    )
    assert [
        row[2] for row in connection.execute("PRAGMA index_info(company_research_latest_idx)")
    ] == ["ticker", "as_of", "recorded_at", "research_id"]
    for column, value in (("payload", '{"changed":true}'), ("ticker", "5678")):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f'UPDATE company_research SET "{column}"=?', (value,))
    for prior, error in (("missing", "FOREIGN KEY"), ("research-two", "CHECK")):
        with pytest.raises(sqlite3.IntegrityError, match=error):
            connection.execute(
                "INSERT INTO company_research VALUES (?,?,?,?,?,?,?)",
                ("research-two", *values[1:], prior, "{}", "0" * 64),
            )


def test_judgment_retrospective_has_only_record_headers_and_is_immutable(connection):
    assert [row[1] for row in connection.execute("PRAGMA table_info(judgment_retrospective)")] == [
        "retrospective_id",
        "ticker",
        "as_of",
        "recorded_at",
        "payload",
        "payload_sha256",
    ]
    assert [
        row[2]
        for row in connection.execute(
            "PRAGMA index_info(judgment_retrospective_ticker_recorded_idx)"
        )
    ] == ["ticker", "recorded_at", "retrospective_id"]
    connection.execute(
        "INSERT INTO judgment_retrospective VALUES (?,?,?,?,?,?)",
        ("retrospective-one", "1234", "2026-10-29", "2026-10-29T08:00:00+00:00", "{}", "0" * 64),
    )
    for column, value in (("payload", '{"changed":true}'), ("ticker", "5678")):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f'UPDATE judgment_retrospective SET "{column}"=?', (value,))


def test_exactly_one_review_and_reference_fk_protects_assessment(connection):
    identifier = assessment(connection)
    connection.execute(
        "INSERT INTO stock_assessment_review VALUES (?,?,?,?)",
        ("review-one", identifier, "2026-10-14T16:01:00+09:00", "{}"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        connection.execute(
            "INSERT INTO stock_assessment_review VALUES (?,?,?,?)",
            ("review-two", identifier, "2026-10-14T16:02:00+09:00", "{}"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        connection.execute("DELETE FROM stock_assessment WHERE assessment_id=?", (identifier,))


@pytest.mark.parametrize(
    "table", ["stock_assessment", "stock_assessment_review", "investment_proposal"]
)
def test_published_payload_and_identity_headers_are_immutable(connection, table):
    identifier = assessment(connection)
    connection.execute(
        "INSERT INTO stock_assessment_review VALUES (?,?,?,?)",
        ("review-one", identifier, "2026-10-14T16:01:00+09:00", "{}"),
    )
    proposal(connection)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(f'UPDATE "{table}" SET payload=?', ('{"changed":true}',))
    column = (
        "ticker"
        if table == "stock_assessment"
        else "reviewed_at"
        if table == "stock_assessment_review"
        else "request_scope"
    )
    value = (
        "5678"
        if column == "ticker"
        else "2026-10-14T16:02:00+09:00"
        if column == "reviewed_at"
        else "discover_new"
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(f'UPDATE "{table}" SET "{column}"=?', (value,))


def test_proposal_item_is_only_index_and_enforces_exact_reference_and_unique_ticker(connection):
    assessment_id, proposal_id = assessment(connection), proposal(connection)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(investment_proposal_item)")}
    assert columns == {"proposal_id", "item_index", "ticker", "action", "assessment_id"}
    connection.execute(
        "INSERT INTO investment_proposal_item VALUES (?,?,?,?,?)",
        (proposal_id, 0, "1234", "buy", assessment_id),
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        connection.execute(
            "INSERT INTO investment_proposal_item VALUES (?,?,?,?,?)",
            (proposal_id, 1, "1234", "no_action", assessment_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        connection.execute(
            "INSERT INTO investment_proposal_item VALUES (?,?,?,?,?)",
            (proposal_id, 1, "5678", "defer", "missing"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        connection.execute("DELETE FROM stock_assessment WHERE assessment_id=?", (assessment_id,))


@pytest.mark.parametrize(
    ("origin", "side", "has_proposal", "valid"),
    [
        ("proposal", "buy", True, True),
        ("retained_reference", "buy", False, True),
        ("manual_fact", "buy", False, True),
        ("proposal", "buy", False, False),
        ("retained_reference", "buy", True, False),
        ("manual_fact", "sell", False, False),
        ("unknown", "buy", False, False),
    ],
)
def test_decision_binding_has_three_explicit_origins_and_buy_only(
    connection, origin, side, has_proposal, valid
):
    proposal_id = proposal(connection) if has_proposal else None
    sql = "INSERT INTO position_decision_reference VALUES (?,?,?,?,?,?,?)"
    values = ("synthetic-reference", "1234", side, origin, proposal_id, "synthetic-source", None)
    if valid:
        connection.execute(sql, values)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(sql, values)


def test_initialization_failure_rolls_back_schema_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "app.sqlite"
    with monkeypatch.context() as patch:
        patch.setattr(application_write, "SCHEMA_SQL", SCHEMA_SQL + "\nINVALID SQL;")
        with pytest.raises(sqlite3.OperationalError):
            initialize_database(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT name FROM sqlite_schema").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert initialize_database(path) == APPLICATION_SCHEMA_VERSION
