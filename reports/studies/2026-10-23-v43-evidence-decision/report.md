---
title: "v43較正の固定母集団再検証と採否"
summary: "forward v8の全差分、欠損の部分識別、取引価格と過去入力の制約を分け、戦略の数値較正への不採用を維持する。"
doc_type: report
status: active
---

# v43較正の固定母集団再検証

[Issue #1569](https://github.com/koumatsumoto/baibai-loop/issues/1569) の調査。計算修正と投資上の採用判断を分離する。
最新asofでのnormalized FY入力一致は既に確認した契約であり、歴史的リターンの精度や母集団の完全性を保証しない。

## 固定した条件と全比較

- code: `48b1d3d8cc002781973524dbdedab29a9daec106`（PR #1570 マージ後）
- release: `20261022T140937Z-9dd17236-9ebdc6c613e8`
- manifest: `fe2d703944aae35a72b49fe5fdfae7ff2c179e5c982cd39c5896cabe4f6745b4`
- 入力SQLite SHA256: `96999fc16e4be23b8e5f459b599a1449f464d14e49ccc8bc86942ede5c67edfc`
- rules: `method/screening/rules/2026-10-16T133000+0900.yaml`、`normalized-fy-input-parity-v43`
- panel hash: `1f6eaf2f3bab6514`、production hash: `f6ce6d27e3dd9103`
- 2020-01-31〜2021-07-30の元19 cohort、5 horizon、95 cohort/horizon組を保持。

旧cache契約とforward v8契約が異なるため正規fresh buildを実行した。旧cacheを新revisionへラベル変更していない。
Build 7992.51秒、evaluate 69.48秒、全差分比較はexit 0。元入力・旧cache/context/evaluation/rulesのbytesとSHAは不変。

| 対象 | 結果 |
| --- | --- |
| panel 70,166行 | 全field・metadata不変 |
| forward 350,925行 | 336,501行不変、14,424行変更 |
| 価格リターン・entry/exit・価格status等 | 全行不変 |
| 配当FY件数・配当合計・total_return | 各11,616行変更 |
| total_return_status | 14,424行変更。他fieldの変更なし |

旧 `unresolved_dividend_period_gap` 14,579行のうち、11,616行はresolved、2,808行はsplit basis未解決へ移り、155行はperiod gapが残る。
これは全5 horizonの件数。3y/5yの正式評価38組では宣言予測数131,348が不変、観測87,280→94,496、欠損44,068→36,852。
重複銘柄・重複期間を含む行数であり、独立標本数ではない。

PR #1570 は年度末entryの翌日から始まる連続FYを誤ってgapとする境界だけを修正した。
[固定HEAD CI](https://github.com/koumatsumoto/baibai-loop/actions/runs/39885448461) と固定5実例の比較を経てマージした。
内部FY欠落を埋めたり、split/rightsを安全と認定したりしていない。内部missing FY原本欠落の実例を今回確認できたとは主張しない。

## 欠損感度の定義

結果を見る前にIssueへ定義を記録した。正式contextの全宣言予測に基づく既存band境界を使用し、
各cohort × 3y/5y × q1〜q5、および重複する別枠の既存8.5%以上群を保持した。
観測済みoutcomeだけで再編するevaluatorのquintileや中央2点平均とは区別する。

母数N、観測n、欠損m=N−n、観測値昇順x、nearest-rank順位k=ceil(N/2)とする。
無借入株式の年率リターンの下限−100%、上限なしの下で、全母集団中央値の部分識別は以下。

- 下端: k≤mなら−1、そうでなければxの1始まり順位k−m。
- 上端: k>nなら上限なし、そうでなければxの1始まり順位k。

これは欠損値の推定・ゼロ補完ではなく、許容される欠損値全体に対して中央値がどこまで動き得るかの記述。
観測結果の正しさ、共通のリターン定義、分類可能な母集団を前提とする。売買不能銘柄の実行可能利益を捏造するものではない。
finite ER不足の分類不能2,888行と価格入力不足8,169行は別記し、都合のよい分母へ付け替えない。
228組の抽出でN=n+m・配列長・昇順を確認し、12個の集約bandは正式contextと一致照合した。

## 感度の結果と作者の判断

全19 cohortを連結したticker等重み行の中央値範囲（年率%）は以下。これは同じ銘柄を複数cohortに含む分布であり、独立標本・等金額ポートフォリオのリターンではない。

| band | 3y 下端〜上端 | 5y 下端〜上端 |
| --- | --- | --- |
| q1 | −13.60〜−0.11 | −10.50〜1.52 |
| q2 | −5.23〜8.31 | −2.52〜10.59 |
| q3 | 0.17〜10.80 | 2.49〜13.14 |
| q4 | 3.47〜14.36 | 5.06〜15.51 |
| q5 | 7.53〜19.45 | 7.85〜18.60 |
| 既存ER≥8.5%群（別枠・重複） | 12.78〜24.12 | 10.02〜20.75 |

欠損だけをどのように補っても集約q5中央値の下端はq1の上端より高い。順位情報が全く無いとは結論しない。
しかしcohort別に分けると、q5下端>q1上端は3yで17/19、5yで19/19。
3yの2020-02-28と2020-03-31は範囲が重なり、全局面での厳密な順序を保証しない。
中間bandの範囲も重なり、五段階の完全な単調順位や数値の精度を保証しない。

既存ER≥8.5%群の中央値下端が8.5%超なのは3yで13/19、5yで11/19。
下端が非正になり得るのは3yで2020-01・2021-05・2021-06、5yではさらに2021-07。
例えば2021-05の5y群はN=14、観測8、欠損6で、中央値範囲は約−10.43〜24.92%。
集約値だけを見て「8.5%以上ならどの時期も十分」とする結論は採らない。

これは母集団中央値の部分識別であり、期待値の較正や信頼区間ではない。予測ERの中央値と実現中央値を比較しても、
各銘柄の期待収益の正確さや将来の条件付き期待値は識別できない。今回の約18か月の起点、重複する3y/5y窓、
企業行動後の未解決対価、過去の規制・EDINET被覆、FY配当proxy、手数料・滑り・売買成立条件が別の制約として残る。

**採用するのは「この固定標本には粗い順位の研究材料がある」という限定的な観察。数値較正や買い閾値の根拠には不採用。**
有利な結果も不利な結果も残し、欠損を理由に有利な順位結果を隠すことも、順位結果を理由に品質gateを外すこともしない。

再現用の順位抽出は `cohort-extract-0.tsv`（3y）と `cohort-extract-1.tsv`（5y）。各114行、計228行。
SHA256はそれぞれ `801697e97e58dbb6599926cdd578a6f2970b696fcfab8b9eebb71e15c83fe93a`、
`bdf71749123ef44b3876ca30a4063ef96775e747d8c24327eccb66a0bb2fb51f`。
作者の集計値は `author-sensitivity-summary.json` に保存。欠損順位の範囲外は今回0件だった。

## entry価格欠損の原典照合

4 ticker/asof組を [Issueの一次資料記録](https://github.com/koumatsumoto/baibai-loop/issues/1569#issuecomment-6357134914) と照合した。
7351・2020-06-30、4011/4012・2020-09-30は新規上場後の初値未成立。7531・2020-06-30は月内最後の値付が6/10。
formal entry_price_gapは7531の3y/5yの2行であり、残り3銘柄のpanel.close欠測と区別する。
将来の初値、公募価格、気配、古い価格を当日の実行可能価格にせず、未解決を維持する。

## stale exit の追加原典確認

固定診断の10,740行中、窓内に上場廃止metadataがある10,728行は、metadataだけで清算価格・交換株式・権利・入金時期を決められない。残る12行は7銘柄・10銘柄月に集約される。月間相場表の銘柄行、日付・売買高・値付日数を作者が関連テキストで照合した。画像検証・全頁精読ではない。

| 銘柄 | 対象月/期限 | 保存exit | JPX原典での確認 | 判定 |
| --- | --- | --- | --- | --- |
| 5918 | 2025-04-30 | 04-14/6700 | 月内最終終値04-14、値付7日、5500株 | 当日値付の欠如。古い価格を実行価格にしない |
| 7531 | 2020-06-30 | 06-10/2750 | 月内最終終値06-10、値付3日、1000株 | 当日値付の欠如。取得漏れとしない |
| 3353 | 2020-11-29 | 11-10/6220 | 月内3日。始値11-04、高値11-04、安値11-10、終値11-30/6320 | 11-30は期限後。期限前の疎な値付と整合 |
| 7565 | 2021-04-29 | 04-07/2892 | 月内2日。始値04-07、終値04-30、両方2892 | 04-30は期限後。後日の同価格を期限内へ移さない |
| 5189 | 2021-08-31（2cohort） | 08-13/4030 | 月内最終終値08-13、値付2日、300株 | 古い価格を期末の実行価格にしない |
| 5189 | 2021-10-31 | 10-15/3830 | 月内最終終値10-15、値付4日、700株 | 同上 |
| 5918 | 2022-01-29/30（2cohort） | 01-12/6000 | 月内値付1日、01-12、300株 | 同上 |
| 7509 | 2022-02-26 | 02-08/3415 | 月内3日。始値02-01、高値02-01、安値02-08、終値02-28/3480 | 02-28は期限後。後日の価格を補完しない |
| 5918 | 2022-04-30 | 04-14/6370 | 月内最終終値04-14、値付2日、200株 | 同上 |
| 9647 | 2022-05-31 | 05-11/3095 | 月内最終終値05-11、値付2日、400株 | 同上 |

資料：
- 2025-04: https://www.jpx.co.jp/markets/statistics-equities/price/um3qrc000001g4yw-att/st_202504.pdf p.15
- 2020-06: https://www.jpx.co.jp/markets/statistics-equities/price/nlsgeu000004ttjj-att/st_202006.pdf p.23
- 2020-11: https://www.jpx.co.jp/markets/statistics-equities/price/nlsgeu0000055nh5-att/st_202011.pdf p.6
- 2021-04: https://www.jpx.co.jp/markets/statistics-equities/price/nlsgeu000005jmz2-att/st_202104.pdf p.23
- 2021-08: https://www.jpx.co.jp/markets/statistics-equities/price/nlsgeu000005s6d8-att/st_202108.pdf p.14
- 2021-10: https://www.jpx.co.jp/markets/statistics-equities/price/nlsgeu000005zhmf-att/st_202110.pdf p.14
- 2022-01: https://www.jpx.co.jp/markets/statistics-equities/price/nlsgeu0000065y08-att/st_202201.pdf p.16
- 2022-02: https://www.jpx.co.jp/markets/statistics-equities/price/nlsgeu00000680fu-att/st_202202.pdf p.23
- 2022-04: https://www.jpx.co.jp/markets/statistics-equities/price/nlsgeu000006edas-att/st_202204.pdf p.16
- 2022-05: https://www.jpx.co.jp/markets/statistics-equities/price/nlsgeu000006g7x8-att/st_202205.pdf p.31

これは12行の原因照合であり、10,728行の上場廃止対価経路を全件復元したものではない。価格制約を緩和せず、未解決を残す。薄商いを制度上の売買停止と断定しない。


## 過去入力を回復する場合の現実的な境界

- **5FY**: 初期2020年1〜4月の5FY非nullは各2銘柄。現在の保存期間だけを再取得しても早期の5年系列は増えない。
  [J-Quantsデータ仕様](https://jpx-jquants.com/ja/spec/data-spec)では財務summary最古日は2008-07-07、
  [現行プラン](https://jpx-jquants.com/ja?lang=ja)はLight5年1650円/月、Standard10年3300円/月、Premium20年16500円/月（税込）。
  2026年から10年では早期2020年の5年分を常に満たせない。契約の実権限とticker別原本被覆は未確認であり、追加契約や課金をしていない。
  20年契約でも全銘柄5FYや訂正履歴の完全性を保証しない。
- **EDINET**: 全19 cohortの既存EDINET軸非nullは0。これは当時資料が存在しない証明ではなく、固定入力に有効な履歴がないという結果。
  [API v2仕様](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf)の縦覧延長では有報等は10年。
  2019年公表有報の取得と当時比較表を利用する経路は候補になるが、後年訂正・taxonomy・期間/連単・株式basisを追跡する必要がある。
  取得可否・保存被覆とparser適用を有限pilotで確認する前に、現在の値で過去軸を埋めない。
- **JPX規制**: 全19 cohortのunavailableは規制日次snapshotの不足。上場廃止eventの保有とは別。
  [指定履歴](https://www.jpx.co.jp/listing/measures/alert/01.html)は2019〜2023年の特別注意銘柄履歴等を参照できるが、
  それだけで特別注意・整理・取引停止・上場廃止警告の全必要flagを各過去日へ復元できない。
  月間相場表の属性や現在の空リストを、当時「規制なし」の証明へ変換しない。

無料公開原本での有限pilotが先。金融商品購入・追加課金・認証拡張は今回行わない。
必要工数はデータ件数・parser被覆が未確認のため精密な時間見積りを捏造しない。
単純な再buildでは解決せず、原本受領履歴・企業行動の対価/権利とPIT入力復元が別の実装/データ取得テーマになる。

## 採否と運用

新context生成日時は `2026-10-23T13:14:28+09:00`、SHA256は
`77de2e3d7a6f84a7d336ba58bc99e5c7f40b9342ad0a73c1e714c93bbdf1f6f3`。
`evidence_complete=false`、`integrity_blocked`、38組の `total_return_missingness_unmeasured:er_level_calibration`、
2020-06-30の3y/5yの `entry_price_gap` を維持する。外部の手計算感度でformal gateを書き換えない。
生成日を10月21日へ書き換えず、本番R2・旧公開contextは変更しない。

観測済み分布は研究上の記述として保持するが、期待リターンの数値較正、買い閾値の最適化、
現在の戦略優位性や将来収益の保証には採用しない。価格の対価経路と過去入力を回復するまで、
欠損を落としてgreenにする、現値・翌日価格を入れる、配当をゼロにする、失敗cohortを削除する案は不採用。


## 証跡と読取範囲

全比較は専用環境の固定入力/cacheへ対する機械処理で、作者は抽出結果・固定コード・原典・統計定義を照合した。
比較JSON SHAは `b55c44d18796061b583448c714c8ef904428bac0eb1e6d149dc0b1e1b4a74f42`、
band配列JSON SHAは `39fb142c71f27a438fc28c19b5f3b6a964552df42c23e08f92bcf09b79cca5b9`。
元配列全体・全変更行を作者が一行ずつ精読したとの主張ではない。
大きな原成果物のLibrary転送は認証401で停止し、別経路で認証を回避していない。
本報告に使う順位抽出は元配列と機械照合したTSVを受領し、作者側で行数・SHAを照合して計算した。
通常CLIと算術分析であり、ローカルtestsuiteは実行していない。
