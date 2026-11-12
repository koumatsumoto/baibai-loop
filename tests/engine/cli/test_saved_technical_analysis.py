"""固定モデルを問いに必要な小セクションだけで読むCLI。"""

import json
import sqlite3

import pytest
from tests.helpers.model_generation import generation, publish_generation

from baibai_engine.cli.app import main
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    ModelGeneration,
)

_MODEL_IDS = ("technical_analysis", "market_context")


def _seed(path, *, schema_version=2):
    original = generation()
    ta = original.result("technical_analysis", "1234").model_copy(
        update={
            "payload": {
                "as_of": "2026-10-23",
                "history_end_date": "2026-10-22",
                "price_basis": "price_adjusted_close",
                "read_semantics": "retained_not_original_vintage",
                "price_path": {
                    "expected_price_date": "2026-10-23",
                    "freshness": "stale",
                    "current_price": None,
                    "observations": {"R20": {"value": None, "unavailable_reason": "stale"}},
                    "annotation": {"status": "unavailable"},
                },
                "rsi_14bar": {"value": 0, "status": "available", "history_end_date": "2026-10-22"},
                "price_momentum": None,
                "not_requested": {"large": "payload"},
            }
        }
    )
    market = GeneratedModel(
        model_id="market_context",
        subject="1234",
        calculation_revision="market-v1",
        payload={
            "master": {"name": "合成企業", "snapshot_date": "2026-10-23"},
            "price": {
                "resolved_date": "2026-10-22",
                "price_basis": "price_adjusted_close",
                "avg_turnover_20d_oku": 0,
                "turnover_window": {"valid_turnover_count": 20, "median_yen": 0},
            },
            "relative": {"sector": None},
        },
    )
    plan = {
        "1234": GenerationSubject(scope="stock", model_ids=("company_financials", *_MODEL_IDS)),
        "5678": GenerationSubject(scope="stock", model_ids=_MODEL_IDS),
        "market": GenerationSubject(scope="market", model_ids=("market_context",)),
    }
    fixed = ModelGeneration(
        **original.model_dump(exclude={"models", "schema_version"}),
        schema_version=schema_version,
        subject_plan=plan if schema_version == 2 else None,
        models=(ta, market, *[m for m in original.models if m.model_id != "technical_analysis"]),
    )
    publish_generation(fixed, path=path)
    publish_generation(fixed.model_copy(update={"generation_id": "other-generation"}), path=path)
    # Reading the full generation, the shared market, or unrelated financials fails.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE generated_model SET payload_gzip=? "
            "WHERE generation_id!=? OR subject='market' OR model_id='company_financials'",
            (b"not-gzip", fixed.generation_id),
        )
    return fixed


def _args(path, *extra, generation_id="synthetic-generation"):
    return [
        "technical-analysis",
        "saved",
        "--generation-id",
        generation_id,
        "--runs-path",
        str(path),
        *extra,
    ]


def test_all_stock_sections_preserve_values_dates_missingness_and_fixed_source(tmp_path, capsys):
    path = tmp_path / "runs.sqlite"
    fixed = _seed(path)
    before = path.read_bytes()
    assert (
        main(
            _args(
                path,
                "--all-stocks",
                "--section",
                "technical_analysis.rsi_14bar",
                "--section",
                "technical_analysis.price_momentum",
                "--section",
                "market_context.price",
                "--section",
                "technical_analysis.older_missing_section",
            )
        )
        == 0
    )
    first, missing = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert first["subject"] == "1234"
    assert first["generation_id"] == fixed.generation_id
    assert first["as_of"] == "2026-10-23"
    assert first["evaluation_time"] == fixed.evaluation_time.isoformat()
    assert first["planned_model_ids"] == list(fixed.subject_plan["1234"].model_ids)
    for model, section in (
        ("technical_analysis", "rsi_14bar"),
        ("technical_analysis", "price_momentum"),
        ("market_context", "price"),
    ):
        assert (
            first["sections"][f"{model}.{section}"] == fixed.result(model, "1234").payload[section]
        )
    assert first["sections"]["technical_analysis.rsi_14bar"]["value"] == 0
    assert first["sections"]["technical_analysis.price_momentum"] is None
    assert first["price_context"]["technical_analysis"]["price_path"]["freshness"] == "stale"
    assert first["price_context"]["technical_analysis"]["history_end_date"] == "2026-10-22"
    assert "observations" not in first["price_context"]["technical_analysis"]["price_path"]
    assert first["calculation_revisions"] == {
        "market_context": "market-v1",
        "technical_analysis": "v1",
    }
    assert first["not_recorded_sections"] == ["technical_analysis.older_missing_section"]
    assert first["missing_models"] == []
    assert missing["subject"] == "5678"
    assert missing["planned_model_ids"] == list(_MODEL_IDS)
    assert missing["missing_models"] == list(_MODEL_IDS)
    assert missing["sections"] == {}
    assert path.read_bytes() == before


@pytest.mark.parametrize("schema_version", [1, 2])
def test_explicit_tickers_are_finite_normalized_and_can_read_full_original_sections(
    tmp_path, capsys, schema_version
):
    path = tmp_path / "runs.sqlite"
    fixed = _seed(path, schema_version=schema_version)
    assert (
        main(
            _args(
                path,
                "--ticker",
                "9999",
                "--ticker",
                "0000",
                "--ticker",
                " 1234 ",
                "--ticker",
                "1234",
                "--section",
                "technical_analysis.price_path",
            )
        )
        == 0
    )
    before, first, absent = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert before["subject"] == "0000"
    assert before["sections"] == {}
    assert before["missing_models"] == list(_MODEL_IDS)
    assert first["subject"] == "1234"
    assert (
        first["sections"]["technical_analysis.price_path"]
        == fixed.result("technical_analysis", "1234").payload["price_path"]
    )
    assert absent["subject"] == "9999"
    assert absent["planned_model_ids"] is None
    assert absent["missing_models"] == list(_MODEL_IDS)
    assert absent["not_recorded_sections"] == ["technical_analysis.price_path"]
    if schema_version == 1:
        assert first["planned_model_ids"] is None
        assert main(_args(path, "--all-stocks", "--section", "market_context.price")) == 1
        assert "saved subject plan" in capsys.readouterr().err


def test_read_failure_is_not_an_empty_success_and_never_creates_missing_store(tmp_path, capsys):
    path = tmp_path / "missing.sqlite"
    assert main(_args(path, "--all-stocks", "--section", "market_context.price")) == 1
    assert not path.exists()
    _seed(path)
    assert (
        main(
            _args(
                path, "--all-stocks", "--section", "market_context.price", generation_id="missing"
            )
        )
        == 1
    )
    assert capsys.readouterr().out == ""
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO generated_model VALUES (?,?,?,?,?)",
            ("synthetic-generation", "technical_analysis", "5678", "v1", b"not-gzip"),
        )
    assert main(_args(path, "--all-stocks", "--section", "market_context.price")) == 1
    output = capsys.readouterr()
    assert "technical analysis read failed" in output.err
    assert [json.loads(line)["subject"] for line in output.out.splitlines()] == ["1234"]
    assert main(_args(path, "--ticker", "1234", "--section", "market_context.price")) == 0


@pytest.mark.parametrize(
    "flags",
    [
        ("--all-stocks", "--section", section)
        for section in (
            "company_growth.periods",
            "price",
            "technical_analysis.",
            "market_context.price.close",
        )
    ]
    + [
        ("--section", "market_context.price"),
        ("--all-stocks",),
        ("--all-stocks", "--ticker", "1234", "--section", "market_context.price"),
        ("--all-stocks", "--section", "market_context.price", "--asof", "2026-10-23"),
    ],
)
def test_saved_command_rejects_scope_and_section_misuse(tmp_path, flags):
    with pytest.raises(SystemExit) as result:
        main(_args(tmp_path / "unused.sqlite", *flags))
    assert result.value.code == 2


@pytest.mark.parametrize("price_payload", [{}, {"price": None}, {"price": {}}])
def test_older_unrecorded_and_null_price_context_are_not_filled(tmp_path, capsys, price_payload):
    path = tmp_path / "runs.sqlite"
    original = generation()
    fixed = original.model_copy(
        update={
            "models": (
                *original.models,
                GeneratedModel(
                    model_id="market_context",
                    subject="1234",
                    calculation_revision="older",
                    payload=price_payload,
                ),
            )
        }
    )
    publish_generation(fixed, path=path)
    assert main(_args(path, "--ticker", "1234", "--section", "market_context.price")) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["price_context"]["technical_analysis"] == {}
    assert row["price_context"]["market_context"] == price_payload
    if "price" in price_payload:
        assert row["sections"]["market_context.price"] == price_payload["price"]
        assert row["not_recorded_sections"] == []
    else:
        assert row["sections"] == {}
        assert row["not_recorded_sections"] == ["market_context.price"]
