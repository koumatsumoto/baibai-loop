"""固定共有subjectを発見・明示読取し、企業への関連や採択を補わない。"""

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

GENERATION = "synthetic-generation"
INDUSTRY = "industry:unlinked-survey"
GLOBAL = "market:global"


def seed(path, *, legacy=False):
    original = generation()
    plan = {
        "1234": GenerationSubject(
            scope="stock", model_ids=("technical_analysis", "company_financials")
        ),
        INDUSTRY: GenerationSubject(scope="sector", model_ids=("industry_trends",)),
        "industry:not-generated": GenerationSubject(scope="sector", model_ids=("industry_trends",)),
        "market": GenerationSubject(scope="market", model_ids=("market_context",)),
        GLOBAL: GenerationSubject(scope="market", model_ids=("social_trends",)),
    }
    industry = GeneratedModel(
        model_id="industry_trends",
        subject=INDUSTRY,
        calculation_revision="synthetic-industry",
        payload={
            "observations": [
                {
                    "claim": {
                        "negated": True,
                        "conditions": ["地域限定"],
                        "quantity": {"value": "0", "unit": "percent"},
                        "uncertainty": ["母集団未確定"],
                    },
                    "exact_evidence": {
                        "artifact_id": "fixed-artifact",
                        "artifact_sha256": "b" * 64,
                    },
                }
            ],
            "comparisons": [{"kind": "not_comparable", "delta": None}],
            "coverage": {"status": "partial"},
            "display": {"omitted_observations": 3},
            "detail": {"input_manifest_sha256": "c" * 64},
        },
    )
    social = GeneratedModel(
        model_id="social_trends",
        subject=GLOBAL,
        calculation_revision="synthetic-social",
        payload={"observations": [{"schedule_status": "date_reached_implementation_unconfirmed"}]},
    )
    fixed = ModelGeneration(
        **original.model_dump(exclude={"models", "schema_version"}),
        schema_version=1 if legacy else 2,
        subject_plan=None if legacy else plan,
        models=(*original.models, industry, social),
    )
    publish_generation(fixed, path=path)
    publish_generation(fixed.model_copy(update={"generation_id": "later-generation"}), path=path)
    return fixed


def args(path, *extra, generation_id=GENERATION):
    return [
        "investment-judgment",
        "shared-context",
        "--generation-id",
        generation_id,
        "--runs-path",
        str(path),
        *extra,
    ]


def corrupt(path, condition):
    with sqlite3.connect(path) as db:
        db.execute("UPDATE generated_model SET payload_gzip=? WHERE " + condition, (b"bad-gzip",))


def test_catalog_reads_only_fixed_header_and_pages_unlinked_shared_subjects(tmp_path, capsys):
    path = tmp_path / "runs.sqlite"
    fixed = seed(path)
    corrupt(path, "1=1")
    before = path.read_bytes()
    assert main(args(path, "--limit", "2")) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["generation_id"] == fixed.generation_id
    assert page["as_of"] == fixed.as_of.isoformat()
    assert page["evaluation_time"] == fixed.evaluation_time.isoformat()
    assert page["scope_status"] == "recorded"
    names = [row["subject"] for row in page["subjects"]]
    assert names == ["industry:not-generated", INDUSTRY]
    assert page["subjects"][1]["definition"] == fixed.subject_plan[INDUSTRY].model_dump(mode="json")
    assert page["next_after_subject"] == INDUSTRY
    assert "models" not in page
    assert main(args(path, "--limit", "2", "--after-subject", page["next_after_subject"])) == 0
    last = json.loads(capsys.readouterr().out)
    assert [row["subject"] for row in last["subjects"]] == ["market", GLOBAL]
    assert last["next_after_subject"] is None
    assert path.read_bytes() == before


def test_exact_shared_read_keeps_raw_payload_and_does_not_load_companies_or_other_subjects(
    tmp_path, capsys
):
    path = tmp_path / "runs.sqlite"
    fixed = seed(path)
    corrupt(path, "generation_id!='synthetic-generation' OR subject!='industry:unlinked-survey'")
    before = path.read_bytes()
    assert main(args(path, "--subject", INDUSTRY)) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["subject"] == INDUSTRY
    assert value["models"] == [fixed.result("industry_trends", INDUSTRY).model_dump(mode="json")]
    assert value["not_generated"] == []
    assert value["input_identities"] == {
        name: identity.model_dump(mode="json", exclude={"descriptor"})
        for name, identity in fixed.inputs.items()
    }
    assert "related_tickers" not in value
    assert "candidate_tickers" not in value
    assert path.read_bytes() == before


def test_market_schedule_and_planned_but_not_generated_are_distinct(tmp_path, capsys):
    path = tmp_path / "runs.sqlite"
    fixed = seed(path)
    assert main(args(path, "--subject", GLOBAL)) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["models"] == [fixed.result("social_trends", GLOBAL).model_dump(mode="json")]
    assert main(args(path, "--subject", "industry:not-generated")) == 0
    missing = json.loads(capsys.readouterr().out)
    assert missing["models"] == []
    assert missing["not_generated"] == ["industry_trends"]


@pytest.mark.parametrize("subject", ["1234", "industry:unknown"])
def test_stock_and_unknown_scope_cannot_become_shared_context(tmp_path, capsys, subject):
    path = tmp_path / "runs.sqlite"
    seed(path)
    assert main(args(path, "--subject", subject)) == 3
    assert capsys.readouterr().out == ""


def test_v1_scope_is_unknown_not_todays_catalog_or_inferred_market(tmp_path, capsys):
    path = tmp_path / "runs.sqlite"
    seed(path, legacy=True)
    assert main(args(path)) == 0
    old = json.loads(capsys.readouterr().out)
    assert old["scope_status"] == "not_recorded"
    assert old["subjects"] == []
    assert old["next_after_subject"] is None
    assert main(args(path, "--subject", "market")) == 3
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mode", ["missing_store", "missing_generation", "unreadable_payload"])
def test_failure_is_not_empty_success_and_does_not_create_store(tmp_path, capsys, mode):
    path = tmp_path / "runs.sqlite"
    if mode != "missing_store":
        seed(path)
    if mode == "unreadable_payload":
        corrupt(path, "subject='industry:unlinked-survey'")
    assert (
        main(
            args(
                path,
                "--subject",
                INDUSTRY,
                generation_id="absent" if mode == "missing_generation" else GENERATION,
            )
        )
        == 3
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err
    if mode == "missing_store":
        assert not path.exists()


@pytest.mark.parametrize(
    "extra",
    [("--limit", "0"), ("--limit", "101"), ("--subject", INDUSTRY, "--after-subject", "market")],
)
def test_invalid_pagination_is_usage_error(tmp_path, extra):
    with pytest.raises(SystemExit) as error:
        main(args(tmp_path / "unused.sqlite", *extra))
    assert error.value.code == 2
