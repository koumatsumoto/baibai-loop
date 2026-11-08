"""前回公開モデルは有限の任意seedであり、L1の代替ではない。"""

import gzip
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.model_generation import materialize_generation

from baibai_engine.machine_analysis.generation import query
from baibai_engine.machine_analysis.generation.comparison_evidence import SavedComparisonEvidence
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    InputSnapshot,
    ModelGeneration,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore

DAY = date(2026, 11, 1)
AT = datetime(2026, 11, 1, tzinfo=UTC)
MARKET = InputSnapshot(kind="lake_release", reference="serving", sha256="a" * 64)


def generation(*, reference="serving", market=MARKET, as_of=DAY, evaluated=AT):
    return ModelGeneration(
        generation_id=f"models-{reference}",
        as_of=as_of,
        evaluation_time=evaluated,
        inputs={"market": market},
        models=tuple(
            GeneratedModel(
                model_id=model,
                subject=ticker,
                calculation_revision="fixture",
                payload={
                    "financial": {
                        "reported_comparisons": [
                            {
                                "observation": {
                                    "ticker": ticker,
                                    "document": f"{reference}-{ticker}",
                                }
                            },
                            {"observation": {"ticker": "wrong-subject"}},
                            None,
                        ]
                    }
                },
            )
            for ticker in ("1234", "5678", "9999")
            for model in ("company_financials", "company_growth")
        ),
    )


def observations(path, *, market=MARKET, tickers=("1234",)):
    return SavedComparisonEvidence(market, path).observations(
        tickers=tickers, as_of=DAY, known_at=AT
    )


def test_only_pinned_serving_generation_is_read_not_newer_or_failed_attempt(tmp_path):
    path = tmp_path / "runs"
    store = ModelGenerationStore(path)
    store.publish(generation())
    store.publish(generation(reference="newer"))
    failed = generation(reference="failed")

    def partial():
        yield failed.models[0]
        raise ValueError("failed publication")

    with pytest.raises(ValueError, match="failed publication"):
        store.publish_stream(
            ModelGenerationStream(
                GenerationMetadata.model_validate(failed.model_dump(exclude={"models"})),
                ("1234",),
                partial(),
            )
        )
    assert list(observations(path)) == [{"ticker": "1234", "document": "serving-1234"}]
    assert list(observations(path, market=MARKET.model_copy(update={"reference": "absent"}))) == []
    assert list(observations(path, market=MARKET.model_copy(update={"reference": "failed"}))) == []


@pytest.mark.parametrize("change", ["hash", "reference", "as_of", "evaluation_time", "kind"])
def test_identity_and_future_clocks_reject_before_payload_decode(tmp_path, monkeypatch, change):
    path = tmp_path / "runs"
    value = generation()
    if change in {"hash", "reference", "kind"}:
        field, replacement = {
            "hash": ("sha256", "b" * 64),
            "reference": ("reference", "other"),
            "kind": ("kind", "sqlite_snapshot"),
        }[change]
        value = value.model_copy(
            update={"inputs": {"market": MARKET.model_copy(update={field: replacement})}}
        )
    else:
        value = value.model_copy(
            update={change: (DAY if change == "as_of" else AT) + timedelta(days=1)}
        )
    ModelGenerationStore(path).publish(value)

    def forbidden(*args):
        raise AssertionError("rejected metadata must not expand model payloads")

    monkeypatch.setattr(query, "_decode_model", forbidden)
    assert list(observations(path)) == []


@pytest.mark.parametrize(
    "damage", ["missing", "obsolete", "not_sqlite", "gzip", "json", "payload", "shape"]
)
def test_missing_legacy_or_damaged_optional_store_is_a_cold_miss(tmp_path, damage):
    path = tmp_path / "runs"
    if damage == "obsolete":
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE legacy(value TEXT)")
    elif damage == "not_sqlite":
        path.write_bytes(b"not a database")
    elif damage != "missing":
        ModelGenerationStore(path).publish(generation())
        body = {
            "gzip": b"broken gzip",
            "json": gzip.compress(b"{"),
            "payload": gzip.compress(b"[]"),
            "shape": gzip.compress(b'{"financial":{"reported_comparisons":"obsolete"}}'),
        }[damage]
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE generated_model SET payload_gzip=?", (body,))
    before = path.read_bytes() if path.exists() else None
    assert list(observations(path)) == []
    assert (path.read_bytes() if path.exists() else None) == before


def test_only_requested_company_financials_decode_one_company_per_iteration(tmp_path, monkeypatch):
    path = tmp_path / "runs"
    ModelGenerationStore(path).publish(generation())
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE generated_model SET payload_gzip=? WHERE model_id!='company_financials' OR subject='9999'",
            (b"unrelated malformed payload must never decode",),
        )
    decoded = []
    decode = query._decode_model

    def record(model, ticker, *args):
        decoded.append((model, ticker))
        return decode(model, ticker, *args)

    monkeypatch.setattr(query, "_decode_model", record)
    stream = observations(path, tickers=("5678", "1234", "1234"))
    assert decoded == []
    assert next(stream)["ticker"] == "1234"
    assert decoded == [("company_financials", "1234")]
    assert next(stream)["ticker"] == "5678"
    assert decoded == [("company_financials", "1234"), ("company_financials", "5678")]
    assert list(stream) == []


@pytest.mark.parametrize(("other_ticker", "expected_parses"), [("5678", 0), ("1873", 1)])
def test_generation_reuses_saved_comparisons_with_identical_complete_output(
    tmp_path, mocker, other_ticker, expected_parses
):
    from tests.helpers.lake_release import market_store
    from tests.jobs.integration.test_edinet_fixed_consumer_input import ASOF, fixtures

    from baibai_engine.machine_analysis.generation import inputs
    from baibai_engine.machine_analysis.generation.edinet_inputs import (
        read_edinet_input,
        retain_edinet_input,
    )
    from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
        DEFAULT_TTM_RULES,
    )
    from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
    from baibai_engine.machine_analysis.observations.financials import edinet_comparison_reader
    from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
    from baibai_jobs.integration.edinet_list_native import materialize_lists

    store, _, _, lists, archives, _ = fixtures(tmp_path / "native")
    market = market_store(tmp_path / "market.sqlite")
    materialize_lists(lists, market)
    runs = tmp_path / "runs.sqlite"
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "lake") as snapshot:
        fixed = read_edinet_input(
            store, retain_edinet_input(store, lists, archives, selected_at=snapshot.sealed_at)
        )
        arguments = dict(
            generation_id="models-serving",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="serving", manifest_sha256="a" * 64),
            economic_input=None,
            edinet_input=fixed,
            as_of=ASOF,
            evaluation_time=snapshot.sealed_at,
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("436A", f" {other_ticker} ", "436A"),
        )
        store.get_keys.clear()
        cold = materialize_generation(inputs.read_and_generate_models(**arguments))
        cold_archive_gets = [key for key in store.get_keys if key.endswith(".zip")]
        assert cold_archive_gets
        assert cold.result("company_financials", "436A").payload["financial"][
            "reported_comparisons"
        ]
        ModelGenerationStore(runs).publish(cold)
        source = SavedComparisonEvidence(MARKET, runs)
        observations_spy = mocker.spy(SavedComparisonEvidence, "observations")
        extract_spy = mocker.spy(edinet_comparison_reader, "extract_comparisons")
        decode_spy = mocker.spy(query, "_decode_model")
        store.get_keys.clear()
        stream = inputs.read_and_generate_models(**arguments, comparison_evidence=source)
        warm = materialize_generation(stream)
        assert warm == cold
        assert [key for key in store.get_keys if key.endswith(".zip")] == cold_archive_gets
        assert [call.args[:2] for call in decode_spy.call_args_list] == [
            ("company_financials", ticker) for ticker in sorted(("436A", other_ticker))
        ]
        assert observations_spy.call_args.kwargs == {
            "tickers": tuple(sorted(("436A", other_ticker))),
            "as_of": ASOF,
            "known_at": snapshot.sealed_at,
        }
        assert extract_spy.call_count == expected_parses
        assert all(call.kwargs["ticker"] != "436A" for call in extract_spy.call_args_list)
