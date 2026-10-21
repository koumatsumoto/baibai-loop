---
title: "現行methodへの較正文脈再生成の適用可否"
summary: "固定19月と保存済み市場入力から、現行methodの記述分布を再生成できる範囲と不足を確認する。"
doc_type: measurement-record
status: in-progress
date: 2026-10-21
---

# 現行methodへの較正文脈の適用可否

refs [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481)。本件は現行methodへの適用可否の確認であり、E[r]式・選定閾値・L3要求率の変更や収益優位の実証ではない。結果を確認してから対象月や不足の判定を緩めない。

## 固定した対象と判断条件

- 計算code: `23128077a76055e43fdc44d1022aa14d9dbf2d79`。
- 入力release: `20261021T025101Z-1c89ffb3-6e62673cc823`、manifest SHA-256: `81be1730fbaeb7f718bc438cd368e7549f90249f12488ba9028a031d7a6b133f`。
- 対象は[既存v26文脈の受入](../2026-10-14-er-context-v26/report.md)と同じ2020-01-31～2021-07-30の19月末。新しい有利な月への差替えや失敗月の除外をしない。3y/5y、`estimator_policy`、追加metric `er_level_calibration`を使う。
- 保存済み入力だけを使い、providerの再取得・本番storeへの書込・既存判断のrun付替えは行わない。
- 現行panel/forwardが正規CLIで生成でき、required月・method identity・入力整合を満たす場合だけ、新しい記述分布を採用候補にする。旧artifactの日付・hashだけを変更しない。
- 結果欠測と入力不完全を区別する。正常なentry/exit・配当観測不能は理由と分母を残し、formal evidenceの不成立を隠さない。未来master・母集団不整合・入力窓不足・未知status・partition不整合をpartialの名で許容しない。詳細は[較正の正本](../../../docs/reference/estimate-calibration.md#er-level-distribution-context)に従う。
- 再生成できない場合は必要な不足と再開条件を記録する。全履歴再取得、互換層、別store設計、閾値変更へ範囲を拡大しない。

## 再生成が必要な理由

旧artifactは `dividend-period-freshness-v26`、production hash `aa922907ebc421a0`、panel hash `52f76944347543e8`で生成された。現行はvaluation `annual-period-cohort-v42`、market context `native-master-and-margin-basis-v4`、universe `native-common-stock-one-bar-v3`、forward `reported-dividend-period-gaps-v7`である。

価格調整と財務の株式単位換算の分離、連結/個別と会計期間に沿った実績・予想選択、完了した比較可能年度だけを使う正規化EPSなど、実際の入力解釈が変わっている。rulesのfield改名だけの問題ではない。現行reader/evaluatorは旧method/cacheを拒否するため、旧panelへの再評価だけでは足りない。

## 保存入力の事前確認

2026-10-21のread-only集計では、上記releaseを保持するmarketに次を確認した。行数とcoverage記録は、全fieldの正しさや厳密な過去時点の原本復元の証明とは区別する。

| 入力 | 保存範囲・件数 | 今回への意味 |
| --- | --- | --- |
| 価格 | 2016-10-18～2026-10-20、10,138,014行、2,443日 | 最初の対象月の1200日窓開始とcoverage開始が一致 |
| 財務summary | 開示日2016-10-18～2026-10-20、190,898行 | 主730日窓は期間上被覆。補助2200日EPS窓は全期間を保持しない |
| master | 2016-10-31～2026-10-20、123断面 | 対象19月末はすべて同日断面が存在 |
| EDINET metrics | asof 2026-10-18～20、11,919行 | 過去19月の行は0。過去EDINET軸を復元済みとしない |
| 上場廃止 | 781行、2017-01-16～2027-04-01 | forward終了イベントとして対象窓ごとの評価が必要 |
| 現金公開買付け退出代理値 | 239行、2023-10-26～2026-10-09 | 予測asof期間内0行でも、3y/5y結果窓全体に代理値がないとは限らない |

価格・財務のsource coverageは2016-10-18～2026-10-20の連続区間でstatusはok。調整イベント条件に該当する価格行は2,511、全価格行のfactor nullは0だが、これだけで株式単位の意味がすべて確定したとは扱わない。

対象master日付は2020年の01-31、02-28、03-31、04-30、05-29、06-30、07-31、08-31、09-30、10-30、11-30、12-30、2021年の01-29、02-26、03-31、04-30、05-31、06-30、07-30。

## 比較と解釈の限界

旧v26 artifactとの比較は、宣言/観測/未評価数、固定帯、分布と不足理由の変化を記述する。同時に入力releaseと計算が変わるため、その差を単一修正の因果効果や投資成果改善とは呼ばない。現在取得可能な過去開示には改訂履歴の限界があり、同日masterの存在だけで完全なpoint-in-time再現を保証しない。

正規化EPSの補助履歴不足、過去EDINET軸の未観測、退出値・配当の不足を別々に確認する。19月の窓は重複し、独立19標本ではない。L3の判断品質や4 Approachの優越性をこの分布だけで実証しない。

## 実行・受入状況

固定releaseの隔離読取を準備中。現行methodでのbuild/evaluate、artifact採用、公開後読戻しは未実施。旧published artifactは変更していない。
