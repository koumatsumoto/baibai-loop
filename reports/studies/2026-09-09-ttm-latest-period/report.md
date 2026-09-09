# TTMの最新会計期間選択

価値tier: T2 — 過年度訂正でTTMの基準期間が巻き戻り、倍率と候補順位を誤る経路を修正する。

## 修正と比較条件

#1275のcorrectness修正。基点は`405c8fb693492888854761a9bf2e6eb3b7a6b6be`。
`_ttm_value`の基準行を開示日順から既存の会計期間順序へ切り替える。
実績行の定義、合成式、前年FY・前年同期間の選択、予想・配当・他の期間選択は変更しない。
必要fieldは期間選択後に確認し、最新revisionの欠損を過年度・旧revisionへ戻して埋めない。
valuation calculation revisionは`actual-ttm-latest-accounting-period-v22`。

採否は期間契約と算術の正しさで決め、順位・収益の改善を採否条件にしない。
9月9日にローカルmarket storeをSQLite read-only backupで固定し、9月8日断面を比較した。
通常の1,200日bar・730日financial入力と補助FY/split履歴、同じrules、同じproduction
`run_command`・`build_review_set`・Approach順序ownerを使う。
基点commitの`_ttm_value`を読み込んだbeforeと、修正後afterを同一入力で計算した。
runのpublish呼出しは一時計測で捕捉し、canonical run・Review Setは生成していない。
旧run・正式判断・ledgerは上書きしていない。

入力はmaster 4,435行、bar 3,378,231行、financial 36,094行、補助FY 37,898行、
split event 1,147行。通常universeは3,705社。
保存済み9月8日runのmethod hashは`c525a6c55309450f`で、比較基点のv21とは異なる。
したがって以下は保存済みrunとの差ではなく、指定mainに対する同入力比較である。
保存済みrunと再計算beforeのNormalized Earnings Power・Asset Valueの全順位は一致した。

## 確認できた影響

TTMが変わるのは3174・4088・6195・8572・8798の5社。
表の金額はすべて保存済みprovider入力から計算した百万円であり、会社の現在の実績を別途認定するものではない。
`null`は必要入力から評価できないことを示す。

| ticker | 純利益TTM（前→後） | 売上TTM（前→後） | CFO TTM（前→後） | 営業利益TTM（前→後） |
| --- | ---: | ---: | ---: | ---: |
| 3174 | -808→-532 | 8,841→8,466 | 170→null | -404→-271 |
| 4088 | null→7,739 | null→1,084,606 | null→90,242 | null→37,817 |
| 6195 | 264→171 | 3,631→3,603 | 229→null | 344→247 |
| 8572 | 79,635→64,657 | 337,709→344,068 | 12,096→null | 100,394→101,897 |
| 8798 | null→-820 | null→6,947 | null→null | null→-147 |

純利益の独立検算は以下のとおり。

- 3174: 2026年5月末3Qを基準に`-158 + (-808) - (-434) = -532`。8月13日の過年度FY行を基準にしない。
- 4088: 2025年9月末2Qを基準に`-21,179 + 49,074 - 20,156 = 7,739`。7月31日の同FY 1Q訂正へ巻き戻さない。
- 6195: 2026年6月末1Qを基準に`-111 + 264 - (-18) = 171`。8月17日の前年FY行は比較項に使う。
- 8572: 2026年6月末1Qを基準に`19,141 + 79,635 - 34,119 = 64,657`。8月24日の前年FY行は比較項に使う。
- 8798: 2025年12月末1Qの8月14日revisionを基準に`-2 + (-1,539) - (-721) = -820`。8月17日の前年同期間訂正は減算項に使う。

3174・6195・8572の最新四半期CFOは欠損しており、前年FYのCFOだけでPCFRを出さなくなる。
4088・8798の基準期間が古いことは入力の限界として残る。復元値を足元の決算が揃った証拠とは読まない。

| ticker | 実績PER（倍、前→後） | P/S（倍、前→後） | PCFR（倍、前→後） |
| --- | ---: | ---: | ---: |
| 3174 | null→null | 0.14→0.15 | 7.4→null |
| 4088 | null→89.30 | null→0.64 | null→7.7 |
| 6195 | 10.35→15.97 | 0.75→0.76 | 11.9→null |
| 8572 | 9.52→11.73 | 2.25→2.20 | 62.7→null |
| 8798 | null→null | null→0.72 | null→null |

倍率はproduction publicationと同じ丸め表示。
予想PER・PBR・EV/EBITDA、予想・配当・資本・成長率に関する出力は全3,705社で不変。

| Approach | eligible（前→後） | 順位が変わる銘柄数 | 上位20社の入替 |
| --- | ---: | ---: | --- |
| Current Earnings Power | 2,341→2,341 | 96 | なし |
| Normalized Earnings Power | 1,872→1,872 | 0 | なし |
| Asset Value | 1,080→1,080 | 0 | なし |
| Reinvestment Value | 103→103 | 0 | なし |

Nomination unionは77→77社で、各NominationのApproach内順位まで完全一致した。TTM変化5社自身のApproach順位も不変で、
96社の移動はsector gapへの波及である。Security Analysisのmetrics集合は35社で変わる。
7183の機械E[r]は-4.13%→-3.98%、sector由来FVは149.1806→152.9607円へ波及するが、
E[r]はNomination membershipや順位の入力には使わない。
比較runは前後とも既存の`partial warning`。共通適格母集団の必須TTM非exactは
1,708→1,709項目であり、銘柄数ではない。既存の成長率入力欠損56社は不変だった。
新score・Approach・閾値・売買条件・汎用基盤は追加しない。

## 較正文脈

全82 cohort（2019年11月29日〜2026年8月31日）を同じ固定入力から再構築した。
panel 309,073行、forward 1,545,775行、resolved 1,078,761行。integrity_checkはokで、
全cohortのmethod hashは`6571c41413725c5c`に一致した。
旧値へのhash付け替えは行わず、既存`calibration-build` / `calibration-evaluate`を使った。
required scopeは既存ownerが判定する3y/5y両方のintegrity・必須metric eligible月の全共通集合とした。
収益率・順位・符号で月を選ばず、共通集合が空なら生成不能とする条件で実行した。
3yは40か月、5yは18か月、共通は17か月（2020年1月〜2021年7月、2020年6月・2021年4月を除く）。
必須34組はすべてeligible、evidence_completeはtrue。production hashは`819055f83303c2a6`。
[再生成context](../../published/er-level-calibration-latest.yaml)は2026年10月24日まで有効だが、
表示するrunとのmethod一致も必要になる。required scopeの成立を全82 cohortの全horizonが
完全であるという主張には広げない。未成熟、入力不足、未価格退出の不確実性は既存判定のまま残る。
較正の分布変化を修正の将来収益効果と解釈しない。

## 検証と限界

最新四半期後の前年FY・前年同期・さらに古いFY訂正について、4fieldの値更新・欠損、
入力正順・逆順をテストした。最新必要field欠損の後に過年度訂正が来ても未評価を維持する。
修正前に36 subtest失敗を再現し、修正後のmetrics検証は138 tests・84 subtests成功。
独立Reviewは3 operand×24並び順の欠損と短縮FYも検証した。
実データについても5社の3 operandを生SQLから独立選択し、4fieldの20値すべてが一致した。
全3,705社のTTM・倍率と、TTMの直接・間接影響以外の全serialized fieldの不変性も独立確認した。

会計期間情報が欠けた行の順序は既存helperの契約を維持する。最新同期間訂正で必要fieldと
期間情報が同時欠損すると旧revisionが選ばれる境界はあるが、固定入力全183,078財務行で
`period_start`・`period_end`欠損はそれぞれ0件だった。今回の修正を期間情報の欠けた入力全般の
解決とは呼ばない。元provider responseの全履歴は保存しておらず、PITの開示日制約だけで
後日のbackfillが無かったとは証明できない。


## 最新main全体の再確認

再構築終了時にもremote mainは基点`405c8fb6`だった。
full local Python gatesは2,688 passed、8 skipped、coverage 84.86%。skipは専用R2 acceptance資格情報の不足による。
Ruff format/check、mypy、import境界、全drift、Bandit、Python依存監査も成功した。
frontend 192 tests、Worker 37 tests、lint/build/typecheck、Wrangler dry-runは成功した。
ただし既存Worker開発依存の`sharp 0.35.2`に対し、9月8日公開の
[High advisory](https://github.com/advisories/GHSA-rgj7-g3m4-5g8c)でNode audit gateが失敗した。
既存overridesへ`sharp: 0.35.4`を加えた隔離コピーでは、High audit・Worker 37 tests・型検査・dry-runが成功した。
既存High auditを解消するため、この最小の依存修正を同じPRへ含めた。

frontend/edgeには[Vitestのmoderate advisory](https://github.com/advisories/GHSA-82fw-gwwq-j7x9)も残る。
独立security Reviewでは、現在の設定に未信頼画像decodeや公開mocker pluginの入口がなく、
両advisoryの本番悪用経路は確認できなかった。これはadvisory自体の不存在や安全性の保証ではない。
全repositoryに未解決問題が一切ないとは報告しない。


## 次の1作業

このPRを適用し、検証済みv22較正snapshotをcurrentへ反映する。必要なserving更新を既存手順で行い、
次の通常dailyでv22のrunが生成されることを確認する。旧run・正式判断・ledgerの再発行は行わない。
