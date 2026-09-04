---
title: "Screening runtime reference"
summary: "Security Analysis、4つの価値評価法、Review Set、Research Triageの安定した意味と実行境界の正本。"
doc_type: reference
status: active
---

# Screening runtime

## 役割

screeningは全対象銘柄のobserved / derived / estimateをSecurity Analysisへ固定し、4つの価値評価法から有限のReview Setを作る。Review Setは各Approachのtop20 Nominationを失わずAIへ渡す機械成果物であり、投資判断ではない。Research Triageだけが各entryへ`research / skip`とresearch priorityを発行する。

E[r]、FV、macro context、portfolio stateは参考文脈である。Review Setのnomination、membership、orderには使わない。

## Public CLI

screening runtimeの公開commandは`baibai-engine screening --help`から辿る。本書で扱う
`run`、`review-set publish / show`、`research-triage publish`、`prune`のoptionのrequired / default / choiceと出力形式は各commandのpublic
`--help`を正本とする。日常運用の実行順と停止条件は
[`research-triage` skill](../../.agents/skills/research-triage/SKILL.md)と
[`batch/OPERATIONS.md`](../../batch/OPERATIONS.md)が所有する。

`run`のexit 2はpublication済みpartial warningである。warningを確認してから同じ`run_revision_id`で後続へ進む。Review Setの再表示に再発行を使わない。`--run-revision-id` + `--run-at`と`--review-set-id` + `--created-at`はcallerがidentityを固定する場合のsame-ID idempotencyを提供する。daily runnerは実行場所にかかわらずserver-generated identityを使い、AI outputからIDやclockを受け取らない。

## Security Analysis

run storeはrun identityと全銘柄のSecurity Analysis、source runに束縛した
再現可能なReview Setを保持する。field、table、schema versionはengine modelとDB
schemaを正本とする。

Security Analysisは`observed / derived / estimate`を混同しない。欠損を0へ補完せず、解釈・因果・売買判断を書かない。

## Candidate Discovery

共通eligibilityは時価総額100億円以上、上場期間182日以上、JPX flag、これら必須factの有無だけを扱う。ADVは値が低い場合も欠損時も除外に使わず、Security Analysis、Review Set、Research Triage、UIへ執行可能性のcontextとして残す。その後、各approachが独立に上位20件をnominateする。

| valuation approach | 主座標 | target |
| --- | --- | ---: |
| `current-earnings-power` | sector-relative current PER、current cash-flow yield | 20 |
| `normalized-earnings-power` | 3FY normalized PERのsector gap | 20 |
| `asset-value` | 正のasset-backed ratio、PBR context | 20 |
| `reinvestment-value` | 割安なsector-relative P/S、sector-relative capital-return proxy / margin、growth、FCF yield | 20 |

Asset Valueは`asset_backed_ratio > 0`だけをnative eligibilityとし、銀行業、保険業、その他金融業、証券・商品先物取引業を除外する。金融業ではcash、debt、securitiesが事業上のasset / fundingそのものなので、非金融企業向けgross asset proxyを残余価値として適用しない。`net_cash_to_market_cap`は`analysis.asset_value`へ残すが、eligibilityとorderには使わない。orderはasset-backed ratio降順、PBR sector gap昇順、PBR昇順、equity ratio降順、ticker昇順である。

Reinvestment Valueは正のP/S、sales growth、FCF yield、operating profit、sales、assets、equity ratioと、有限のdebt / cashを要求する。さらに`p_s_sector_gap < 0`、`operating_margin`と`operating_return_on_capital_proxy`がそれぞれ同じsector 33のmedian以上であることを要求する。median populationは、この共通eligible母集団のうち既存のReinvestment入力がすべて成立する行である。sector母数が`MIN_SECTOR_MEDIAN_POPULATION`未満の場合だけ同じpopulationのmarket medianへfallbackし、境界件数はsector medianを使う。通過後のorderはP/S gap昇順、capital return降順、sales growth降順、margin降順、FCF yield降順、ticker昇順である。

primary coordinateがnull、非有限、またはapproachの要件を満たさない銘柄は、そのapproachでnominateしない。金融sector除外、quality threshold、median population / fallback、従キー、tickerまでmethod hashに含め、同じSecurity Analysisとrulesから同じ結果を再構成する。

## Review Set

各Valuation Approachは独立に上位20件をNominateする。Review Setはそのticker unionそのもので、重複tickerは1entryへまとめ、全Nominationを保持する。最大件数は`4 × 20 = 80`である。ticker昇順はbyte-equivalentなserializationのためだけに使い、global rankやresearch priorityを意味しない。

publisherはsource run、as-of、rules hash、method hash、全Security Analysisからpayloadを再計算し、不一致を拒否する。published rootは`screening_rules_hash`をprovenanceとして持つ。Review Setはapplication DBやResearch Triageを読まず、同じrun・rules・implementationから同じNomination unionを再構築するL2である。各entryはidentity、`nominations`、grouped `analysis`だけを持つ。`analysis.expected_return`はsecondary machine priorのsnapshotであり、membershipを持たない。

Normalized Earnings Powerのnative eligibility/orderは`normalized_per_3fy`と同sector gapを使う。FV/E[r] estimatorは`normalized_per_3fy`を入力にしない。

## Research Triage

application DBのcurrent `research_triage`はReview Set全entryをexactly onceで保持する。
current writerとactive read pathはcurrent schemaだけを扱い、retired shapeを補完・別名投影しない。

- `research`: contiguousな`priority`、`rationale`、`research_question`、`key_risk`が必須
- `skip`: `rationale`が必須で、`priority`、`research_question`、`key_risk`は禁止

manual scaffoldはReview Set IDからrun storeのcanonical publicationを解決し、Review Setのas-of以下で最新のMacro Contextと、application DBのlatest current Triage IDを取得する。通常の`analysis run`はeditable scaffoldを介さず、strictなjudgment fieldから同じdomain objectを組み立てる。publisherは`review_set_id`、`run_revision_id`、`as_of`、`screening_rules_hash`、Candidate Discovery method、全tickerを検証し、`candidate_snapshot`をsource Review Setから上書きする。snapshotはidentity、`nominations`、grouped `analysis`だけを持つ。

global headは`as_of DESC, julianday(published_at) DESC, research_triage_id DESC`の実時刻total orderで決める。`expected_prior_research_triage_id`はpublish transaction内でこのheadとCASする。新規publicationは`as_of`を後退させず、awareな`published_at`を現headより進め、未来時刻またはJST換算日が`as_of`より前の時刻を使わない。同じIDと同じcanonical payloadの再送だけは、後続headの有無にかかわらず冪等に成功する。これにより全てのnon-idempotent publicationが新headになり、同じpriorから分岐したdraftを拒否する。

Contextが無ければ`null`は正常、古ければwarningであり、どちらもReview Setのnomination、membership、orderを変えない。eligible Contextがあるのに`null`は拒否する。明示的に古いeligible revisionを選ぶことはできるが、選択理由を既存の判断文へ残す。発行後payloadはimmutableである。`rationale`、および非`null`の`research_question` / `key_risk`は空白だけの値を拒否するが、検証時に前後空白を書き換えない。

ADVは固定floorのgateや自動skip条件ではない。Triageは低値・欠損だけで`skip`やpriorityを決めず、価値仮説を比較した後の実行可能性contextとしてrationaleへ反映できる。最終的な注文可否と数量はCapital Allocation後のhuman executionが所有する。

Research TriageはResearch Setのadmission可能範囲を定める。人間は`research` entryの部分集合だけをResearch Setとして確定できる。`research prepare --research-triage-id ... --ticker ...`はas-ofと比較snapshotをcanonical payloadから導出し、選択集合をmanifestへ固定し、そのnon-empty集合のResearch開始Operationを同時に作る。空集合はOperationなしの正常結果である。Review Set fileやrun storeは要求しない。status・scaffold・promoteを含む各gateがapplication DBを再読してpayload hashとresearchable ticker集合を検証し、workspaceへの後書きadmissionを拒否する。

E[r] calibration contextは共有read contractがartifact schema、generated/expiry、3y/5y、quantile/band、rules hash、E[r] model versionを検証する。ResearchはTriage rootのrules hashとcandidate `analysis.expected_return`のmodel/version・ratio-valued `er_annual`を使い、利用不能理由を明示する。これはhistorical contextで、membership/order、Triage、FV、buy judgmentへ伝播しない。

## Daily batchとlocal analysis

daily runnerはcloud scheduleまたは明示的なlocal実行でL1 / L2 machine処理だけを行い、同じcanonical runs storeを進める。

```text
screening run -> review-set publish -> web materialize -> publish
```

cloudからL3 Research Triageを発行しない。local `baibai-batch analysis run`はpull済みruns storeから対象`as_of`のlatest published Review Setを読み、必要な場合だけReview Set全体を原則1 requestで分類してResearch Triageを発行する。対象日にReview Setが無ければ前営業日へfallbackしない。Research Set確定とCapital Allocation Assessmentは人間gateの後に残す。full-depth Macro Context、migration、全期間再取得はどちらの日次経路にも載せない。

## Pruneと履歴

`prune --keep N`は新しいrunをN世代残し、子のSecurity AnalysisとReview Setを同一transactionで削除して`VACUUM`する。Research Triage、thesis、Capital Allocation Assessment、Position Reviewはapplication DBのcanonical judgmentなのでrun pruneから独立する。

## Market store inputs

screeningはmarket storeの価格、財務、銘柄master、calendar、信用・空売り、JPX規制、
EDINET、上場廃止・現金公開買付けのPIT入力とcoverageを読む。以下のtable名は
inputの読み先を示す。columnと現行layoutはDB schema、lake datasetのinventoryは
`baibai-engine lake inventory`、値の意味、PIT、coverage、source authorityは
[data-sources.md](./data-sources.md)と[valuation-metrics.md](./valuation-metrics.md)を正本とする。

- `jquants_daily_bars()`
- `jquants_fin_summaries()`
- `jquants_master_snapshots()`
- `jpx_earnings_calendar()`
- `jquants_market_calendar()`
- `jquants_weekly_margin()`
- `jquants_margin_alerts()`
- `jquants_all_issues_daily_margin()`
- `jquants_short_sale_reports()`
- `jpx_regulation_sources()`
- `jpx_regulation_flags()`
- `edinet_document_lists()`
- `edinet_documents()`
- `edinet_metrics()`
- `tse_capital_policy_snapshots()`
- `jpx_delistings()`
- `tender_offer_exit_values()`
- `source_coverage()`
- `lake_store_origin()`

## 検証

- rules/method hash一致
- 同一入力でReview Setがbyte-equivalent
- E[r]だけを変更してもmembership/order不変
- Research Triageの全entry一致、priority、rules/method identity、expected prior ID
- 対象日のReview Setなし、空Review Set、exact既存Triageでmodel process 0。active Operationはdaily Triageを止めない
- 通常Review Setは共有Macro projectionを1回だけ含む1 AI request、invalid resultはcanonical write 0
- Triage publishではOperation 0。人間がnon-empty Research Setを確定してResearchを開始した時だけOperation 1
- run storeとapplication DBがcurrent schemaに一致
- Web/APIがReview Set、Research Triage、Capital Allocation Assessmentを同じbindingで表示
