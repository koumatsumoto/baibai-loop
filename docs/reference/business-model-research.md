---
title: "Business-model research guide"
summary: "個別銘柄researchでbusiness model固有の問いを選び、claimの一次性とissuerからの独立性を分けて検証するpilot guide。"
doc_type: reference
status: active
related_docs:
  - "./thesis.md"
  - "./bargain-assessment.md"
  - "../../method/research/playbooks/README.md"
---

# Business-model research guide

このguideは「なぜ安く見えるか」を扱うMachine Evidence Patternとは別に、「企業がどう稼ぎ、どのKPIと制約が5年価値を決めるか」を確認するためのpilot question lensである。business model分類そのものを投資根拠、screening条件、thesis fieldにせず、問いの抜けを減らすためだけに使う。pilot期間はoperation sessionで指定したprimary-research対象だけへ適用し、対象外へ一律に強制しない。

## 適用手順

1. research開始時に、5年base FVを最も左右するvalue-capture経路からprimary lensを1つ選び、理由を1〜2行で記録する。
2. 複合modelで別segmentも5年評価を左右する場合だけsecondary lensを1つ選び、その節からmaterialな問いを追加する。売上区分だけで機械分類しない。
3. primary lensの全required questionsと、secondary lensから選んだ全material questionsを確認し、各問を`answered / unknown / not_applicable`のいずれかにする。`not_applicable`にはbusiness modelの根拠sourceと理由、`unknown`には試したsourceとscenario・permanent-loss判断への影響を残す。
4. 回答にはsource IDとclaim classを接続し、下記triangulation規約に従う。開示されないKPIを同業平均や推測で埋めない。
5. どのpilot lensにも適合しない企業を無理に分類せず、skill `research` の共通確認へ戻る。pilot中にlensを追加せず、2〜3件の自然発生run後に維持・修正・撤回・拡張を別Issueで判断する。

findingsでは`answered`を既存の`domain_findings`へ置き、question IDを`heading`、回答とlens適合理由を`conclusion`、根拠を`evidence.statement / kind / source_ids`へ置く。`not_applicable`もbusiness modelの根拠sourceを持つ`domain_findings`として、理由を`conclusion`に明記する。部分回答は確認できた部分だけを`domain_findings`へ置き、未確認部分を`unknowns`へ分ける。source取得不能時は偽のevidenceを作らず、`unknowns`へ`<question_id>: unknown — attempted source / decision impact`として置く。`answered / unknown / not_applicable`、source role、claim class、load-bearing、triangulation statusはguide上のreview語彙であり、新しいYAML field、enum、schema、artifactにしない。必要な区分は既存の`conclusion`、`evidence.statement`、`unknowns`、thesis sourceの`used_for`へ文章で残す。

## Claim triangulation

### 一次性と独立性

sourceの一次性とissuerからの独立性は別軸である。

- **issuer-primary**: 有価証券報告書、決算資料、適時開示、製品資料など、対象企業が直接公表したsource。
- **independent-primary**: 規制当局・取引所自身の調査・認定・処分・統計、政府統計、顧客やpartnerが自身の観測として公表した事実、公共調達記録、platform運営者自身のdataなど、claim対象を対象企業以外が直接作成したsource。URL hostではなくclaimの作成主体・originで判定する。
- **independent-secondary**: 出所と方法が確認できる業界統計・調査・事実報道。一次sourceの代替ではなく、外部claimの相互検算に使う。

issuerが作成してEDINET/TDnet/JPX経由で配布した文書はissuer-primaryかつ同じissuer familyであり、hostが規制当局・取引所でも独立sourceにならない。対象企業のWeb siteに転載された顧客事例、対象企業が作成・sponsorしたsurvey、販売代理店だけの紹介、共同文面・issuer提供数値・同一press kitの再掲はissuerまたはcoordinated familyとして扱う。customer/partner sourceがindependent-primaryになり得るのは、その組織が直接観測・管理する事実を自身の責任で公表する範囲だけである。競合企業の資料は、その競合自身の価格・製品・行動には一次sourceだが、対象企業の優位性を中立に証明しない。

### Claim class別の要求

各claimはさらに`load-bearing / supporting`を区別する。claimが変わるとpermanent-loss結論、base scenario/FV、selected ticker、`buy / defer / reject`、sizingまたはhuman override要否のいずれかが変わる場合はload-bearingである。独立裏取りはload-bearingな外部状態・需要・競争優位・因果・pipeline成功claimへ要求し、全記述を機械的に二重source化しない。

| claim class | issuer-primaryで確定できる範囲 | 独立裏取り | 記録上の境界 |
| --- | --- | --- | --- |
| 自社の過去財務・資本・契約会計 | EDINET/TDnet/監査済み開示から期間・単位・会計方針を確認できれば足りる | 数値矛盾やscope不明時に必要 | `observed`。非GAAP KPIは定義とreconciliationを要求 |
| 自社の過去operating KPI | 定義、対象範囲、時系列が継続開示されていれば、その公表値には足りる | KPIが需要・優位性を示すという因果には必要 | 公表値は`observed`、analystの因果解釈は`estimate` |
| 経営計画・guidance・pipeline stage | 「経営陣がその計画・stageを公表した」ことだけを確認できる | 実現可能性・成功確率・外部需要には必要 | 内容は`management_claim`。計画値を実績factにしない |
| 市場規模・規制・業界成長 | issuer資料だけでは確定しない | 規制当局、政府・業界統計、方法開示済み独立資料が必要 | 独立sourceなしなら`unknown`または幅を持つestimate |
| 顧客需要・採用・継続・導入効果 | issuerの受注・顧客数など自社KPIの公表範囲まで | 顧客自身の公表、調達記録、独立usage/industry dataのいずれかが必要 | issuer-curated事例だけなら`management_claim`のまま |
| 競争優位・market share・差別化・pricing power | 自社価格・解約・margin等の公表値まで | 顧客、競合比較、業界統計、規制資料のいずれかが必要 | 裏取り後も優位性の持続は`estimate`。観測factへ昇格しない |
| pipeline成功・製品hit・承認・稼働効果 | 発売/申請/契約/稼働の公表stageまで | platform、規制当局、partner/customer、独立需要dataのいずれかが必要 | stageと成功確率を分離し、findingsでは`estimate`、数値仮定はthesis `estimates.scenarios`へ置く |
| 否定・不存在 | 明示された開示scope内で「該当なし」と報告したことまで | registry、規制当局、counterparty、対象範囲を覆う複数経路が必要 | 非開示・検索不発から「競合/集中/riskなし」をobservedにせず`unknown` |

独立sourceがclaimを支持しても、`management_claim`やestimateをobserved factへ変換しない。findingsの有効な`evidence.kind`は`observed / derived / estimate / management_claim`だけで、thesisのjudgment namespaceを`kind: judgment`として使わない。複数sourceが同じissuer発表を転載しているだけならtriangulationにならない。

同じissuerの決算短信、説明資料、統合報告書、製品newsはdocument数にかかわらず1 source familyとして扱う。同じ通信社記事の転載や同じ調査datasetを引用する複数記事も1 familyである。source familyはtriangulationの独立性を数える概念だけに使い、対象期・公表日・`used_for`が異なるthesisのsource recordとsource IDは個別に維持する。

独立sourceはclaimとsubject、population、期間、地域、metricが一致する範囲だけを支持する。単一customer事例はそのcustomerでの導入事実、partner公表は関係の存在と公表範囲、platform指標は当該platform・cohort・期間の観測だけをcorroborateできる。複数事例への一般化、継続率、pricing power、市場全体の需要、hit確率は母集団dataが無ければestimateのまま保守的scenarioへ置く。矛盾するsourceは都合のよい方だけを採用せず、解消できなければunknownへ戻す。

### Unknown・blocked・defer

- issuer発表を確認できた内容は`management_claim`として残す。独立裏取りを必要とするload-bearing claimに適切な独立sourceが無い場合、claimを削除したり`kind: unknown`を作らず、独立裏取り未了とdecision impactを`conclusion`または`unknowns`へ残す。issuer発表自体も確認できない内容だけをunknownとする。issuer-primaryで確定できる過去の公表値は、その値と因果解釈を分離できていればunknownへ戻さない。
- required questionに回答できないcheckは`blocked`としてよい。`blocked`は個別checkの状態で、laneのdispositionではない。check未完のlaneは`research`に留め、自動的に`reject`へ変えない。
- 独立裏取りが必要なload-bearing claimをissuer familyだけで支える場合、claimは`management_claim`またはestimateのまま、canonical thesisの`judgment.confidence`は最大`medium`とし、そのpositive claimを無条件にbase/FVへ入れない。sourceが矛盾し未解決なら同confidenceを`low`とする。findingsのconfidenceはthesisを上回らず、HTML前content reviewの`primary source traceability`と`countercase and unknowns`で両者を照合する。
- unresolved claimが、割安と構造的毀損の区別、永久損失軸、またはrequired 5y returnを満たすscenarioの成立にload-bearingで、保守的な範囲も置けない場合だけ購入判断を`defer`する。
- unknownをbear caseへ保守的に置いても十分な余裕があり、他の一次sourceでpermanent lossを評価できる場合は、confidenceとmonitoring triggerを明示して比較を続けてよい。
- `reject`は欠損そのものではなく、確認できた事実と保守的scenarioが恒久毀損または必要利回り不足を示す場合に使う。

## Pilot 1 — 継続契約software / data

### Value-capture経路

契約継続、利用拡大、価格改定がrecurring revenueと粗利へ届き、獲得・導入・hosting費用を差し引いたcashが一株価値になるmodel。SaaS、期間license、保守、data subscriptionを同一視せず、収益認識と解約可能性を分ける。

### Required questions

1. `recurring.revenue_boundary`: 何がsubscription、license、保守、従量課金、導入serviceで、売上・粗利の構成はどう違うか。
2. `recurring.retention`: 更新率、解約率、NRR/GRR、契約継続年数のどれが開示され、対象customer・期間・分母は一貫しているか。
3. `recurring.expansion`: customer増、seat/usage増、upsell、価格改定のどれが成長を作り、値上げ後の解約やdowngradeはどうか。
4. `recurring.contract_cash`: 契約期間、請求時点、解約条項、前受/契約負債、RPO/backlogが売上・cashへ変わる時期はいつか。
5. `recurring.delivery_cost`: 導入・customization・support・cloud/royalty費用は誰が負担し、recurring成長でgross marginとFCFが拡大するか。
6. `recurring.acquisition`: sales/channel費用、導入期間、CAC/paybackを直接または代替KPIで保守的に評価できるか。
7. `recurring.concentration`: 大口customer、業界、地域、platform/technology supplierへの集中と切替costはどちら側にあるか。

### 重要KPI

recurring売上・粗利比率、ARR/MRR、NRR/GRR、logo/売上churn、ARPU/seat/usage、customer数、RPO/契約負債、gross margin、sales費用、導入backlog、support/hosting費用。会社が定義しないKPIを逆算で創作せず、定義変更・acquisition影響を分離する。

### 典型的な誤読

- recurring売上比率が高いだけで、解約可能性・値下げ・導入service負担を確認せず耐久性が高いとする。
- ARR、RPO、受注残、契約負債を同じものとして扱い、将来売上と将来cashを二重計上する。
- customer数やseat増を、ARPU低下・無償枠・acquisitionを分けずorganic growthとする。

### Blocked / defer条件

retentionまたはrecurring境界がunknownなのに継続率がFVの主要根拠、serviceとsoftwareのmarginを分けられずoperating leverageを評価不能、大口解約・platform依存がpermanent-loss判断を左右する場合は該当checkをblockedとする。購入判断は、そのunknownがload-bearingで、ゼロ成長・margin低下などの保守的な範囲も置けない場合だけdeferする。

## Pilot 2 — 人員依存project / outsourcing

### Value-capture経路

受注、稼働人員、単価、稼働率、delivery品質が売上とproject marginへ届き、採用・賃上げ・外注・手戻り・運転資金を差し引いたcashが一株価値になるmodel。請負、準委任、派遣、managed service/BPOを分ける。

### Required questions

1. `people.contract_mix`: 固定価格、time-and-material、派遣、成果報酬、recurring managed serviceの構成とrisk負担はどう違うか。
2. `people.orders_conversion`: 受注、backlog、book-to-billはどの期間の売上へ転換し、取消・scope変更・検収条件は何か。
3. `people.capacity`: 技術者/運用人員、稼働率、billable比率、単価、1人当たり売上のどれが開示され、capacityと需要を分けられるか。
4. `people.labor_bridge`: 採用、離職、賃上げ、外注比率、offshore/automationがgross marginへどう届くか。
5. `people.delivery_risk`: 不採算project、追加原価、品質事故、SLA penalty、引当の発生と再発防止は何か。
6. `people.concentration`: 上位customer・vendor・業界への集中、更新周期、値上げ交渉力、内製化riskはどうか。
7. `people.cash_conversion`: 契約資産、未請求、売掛、前受、検収遅延が利益と営業CFをどれだけ乖離させるか。

### 重要KPI

受注高・backlog・book-to-bill、売上転換率、稼働率、技術者/運用人員、採用・離職、単価、1人当たり売上、外注比率、人件費、gross/project margin、不採算project損失、契約資産、DSO、営業CF conversion。backlogの定義と期間を必ず併記する。

### 典型的な誤読

- backlog全額が過去と同じmarginで売上化すると仮定し、取消、検収、delivery capacityを無視する。
- 人員増を即時の売上capacity増とみなし、採用費、training、bench、離職を落とす。
- 「DX需要が強い」という市場claimだけで、対象企業の受注・単価・margin・cash conversionを確認せず成長を置く。

### Blocked / defer条件

受注/backlogと売上のreconciliationが不明、headcount・単価・稼働率・labor costのうちgrowth/marginを説明する材料が無い、契約資産や不採算projectが急増しcash回収と損失上限を評価できない場合は該当checkをblockedとし、5年scenarioを保守的にも置けなければ購入判断をdeferする。

## Pilot 3 — IP / live-service / hit portfolio

### Value-capture経路

既存IPの継続課金・updateと、新作・新地域・新platformのlaunchが売上へ届き、開発・運営・marketing・platform fee・royaltyを差し引いたportfolio cashが一株価値になるmodel。既存live-serviceのdurabilityと未発売titleのoption valueを分け、単一hitを通常収益力へ外挿しない。

### Required questions

1. `content.existing_cohorts`: 既存title/IPごとの売上、payer/active user、retention、ARPU、地域・platform、経過年数をどこまで分けられるか。
2. `content.flow_durability`: update cadence、content backlog、運営team、community、seasonalityが既存flow収益とmarginを何年支え得るか。
3. `content.pipeline_stage`: 各案件を発売済み、発売日確定、test中、年だけ公表、未定に分け、完全新作、地域展開、platform展開、update、publishingを区別したか。
4. `content.pipeline_economics`: 開発・marketing・minimum guarantee・royalty・platform fee・revenue share、capitalized development、回収期間をtitle別または保守的なportfolio幅で評価できるか。
5. `content.hit_evidence`: test参加、engagement、wishlist/pre-registration、paid conversion、retentionなどのうち、issuerから独立した需要evidenceは何か。規模の異なる指標をhit確率へ直結していないか。
6. `content.concentration`: 上位title/IP、地域、platform、license owner、studio/key personへの集中と契約更新・終了条件は何か。
7. `content.downside_policy`: launch遅延、中止、初動不振、既存title減衰時にmarketing・開発・運営費をどこまで止められ、balance sheetが何回の失敗に耐えられるか。

### 重要KPI

title/IP別売上、MAU/DAU、payer数・payer率、ARPU/ARPPU、retention、bookings、地域/platform mix、update cadence、test engagement、wishlist/pre-registration、開発・marketing費、platform fee/royalty、capitalized development、title別/portfolio margin、既存titleだけの営業CF。定義・対象地域・platform・cohort・期間が無いKPIを比較しない。

### 典型的な誤読

- 発売予定表にある全titleをbase caseへ入れ、stage、延期率、開発費、publisher/地域/platform展開を区別しない。
- test参加者、wishlist、事前登録をpaid userや長期retentionと同一視し、blockbuster成功確率を過大評価する。
- 単一hitのpeak売上・marginをportfolioの正常収益へ外挿し、既存title減衰と次作失敗を同時に置かない。

### Blocked / defer条件

既存titleだけのflow収益と固定費耐性を評価できない、未発売pipeline成功を除くとrequired returnを満たさないのにstage・cost・独立需要evidenceがunknown、または上位IP/license/platform契約の終了条件がunknownなら該当checkをblockedとする。購入判断は、そのunknownがload-bearingで、pipeline価値ゼロ・既存title減衰・margin低下などの保守的な範囲も置けない場合だけdeferする。未発売titleを除いた既存flowで事業継続と十分な価値を支えられるなら、pipelineをbull/optionに限定して比較を続けてよい。

## Pilotの見直し

次回以降の自然発生researchから2〜3件で、共通checklistと比べて新たに立った問い、残ったunknown、scenario/FVまたはdispositionへの影響、追加負担をoperation sessionへ残す。確認のために候補選定、research、売買を強制せず、`buy / reject / defer / no actionable bargain`をすべて正常結果とする。1件だけで投資精度の改善を断定せず、pilot lensが問いを増やすだけで判断を変えない場合は、項目追加より削減・統合・撤回を優先する。
