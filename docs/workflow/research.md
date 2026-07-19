---
title: "Workflow — research"
summary: "人間が選んだprimary-research setを一次情報、永久損失、3年/5年scenario、反証で比較し、最良0〜1件をdecision packetへ固定する。"
doc_type: workflow
status: active
last_reviewed: 2026-07-15
related_docs:
  - "./screening.md"
  - "../reference/decision-packet.md"
  - "../operations/decision-cycle.md"
---

# Workflow — 個別銘柄research

researchの目的は、安く見える理由が一時的な誤解か、企業価値の構造的毀損かを区別することである。AIは一次情報、scenario、反証をdecision packet/reviewへ固定し、人間が購入判断とbroker操作を行う。

## Input and lineage

| input | 用途 | boundary |
| --- | --- | --- |
| selection output / audit pool | 候補抜けとrankingの確認 | canonical judgmentではない |
| ticker profile | price、relative、events、screen facts | observed/derived/estimateを維持 |
| company IR / TDnet / EDINET / JPX | load-bearing claim | source URL、公表日、対象期が必須 |
| canonical ledger snapshot | held/reserved/concentration annotation | 投資価値rankを先に変えない |
| macro context | materialな外部経路 | screen rank、sizing formulaへ入れない |

検索snippet、ニュース見出し、外部AI要約を観測事実にしない。二次情報は一次sourceの所在確認と相互検算だけに使う。

operation Issueでbusiness-model guide pilotの対象に指定したlaneでは、[`business-model research guide`](../reference/business-model-research.md)からprimary lensを1つ選び、複合modelで5年評価にmaterialな場合だけsecondary lensを1つ追加する。primary lensの全required questionsとsecondary lensから選んだ全material questionsを`answered / unknown / not_applicable`とし、`answered / not_applicable`は根拠sourceを持つ既存`domain_findings`、`unknown`は既存`unknowns`へ同guideの規約で置く。issuer-primaryだけで確定できるclaimと独立裏取りが必要なclaimを同guideの区分で判定し、適合するlensが無ければ分類を強制せず共通checklistへ戻る。pilot対象外のlaneへ一律に強制しない。

## Candidate stages

候補件数は段階ごとに意味が異なる。

| stage | artifact | contract |
| --- | --- | --- |
| production recommendations | screening selection outputの`recommendations` | rulesの`research_selection_target_max`を適用した通常表示 |
| human-review shortlist | OP3のcandidate report | 件数と選定手順は[`decision-cycle` OP3](../operations/decision-cycle.md#opportunity-path)を正本とする |
| primary-research set | workspaceの`selection.yaml.shortlist` | 人間がreportから選ぶ。推奨2〜4件で、selection outputの`research_selection_target_max`を上限とする |
| selected | `research-comparison.yaml.selected_ticker` | 一次情報で全対象を比較した後の最良0〜1件 |

上位非選択候補にも理由を残し、保有済み、予約中、予算外だけを理由に除外しない。

| rank | ticker | temporary mispricing | permanent loss | 5y CAGR/FV | portfolio annotation | strongest countercase | disposition/reason |
| --- | --- | --- | --- | --- | --- | --- | --- |

primary-research setの比較は永久損失、5年期待return/FV乖離、portfolio marginal value、購入可能性の順。単一合成scoreで畳まない。

## Parallel research lanes

人間がprimary-research setを複数選んだ場合、共有workspaceのselection output / ledger hashを共通lineageとして、tickerごとの`.cache/opportunity/ASOF_DATE/<ticker>/` laneを作る。一次source確認、永久損失7軸、scenario、packet、独立reviewはlane間で並行できる。各laneは自tickerのdraftとchecklistだけを変更し、他tickerの成果物をcopyまたは上書きしない。

並行化するのは調査と反証までである。全laneを同じ比較表で評価した後、現在の提案roundの`selected_ticker`は0〜1件に保つ。複数laneがviableなら、最上位の人間判断と必要なcanonical ledger更新を完了してから次のlaneを再比較し、最新ledgerで指値を再計算する。selection時点のledger hashを複数proposalへ使い回さない。

## Primary-source record

| source_id | class | document / URL | period | published_at | accessed_at | status | used_for |
| --- | --- | --- | --- | --- | --- | --- | --- |

statusは`ok / missing / stale`。取得不能時はattempted sourceとdecision impactを残し、推定値で埋めない。公表日、対象期、単位、tickerの取り違えを確認する。

## Permanent-loss checks

schemaの7軸を全件評価する。

1. `funding_liquidity`
2. `debt_repayment`
3. `cash_flow`
4. `dilution`
5. `customer_concentration`
6. `structural_decline`
7. `governance_accounting`

各軸はassessment、evidence status、as-of、source IDsを持つ。`corporate_action`は永久損失軸ではなく、価格、株数、EPS、配当basisの事前checkとして別に確認する。unresolvedなcorporate action、価格basis、fundingは指値へ進めない。

## Scenario and valuation

3年は予測可能性のsanity、5年は主評価。bear/base/bullをそれぞれ作り、starting earnings、shares、growth、share-count change、terminal multiple、cumulative dividendからterminal valueとtotal-return CAGRを機械再計算する。

- peak profitを正常利益として外挿しない。
- dilutionをshare-count changeへ反映する。
- dividendをterminal priceとreturnへ二重計上しない。
- FV、entry price、required 5y CAGRのsource/as-ofを固定する。
- E[r]とscreening FV anchorはestimateで、個別FVの代替ではない。
- `baibai-engine research evaluate`の5年base break-evenを使い、terminal multipleとearnings growthが要求CAGRまで持つ余裕を確認する。

Research FV確定時、screening FV anchorとの差率を`(research_fv / screening_fv_anchor - 1) * 100`で機械計算する。
主要説明要因を`earnings_normalization / growth / shares / multiple / dividend / required_return / other`から1つ選び、noteは1〜2行に限定する。
複数要因のwaterfall分解や合計100%への配賦は行わず、判断困難な場合は`other`として理由をnoteに残す。
このbridgeは改善計測用であり、screening順位、FV anchor、購入提案を自動補正しない。
scaffold済みscreening estimateを手書きで置換せず、欠損時はbridgeを推定で埋めない。

算術とfield意味は[`../reference/decision-packet.md`](../reference/decision-packet.md)を正本とする。

## AI value capture

AIはテーマではなく企業別のvalue captureとして評価する。role、競争優位、価格決定力、必要capex、顧客交渉力、株主への帰属をsource付きで判断する。AI需要だけで採用、順位、sizingを決めない。disruptionを認める場合はstructural decline軸と同じsourceへ接続する。

## Portfolio annotation and affordability

ledgerから`unheld / held / reserved / held_and_reserved`を付け、追加後concentrationと既存proposalの関係を示す。追加資金と通常注文額のplanning baselineは[`portfolio-management`](../portfolio-management.md#capital-guidance)を正本とする。cash、dry powder、集中はwarningであり、永久損失と5年期待値を比較する前のhard filterではない。

## Packet scaffold

通常の候補調査は[`operations/decision-cycle.md#opportunity-path`](../operations/decision-cycle.md#opportunity-path)のpublic recipeでworkspaceを作る。`packet-scaffold`はprimary-research setに含まれるtickerのlaneだけに作成し、このgateを保有reviewのために緩めない。workspaceの`ASOF_DATE`に対して`--target-session`には次の取引sessionを指定し、解決したraw close日がmanifest `as_of`と異なる場合は停止する。

決算・material eventによる保有更新は[`operations/decision-cycle.md#earnings-and-material-event-path`](../operations/decision-cycle.md#earnings-and-material-event-path)の`holding-prepare`を使う。canonical ledgerの実在open holdingが調査対象を決め、audit pool、shortlist、selected tickerを同じ1銘柄に固定するため、screening selection outputを要求しない。どちらのworkspaceでもscaffoldが埋めないjudgmentを推測で補完せず、checklistを`complete / blocked`にする。

packetは次を分離する。

- `input_snapshot`: source付きの最小fact
- `derived`: formulaとinput IDsを持つ再計算値
- `estimates`: 3年/5年scenario、FV、required return
- `judgment`: recommendation、永久損失結論、countercase、sizing、AI value capture

raw candidate YAML、SQLite path依存、検索snippet、fixture copyをcanonical packetへ残さない。

## Independent review

packet authorと別roleが、候補抜け、一次source、scenario算術、永久損失7軸、countercase、代替候補、portfolio annotation、limit/quantityを再確認する。reviewはpacketを直接編集せず、decision-review draftだけを返す。

5年baseのreviewでは`baibai-engine research evaluate <packet>`を実行する。reviewerは既存の`scenario.base_3y_5y` checkをいったん`pending`へ戻し、次を記録・確認した後だけ`complete`へ戻す。

- base / break-even terminal multipleと、その差であるdownside buffer
- base / break-even annual earnings growthと、その差であるdownside buffer（percentage points）
- 観測trailing multipleのfact IDと値、およびbase multipleとの差
- terminal multiple仮定を維持・修正した判断理由と、proposalへの影響

base terminal multipleが観測trailing multipleを上回る場合は、premiumを支えるpacket内fact IDと、そのfactへ接続する一次source IDを同じcheckに記録する。観測anchorまたはbreak-even計算が解決しない、あるいはpremiumを支える一次情報を特定できない場合はcheckを`complete`にせずpacket authorへ戻す。downside bufferが0以下であること自体は有効な計算結果であり、review不備とはしない。ただし買い提案と必要利回りが整合するかを`decision_impact`で明示し、scenarioやproposalを変える必要がある場合、review draftは`proposal_changed=true`とする。数値が良好であることだけをscenario仮定の根拠にしない。

`proposal_changed=true`ならpacketへ戻る。packet core hashが変わった後のreviewはstaleで、promotionへ使えない。

## Integrated research report and content review

全lane比較後は[`research-decision-report`](../reference/research-decision-report.md)の共通findings templateへ、指定質問への回答、business model、value capture、growth quality、財務耐久性、業種固有分析、unknown、monitoringを統合する。数値scenario、FV、7軸、採否、指値・数量をfindingsへ複製せず、packet / comparison / proposalからrendererがjoinする。

OP3 shortlist narrativeの`research`確認事項は、primary-research setの各銘柄についてfindingsでansweredにするか`unknowns`へ残す。購入提案がある場合は`decision_context.entry_timing`に、選択銘柄の直近dated material eventとevent前に買う判断理由を書く（契約は[`research-decision-report`](../reference/research-decision-report.md)を正本とする）。

growth qualityは開示範囲でvolume、price、mix、upsell/churn、FXへ分ける。海外展開は商品coverage、契約所在地/請求通貨/地域売上、ultimate customer originを別々に評価する。management claimにはその区分を付け、未開示値はunknownに残す。

HTML生成前に、report compilerと別roleが軽量なfindings / comparison / packet / proposalをreviewする。source freshness、指定質問への回答、一次source traceability、fact/estimate分離、countercase/unknown、scenario/FV、横比較/portfolio fit、購入方法bindingの全checkがpassで、reviewed input hashが現在値と一致する場合だけHTMLを生成する。HTMLそのものはreview対象にしない。

## Result states

| state | 条件 | 次 |
| --- | --- | --- |
| `research` | source/check未完 | 一次source取得またはblocked記録 |
| `reject` | structural decline、永久損失、FV不足等 | 理由を残して終了 |
| `selected` | 全候補比較後の最良1件 | packet/review |
| `defer` | load-bearing fact/corporate action/price basis未解決 | dated taskまたはsource待ち |
| `no actionable bargain` | viable候補0件 | 正常終了、packetなし |

promotionはpacket/review/hash/schema/pathが一致するときだけ行う。test fixtureのcopy、旧thesis Markdown、旧position Markdownへ書かない。

## Failure / stop conditions

- Tier 1 sourceなしでload-bearing claimを確定しようとしている。
- corporate actionまたはprice basisがunresolved。
- 7永久損失軸、3年/5年scenario、countercaseが欠ける。
- packetとreviewのhashが一致しない。
- integrated content reviewが未完、changes required、または入力hashと一致しない。
- budget fitだけで上位候補を入れ替えている。

## Validation

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-engine research evaluate records/03-thesis/YYYY/MM/YYYY-MM-DD-XXXX-decision.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-engine validate --target decision-packet
```

## Related

- [`./screening.md`](./screening.md)
- [`./position.md`](./position.md)
- [`../reference/decision-packet.md`](../reference/decision-packet.md)
- [`../portfolio-management.md`](../portfolio-management.md)
