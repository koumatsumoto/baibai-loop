"""scaffoldから執筆・保存し、有限一覧からexactな旧判断へ再訪する。"""

import json
from datetime import datetime, timedelta

from tests.helpers.assessment_v1 import assessment_id, pair_payload

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def test_authoring_and_recent_list_to_exact_get(tmp_path, capsys):
    path, draft_path = tmp_path / "app", tmp_path / "retrospective.json"
    document, review = pair_payload()
    AssessmentStoreService(path, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(), document, review
    )
    prefix = ["judgment-retrospective"]
    assert (
        main(
            [
                *prefix,
                "scaffold",
                "--assessment-id",
                assessment_id(),
                "--retrospective-id",
                "r1",
                "--asof",
                "2026-10-14",
                "--milestone",
                "決算後に未確認条件を再訪",
                "--db",
                str(path),
                "--out",
                str(draft_path),
            ],
            now=NOW,
        )
        == 0
    )
    scaffold = json.loads(capsys.readouterr().out)
    assert json.loads(draft_path.read_text(encoding="utf-8")) == scaffold
    draft = scaffold["draft"]
    assert draft["document"]["ticker"] == "1234"
    assert draft["document"]["origin"]["assessment_id"] == assessment_id()
    assert draft["ledger_append_head"] is None
    assert main([*prefix, "save", str(draft_path), "--db", str(path)], now=NOW) == 3
    assert capsys.readouterr().err

    draft["document"]["comparisons"] = [
        {
            "original_claim": document["findings"]["hypotheses"][0]["statement"],
            "observed": "継続収益に関する追加開示は確認できなかった",
            "result": "unresolved",
            "reason": "今後の収益維持を今回の資料では判定できない",
            "citations": [],
        }
    ]
    draft["document"]["lesson"] = {
        "next_time": "次回決算で継続収益の構成を先に確認する",
        "rationale": "未確認のまま利益寄与を確定させない",
    }
    draft_path.write_text(json.dumps(scaffold), encoding="utf-8")
    assert main([*prefix, "save", str(draft_path), "--db", str(path)], now=NOW) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["document"]["comparisons"] == draft["document"]["comparisons"]

    # A later correction of the same information date must be discoverable as recent.
    draft["retrospective_id"] = "r2"
    draft["document"]["corrects_ref"] = first["reference"]
    draft["document"]["lesson"]["next_time"] = "半期報告の収益構成を先に確認する"
    draft["document"]["lesson"]["rationale"] = "前の記録の確認先を決算から半期報告へ訂正する"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    assert (
        main([*prefix, "save", str(draft_path), "--db", str(path)], now=NOW + timedelta(days=1))
        == 0
    )
    saved = json.loads(capsys.readouterr().out)
    assert main([*prefix, "list", "--ticker", "1234", "--limit", "1", "--db", str(path)]) == 0
    recent = json.loads(capsys.readouterr().out)
    assert recent["has_more"] is True
    assert recent["limit"] == 1
    assert len(recent["items"]) == 1
    item = recent["items"][0]
    assert item["reference"] == saved["reference"]
    assert item["origin"] == draft["document"]["origin"]
    assert item["corrects_ref"] == first["reference"]
    assert (
        main(
            [
                *prefix,
                "get",
                "--retrospective-id",
                item["reference"]["retrospective_id"],
                "--sha256",
                item["reference"]["payload_sha256"],
                "--db",
                str(path),
            ]
        )
        == 0
    )
    readback = json.loads(capsys.readouterr().out)
    assert readback["record"] == saved
    assert readback["context"] == scaffold["context"]
    assert main([*prefix, "list", "--ticker", "5678", "--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
