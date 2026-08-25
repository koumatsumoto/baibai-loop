---
name: ops-maintenance
description: daily batch、store 同期、配信、障害復旧、定期 maintenance、運用 task を扱い、機械事実の再現性を守る。投資判断には使わない。
---

# Ops Maintenance

操作前に [`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md) の該当節と public `--help` を読む。調査、移行、再構築はローカルで完結させ、cloud batch を試行錯誤に使わない。

## 操作の選択

| trigger | action |
| --- | --- |
| daily batch の失敗・欠測 | 通知と run summary から失敗した step を特定し、同じ CLI をローカルで再現する。原因を直して local gate を通す。再実行は、必要かつ成功する見込みがある場合の最終確認だけに使う |
| research FV への価格到達 | `baibai_engine.research_watch` を実行し、triggered lane を `research` skill へ渡す。価格だけで注文しない |
| 注文の約定・失効 | `tools.experiments.measure_limit_outcomes` で全体を再計測する。少数結果で policy を変えない |
| store 読み取り・同期 | 下の authority と no-loss 規律に従う |
| app / view の配信 | application store の反映と serving materialize を OPERATIONS の順で行う。ユーザーが手段を指定した場合は、その指示を優先する |
| 定期 maintenance | calibration、PMI、lake audit、capital-control の dated task だけを due 時に実行する |

## Store authority

| store | authority | local operation |
| --- | --- | --- |
| market | lake release + cloud / local の補完 table | 読む前に `pull-market` / `pull-machine` → `hydrate-market`。反映は `publish-lake` → `push-market`。merge が no-loss を証明できない場合は upload しない |
| runs | cloud only | `pull-machine` で読む。local run を cloud へ push しない |
| macro | cloud rolling window + local full history | `pull-machine` で読み、`push-macro` は merge 後だけ。誤値は delete でなく retraction |
| application | local | pull で置換しない。判断成果物を完成させてから canonical publish 手順で反映する |

pull は batch の実行中を避ける。世代が途中で変わった場合はローカル store を置換せず、batch の完了後に引き直す。schema migration は code を main へ入れ、integrity と行数を照合した store を同じ作業内で反映する。

## 障害対応

1. error が示す step、dataset、store、schema version、run identity を保存する。
2. cloud が使った入力をローカルの staging へ取り込み、同じコマンドと引数で再現する。
3. source 取得、validation、automation、publication identity の失敗を分けて原因を直す。schema や field の意味を推測で変えない。
4. full local gate を通す。macro を変更した場合は、さらに `validate-macro-stores` を通す。
5. 正本を no-loss で反映し、次の定時 batch または明示された最小確認で復旧を確認する。

## 定期 maintenance

- calibration panel: 月初に前月完全月末まで `calibration-build` → `calibration-evaluate`。`--force` は保持全 cohort を覆う全再構築だけに使う。契約は [`estimate-calibration.md`](../../../docs/reference/estimate-calibration.md)。
- PMI manifest: 公表翌週に dry-run、本実行、対象月の `macro refresh`、公表値照合、commit の順。
- lake audit: 前回から7日後に full-history audit。command は [`market-lake.md`](../../../docs/reference/market-lake.md) を正本とする。
- capital-control: 翌月15日以降に refresh と exit build を行い、hydrate 済み store から `publish-lake` → `push-market` で反映する。

## 運用 task

application DB の `baibai-engine task` だけを writer とする。dated の判断待ち、決算、注文期限、定期 maintenance は task にし、開発作業は GitHub issue にする。追加前に open task と照合し、期限、種別、領域が同じ task は統合する。ただし、保有・売買判断と高重要 task は個別に持つ。完了時は canonical 更新を確認し、反復 task は同じ実行で次回分を作る。外部更新を待つ場合は close せず、due と待機対象を更新する。

## 停止条件

次の場合は停止する。

- credential、前提 artifact、store authority、schema、対象 run を確認できない
- cloud / local merge が行の取り残し、世代 drift、integrity failure を示す
- 原因不明の dispatch、Actions を使った試行錯誤、日次 batch への migration の委譲を行おうとしている

## 正本

- cloud / R2 / recovery: [`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md)
- store と writer 境界: [`architecture.md`](../../../docs/architecture.md)
- local / CI gate: [`python-foundation.md`](../../../docs/reference/python-foundation.md)
