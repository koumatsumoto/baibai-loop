# 非実績開示が実績TTMを消す不具合の訂正

価値tier: T2 — 利用可能な実績を既存Approachの判断入力へ戻し、事実選択による候補の取りこぼしを減らす。

## 結論と変更範囲

配当・予想のみの最新開示が、利用可能な実績TTMを未評価へ変える不具合を訂正した。
実績行だけから最新期間・前年FY・前年同期間を選び、その後で必要fieldの充足を確認する。
最新の実績訂正に欠損がある場合は、値がある古い訂正へ戻らず未評価とする。
実績行の定義とTTM式は既存ownerを使い、予想EPS撤回・配当修正は元の選択経路を維持した。

productionとcalibrationは同じmetrics ownerを使う。新しいscore、Approach、閾値、schema、
public CLI、売買条件は追加していない。旧canonical run、正式判断、取引事実は上書きしない。
計算意味が変わるためvaluation revisionをv21へ進め、旧較正値に新しいhashを付け替えない。

## 事前固定と実入力の再現

[plan](./plan.md)の条件で、9月7日・全3,705社・同一market入力・同一rulesについて修正前後を比較した。
対象を結果から選ばず、production ownerによるSecurity AnalysisとApproach順序、Nomination unionを取得した。
比較入力は9月8日のmachine pull前に読み終えている。

修正前の再計算は保存済みrunと全4Approachの上位20社・union 77社が一致した。
ただしnormalized全順位は完全一致ではない。9166のnormalized PERは保存22.5748に対し再計算29.3223で、
その移動により249位置がずれる。他3Approachの全順位は一致する。
この既存不一致の原因は確定しておらず、今回の差は同入力・同ownerでの修正前後の効果として報告する。
元provider responseの保存はなく、公表日制約だけで後日のbackfillが無かったとは証明できない。

## 4812の実績と予想の分離

最新の非実績開示を挟んでも、実績の売上TTM173,275百万円、純利益TTM17,569百万円を復元した。
当該断面の未調整価格2,852円・株数195,547,440株に対し、実績PER31.68、P/S3.21となる。
最新予想EPSはnullのまま、予想配当22.5円も維持し、古い予想EPS92.22を復活させない。
[8月28日開示](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260828/20260828527772.pdf)の
TOB予定価格2,880円という背景を別に認識する。4812は修正後もNominationに入らず、指標復元を割安機会とは呼ばない。

## 全断面への影響

| 指標 | 未評価から復元 | 評価値から未評価 |
| --- | ---: | ---: |
| 実績PER | 96社 | 0社 |
| P/S | 113社 | 0社 |
| PCFR | 39社 | 3社 |

予想PER・PBR・EV/EBITDAは全3,705社で不変。予想配当・配当利回り・予想損失等のflagsも不変だった。
PCFRが未評価になる285A・3997・5027は、選択すべき前年同期間の最新実績訂正でCFOが欠損している。
旧処理はfieldがある古い訂正を探していた。最新比較行から必要値を確定できないため今回は保留する。
行のnullを会社の実際のCFOが消滅したという主張へ置換せず、古い値の無条件補完も行わない。
主担当と独立担当が実入力の各訂正を照合した。

| Approach | eligible（前→後） | 上位20社の追加 | 除外 |
| --- | ---: | --- | --- |
| current-earnings-power | 2,308→2,349 | 7490 | 6862 |
| normalized-earnings-power | 1,880→1,880 | なし | なし |
| asset-value | 1,086→1,086 | なし | なし |
| reinvestment-value | 105→105 | 3843 | 9145 |

Nomination unionは77→77社。7490・3843が加わり、6862・9145が外れる。
reinvestmentの入替はP/Sのsector gap順序への波及であり、eligible条件は変えていない。
Security Analysis payloadは3,066社で変わるが、sector中央値・gap・見積り文脈への波及を含む。
「3,066社の事実を新たに発見した」という意味ではない。

## 較正文脈の再生成

valuation revision変更により、旧methodの較正cacheを再利用せず全82 cohort
（2019年11月29日〜2026年8月31日）をローカルで再構築した。
forward行1,545,775件、resolved 1,078,761件。退出や未成熟・入力不足の判定は既存ownerのままで、
このresolved件数を独立標本数とは呼ばない。完成snapshotのintegrity_checkはok、methodは全cohortで同一だった。

成績の符号・順位で月を選ばず、事前固定したintegrityと必須metricの適格性だけでscopeを決めた。
3y/5y共通17か月のrequired 34組はすべてeligibleで、evidence_completeはtrue。
全体のdiagnosticにはentry price gap、未価格退出による符号不確定、未成熟が残る。
required scopeの成立を、全82 cohort・全horizonに不確実性がないという意味に広げない。

再生成した[較正文脈](../../published/er-level-calibration-latest.yaml)は3yが40 cohort、5yが18 cohort、
共通windowが17 cohort。production hashは`dc5f248d0ea42031`、calibration method hashは`481bb041f3c6274d`。
評価全体はprivate保存し、公開contextには既存形式の分位帯・実現年率分布を生成した。
旧snapshotの変換やhash差し替えは行っていない。旧文脈とのcohort集合や入力時点も異なるため、
分布差をTTM修正の収益効果と解釈しない。

## 検証と限界

合成入力は最新の非実績開示、最新の実績欠損、TTMの3項それぞれの4field欠損訂正を検証した。
metrics直接検証は136 tests / 32 subtests成功。独立Reviewも別の合成入力、逆順入力、
真正欠損、実入力のPCFR3件を反証し、修正必須のfindingはなかった。

full local gatesはPython 2,686 passed / 8 skipped、coverage 84.86%、Ruff format/check、mypy、
import境界、全drift、Bandit、dependency auditが成功。frontend 192 tests、edge 37 tests、
lint/build/typecheck、Wrangler dry-run、npm auditも成功した。
較正文脈の更新で古い固定日が生成日前になる既存テストは、有効性確認だけ成果物の生成日へ合わせた。
未来日・期限切れの固定条件は維持し、production readerの判定を緩めていない。

今回で確認したのは事実選択の正しさと既存選定への影響であり、銘柄選択・購入・将来収益の改善ではない。
次の1作業は#1269で、追加調査前の同一通常Triage入力から現行側・固定仮説側を各最大4社選び、
固定後の和集合だけを同じ深度で調査すること。追加調査後の理由付けを優先順位改善の成功条件にしない。
