"""A generation may read only the unique originals in its finite latest-price input."""

from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from tests.helpers.external_assessment_html import monex_eps_section, monex_html
from tests.helpers.external_assessment_responses import response

from baibai_engine.data_collection.receipts.public_pages.receipts import insert_response
from baibai_engine.machine_analysis.observations.external_assessments import monex_generation
from baibai_engine.machine_analysis.observations.external_assessments.normalization import monex
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

NOW = datetime(2026, 5, 7, 15, 0, 0, 384, tzinfo=UTC)


def receipt(ticker, *, at=NOW, extra=None):
    body = monex_html(ticker) + (monex_eps_section() if extra is None else extra)
    return response(
        "monex", ticker, body.encode(), url=monex.public_url(ticker), received_at_utc=at
    )


def test_seal_reads_only_unique_selected_bodies_and_reuses_one_identity_checked_dom(
    tmp_path, capsys
):
    older = NOW - timedelta(days=1)
    current = receipt("7203")
    prior = receipt("6758", at=older)
    duplicate = receipt("1234")
    corrupted = receipt("4567")
    others = [
        duplicate,
        receipt("1234"),
        corrupted,
        receipt("4568"),
        receipt("7203", at=NOW - timedelta(hours=2)),
        receipt("6758", at=older - timedelta(days=30)),
        replace(receipt("7203"), source="kabumap"),
    ]
    latest = {
        x.requested_ticker: decode_attempt((x,)) for x in [current, prior, duplicate, corrupted]
    }
    with closing(open_connection(tmp_path / "snapshot.sqlite")) as conn, conn:
        for item in [current, prior, *others]:
            insert_response(conn, item)
        conn.execute(
            "UPDATE equity_web_source_responses SET body_gzip=x'00' WHERE receipt_id=?",
            (corrupted.receipt_id,),
        )
        sql = []
        conn.set_trace_callback(sql.append)
        with (
            patch.object(
                monex_generation, "read_responses", wraps=monex_generation.read_responses
            ) as read,
            patch.object(monex, "document", wraps=monex.document) as documents,
        ):
            result = monex_generation.read_generation_monex_eps(
                conn, latest=latest, cutoff=NOW, as_of=NOW.date() + timedelta(days=1)
            )
        assert {args.args[1][0] for args in read.call_args_list} == {
            current.receipt_id,
            prior.receipt_id,
            corrupted.receipt_id,
        }
        assert documents.call_count == 2
        assert [item.count("received_on=") for item in sql if "INDEXED BY" in item] == [1, 1]
        plans = conn.execute(
            "EXPLAIN QUERY PLAN " + monex_generation._DAY_KEYS, (NOW.date().isoformat(),)
        ).fetchall()
        assert any("USING INDEX idx_equity_web_source_responses_received_on" in p[3] for p in plans)
    assert capsys.readouterr().err.strip() == (
        "model inputs: phase=monex_fiscal_eps eligible=4 indexed_days=2 unique_body_reads=3 "
        f"decoded_bodies=2 decoded_bytes={len(current.body) + len(prior.body)} "
        "ambiguous=1 failed=1 accepted=2"
    )
    assert {t: r.receipt_id for t, r in result.items()} == {
        "7203": current.receipt_id,
        "6758": prior.receipt_id,
    }
    assert result["7203"].reference_eps.current_fy.eps_jpy_per_share == -3.5
    assert result["6758"].reference_eps.next_fy.eps_jpy_per_share == 0


@pytest.mark.parametrize(
    "difference", ["price", "origin", "parser", "url", "clock", "day", "only_rolling"]
)
def test_foreign_old_absent_or_out_of_cutoff_is_not_bound_to_a_model(tmp_path, difference):
    body = monex_eps_section()
    if difference == "only_rolling":
        body = body.replace("今期予想コンセンサス", "今期会社予想").replace(
            "来期予想コンセンサス", "何か別の項目"
        )
    native = receipt("7203", extra=body)
    price = decode_attempt((native,))
    if difference == "price":
        price = price.model_copy(update={"pbr": 123456.0})
    if difference == "origin":
        price = price.model_copy(update={"per_consensus_origin": "other"})
    if difference == "parser":
        price = price.model_copy(update={"parser_version": "old-or-new"})
    if difference == "url":
        price = price.model_copy(update={"source_url": monex.public_url("6758")})
    with closing(open_connection(tmp_path / "snapshot.sqlite")) as conn, conn:
        insert_response(conn, native)
        result = monex_generation.read_generation_monex_eps(
            conn,
            latest={"7203": price},
            cutoff=NOW - timedelta(microseconds=1) if difference == "clock" else NOW,
            as_of=NOW.date() if difference == "day" else price.snapshot_date,
        )
    assert result == {}
