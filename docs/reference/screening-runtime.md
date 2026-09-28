---
title: "Screening runtime reference"
summary: "Security Analysis、current Candidate Discovery Method、Review Set、Research Triageの実行境界の正本。"
doc_type: reference
status: active
---

# Screening runtime

## 役割

screeningは全対象銘柄のobserved / derived / estimateをSecurity Analysisへ固定し、configured Valuation Approachesが生成したNominationのexact unionをReview Setにする。Review Setは機械成果物であり、投資判断ではない。Research Triageだけが各entryへ`research / skip`とresearch priorityを発行する。

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

application DBのcurrent `research_triage`はReview Set全entryをexactly onceで保持する。
current writerとactive read pathはcurrent schemaだけを扱い、retired shapeを補完・別名投影しない。

- `research`: contiguousな`priority`、`rationale`、`research_question`、`key_risk`が必須
- `skip`: `rationale`が必須で、`priority`、`research_question`、`key_risk`は禁止

manual scaffoldはReview Set IDからrun storeのcanonical publicationを解決し、Review Setのas-of以下で最新のMacro Contextと、application DBのlatest current Triage IDを取得する。通常の`analysis run`はeditable scaffoldを介さず、strictなjudgment fieldから同じdomain objectを組み立てる。publisherは`review_set_id`、`run_revision_id`、`as_of`、`screening_rules_hash`、Candidate Discovery Method、全tickerを検証し、Review Set Entry snapshotをsource Review Setから上書きする。persisted field `candidate_snapshot`はstorage contractとして維持し、snapshotはidentity、`nominations`、grouped `analysis`だけを持つ。

発行は最新Triageを期待するCASで競合を検出し、as-ofと発行時刻の後退を拒否する。同じID・同じcanonical payloadの再送は冪等であり、同じpriorから分岐した別draftは発行できない。headの選び方とtransactionは[ResearchTriageService](../../engine/src/baibai_engine/screening/research_triage.py)、payloadの厳密な条件は[ResearchTriage model](../../engine/src/baibai_engine/foundation/research_triage.py)が所有する。

Contextが無ければ`null`は正常、古ければwarningであり、どちらもReview Setのnomination、membership、orderを変えない。eligible Contextがあるのに`null`は拒否する。明示的に古いeligible revisionを選ぶことはできるが、選択理由を既存の判断文へ残す。発行後payloadはimmutableである。

ADVは固定floorのgateや自動skip条件ではない。Triageは低値・欠損だけで`skip`やpriorityを決めず、価値仮説を比較した後の実行可能性contextとしてrationaleへ反映できる。最終的な注文可否と数量はCapital Allocation後のhuman executionが所有する。

Research TriageはResearch Setのadmission可能範囲を定める。人間は`research` entryの部分集合だけをResearch Setとして確定できる。`research prepare --research-triage-id ... --ticker ...`はas-ofと比較snapshotをcanonical payloadから導出し、選択集合をmanifestへ固定し、そのnon-empty集合のResearch開始Operationを同時に作る。空集合はOperationなしの正常結果である。Review Set fileやrun storeは要求しない。status・scaffold・promoteを含む各gateがapplication DBを再読してpayload hashとadmissible ticker集合を検証し、workspaceへの後書きadmissionを拒否する。

E[r] calibration contextは共有read contractがartifact schema、generated/expiry、3y/5y、quantile/band、rules hash、E[r] model versionを検証する。ResearchはTriage rootのrules hashとReview Set Entryの`analysis.expected_return`のmodel/version・ratio-valued `er_annual`を使い、利用不能理由を明示する。これはhistorical contextで、membership/order、Triage、FV、buy judgmentへ伝播しない。

## Daily batchとlocal analysis

daily runnerはcloud scheduleまたは明示的なlocal実行でL1 / L2 machine処理だけを行い、同じcanonical runs storeを進める。

```text
screening run -> review-set publish -> web materialize -> publish
```

cloudからL3 Research Triageを発行しない。local `baibai-batch analysis run`はpull済みruns storeから対象`as_of`のlatest published Review Setを読み、必要な場合だけReview Set全体を原則1 requestで分類してResearch Triageを発行する。対象日にReview Setが無ければ前営業日へfallbackしない。Research Set確定とCapital Allocation Assessmentは人間gateの後に残す。full-depth Macro Context、migration、全期間再取得はどちらの日次経路にも載せない。

## Pruneと履歴

`prune --keep N`は新しいrunをN世代残し、子のSecurity AnalysisとReview Setを同一transactionで削除して`VACUUM`する。Research Triage、thesis、Capital Allocation Assessment、Position Reviewはapplication DBのcanonical judgmentなのでrun pruneから独立する。

## Market store inputs

価格、財務、銘柄master、calendar、信用・空売り、JPX規制、EDINET、上場廃止・現金公開買付けとcoverageを読む。現行table/columnはschema、lake datasetは`baibai-engine lake inventory`を参照する。

値の意味は[valuation metrics](./valuation-metrics.md)、sourceの選択は[data sources](./data-sources.md)、PIT・coverage・hydrateは[market lake](./market-lake.md)に従う。

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
