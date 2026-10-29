"""部分調査CLIのscaffold→save→exact readと有限catalog。"""

import json
from datetime import datetime

from baibai_engine.cli.investment_judgment import main

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def test_partial_authoring_and_revisit_cli(tmp_path, capsys):
    path, draft = tmp_path / "app", tmp_path / "research.json"
    prefix = ["company-research"]
    assert (
        main(
            [
                *prefix,
                "scaffold",
                "--ticker",
                "1234",
                "--research-id",
                "r1",
                "--asof",
                "2026-10-14",
                "--db",
                str(path),
                "--out",
                str(draft),
            ],
            now=NOW,
        )
        == 0
    )
    scaffold = json.loads(capsys.readouterr().out)
    assert scaffold["document"]["investigated_scope"] == ""
    assert not path.exists()
    scaffold["document"]["investigated_scope"] = "IRページで契約更新条件を調べたが未掲載"
    scaffold["document"]["unresolved_questions"] = ["次回決算で契約更新条件を確認する"]
    draft.write_text(json.dumps(scaffold), encoding="utf-8")
    assert main([*prefix, "save", str(draft), "--db", str(path)], now=NOW) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["recorded_at"] == NOW.isoformat()
    assert (
        main(
            [
                *prefix,
                "scaffold",
                "--ticker",
                "1234",
                "--asof",
                "2026-10-14",
                "--from-research-id",
                "r1",
                "--known-at",
                "2026-10-14T18:00:00",
                "--db",
                str(path),
            ],
            now=NOW,
        )
        == 3
    )
    assert "timezone" in capsys.readouterr().err
    assert (
        main(
            [
                *prefix,
                "get",
                "--research-id",
                "r1",
                "--sha256",
                saved["reference"]["payload_sha256"],
                "--db",
                str(path),
            ],
            now=NOW,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == saved
    assert (
        main(
            [*prefix, "latest", "--ticker", "1234", "--asof", "2026-10-14", "--db", str(path)],
            now=NOW,
        )
        == 0
    )
    context = json.loads(capsys.readouterr().out)
    assert context["research"]["record"] == saved
    assert context["assessment"]["status"] == "not_found"
    assert (
        main(
            [
                *prefix,
                "catalog",
                "--asof",
                "2026-10-14",
                "--known-at",
                NOW.isoformat(),
                "--limit",
                "1",
                "--db",
                str(path),
            ],
            now=NOW,
        )
        == 0
    )
    catalog = json.loads(capsys.readouterr().out)
    assert catalog["items"][0]["ticker"] == "1234"
    assert catalog["known_at"] == NOW.isoformat()
    assert (
        main(
            [
                *prefix,
                "scaffold",
                "--ticker",
                "1234",
                "--asof",
                "2026-10-14",
                "--from-research-id",
                "r1",
                "--db",
                str(path),
                "--out",
                str(draft),
            ],
            now=NOW,
        )
        == 4
    )
    capsys.readouterr()
    assert (
        main(
            [*prefix, "get", "--research-id", "r1", "--sha256", "a" * 64, "--db", str(path)],
            now=NOW,
        )
        == 3
    )
    capsys.readouterr()
    scaffold["document"]["unresolved_questions"] = ["変更済みの別内容"]
    draft.write_text(json.dumps(scaffold), encoding="utf-8")
    assert main([*prefix, "save", str(draft), "--db", str(path)], now=NOW) == 4
