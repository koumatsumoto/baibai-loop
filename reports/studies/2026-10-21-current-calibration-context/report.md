---
title: "現行methodへの較正文脈再生成の適用可否"
summary: "固定19月と保存済み市場入力から、現行methodの記述分布を再生成できる範囲と不足を確認する。"
doc_type: measurement-record
status: in-review
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

## 実行結果と適用判断

**現行methodに一致する記述分布は再生成できた。正式な方法変更の証拠は未成立（32/38）であり、そのまま残す。** 本PRはこの記述文脈の更新で、機械E[r]算式・閾値・L3要求率・候補順位を変えない。掲載した歴史分布を個別銘柄の期待収益や購入条件へ代用しない。

固定releaseを空starterへhydrateすると、価格/財務の行は復元されたが、store-localな `source_coverage` は0行だった。通常のmarket-only取得で同releaseのseedを読み、ETag前後一致とoriginを確認してから固定releaseを再hydrateした。取得記録を手作業で作成していない。coverage 9,871行の全列hashはseedと復元後で一致した。

- build: 2026-10-21 11:27:39〜12:20:10 UTC、3,151.054404秒、exit 0。
- evaluate: 同日12:21:27〜12:22:00 UTC、32.850313秒、exit 0。
- 指定19日すべて一致。panel 70,166行、forward 350,925行。各cohort/horizonのforwardはpanelより1行多く、builderがTOPIX ETF proxyを追加する契約と対応する。benchmarkを有限E[r]母数へ加えない。
- code/rules、market DBとcoverage内容は実行前後で不変。provider再取得・本番store書込・既存Researchの付替えなし。
- capacity guardを変更していない。最終free 18,435,055,616 bytes、通常sealed snapshot要求 15,417,933,824 bytes。
- 新context: schema 4、生成2026-10-21 21:21:57 JST、有効期限2026-12-05。screening hash `16a1c1441ac44f8b`、panel hash `8e2909c2a526b7ca`、snapshot contract `ee2c8ae897e50b5c`。本番保存runのmethod identityと一致する。期限やhashだけを編集したものではない。

### 正式evidenceの不足

19月×3y/5yの全38ペアを残し、32がeligible、6がblocked。両metric（`er_calibration` / `er_level_calibration`）が計算可能でも、integrityを通過した意味にはしない。

| 対象 | 不成立理由 |
| --- | --- |
| 2020-05-29 × 3y、2021-07-30 × 3y、2020-01-31 × 5y、2020-02-28 × 5y | `unpriced_exit_flips_direction` |
| 2020-06-30 × 3y / 5y | `entry_price_gap` |

`evidence_complete: false`、`missing_cohorts: []`。失敗月を除外せず、欠測returnを0で埋めない。旧v26の36/38を現行の結果として扱わない。entry鮮度の解釈は [#1491](https://github.com/koumatsumoto/baibai-loop/issues/1491)、subjectごとの頑健性は [#1492](https://github.com/koumatsumoto/baibai-loop/issues/1492)へ残す。この再生成の結果を見てgateを緩めない。

### 観測分布と欠測

有限E[r]の宣言母数は両horizonとも **65,674 ticker-asof観測**。同じ銘柄を月ごとに含むため独立銘柄数ではない。以下はQ1〜Q5のみの合計で、重複する8.5%以上帯を足さない。

| horizon / basis | 観測 | 未評価 | 観測率 |
| --- | ---: | ---: | ---: |
| 3y FY実績配当込み | 44,395 | 21,279 | 67.60% |
| 3y 価格のみ | 62,102 | 3,572 | 94.56% |
| 5y FY実績配当込み | 42,885 | 22,789 | 65.30% |
| 5y 価格のみ | 61,077 | 4,597 | 93.00% |

配当込みの未評価理由（3y / 5y）は、株式単位未解決11,297 / 9,625、会計期間gap 4,679 / 4,409、配当欠測1,574 / 3,954、FY観測なし20 / 20、rights issue 137 / 184、stale exit 3,571 / 4,596、entry欠測1 / 1。これらは結果の測定可能性であり、企業の投資不適格や損益0を意味しない。価格のみの未評価はstale exitとentry欠測で全件を説明する。

固定五分位境界は -3.6708%、-0.8408%、1.4640%、3.6161%。両horizon・basisで同じ予測母数と境界を使う。8.5%以上帯は従来の較正座標であり、L3の要求率を8.5%へ変えない。

観測済み対象では高E[r]帯ほど実現年率中央値が高い。ただしQ5の3y配当込みは中央値13.3095%に対しq10は-2.0157%、5yは中央値12.9911%に対しq10は-0.3597%。Q5でも未評価は3yで4,484 / 14,063、5yで4,759 / 14,063ある。上位帯を安全な買いと認定せず、観測対象の選択偏り・欠測・相場期間を併記する。trap rateは同cohortの観測母集団中央値に対する累積returnの20ポイント以上の劣後であり、絶対損失確率ではない。

旧v26は有限E[r]66,805、配当込み観測率3y85.49% / 5y76.16%、価格のみ94.62% / 93.06%だった。今回の配当込み観測率低下は、不明な株式単位・会計期間を推定で実績化しない現行規約の下で現れた差である。ただし入力releaseも変わるので、差の全量を特定修正の因果効果とは断定しない。未評価増を運用品質悪化と即断するのも、保守的になったから正しいと免責するのも不適切である。

## 再現と成果物の同一性

buildは上記code/rulesと隔離marketを使い、`screening calibration-build --start 2020-01-01 --end 2021-07-31 --force`。evaluateは同じcalibration store/rulesに対し、`--horizon 3y --horizon 5y --run-purpose empirical_change_evidence --decision-subject estimator_policy --required-metric er_level_calibration` と全19日の `--required-asof`、隔離 `--out` / `--context-out` を指定した。buildの全5horizon保存と、今回の3y/5y採否対象を区別する。

| 対象 | SHA-256 |
| --- | --- |
| 入力market DB（前後一致） | `b9318b2278652a4be9cff5c0af389a8dc2ebc86d8e79c07935e81c0a4a03afb8` |
| coverage全列内容（前後一致） | `55915d910940f324017810f632d777de28a22f02bb57531f45dca6cb60d8478a` |
| rules file（前後一致） | `21abc5bdf26608d0df6b2be2f950b1af555ca70f8605e5e101ae330415997737` |
| 生成context（19,830 bytes） | `68c5347aa7eb896a4e66230e3596c76a14dd25084b6d9b37fb7fe0aad53d6571` |
| evaluation（3,226,794 bytes） | `8211ad775737fd3b593557b232427317df9b92d8f3ed0650df8e7350e777b21c` |
| calibration snapshot（509,591,552 bytes） | `8c9ca47dbb35f8264a3b5e918680e24aff8b002fd9d81c927d64fa7101645149` |
| 旧v26 schema4 context | `89cc4917964d7b8e89e04e2af8acc0596a21662c88e8ee1a455ed22c3661bb89` |

新contextは [published artifact](../../published/er-level-calibration-latest.yaml)、直前の旧schema4元bytesは [retained-v26-context.yaml](./retained-v26-context.yaml) に保持する。固定scope・formal判定の抽出は [evaluation-summary.json](./evaluation-summary.json) に記録する。summaryは元evaluationの指定fieldの抽出であり、全3.2MBの評価や元DBをこのreportで全行レビュー済みとはしない。contextは分割転送後に元bytesのSHA一致を確認した。

## レビュー・公開受入

主担当がproducer、shared contract、reader、Research照会、Web reader、sensitivity、CLIの適用条件を直接照合した。記述文脈の受入とformal evidence、価格と配当込み、観測数と独立標本数、生成日とrun分析日を分離する。既存Stock Assessment / Investment Proposalのrun identityは変更しない。

現時点はPRレビュー中。最終HEADのGitHub CI、main反映、公開read modelの同一artifact読戻しは未実施。親 #1481 の残る政策比較や #1411 の通常日次受入はこの再生成だけで完了扱いにしない。
