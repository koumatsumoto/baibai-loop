---
title: "Screening runtime reference"
summary: "Stock Analysis、candidate context、単一runの保存・読取境界の正本。"
doc_type: reference
status: active
---

# Screening runtime

## 役割

screeningはValuationの財務/倍率計算・機械推計とMarket Contextの価格/需給結果を全対象のStock Analysisへ固定する。同じrunで候補文脈を確定し、`candidate.reasons`が空でない行を候補として表示する。独立した候補一覧のID・table・発行時刻・後付け再発行は持たない。候補外も分析済みであり、Researchの企業評価を禁止する資格ではない。

候補は4つのValuation Approachそれぞれのtop20のunionである。機械E[r]/FV anchor、Market Context Assessment、portfolio stateは候補membership・順位に使わない。機械的な候補理由はApproach/Method/rankを示し、AIがResearch対象を選ぶ理由とは分ける。ER/FVは[Valuation metrics](./valuation-metrics.md#machine-valuation)、Researchの対象選定は[Research](../../.agents/skills/research/SKILL.md)を参照する。

<a id="rule-purpose"></a>

## ルールの目的と根拠

Screeningは[入力scope](#input-scope)でStock Analysisの計算対象を定め、その計算結果に[共通factの資格](#history-context)を適用してCandidate Discovery・比較中央値の対象を選び、[4 Approach](#approach-applicability)それぞれの条件と指標から候補を推薦する。[品質context](#data-quality-context)はrunの利用判断、[候補容量](#nomination-capacity)は有限の探索枠であり、企業の経済的rejectや購入条件ではない。観測・取得の失敗は[情報源の状態](./data-sources.md)で扱い、未観測を0や対象なしへ変えない。

数値と判定式は[現行rules](../../method/screening/rules/)、[`CommonEligibilityRules.matches`](../../engine/src/baibai_engine/screening/rule_config.py)、[候補文脈生成](../../engine/src/baibai_engine/screening/discovery/candidates.py)、[業種中央値の実装](../../engine/src/baibai_engine/valuation/metrics/relative.py)が所有する。業種母数の理由は[valuation metrics](./valuation-metrics.md#median-population-rationale)、推薦集合と資格の回帰は[候補回帰](../../tests/engine/test_candidate_discovery.py)を参照する。算術・保存契約の再現は閾値の収益上の最適性を示さない。

Researchの対象選択、企業評価、資本配分、保有判断へ進む関係は[判断条件の見取り図](../README.md#判断条件の見取り図)から各責務の正本へ辿る。2026-10-13の固定断面・部分感度など当時の測定は[#1481の点検記録](https://github.com/koumatsumoto/baibai-loop/issues/1481#issuecomment-6160691018)と[#1517](https://github.com/koumatsumoto/baibai-loop/pull/1517)に残し、DPS欄追加前のentry payload一致という時点の限定は[#1527](https://github.com/koumatsumoto/baibai-loop/pull/1527)を参照する。現行判断と未測定の進捗は[#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481)が管理する。

## Public CLIと単一保存

公開optionは`baibai-engine screening --help`と各commandの`--help`を正本とする。`run`は丸め済みの全分析から候補文脈を一回計算し、run headerと全Stock Analysisを一つのSQLite transactionで保存する。呼出側は明示rulesを渡し、そのrules hashと入力headerの一致をwriterが確認する。caller作成の候補や保存済み候補の再発行は受け付けない。

`run`のexit 2は保存済みrunのpartial warningである。出力のrun revisionと候補件数を確認する。`--output-path`へはDBから読戻した確定revisionをexportし、値を再丸め・再計算しない。DB成功後にfile exportが失敗した場合は診断中のrevisionを使って回収する。

```bash
uv run baibai-engine screening run-show --run-revision-id run-revision-0123456789abcdef0123456789abcdef --format yaml
uv run baibai-engine screening candidates show --run-revision-id run-revision-0123456789abcdef0123456789abcdef --format json
```

どちらも保存済みrevisionのread-only出力で、`--output-path`・`--force`は出力fileだけに作用する。未知revision・破損storeは失敗、候補0件は正常結果である。独立した候補一覧を発行するCLIは持たない。

## Stock Analysis

個別分析のbuild直後だけ`candidate=None`を許す。writerは全行に確定contextを付け、headerにmethod/diagnosticsを一度だけ保存する。通常readerは候補欠損を非候補へ補完しない。候補のみ・ticker filter・paginationの前に、対象run全体のcandidate shapeを確認する。

`candidate.common_eligible`は比較母集団への資格、`reasons`はApproach順の候補理由、`normalized_per_3fy_sector_gap`はcommoneligible母集団で計算した値である。候補容量外でもcommoneligibleになり得る。gapのnullは計算不能であり、既知の0とは異なる。一般市場metricsの同名値へ差替えない。

Stock Analysisは`observed / derived / estimate`を混同しない。欠損を0へ補完せず、解釈・因果・売買判断を書かない。field・table・schema versionはengine modelとDB schemaが所有する。

## Candidate Discovery

<a id="input-scope"></a>

Stock Analysisは普通株・対象市場・as-of以前の有効価格barが1本以上ある銘柄を評価対象とする。価格barが0本、最新終値またはas-of基準の最新価格が非正・非有限な入力は評価対象にしない。sourceの分割factor `0` はイベントなしを示し、終値以前・当日・以後のどの位置でも銘柄除外の理由にしない。factorを適用するのは価格日より後・as-of以前のイベントだけである。平均売買代金は20barすべての売買代金が揃う場合だけ計算し、足りなければnullにする。株数・現在価格・分割basisの欠測を補完して候補にしない。`listing_span_days`は最初の利用可能barからの期間proxyであり正式な上場日ではない。`price_history_sessions_750d`と`price_history_coverage_750d`は短い履歴・疎な履歴の品質contextとして残す。required-field preflightとrepairの資本判定は、評価計算と同じ会計期間・文書revision・株式basis resolverを使う。同日の別文書から発行済と自己株を合成しない。cache coverageのrequired-field 75%とbar・財務密度は、as-of masterの対象市場かつ東証33業種の普通株を母集団とし、価格・財務が欠けた普通株も分母に残す。対象市場の未知業種は分類を更新するまで停止する。各指標の入力期間・TTM・株式basisは[Valuation metrics](./valuation-metrics.md)に従い、productionと較正replayで共有する。入力不足はnullや品質contextとして残し、実績・予測・観測期間を混ぜて穴埋めしない。

<a id="history-context"></a>

共通eligibilityは正の有限な時価総額fact、非負で有限な価格barの期間、JPX flag、これら必須factの有無を扱う。現行rulesの時価総額・bar期間floorは0で、100億円未満や182日未満も候補発見と業種・市場の比較母集団に含める。bar期間は最古の利用可能な価格barからの日数proxyで、実上場日や財務履歴ではない。短い履歴、サイズ、ADVはcontextとしてStock AnalysisとResearch入力へ残し、各指標に必要な履歴・財務入力とJPX制約は保つ。ADVは値が低い場合も欠損時も除外に使わない。その後、各approachが独立にnominateする。採用理由は[#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481)、現行値は[screening rules](../../method/screening/rules/)、完全なeligibility・並び順・method hashは[候補実装](../../engine/src/baibai_engine/screening/discovery/candidates.py)を正本とする。

<a id="approach-applicability"></a>

| valuation approach | 主座標・適用する理由 |
| --- | --- |
| `current-earnings-power` | 正のcurrent PERとsector gapで、現在利益の持続性と業種割安の理由を問う。CFは従キーと裏付けのcontextで、一過性利益はResearchで反証する |
| `normalized-earnings-power` | 3FY normalized PERのsector gapで、一期間の異常から離れた利益力を問う。株式basisと連続FYを揃え、事業構造の変化はResearchで確認する。[先行測定](../../reports/studies/2026-08-02-normalized-per-production/report.md)があるが、3FYを全社共通の調査資格にはしない |
| `asset-value` | 正のasset-backed ratioから、株主へ実現する資産の拘束・換金可能性を問う。非金融のasset proxyを金融資産・資金調達そのものへ適用しない |
| `reinvestment-value` | 割安なsector-relative P/Sと相対的な収益性で、売上成長が価値へ変わるかを問う。FCFの符号だけで投資回収や資金余力を認定しない |

Asset Valueは正のasset-backed ratioを持つ非金融企業を対象とする。金融業ではcash、debt、securitiesが事業上のasset / fundingそのものなので、非金融企業向けgross asset proxyを残余価値として適用しない。net cashは比較文脈として残し、候補の採否には使わない。

Reinvestment Valueはsector-relative P/Sの割安さと、利益率・capital returnの質を組み合わせる。sector母数が薄い場合はmarket medianを使う。capital returnは`operating_profit_ttm / (same_state_equity_yen + debt - cash)`で、自己資本はJ-Quantsの同一実績行、debt/cashは選択したEDINET recordに基づく。両sourceのBS日一致を保証する厳密ROICではない。TTM営業利益が作れなければ累計利益の年率換算や別の利益で補わない。株式・会計basisは[valuation metrics](./valuation-metrics.md#51-資本の分母)に従う。

FCF yieldは有限な既知値を必須とし、0・負値も推薦対象にする。既存の利益率・capital-return floorは維持し、FCFは順位の末尾の従キーで高い順に比較する。負FCFを成長投資と認定せず、欠測を0へ補完しない。ResearchはCFのsource・品質・期間、維持投資/成長投資・運転資金・回収・資金余力を確認する。半期の`approximated`値や書類metadataの年度末を、通年実績CFや測定期間へ読み替えない。

Reinvestmentの選抜条件と、Stock Analysisの表示観測は分ける。他Approachで推薦された銘柄も、既知のP/S gap・売上YoY・FCF yield・利益率・capital-return proxyを項目ごとに保持し、負値や0を欠測へ変えない。比率は必要入力が揃い分母が正の場合だけ計算し、計算不能な項目はnullとする。過去の保存snapshotがグループ全体をnullとしていた場合、その欠落を現在値で補わない。

primary coordinateがnull、非有限、またはapproachの要件を満たさない銘柄は、そのapproachでnominateしない。金融sector除外、quality threshold、median population / fallback、従キー、tickerまでmethod hashに含め、同じStock Analysisとrulesから同じ結果を再構成する。

## 候補の投影とidentity

<a id="nomination-capacity"></a>

各Approach top20は有限の発見枠で、rank21以下を経済的rejectにしない。全Approachで重複するtickerもStock Analysisは一行だけで、全候補理由を同じ行に保持する。候補投影は元のraw rowの値を変更せず、ticker昇順を非経済的なcanonical順として使う。総合rank・Research priorityは作らない。

候補純関数は丸め済み行をproductionと較正で共有する。normalized gapの母集団、reinvestment qualityのresolved母集団・sector minimum・market fallback、tie-breakはmethod契約で固定する。既存method hashのpreimageに残る`review_set_membership`等と`nomination_depth`はアルゴリズムidentityの凍結wireであり、旧publicationの存続ではない。候補正本の統合だけではproduction rules hash、ER model version、Valuation calculation revisionを変更しない。

exact読取は保存された値・methodを返し、現在rulesを読み直さない。旧rules fileを指定した新しい計算を、過去の算式・当時値の再現とは扱わない。旧run cacheの退避と現行schemaへの切替は[Research domainの切替](../../batch/OPERATIONS.md#research-domain-cutover)で完了している。旧schemaを通常readerで読み替えたり、旧cacheを現行正本へ戻したりしない。今後のschema変更は[明示的な切替手順](../../batch/OPERATIONS.md#schema-cutoverを修正するとき)に従い、必要な評価・提案・保存入力の根拠を保持する。

較正cohort/forward rows/ER artifactはrun cacheと別の測定正本であり、run cacheの切替で再生成・削除しない。`PanelRow.in_review_set`、`valuation_approaches`、`valuation_approach_ranks`等は既存測定wireとして保持する。その値は同じ純関数の候補理由から導出する。[較正](./estimate-calibration.md)のidentity一致条件を保つ。

<a id="data-quality-context"></a>

## 品質context

TTM非exactと業績YoY欠測の件数・比率はrunのpartial warningを決める。書込済み成果物の品質を説明するもので、会社を一律除外するgateではない。指標を候補選抜へ使えるかは各Approachの必要入力で決める。旧`yoy_deterioration_threshold`は現行rulesから除き、較正の業績変化による[診断群](./estimate-calibration.md#diagnostic-thresholds)とは分ける。

## Daily batchと読取

日次はL1取得・L2の全分析と候補文脈を進め、同じrevisionから一世代のWeb viewsを生成する。Screening不足・失敗から独立したindicator refresh、日本休日のScreening skip、commit済みL1 chunkの保持は既存運用を維持する。[batch運用](../../batch/OPERATIONS.md)を参照する。

最新runに候補が0件でも古い候補runへfallbackしない。前runが無い最初の新cacheでは日次差分を未測定とし、次の自然なrunから比較する。候補entry/exitはmethod hashとscreening rules hashが一致する場合に比較し、E[r]差分だけはER model versionの一致も要求する。候補が空のrun同士も正常に比較できる。

read ownerの`analysis_run`はResearch日以下のanalysisと`known_at`以下のrun_atを同じread transactionで選ぶ。explicitの未知/未来revisionを最新runへ差替えない。日付labelはrevision identityではなく、同日再計算は新run_atと新UUIDを持つ。保存されたResearch入力からの再開はrun pruneに依存しない。

## Pruneと履歴

`prune --keep N`は新しいrunをN世代残し、子のStock Analysisを同一transactionで削除して`VACUUM`する。候補tableは無い。評価・提案・保存Research入力はapplicationの正本であり、run pruneと分ける。候補正本の統合では既存retentionを変更しない。

市場・業種・個別相対の算術とcutoffは[Market Context読取](./market-context.md#market-context-reads)が所有する。Screeningは既存の比較母集団を渡し、ValuationのER/FVや4 Approachの候補式へ環境scoreを追加しない。

## Market store inputs

価格、財務、銘柄master、calendar、信用・空売り、JPX規制、EDINET、上場廃止・現金公開買付けとcoverageを読む。現行table/columnはschema、lake datasetは`baibai-engine lake inventory`を参照する。

値の意味は[valuation metrics](./valuation-metrics.md)、sourceの選択は[data sources](./data-sources.md)、PIT・coverage・hydrateは[market lake](./market-lake.md)に従う。

<a id="jpx-earnings-calendar"></a>

[JPXの決算予定](https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html)は、3月期・9月期決算会社の翌営業日分の日次資料と、期末・四半期末を迎えた月別資料で対象が異なる。日次資料は予定なしならリンクが掲載されないことがあり、欠落だけでlayout障害と判定しない。月別資料は翌月上旬掲載後に更新される。ファイル名の日付、本文の基準日、取得時刻は別であり、現在取得した資料だけで過去時点の完全履歴を再現できるとは扱わない。[予定変更後が未決なら「未定」と登録する](https://faq.jpx.co.jp/disclo/tse/web/knowledge7701.html)ため、未定は単なる不正日付ではない。ファイル間の優先順位はprovider側の解決方針であり、JPX公式の優先規則と説明しない。 同じ銘柄の未定・判読不能・矛盾は統合まで保持し、上位の資料で日付が未解決なら下位の古い日付で補わない。同順位の資料が食い違う場合もURL順で日付を決めず、その銘柄の予定は不明とする。他銘柄の既知予定は残す。

JPX決算calendarは銘柄ごとの次回決算日の注記だが、snapshotの取得失敗は現行`run`全体を保存前に止める。個別銘柄にcalendar行がないことと同じ停止条件ではない。予定日当日・経過後の状態は`scheduled_date_reached`であり、発表確認を意味しない。保存済みの旧`announced`もWeb APIの投影とResearch入力では同じ意味として扱い、exact読取と元snapshotは保全する（[#1507](https://github.com/koumatsumoto/baibai-loop/issues/1507)）。EDINETのCSV抽出にhard failureが1件でもあると前処理snapshotを書かず、dailyもScreeningを進めない。いずれも取得・成果物の完全性に関する現行運用境界であり、銘柄の経済的な除外根拠や停止による機会損失の実測ではない。

J-Quantsのread取得は[provider](../../engine/src/baibai_engine/market/providers/jquants.py)が生成するClientV2のHTTPS adapterで再試行を所有する。単一GETは初回＋最大5回で、429と既存の一時HTTP失敗を対象にする。有効な`Retry-After`の秒数／HTTP-dateを優先し、無いか不正なら30・60・120・300・600秒で待つ。認証・403・解析失敗を再試行せず、SDKのページ・日付range全体も再実行しない。従来の成功chunk保存・cacheからの再開を維持する。job残時間をproviderへ渡す経路は現行に無く、残時間による待機前終了は未実装である。永続cooldownや他providerのretryはこの契約に含めない。

## 検証

候補集合・rank・母集団・丸め・method/rules/model identityと、同じ入力のPanelRow全値を旧commitの合成goldenへ照合する。保存では全行context、空分析/空候補、rollback、完全冪等、rules mismatch、日時の内容Conflictを確認する。readはexact revision・同日複数・Research cutoffと、破損をfilterで隠さない経路を検証する。

最終consumer treeのCLI、Research、Web/batch、生成contracts、materialize、frontendは[対象HEADのCIと必要な実store検証](./python-foundation.md#9-ci-and-local-parity)で確認する。fixture成功を実store検証・provider読取・本番切替成功と読み替えない。
