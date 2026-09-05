---
name: ops-maintenance
description: daily batch、local analysis、store同期、配信、障害復旧、定期maintenanceを扱い、機械事実の再現性を守る。投資判断には使わない。
---

# Ops Maintenance

操作前に [`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md) の該当節とpublic `--help`を読む。調査、移行、再構築はローカルで完結させ、cloud batchを試行錯誤に使わない。

## Local analysisの調査順

`baibai-batch analysis run`は1 commandでfreshに実行する。通常成功時はartifactやlogを読まない。失敗時だけstdoutのexact `log_path`を使い、次の順で必要な範囲だけ読む。

1. compactなstdout / `summary.json`のstatusとreason
2. `run.log`のboundedなredacted内容

random retry、別as-ofへの置換、「最新Review Set」の再検索をしない。AI result不正やbinding / CAS conflictではcanonical write 0を確認し、原因を直して同じcommandをfreshに再実行する。旧runのresume、active pointer、force-new、candidate cacheは使わない。

## 操作の選択

| trigger | action |
| --- | --- |
| daily batch / analysisの失敗・欠測 | stdout / summaryから失敗stageを特定し、同じCLIをローカルで再現する。原因を直してlocal gateを通す。再実行は成功する見込みがある最終確認だけに使う |
| research FVへの価格到達 | `baibai_engine.research_watch`を実行し、triggered caseを`research` skillへ渡す。価格だけで注文しない |
| 注文の約定・失効 | `tools.experiments.measure_limit_outcomes`で全体を再計測する。少数結果でpolicyを変えない |
| store読み取り・同期 | 下のauthorityとno-loss規律に従う |
| app / viewの配信 | application storeの反映とserving materializeをOPERATIONSの順で行う |
| 定期maintenance | calibration、PMI、lake audit、TSE capital policy / JPX delistingのdated taskだけをdue時に実行する |

## Store authority

| store | authority | local operation |
| --- | --- | --- |
| market | lake release + cloud / localの補完table | 読む前に`pull-market` / `pull-machine` → `hydrate-market`。反映は`publish-lake` → `push-market`。mergeがno-lossを証明できない場合はuploadしない |
| runs | R2 canonical。cloud / 明示的local dailyが同じ履歴を進める | `pull-machine`で読み、local dailyは直前pullの全store CASを通す`push-machine`だけで反映 |
| macro | cloud rolling window + local full history | `pull-machine`で読み、`push-macro`はmerge後だけ。誤値はdeleteでなくretraction |
| application | local | pullで置換しない。判断成果物を完成させてからcanonical publish手順で反映する |

pullはbatch実行中を避ける。世代が途中で変わった場合はローカルstoreを置換せず、batch完了後に引き直す。schema migrationはcodeをmainへ入れ、integrityと行数を照合したstoreを同じ作業内で反映する。

## 障害対応

1. errorが示すstep、dataset、store、schema version、run identityを保存する。
2. cloudが使った入力をローカルのstagingへ取り込み、同じcommandと引数で再現する。
3. source取得、validation、automation、publication identityの失敗を分けて原因を直す。schemaやfieldの意味を推測で変えない。
4. full local gateを通す。macroを変更した場合は、さらに`validate-macro-stores`を通す。
5. 正本をno-lossで反映し、次の定時batchまたは明示された最小確認で復旧を確認する。

## 定期maintenance

- calibration panel: 月初に前月完全月末まで、同じ`--rules-path`で`calibration-build` → `calibration-evaluate`。`--force`は保持全cohortを覆う全再構築だけに使う。
- PMI manifest: 公表翌週にdry-run、本実行、対象月の`macro refresh`、公表値照合、commitの順。
- lake audit: 前回から7日後にfull-history audit。
- valuation catalyst / delisting: 翌月15日以降に`refresh-tse-capital-policy`、`refresh-jpx-delistings`、`build-tender-offer-exits`を行い、hydrate済みstoreから`publish-lake` → `push-market`で反映する。

## 運用task

application DBの`baibai-engine task`だけをwriterとする。datedの判断待ち、決算、注文期限、定期maintenanceはtaskにし、開発作業はGitHub issueにする。追加前にopen taskと照合し、期限、種別、領域が同じtaskは統合する。完了時はcanonical更新を確認し、反復taskは同じ実行で次回分を作る。

## 停止条件

- credential、前提artifact、store authority、schema、対象runを確認できない
- cloud / local mergeが行の取り残し、世代drift、integrity failureを示す
- 原因不明のdispatch、Actionsを使った試行錯誤、日次batchへのmigration委譲を行おうとしている

## 正本

- cloud / R2 / recovery: [`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md)
- storeとwriter境界: [`architecture.md`](../../../docs/architecture.md)
- local / CI gate: [`python-foundation.md`](../../../docs/reference/python-foundation.md)
