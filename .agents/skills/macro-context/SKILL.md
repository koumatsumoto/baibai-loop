---
name: macro-context
description: 人間の明示的な依頼でfull-depthのMacro Contextを評価し、独立review後に発行する。
---

# Macro Context

人間の明示的な依頼で実行する。日次の機械series/Reading更新からContext執筆を自動起動しない。成果物の構成と深度は[macro reference](../../../docs/reference/macro.md#depth-contract)に従う。

## 外部Chatとの引継ぎ

外部draftは[Macro handoff](../../../docs/reference/macro-handoff.md)に従って原稿と入力を照合する。構造検証済みのhandoffも未発行の入力であり、以下のreviewを省略しない。

## 手順

1. **今回の機械入力を固定する。** 対象as-ofのseries/Readingとmarket snapshotを用意する。追加取得には公開CLIを使い、同期はOps Maintenanceに従う。application DBをpullしない。Reading全体の鮮度・不足・極値を確認し、任意の一系列の不足を全工程の停止理由にしない。必要な機械入力が不正な場合は先に解消する。

2. **今回の評価を作る。** 前回Contextの本文・確率・scorecard条件や同内容のreportを読まず、今回の一次情報からcore、synthesis、scenario、connectionを作る。判断を支えるsourceと反証を確認し、未確認を補完しない。前回分析に事前接触した場合は、汚染のないsessionへ執筆を引き渡す。

3. **今回評価の後で過去を照合する。** draftの`macro context publish --check`が通った後だけ、前回Contextと`macro context scorecard`を開く。返されたsnapshot identityを使い、成立結果を`previous_scorecard_review`へ接続する。今回の結論を前回に合わせない。

4. **文章を編集して独立reviewを受ける。** [判断文書の編集](../../../docs/reference/judgment-writing.md)に従う。authorと別sessionのreviewerへ固定draftと引用sourceを渡し、入力から判断・要約・connectionまでを確認する。review中はdraftを変えず、結果を受けて修正する。編集前後の意味は原稿差分とsourceで確認し、別のclaim ledger作成を必須にしない。新しいsource・因果・評価を採用した場合は、影響する判断と下流を再確定してreviewする。

5. **review結果を処理する。** 独立reviewは初回を含め最大3巡とし、PASSしたら発行へ進む。原稿やsourceの変更で巡数をリセットしない。3巡目もBLOCKEDなら指摘を修正してpublish checkまで行い、発行せず人間へ上げる。再開は人間のdraft承認または追加reviewの明示指示に従う。

6. **発行する。** indicator inputは`baibai_engine.macro.context.scaffold_inputs`で作る。検証は`macro context publish <draft> --check`、発行時は確認した`macro context head`の`context_id`値だけを`--expected-head`へ渡す。cloud反映は[Ops Maintenance](../ops-maintenance/SKILL.md)へ進む。

## 判断の境界

Macroから売買時期、現金比率、個別sizing、候補のhard gate、機械rankingの変更を直接指示しない。重要な根拠不足、一次sourceと判断の矛盾、必要な独立reviewの未完了があれば発行しない。
