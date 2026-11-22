"""記録draftの構文エラーを本文やtracebackのないCLIエラーへ戻す。"""

import pytest
import yaml
from tests.helpers.proposal_records import not_purchased_report, ref
from tests.helpers.proposal_records import records_case as records_case

from baibai_engine.cli.app import main


@pytest.mark.parametrize("command", ["correct", "report"])
def test_malformed_proposal_record_yaml_does_not_expose_private_input(tmp_path, capsys, command):
    draft = tmp_path / "日本語 draft.yaml"
    draft.write_text("statement: [SYNTHETIC_PRIVATE_CONTENT\n", encoding="utf-8")
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
