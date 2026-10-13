---
title: "Screening runtime reference"
summary: "Security Analysis、current Candidate Discovery Method、Review Setの実行境界の正本。"
doc_type: reference
status: active
---

# Screening runtime

## 役割

screeningはValuationの財務/倍率計算・機械推計とMarket Contextの価格/需給結果を組み込み、全対象銘柄のobserved / derived / estimateをSecurity Analysisへ固定し、configured Valuation Approachesが生成したNominationのexact unionをReview Setにする。Review Setは機械成果物であり、投資判断ではない。Research Triageだけが各entryへ`research / skip`とresearch priorityを発行する。

機械E[r]/FV anchor、Market Context Assessment、portfolio stateは参考文脈である。Review Setのnomination、membership、orderには使わない。ER/FVの推計・保存済み照会は[Valuation metrics](./valuation-metrics.md#machine-valuation)、企業固有の条件付き評価は[Thesis](./thesis.md#scenario-arithmetic)が所有する。

## Public CLI

screening runtimeの公開commandは`baibai-engine screening --help`から辿る。本書で扱う
`run`、`review-set publish / show`、`prune`のoptionのrequired / default / choiceと出力形式は各commandのpublic
`--help`を正本とする。日常運用の実行順と停止条件は
[`research-triage` skill](../../.agents/skills/research-triage/SKILL.md)と
[`batch/OPERATIONS.md`](../../batch/OPERATIONS.md)が所有する。

通常の`run`はSecurity AnalysisとReview Setを先に計算・検証し、同じSQLite connectionの一つのtransactionでrun・全子データ・Review Setを保存する。出力には両IDを含む。Review Setの挿入失敗は新runを含めてrollbackする。paired Review SetのIDとcreated_atはimmutable runのIDとrun_atに固定し、同じrunの再試行で新しい集合を作らない。

`run`のexit 2は両publication済みのpartial warningである。warningを確認してから出力の両IDで後続へ進む。明示的`review-set publish`は保存済みrunを対象にする入口として残すが、通常dailyは再発行しない。Review Setの再表示に再発行を使わない。`--run-revision-id` + `--run-at`と`--review-set-id` + `--created-at`はcallerがidentityを固定する場合のsame-ID idempotencyを提供する。daily runnerは実行場所にかかわらずserver-generated identityを使い、AI outputからIDやclockを受け取らない。

## Security Analysis

run storeはrun identityと全銘柄のSecurity Analysis、source runに束縛した
再現可能なReview Setを保持する。field、table、schema versionはengine modelとDB
schemaを正本とする。

Security Analysisは`observed / derived / estimate`を混同しない。欠損を0へ補完せず、解釈・因果・売買判断を書かない。

## Candidate Discovery

共通eligibilityは時価総額100億円以上、上場期間182日以上、JPX flag、これら必須factの有無だけを扱う。ADVは値が低い場合も欠損時も除外に使わず、Security Analysis、Review Set、Research Triage、UIへ執行可能性のcontextとして残す。その後、各approachが独立にnominateする。現行の採用値は[screening rules](../../method/screening/rules/)、完全なeligibility・並び順・method hashは[Review Set実装](../../engine/src/baibai_engine/screening/discovery/review_set.py)を正本とする。

| valuation approach | 主座標 |
| --- | --- |
| `current-earnings-power` | sector-relative current PER、current cash-flow yield |
| `normalized-earnings-power` | 3FY normalized PERのsector gap |
| `asset-value` | 正のasset-backed ratio、PBR context |
| `reinvestment-value` | 割安なsector-relative P/S、sector-relative capital-return proxy / margin、growth、FCF yield |

Asset Valueは正のasset-backed ratioを持つ非金融企業を対象とする。金融業ではcash、debt、securitiesが事業上のasset / fundingそのものなので、非金融企業向けgross asset proxyを残余価値として適用しない。net cashは比較文脈として残し、候補の採否には使わない。

Reinvestment Valueはsector-relative P/Sの割安さと、利益率・capital returnの質を組み合わせる。sector母数が薄い場合はmarket medianを使う。capital returnは`operating_profit_ttm / (same_state_equity_yen + debt - cash)`で、自己資本はJ-Quantsの同一実績行、debt/cashは選択したEDINET recordに基づく。両sourceのBS日一致を保証する厳密ROICではない。TTM営業利益が作れなければ累計利益の年率換算や別の利益で補わない。株式・会計basisは[valuation metrics](./valuation-metrics.md#51-資本の分母)に従う。

primary coordinateがnull、非有限、またはapproachの要件を満たさない銘柄は、そのapproachでnominateしない。金融sector除外、quality threshold、median population / fallback、従キー、tickerまでmethod hashに含め、同じSecurity Analysisとrulesから同じ結果を再構成する。

## Review Set

各Valuation ApproachのNominationをtickerで統合し、重複tickerは1entryへまとめ、全Nominationを保持する。各Approachのconfigured nomination depthに従う集合のunionであり、採用Approachとdepthは[現行rules](../../method/screening/rules/)を参照する。ticker昇順はbyte-equivalentなserializationのためだけに使い、global rankやresearch priorityを意味しない。

publisherはsource run、as-of、rules hash、method hash、全Security Analysisからpayloadを再計算し、不一致を拒否する。published rootは`screening_rules_hash`をprovenanceとして持つ。Review Setはapplication DBやResearch Triageを読まず、同じrun・rules・implementationから同じNomination unionを再構築するL2である。各entryはidentity、`nominations`、grouped `analysis`だけを持つ。`analysis.expected_return`はsecondary machine priorのsnapshotであり、membershipを持たない。

Normalized Earnings Powerのnative eligibility/orderは`normalized_per_3fy`と同sector gapを使う。FV/E[r] estimatorは`normalized_per_3fy`を入力にしない。

MCPは保存済みcanonical Review Setをread-onlyで参照できる。これはscreening結果の検証用read boundaryであり、Review Set membership、Nomination、Research Triage、Research Setを変更するauthorityを持たない。tool契約は[Owner MCP](../../tools/owner_mcp/README.md#triage前のreview-set)を参照する。

## Research Triage

ScreeningはL2のReview Setを発行し、Researchが全entryを判断する。Triageの保存契約・CAS・入力参照は[Research Triageの契約](./analysis-operations.md#research-triage-contract)、操作は[Research Triage skill](../../.agents/skills/research-triage/SKILL.md)が所有する。純粋なCandidate型と`PublishedReviewSet`は`screening/discovery/contracts`、Nominationとunionの生成は`screening/discovery/review_set`に分ける。package initから計算をeager importしない。

## Daily batchとlocal analysis

daily runnerはcloud scheduleまたは明示的なlocal実行でL1 / L2 machine処理だけを行い、同じcanonical runs storeを進める。

```text
screening run (run + Review Set atomic commit) + independent indicators
    -> web materialize once -> existing publish path
```

Screeningの必要入力不足・実行失敗から指標refreshを独立させ、日本休日はScreeningだけをskipする。検証してcommitしたL1 chunkは後続失敗でも保持する。公開可能なstoreから全viewsを一度生成し、Screening未更新時はhistoryの生成/uploadとrun pruneを省く。現在画面とmetaは同じoperative runを参照し、過去の単独run・直接ID照会・cutoff付きValuationは引き続き読める。物理store/schemaやcron頻度を変えない。

cloudからL3 Research Triageを発行しない。local `baibai-batch analysis run`はpull済みruns storeから対象`as_of`のlatest published Review Setを読み、必要な場合だけReview Set全体を原則1 requestで分類してResearch Triageを発行する。対象日にReview Setが無ければ前営業日へfallbackしない。有限選択とResearchは明示ユーザー開始で進め、提案完成と最終採否は分ける。旧章立てreport、migration、全期間再取得はどちらの日次経路にも載せない。

## Pruneと履歴

`prune --keep N`は新しいrunをN世代残し、子のSecurity AnalysisとReview Setを同一transactionで削除して`VACUUM`する。Research Triage、thesis、Capital Allocation Assessment、Position Reviewはapplication DBのcanonical judgmentなのでrun pruneから独立する。

市場・業種・個別相対の算術とcutoffは[Market Context読取](./market-context.md#market-context-reads)が所有する。Screeningは業種平均対市場平均に既存の比較母集団を渡し、ValuationのER/FVや4 Approachの推薦式へ環境scoreを追加しない。

## Market store inputs

価格、財務、銘柄master、calendar、信用・空売り、JPX規制、EDINET、上場廃止・現金公開買付けとcoverageを読む。現行table/columnはschema、lake datasetは`baibai-engine lake inventory`を参照する。

値の意味は[valuation metrics](./valuation-metrics.md)、sourceの選択は[data sources](./data-sources.md)、PIT・coverage・hydrateは[market lake](./market-lake.md)に従う。

J-Quantsのread取得は[provider](../../engine/src/baibai_engine/market/providers/jquants.py)が生成するClientV2のHTTPS adapterで再試行を所有する。単一GETは初回＋最大5回で、429と既存の一時HTTP失敗を対象にする。有効な`Retry-After`の秒数／HTTP-dateを優先し、無いか不正なら30・60・120・300・600秒で待つ。認証・403・解析失敗を再試行せず、SDKのページ・日付range全体も再実行しない。従来の成功chunk保存・cacheからの再開を維持する。job残時間をproviderへ渡す経路は現行に無く、残時間による待機前終了は未実装である。永続cooldownや他providerのretryはこの契約に含めない。

## 検証

- rules/method hash一致
- 同一入力でReview Setがbyte-equivalent
- E[r]だけを変更してもmembership/order不変
- Research Triageの全entry一致、priority、rules/method identity、expected prior ID
- 対象日のReview Setなし、空Review Set、exact既存Triageでmodel process 0。active Operationはdaily Triageを止めない
- 通常Review Setは共有Market Context Assessment projectionを1回だけ含む1 AI request、invalid resultはcanonical write 0
- Triage publishではOperation 0。ユーザーの明示開始と固定した新規/保有集合（0件も含む）でOperation 1
- run storeとapplication DBがcurrent schemaに一致
- Web/APIがReview Set、Research Triage、Capital Allocation Assessmentを同じbindingで表示
