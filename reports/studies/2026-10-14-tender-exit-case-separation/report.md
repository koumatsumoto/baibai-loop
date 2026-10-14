---
title: "完結した過去TOBを非上場化案件へ混入させない"
summary: "3990・8890の新旧原資料を照合し、全履歴集約による誤った価格競合・買付者競合を修正する。"
doc_type: measurement-record
status: active
---

# 案件照合（refs #1481）

3990・8890とも、過去の別案件の混入が原因であり、非上場化の現金価格の根拠が崩れた事象ではない。旧値保持や件数増加を採否条件にせず、届出・訂正・報告の親資料ID、対象会社、買付者、公開買付期間を照合した。

| 対象 | 完結した過去案件 | 非上場化に対応する案件 |
| --- | --- | --- |
| 3990 UUUM / E33359 | フリークアウトHD E30648、2023-08-14〜09-08、727円。[届出 S100RP41](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100RP41.pdf)、[訂正 S100RR70](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100RR70.pdf)、[成立報告 S100RU12](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100RU12.pdf)。連結子会社化の案件 | 同じ買付者、2024-11-15〜12-26、532円、現金のみ、完全子会社化。[届出 S100USPQ](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100USPQ.pdf)、[11/18訂正 S100USYT](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100USYT.pdf)、[12/6訂正 S100UWTE](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100UWTE.pdf)、[成立報告 S100V17R](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100V17R.pdf)。JPX廃止日は2025-02-17 |
| 8890 レーサム / E05179 | 自己株式TOB（買付者もE05179）、2023-09-20〜10-18、3,161円。[届出 S100RVOU](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100RVOU.pdf)、[訂正 S100RY2B](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100RY2B.pdf)、[報告 S100S1UW](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100S1UW.pdf)。報告は2,841,768株の買付を記載。非上場化の案件ではない | ヒューリック E00523、2024-09-17〜10-30、5,913円、現金のみ。[届出 S100UDPU](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100UDPU.pdf)、[訂正 S100UHDT](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100UHDT.pdf)、[成立報告 S100UM14](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100UM14.pdf)。JPX廃止日は2025-03-04 |

廃止日・会社名・理由は[JPXの2025年一覧](https://www.jpx.co.jp/listing/stocks/delisted/archives-01.html)とも一致した。

上記13件の索引状態はlegal=1、disclosure=0、withdrawal=0。250/270のparentは各欄の240を指す。2023-08-10〜10-26の全78日の通常EDINET一覧（17,868行）では、対象2社の240/250/270は上記6件、260/280は0件。2024-09〜2025-03の保存一覧では対象2社の該当様式は上記7件、260/280は0件。2023年分は前担当の検証コピーがこの環境になく再取得したが、一度取得した日次応答と本文ZIPは再利用した。本文は上記13件に限定し、全廃止案件の本文を再取得していない。

3990の旧実装は727円・532円のregistration chainを一つのcaseへ集約して価格競合とした。8890は自己株TOBと他社TOBの二買付者を全履歴で集約し、本文を読む前に`multiple_offerors`とした。

修正では、対象会社のregistrationを追跡し、全てが報告・取下げで終わった後の日付の新registrationだけを別roundとして分離する。親資料IDがある訂正・報告は元registrationへ対応させ、遅い旧案件の訂正を新案件に付けない。並行する買付者・二段階案件・同日の終結と開始はまとめたまま、未完了の競合や未知/異なる買付者のparentは未解決にする。parent欠測の既存索引には従来の時系列対応を残す。価格・現金対価・成立・株式basis・exit日・較正算式は変更しない。金融指標の`EDINET_METRIC_REVISION`の対象である財務抽出も変更しない。

## 再現と検証範囲

取得応答は通常`store_edinet_documents`で検証コピーへ取り込み、JPX対象2行を既存writerへ渡した。原資料のCSV ZIPを通常providerから取得し、既存parserとbuilderを通した。価格・docIDを派生exit表へ手入力していない。

同じ2社を旧コードへ渡すと、2023年一覧追加前は2件、追加後は0件（価格競合1、買付者競合1）。修正コードでは2件となり、532円/S100UWTE/S100V17R、5,913円/S100UHDT/S100UM14の値・廃止日・資料日が追加前と全field一致した。[機械記録](./target-proof.json)へ固定する。SQLite integrityは`ok`、FK違反0件。原株式basisの全期間bar再照合は今回未実施であり、この2社の案件照合を全件退出導出や正式較正受入の成功と扱わない。

合成回帰の再現:

```bash
uv run pytest tests/engine/test_tender_offer.py -n 0 -q
```

同じ/異なる買付者の過去成立・取下げ、旧案件の遅い訂正、最新案件未成立、未完了競合、同日境界、parent不明/買付者不一致、親IDによる二段階訂正対応を検証し、従来の競合・複数価格・非現金・撤回の拒否も維持した。

本番write、publication lease、L1/pointer更新、application DB/台帳操作、mergeは未実施。全件導出の177件と追加22件は前担当の停止記録であり、本修正後の全件件数はまだ検証していない。正式較正83cohort・固定19cohort×3y/5yの受入、既存35/38の再判定、schema3 context発行は未完了。原資料不足や不成立scopeを除外して公開しない。

完全local gate: Python 4,247 passed / 専用R2受入8 skipped、coverage87%、frontend295、Worker93、Ruff/mypy/import-linter/全drift/Bandit/runtime・build依存監査/Node監査/build/Wrangler dry-run成功。初回の全Python試験は環境に残る旧packageのbytecodeをnamespace packageとして検出して失敗した。tracked sourceがないことを確認してbytecodeを削除せず退避し、全試験を再実行した。`validate-market-context-stores`はindicator/applicationコピーがないため未実行。対象marketコピーのintegrity/FK検査は上記のとおり実施済み。
