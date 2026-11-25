"""A generation may read only the unique originals in its finite latest-price input."""

from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from tests.helpers.external_assessment_html import monex_eps_section, monex_html
from tests.helpers.external_assessment_responses import response
from tests.helpers.model_generation import generation, publish_generation

from baibai_engine.data_collection.receipts.public_pages.receipts import insert_response
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.models.external_assessment.analysis import analyze
from baibai_engine.machine_analysis.models.external_assessment.models import (
    MONEX_DAY_PAIR_METHOD_VERSION,
    SINGLE_CUTOFF_METHOD_VERSION,
    ExternalAssessment,
)
from baibai_engine.machine_analysis.observations.external_assessments import monex_generation
from baibai_engine.machine_analysis.observations.external_assessments.history import (
    MonexGenerationWindow,
    latest_monex,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization import monex
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observation_population,
    read_observations,
)
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
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


@pytest.mark.parametrize("fault", ["no_original", "no_table", "ambiguous", "bad_gzip"])
def test_eligible_price_survives_missing_eps_without_borrowing_either_neighbors_original(
    tmp_path, fault
):
    db = tmp_path / "market.sqlite"
    a = NOW - timedelta(minutes=5)  # before JST midnight
    past = receipt("7203", at=a - timedelta(days=1))
    target = receipt("7203", at=a, extra="" if fault == "no_table" else None)
    future = receipt("7203", at=NOW)
    save_observations(db, [decode_attempt((x,)) for x in (past, target, future)])
    with closing(open_connection(db)) as conn, conn:
        for x in (past, future):
            insert_response(conn, x)
        if fault != "no_original":
            insert_response(conn, target)
        if fault == "ambiguous":
            insert_response(conn, receipt("7203", at=a))
        if fault == "bad_gzip":
            conn.execute(
                "UPDATE equity_web_source_responses SET body_gzip=x'00' WHERE receipt_id=?",
                (target.receipt_id,),
            )
    day = decode_attempt((target,)).snapshot_date
    window = MonexGenerationWindow(as_of=day, received_through=NOW)
    history = read_observation_population(db, tickers=("7203",), cutoff=NOW, monex_window=window)[
        "7203"
    ]
    anchor = latest_monex(history.model_valuations)
    assert anchor == decode_attempt((target,))
    with (
        closing(open_connection(db)) as conn,
        patch.object(
            monex_generation, "read_responses", wraps=monex_generation.read_responses
        ) as read,
    ):
        eps = monex_generation.read_generation_monex_eps(
            conn, latest={"7203": anchor}, cutoff=NOW, as_of=day
        )
    assert [call.args[1] for call in read.call_args_list] == (
        [(target.receipt_id,)] if fault in {"bad_gzip", "no_table"} else []
    )
    assert eps == {}
    model = analyze(history, evaluated_at=NOW)
    assert model.method_version == MONEX_DAY_PAIR_METHOD_VERSION
    assert "monex_fiscal_eps" not in model.model_dump()
    assert model.model_valuations[0].inputs.previous == decode_attempt((past,))


def test_historical_and_current_methods_remain_distinct_immutable_ledger_payloads(tmp_path):
    db, ledger = tmp_path / "market.sqlite", tmp_path / "runs.sqlite"
    r = receipt("7203")
    price = decode_attempt((r,))
    save_observations(db, [price])
    with closing(open_connection(db)) as conn, conn:
        insert_response(conn, r)
        eps = monex_generation.read_generation_monex_eps(
            conn, latest={"7203": price}, cutoff=NOW, as_of=price.snapshot_date
        )["7203"]
    legacy = read_observations(db, "7203", NOW)
    daily = read_observation_population(
        db,
        tickers=("7203",),
        cutoff=NOW,
        monex_window=MonexGenerationWindow(as_of=price.snapshot_date, received_through=NOW),
    )["7203"]
    simple = analyze(legacy, evaluated_at=NOW)
    current = analyze(daily, evaluated_at=NOW, monex_fiscal_eps=eps)
    archived = {
        "inputs": {"latest": None, "previous": None},
        "availability": "not_observed",
        "forecast_fields": [],
        "comparison_basis": "metric_period_mapping_unconfirmed",
    }
    models = [
        (
            version,
            ExternalAssessment.model_validate(
                {
                    "tradingview": archived,
                    **result.model_dump(mode="json"),
                    "method_version": version,
                }
            ),
        )
        for version, result in (
            ("same-series-v8-tradingview-receipts", simple),
            ("same-series-v9-monex-fiscal-eps", current),
            ("same-series-v10-monex-asof-pair", current),
        )
    ] + [
        (SINGLE_CUTOFF_METHOD_VERSION, simple),
        (MONEX_DAY_PAIR_METHOD_VERSION, current),
    ]
    for version, result in models:
        assert result.method_version == version
        model = GeneratedModel(
            model_id="external_assessment",
            subject="7203",
            calculation_revision=version,
            payload=result.model_dump(mode="json"),
        )
        payload = generation().model_copy(
            update={
                "generation_id": version,
                "models": (model,),
                "as_of": price.snapshot_date,
                "evaluation_time": NOW,
            }
        )
        assert publish_generation(payload, path=ledger)
    db.rename(tmp_path / "unused.sqlite")
    for version, result in models:
        stored = read_generation(version, path=ledger).result("external_assessment", "7203")
        assert stored.calculation_revision == stored.payload["method_version"] == version
        assert stored.payload == result.model_dump(mode="json")
        assert ExternalAssessment.model_validate(stored.payload) == result
    assert "monex_fiscal_eps" not in models[0][1].model_dump(mode="json")
