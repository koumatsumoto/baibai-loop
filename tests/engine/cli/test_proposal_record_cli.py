"""記録draftの構文エラーを本文やtracebackのないCLIエラーへ戻す。"""

import pytest

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
