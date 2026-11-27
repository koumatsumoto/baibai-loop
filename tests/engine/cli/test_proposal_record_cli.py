"""記録draftの構文エラーを本文やtracebackのないCLIエラーへ戻す。"""

import json
import sqlite3
from contextlib import closing

import pytest
import yaml
from tests.helpers.proposal_records import not_purchased_report, ref
from tests.helpers.proposal_records import records_case as records_case

from baibai_engine.cli.app import main


@pytest.mark.parametrize("command", ["correct", "report"])
@pytest.mark.parametrize(
    "invalid_document",
    [
        pytest.param(b"statement: [SYNTHETIC_PRIVATE_CONTENT\n", id="yaml-syntax"),
        pytest.param(b"statement: SYNTHETIC_PRIVATE_CONTENT\xff\n", id="text-encoding"),
    ],
)
def test_malformed_proposal_record_yaml_does_not_expose_private_input(
    tmp_path, capsys, command, invalid_document
):
    draft = tmp_path / "日本語 draft.yaml"
    draft.write_bytes(invalid_document)
    db = tmp_path / "app.sqlite"
    args = [
        "investment-judgment",
        "proposal-records",
        "--db",
        str(db),
        command,
        str(draft),
    ]
    if command == "correct":
        args.extend(["--expected-latest-id", "unused", "--expected-latest-sha256", "0" * 64])

    assert main(args) == 3

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: invalid proposal record YAML\n"
    assert not db.exists()


@pytest.mark.parametrize("command", ["correct", "report"])
def test_ambiguous_proposal_record_draft_is_rejected_before_save(
    records_case, tmp_path, capsys, command
):
    case, original, other, service, correction = records_case
    if command == "correct":
        draft = correction
        duplicate = {"reason": "SYNTHETIC_MANUALLY_TYPED_SECOND_REASON"}
        guard = [
            "--expected-latest-id",
            other.proposal_id,
            "--expected-latest-sha256",
            ref(other).draft_sha256,
        ]
    else:
        draft = not_purchased_report(original)
        duplicate = {"proposal_ref": ref(other).model_dump(mode="json")}
        guard = []
    path = tmp_path / "duplicate-fields.yaml"
    path.write_text(
        yaml.safe_dump(draft.model_dump(mode="json"), sort_keys=False)
        + yaml.safe_dump(duplicate, sort_keys=False),
        encoding="utf-8",
    )
    before = case.app.read_bytes()

    assert (
        main(
            [
                "investment-judgment",
                "proposal-records",
                "--db",
                str(case.app),
                command,
                str(path),
                *guard,
            ]
        )
        == 3
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: invalid proposal record YAML\n"
    assert case.app.read_bytes() == before
    assert service.read(original.proposal_id)["human_reports"] == []
    assert service.read(other.proposal_id) == {"corrections": [], "human_reports": []}


@pytest.mark.parametrize(
    ("command", "shape"),
    [
        ("correct", "mapping"),
        ("report", "mapping"),
        ("show", "mapping"),
        ("correct", "binary"),
        ("report", "binary"),
    ],
)
def test_proposal_record_schema_error_does_not_repeat_private_fields(
    records_case, tmp_path, capsys, command, shape
):
    case, original, other, service, correction = records_case
    # YAML can serialize both a mapping and a binary scalar; neither is valid note text.
    text = "SYNTHETIC_PRIVATE_RECORD_TEXT"
    private = text.encode() if shape == "binary" else {"note": text}
    guard = []
    if command == "show":
        # Deliberately corrupt a synthetic saved receipt, rather than passing a draft.
        report = service.report(not_purchased_report(original))
        raw = report.model_dump(mode="json")
        raw["reason_quote"] = private
        with closing(sqlite3.connect(case.app)) as connection, connection:
            connection.execute("DROP TRIGGER proposal_human_report_no_update")
            connection.execute(
                "UPDATE proposal_human_report SET payload=? WHERE report_id=?",
                (json.dumps(raw), report.report_id),
            )
        target = original.proposal_id
    else:
        if command == "correct":
            raw = correction.model_dump(mode="json")
            raw["reason"] = private
            guard = [
                "--expected-latest-id",
                other.proposal_id,
                "--expected-latest-sha256",
                ref(other).draft_sha256,
            ]
        else:
            raw = not_purchased_report(original).model_dump(mode="json")
            raw["reason_quote"] = private
        draft = tmp_path / "wrong-field-type.yaml"
        draft.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        target = str(draft)
    before = case.app.read_bytes()

    assert (
        main(
            [
                "investment-judgment",
                "proposal-records",
                "--db",
                str(case.app),
                command,
                target,
                *guard,
            ]
        )
        == 3
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: invalid proposal record schema\n"
    assert case.app.read_bytes() == before
