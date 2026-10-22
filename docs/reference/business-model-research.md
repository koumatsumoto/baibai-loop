---
title: "事業モデル別リサーチ"
summary: "個別銘柄のresearchで価値を左右する問いを選び、主張の一次性とissuerからの独立性を分けて検証する指針。"
doc_type: reference
status: active
related_docs:
  - "./stock-assessment.md"
  - "./investment-proposal.md"
  - "../../method/research/playbooks/README.md"
---

<a id="business-model-research-guide"></a>

# 事業モデル別リサーチ

## 目的と非目標

この文書は、機械指標から事業の経済性へ戻るための問いの選び方を定める。「なぜ安く見えるか」を扱う`Valuation Approach`と、企業がどう稼ぎ、誰にcashが残るかという事業モデルを分ける。分類名を投資根拠、screening条件、assessment fieldにはしない。

## 適用手順

1. Baseの残余価値と累積分配を左右する経路を選ぶ。事業名や業種codeではなく、収益の反復性、資本の帰属、費用負担、回収期間を見る。複合事業なら重要な異なる経路だけを加える。
2. 下の観点から、答えによって利益・cash・期間・永久損失または採否が変わる問いを選ぶ。全問を必須にせず、重要な問いが表にない場合は事業固有の問いを立てる。業種ごとのchecklist完成を目的にしない。
3. 確認した事実、将来の仮定、残るunknownを既存欄へ記す。KPIの期間・対象・分母・定義変更とM&A影響を揃え、開示されないKPIを同業平均や推測で埋めない。会社計画を下げるだけで独立した見積りにしたとは扱わない。
4. 重要な未知でも、肯定的な効果を採らず、確認済み事業だけで合理的な幅を置けるかを検討する。その条件で評価できることと、元の成長・競争優位の主張を確認できたことは別である。未確認の重要事実を保守的という言葉で免除しない。
5. 同じ原典の反復で答えが増えないときは、何の開示・実績で再評価するかを残す。調査終了と企業評価の成立、購入採用は別判断である。

## 調査結果の記録

確認結果は[Stock Assessment](./stock-assessment.md)の既存の入力先へ置く。

- `answered`: 公表された観測値は`input_snapshot.facts`、計算値は`derived.metrics`、因果・価値獲得経路は`investment_case.explanation`、数値仮定はBase/Downsideの`calculation`へ接続する。根拠は各`source_ids`から参照する。
- `not_applicable`: 採用しない観点と事業モデル上の理由を、該当するinvestment caseまたはriskの文章に残す。
- 部分回答・`unknown`: 確認済み部分と未確認部分を分け、試したsourceと判断への影響を該当riskの`summary / evidence_status`、caseの`status_reason`、valuationの`unresolved_reason`に応じて残す。未確認内容を観測factへ置かない。

question ID、`answered / unknown / not_applicable`、source role、claim class、`load-bearing`、triangulation statusは調査用語であり、追加のYAML fieldやartifactにはしない。sourceの用途は`used_for`、最強反対仮説は`conclusion.strongest_countercase`に置き、評価と独立Reviewを一組で公開する。

<a id="claim-triangulation"></a>
<a id="一次性と独立性"></a>

## 情報源の一次性とissuerからの独立性

sourceの一次性とissuerからの独立性は、別々に判定する。

- **issuer-primary**: 有価証券報告書、決算資料、適時開示、製品資料など、対象企業が直接公表したsource。
- **independent-primary**: 規制当局・取引所自身の調査・認定・処分・統計、政府統計、顧客やpartnerが自身の観測として公表した事実、公共調達記録、platform運営者自身のdataなど、claim対象を対象企業以外が直接作成したsource。URL hostではなく、claimを作成した主体とoriginで判定する。
- **independent-secondary**: 出所と方法が確認できる業界統計・調査・事実報道。一次sourceの代替ではなく、外部claimの相互検算に使う。

issuerが作成し、EDINET / TDnet / JPX経由で配布した文書は`issuer-primary`であり、同じissuer familyに属する。hostが規制当局や取引所でも、独立sourceにはならない。

対象企業のWeb siteに転載された顧客事例、対象企業が作成またはsponsorしたsurvey、販売代理店だけの紹介、共同文面、issuer提供数値、同じpress kitの再掲は、issuerまたはcoordinated familyとして扱う。customer / partner sourceを`independent-primary`として扱えるのは、その組織が直接観測・管理する事実を、自身の責任で公表する範囲だけである。競合企業の資料は、その競合自身の価格・製品・行動には一次sourceだが、対象企業の優位性を中立に証明するものではない。

公開の外部企業レポートは[Research時の参照先と解釈](./data-sources.md#public-research-reports)から使う。第三者のサイトにあることとissuerから独立した根拠であることを同一視しない。

## 主張区分ごとの証拠要件

各claimは`load-bearing / supporting`を区別する。claimが変わることで、permanent-loss結論、Base/Downsideの価値、投資理由の成立性、企業評価の`supported / uncertain / unsupported`、Proposalの行動結論のいずれかが変わる場合は`load-bearing`である。

独立した裏取りは、外部状態、需要、競争優位、因果、pipeline成功についての`load-bearing`なclaimに要求する。すべての記述へ機械的に二つのsourceを要求するものではない。

| claim class | issuer-primaryで確定できる範囲 | 独立裏取り | 記録上の境界 |
| --- | --- | --- | --- |
| 自社の過去財務・資本・契約会計 | EDINET/TDnet/監査済み開示から期間・単位・会計方針を確認できれば足りる | 数値矛盾やscope不明時に必要 | `observed`。非GAAP KPIは定義とreconciliationを要求 |
| 自社の過去operating KPI | 定義、対象範囲、時系列が継続開示されていれば、その公表値には足りる | KPIが需要・優位性を示すという因果には必要 | 公表値は`observed`、analystの因果解釈は`estimate` |
| 経営計画・guidance・pipeline stage | 「経営陣がその計画・stageを公表した」ことだけを確認できる | 実現可能性・成功確率・外部需要には必要 | 内容は`management_claim`。計画値を実績factにしない |
| 市場規模・規制・業界成長 | issuer資料だけでは確定しない | 規制当局、政府・業界統計、方法開示済み独立資料が必要 | 独立sourceなしなら`unknown`または幅を持つestimate |
| 顧客需要・採用・継続・導入効果 | issuerの受注・顧客数など自社KPIの公表範囲まで | 顧客自身の公表、調達記録、独立usage/industry dataのいずれかが必要 | issuer-curated事例だけなら`management_claim`のまま |
| 競争優位・market share・差別化・pricing power | 自社価格・解約・margin等の公表値まで | 顧客、競合比較、業界統計、規制資料のいずれかが必要 | 裏取り後も優位性の持続は`estimate`。観測factへ昇格しない |
| pipeline成功・製品hit・承認・稼働効果 | 発売/申請/契約/稼働の公表stageまで | platform、規制当局、partner/customer、独立需要dataのいずれかが必要 | stageと成功確率を分離し、解釈はinvestment case、数値仮定は`valuation.base / downside`の`calculation`へ置く |
| 否定・不存在 | 明示された開示scope内で「該当なし」と報告したことまで | registry、規制当局、counterparty、対象範囲を覆う複数経路が必要 | 非開示・検索不発から「競合/集中/riskなし」をobservedにせず`unknown` |

独立sourceがclaimを支持しても、経営陣の主張や推定を観測factへ変換しない。公表の事実と、その内容の実現可能性を分ける。claim classは文章上の区分であり、`ObservedFact.fact_kind`へ追加しない。複数sourceが同じissuer発表を転載しているだけならtriangulationにならない。

同じissuerの決算短信、説明資料、統合報告書、製品newsはdocument数にかかわらず1 source familyとして扱う。同じ通信社記事の転載や同じ調査datasetを引用する複数記事も1 familyである。source familyはtriangulationの独立性を数える概念だけに使い、対象期・公表日・`used_for`が異なるassessmentのsource recordとsource IDは個別に維持する。

独立sourceはclaimとsubject、population、期間、地域、metricが一致する範囲だけを支持する。単一customer事例はそのcustomerでの導入事実、partner公表は関係の存在と公表範囲、platform指標は当該platform・cohort・期間の観測だけをcorroborateできる。複数事例への一般化、継続率、pricing power、市場全体の需要、hit確率は母集団dataが無ければestimateのまま保守的scenarioへ置く。矛盾するsourceは都合のよい方だけを採用せず、解消できなければunknownへ戻す。

## `unknown` / `blocked` / `defer`の扱い

- issuerの公表を確認できても、その将来主張は確定値ではない。独立裏取りが必要な重要claimは、試したsourceと未了の影響を該当するcase・risk・valuationに残す。issuer-primaryで確定できる過去の公表値は、因果解釈から分離して保持する。
- `blocked`は個別の確認事項を終えられない状態で、Stock Assessmentのstanceではない。根拠不足だけで自動unsupportedにせず、投資仮説の維持・不成立を判断できない重要な不明はuncertainとして残す。
- 重要なclaimをissuer familyだけで支える場合やsourceの矛盾が未解決なら、肯定的な価値を無条件にBaseへ入れない。独立Reviewは根拠の検証状態を`primary_source_check`へ反映し、重要な根拠不足のsupportedを通さない。小口配分やoverrideで代替しない。
- 割安と構造的毀損の区別、永久損失、Base/Downsideの成立性を保守的にも評価できなければ、理由付きunresolved valuationを使う。評価が可能でも重要な不明が残る場合はcase・risk・stanceで明示する。
- valuationのresolved/unresolved、企業評価のstance、新規/保有の行動は分けて判断する。企業評価は[Stock Assessment](./stock-assessment.md)、新規購入の条件と行動は[Investment Proposal](./investment-proposal.md)に従う。valuation不足から保有を一律deferにせず、確認済みの重大なbreakによるsellと、今回intactを確認したholdの扱いは[保有継続と回収](./investment-proposal.md#holding)を正本とする。
- 非重要なunknownはReviewの`nonmaterial_unknown_reason`へ根拠を示し、比較を続けてよい。unsupportedは確認した重大な不成立や不適切な企業評価の結論である。現在価格がPmaxを超えたという理由だけで企業評価をunsupportedにしない。現在の購入条件と候補間配分はInvestment Proposalが所有する。

## 経済性に応じた問い

以下は選択する観点であり、固定分類・必須項目数・別の進捗台帳ではない。源泉の異なる重要な収益や資産は分け、同じcashを複数の経路で加算しない。

| 経済性 | 判断を左右する問い | 間違えやすい対照 |
| --- | --- | --- |
| 継続契約software / data | subscription、license、保守、導入・従量課金の粗利と費用負担。更新・解約、seat/usage・単価、顧客集中、前受・RPOの売上/入金化。獲得・導入・hosting費用を含む回収 | recurring比率だけで耐久性を認定しない。ARR、受注残、契約負債は別物。会社定義のないNRR/CACを創作しない |
| 人員依存project / outsourcing | 契約のrisk負担、受注の売上化、単価・稼働人員・稼働率、賃上げ・外注・採用育成、不採算案件、検収・未請求・回収。顧客集中と切替条件 | 受注増を同marginの確定売上にせず、採用人数を即時capacityにしない。DX需要と当該企業の利益獲得は別 |
| IP / license / 有限寿命の権利 | 既存収益の減衰、地域・契約・権利期間、更新/代替投資、royalty、固定費と撤退費。pipelineはstage、費用負担、独立需要/承認の証拠を分ける | 償却の足し戻しは経済寿命の延長ではない。発売・申請・特許期限だけで成功または売上ゼロとせず、既存flowと未実現optionを分ける |
| 原料・市況・加工業 | 数量・価格・mix、在庫評価、加工付加価値、ヘッジ損益の表示位置と対象期間、運転資金と調達。維持・能力増強の支出と稼働率 | 名目増収を数量成長、ヘッジ前営業利益を経済的収益力、負FCFを成長投資の証明にしない。相場反転時の在庫・ヘッジ・資金を一緒に動かす |
| 資産保有・ファンド・複合企業 | 連結scopeと親会社持分、他の出資者・債権者の請求、資金拘束・配当可能性、換金費用・税、運転資金、回収を実行する主体と条件 | 連結資産や預り資金を普通株主の余剰資産にしない。非支配持分の帳簿額を個別資産から一律控除して時価持分とみなさない |
| 金融仲介 | 貸出/預金等の再価格化、調達費、信用費用、債券/OCI、普通株主資本と規制資本、分配と資本余力。利益留保後の終期ROEと倍率の整合 | 預り資金やgeneric net cashを株主の余剰資産にせず、会計自己資本比率とCET1を混同しない。金利方向だけで受益認定しない |
| 事業/資産売却・回復 | 売却益と入金の違い、税・返済・維持費・残存事業、回復に必要な時間と追加資本、契約条件と実行段階 | 一時利益を永続資本化せず、除外するだけで一回限りの資本改善まで消さない。売却前の資産と売却後cashを重ねず、返済はcashとdebt双方へ反映 |

資産・利益・cashを普通株主価値へつなぐ算術は[Stock Assessmentの資本bridge](./stock-assessment.md#capital-bridge)、評価モデルと期間の選択は[同書](./stock-assessment.md#valuation-model-choice)を正本とする。

## 改善を判定する

質問数やunknown数ではなく、原典から確認した経済的な差が、主要仮定・価値の幅・反証条件・調査の継続可否のどれを変えたかを見る。既知事項の再確認、仮定の変更、未解決を区別し、結論不変も記録する。追加の問いが判断に寄与しなければ削減する。少数例での誤読訂正と、投資収益の改善は同一ではない。
