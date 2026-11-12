import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.helpers.jpx_earnings import FILE_A, FIXTURES, SOURCE_FILES, _workbook

from baibai_engine.data_collection.providers.jpx.models import JPXResponse
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    read_complete_bundle,
    retain_bundle_response,
)
from baibai_engine.machine_analysis.observations.disclosures.capital_policy import (
    TSE_CAPITAL_POLICY_URL,
    TSECapitalPolicyRow,
    store_tse_capital_policy_rows,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import rebuild_jpx_snapshot
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import (
    JPX_EARNINGS_CALENDAR_INDEX_URL,
    JPX_REGULATION_URLS,
)
from baibai_engine.machine_analysis.observations.financials import capital_action_pdf
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration import jpx_handoff
from baibai_jobs.integration.jpx_handoff import export_handoff, import_handoff

DAY = date(2026, 10, 15)
STAMP = datetime(2026, 10, 15, 7, tzinfo=UTC)


def earnings(path, stamp, announcement, ticker, *, invalid=False):
    resources = (("index", JPX_EARNINGS_CALENDAR_INDEX_URL), ("workbook:0", FILE_A))
    with closing(open_connection(path)) as conn:
        bundle = create_bundle(conn, "earnings_calendar", DAY, stamp, resources)
        retain_bundle_response(
            conn,
            bundle,
            "index",
            JPXResponse(resources[0][1], stamp, "text/html", b"<a href='kessan.xlsx'>daily</a>"),
        )
        retain_bundle_response(
            conn,
            bundle,
            "workbook:0",
            JPXResponse(
                FILE_A,
                stamp,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"invalid" if invalid else _workbook("", [[announcement, ticker]]),
            ),
        )
        complete_bundle(conn, bundle)
    return bundle


def regulations(path, day, stamp):
    with closing(open_connection(path)) as conn:
        bundle = create_bundle(conn, "regulations", day, stamp, tuple(JPX_REGULATION_URLS.items()))
        for name, url in JPX_REGULATION_URLS.items():
            retain_bundle_response(
                conn,
                bundle,
                name,
                JPXResponse(url, stamp, "text/html", (FIXTURES / SOURCE_FILES[name]).read_bytes()),
            )
        complete_bundle(conn, bundle)
    return bundle


def test_late_global_earnings_bundle_preserves_latest_complete_workbook(tmp_path):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    old = earnings(source, STAMP, "2026-10-20", "130A")
    newer = earnings(target, STAMP + timedelta(hours=1), "2026-10-22", "7203")
    rebuild_jpx_snapshot(target, newer)
    with closing(open_connection(target)) as conn, conn:
        conn.execute(
            "INSERT INTO jpx_regulation_sources VALUES (?, ?, ?)",
            (DAY.isoformat(), "other-source", STAMP.isoformat()),
        )
        expected = conn.execute("SELECT * FROM jpx_earnings_calendar").fetchall()
    selected = export_handoff(source, stage, since=STAMP, collection="earnings_calendar")
    assert selected == (old,)
    result = import_handoff(stage, target, selected, collection="earnings_calendar")
    assert result.complete is True
    assert result.report["replayed_bundles"] == [newer]
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT * FROM jpx_earnings_calendar").fetchall() == expected
        assert conn.execute("SELECT COUNT(*) FROM jpx_source_bundles").fetchone()[0] == 2
        assert (
            conn.execute("SELECT source_name FROM jpx_regulation_sources").fetchone()[0]
            == "other-source"
        )
    with closing(sqlite3.connect(stage)) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert tables == {"jpx_source_bundles", "jpx_source_responses"}
    assert (
        import_handoff(stage, target, selected, collection="earnings_calendar").report[
            "raw_rows_added"
        ]
        == 0
    )


def test_regulations_replay_only_the_selected_asof_day(tmp_path):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    regulations(source, DAY, STAMP)
    next_day = DAY + timedelta(days=1)
    latest = regulations(target, next_day, STAMP + timedelta(days=1))
    rebuild_jpx_snapshot(target, latest)
    with closing(open_connection(target)) as conn:
        before = conn.execute("SELECT * FROM jpx_regulation_flags ORDER BY 1, 2, 3").fetchall()
    selected = export_handoff(source, stage, since=STAMP, collection="regulations")
    import_handoff(stage, target, selected, collection="regulations")
    with closing(open_connection(target)) as conn:
        assert (
            conn.execute(
                "SELECT * FROM jpx_regulation_flags WHERE asof_date = ? ORDER BY 1, 2, 3",
                (next_day.isoformat(),),
            ).fetchall()
            == before
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM jpx_regulation_flags WHERE asof_date = ?", (DAY.isoformat(),)
            ).fetchone()[0]
            > 0
        )


def test_invalid_complete_bundle_is_retained_but_does_not_claim_projection_success(tmp_path):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    prior = earnings(target, STAMP, "2026-10-20", "130A")
    rebuild_jpx_snapshot(target, prior)
    invalid = earnings(source, STAMP + timedelta(hours=1), "2026-10-22", "7203", invalid=True)
    selected = export_handoff(source, stage, since=STAMP, collection="earnings_calendar")
    result = import_handoff(stage, target, selected, collection="earnings_calendar")
    assert result.complete is False
    assert result.report["projection_failures"] == [invalid]
    assert result.report["replayed_bundles"] == []
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT ticker FROM jpx_earnings_calendar").fetchone()[0] == "130A"
        assert conn.execute("SELECT COUNT(*) FROM jpx_source_bundles").fetchone()[0] == 2


def capital_policy(path, stamp, body):
    with closing(open_connection(path)) as conn:
        bundle = create_bundle(
            conn, "capital_policy", DAY, stamp, (("workbook", TSE_CAPITAL_POLICY_URL),)
        )
        retain_bundle_response(
            conn,
            bundle,
            "workbook",
            JPXResponse(TSE_CAPITAL_POLICY_URL, stamp, "application/vnd.ms-excel", body),
        )
        complete_bundle(conn, bundle)
    return bundle


def test_unreadable_capital_policy_is_reported_without_losing_receipt_or_old_view(tmp_path):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    store_tse_capital_policy_rows(
        target,
        (
            TSECapitalPolicyRow(
                snapshot_month_end=date(2026, 9, 30),
                ticker="1301",
                status="disclosed",
                status_change=None,
                updated_on=None,
                contact_requested=False,
                first_disclosed_month_end=date(2026, 9, 30),
                first_disclosure_left_censored=True,
            ),
        ),
    )
    with closing(open_connection(target)) as conn:
        before = conn.execute("SELECT * FROM tse_capital_policy_snapshots").fetchall()
    body = b"invalid capital-policy workbook"
    invalid = capital_policy(source, STAMP, body)
    selected = export_handoff(source, stage, since=STAMP, collection="capital_policy")
    assert selected == (invalid,)
    for attempt in range(2):
        result = import_handoff(stage, target, selected, collection="capital_policy")
        assert result.complete is False
        assert result.report["projection_failures"] == [invalid]
        assert result.report["replayed_bundles"] == []
        assert result.report["raw_rows_added"] == (2 if attempt == 0 else 0)
        with closing(open_connection(target)) as conn:
            retained = read_complete_bundle(conn, invalid)
            assert retained.responses[0].response.body == body
            assert conn.execute("SELECT * FROM tse_capital_policy_snapshots").fetchall() == before


@pytest.mark.parametrize("failure", [RuntimeError("unexpected"), sqlite3.DatabaseError("broken")])
def test_unexpected_or_database_replay_errors_are_not_hidden_as_source_failure(
    tmp_path, monkeypatch, failure
):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    capital_policy(source, STAMP, b"retained source")
    selected = export_handoff(source, stage, since=STAMP, collection="capital_policy")

    def fail_rebuild(*args):
        raise failure

    monkeypatch.setattr(jpx_handoff, "rebuild_jpx_snapshot", fail_rebuild)
    with pytest.raises(type(failure), match=str(failure)):
        import_handoff(stage, target, selected, collection="capital_policy")


def test_legacy_full_schema_stage_is_still_imported(tmp_path):
    legacy, target = tmp_path / "legacy.sqlite", tmp_path / "target.sqlite"
    bundle = earnings(legacy, STAMP, "2026-10-20", "130A")
    result = import_handoff(legacy, target, (bundle,), collection="earnings_calendar")
    assert result.complete is True
    with closing(open_connection(target)) as conn:
        assert read_complete_bundle(conn, bundle).bundle_id == bundle


def test_receipt_stage_with_wrong_shape_is_rejected(tmp_path):
    stage, target = tmp_path / "bad.sqlite", tmp_path / "target.sqlite"
    with closing(sqlite3.connect(stage)) as conn:
        conn.execute("CREATE TABLE jpx_source_bundles(bundle_id TEXT)")
    with pytest.raises(ValueError, match="JPX receipt schema"):
        import_handoff(stage, target, ("bad",), collection="earnings_calendar")


@pytest.mark.parametrize("case", ["same_day", "subsequent", "planned"])
def test_capital_action_handoff_keeps_receipts_and_defers_model_cutoffs(
    tmp_path, monkeypatch, case
):
    fixture = json.loads(
        (
            Path(__file__).parents[2] / "fixtures/jpx/7640_completed_post_period_conversion.json"
        ).read_text()
    )
    if case == "same_day":
        texts = [
            (
                "2026年10月1日(コード:5838東証プライム市場)"
                "本日、本株式交付の効力が発生いたしました。"
                "本転換の結果、当行普通株式の発行済株式数は223,936,153株となり"
            )
        ]
        url = "https://www2.jpx.co.jp/disc/58380/140120260930543771.pdf"
        ticker, disclosure, published = "5838", "20260930543771", date(2026, 10, 1)
        received = datetime(2026, 9, 30, 15, 10, tzinfo=UTC)
    else:
        texts = fixture["texts"]
        if case == "planned":
            texts = [text.replace("消却いたしました", "消却する予定です") for text in texts]
        url = fixture["source_url"]
        ticker, disclosure, published = "7640", "20260909533860", date(2026, 9, 10)
        received = datetime(2026, 9, 9, 15, 10, tzinfo=UTC)

    class Reader:
        def __init__(self, content):
            self.pages = [SimpleNamespace(extract_text=lambda text=text: text) for text in texts]

    monkeypatch.setattr(capital_action_pdf, "PdfReader", Reader)
    source, stage, target = (tmp_path / name for name in ("source", "stage", "target"))
    resource, body = "pdf:" + disclosure, b"%PDF-synthetic-excerpts"
    with closing(open_connection(source)) as conn:
        # An older bundle-selection date is not an instruction to backdate the disclosure.
        bundle = create_bundle(
            conn, "capital_actions", date(2026, 8, 1), received, ((resource, url),)
        )
        retain_bundle_response(
            conn, bundle, resource, JPXResponse(url, received, "application/pdf", body)
        )
        complete_bundle(conn, bundle)
    selected = export_handoff(source, stage, since=received, collection="capital_actions")
    assert selected == (bundle,)
    result = import_handoff(stage, target, selected, collection="capital_actions")
    assert result.complete is (case != "planned")
    assert result.report["raw_rows_added"] == 2
    assert len(result.report["projection_failures"]) == (1 if case == "planned" else 0)
    assert (
        import_handoff(stage, target, selected, collection="capital_actions").report[
            "raw_rows_added"
        ]
        == 0
    )
    with closing(open_connection(target)) as conn:
        assert read_complete_bundle(conn, bundle).responses[0].response.body == body
        assert not capital_action_pdf.read_capital_actions(
            conn, as_of=published - timedelta(days=1), tickers=[ticker], known_at=received
        )
        assert not capital_action_pdf.read_capital_actions(
            conn,
            as_of=date(2026, 11, 12),
            tickers=[ticker],
            known_at=received - timedelta(microseconds=1),
        )
        actions = capital_action_pdf.read_capital_actions(
            conn, as_of=published, tickers=[ticker], known_at=received
        )
        if case == "planned":
            assert not actions
        else:
            assert actions[ticker][0].published_on == published
            assert actions[ticker][0].received_at == received
            if case == "subsequent":
                assert actions[ticker][0].effective_date == date(2026, 8, 31)
