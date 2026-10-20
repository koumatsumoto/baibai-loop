---
title: "Investment Proposal"
summary: "保存評価を価格・cash・保有へ適用した行動と条件、独立内容Review、現在previewの契約。"
doc_type: reference
status: active
related_docs:
  - "../portfolio-management.md"
  - "./stock-assessment.md"
  - "./portfolio-ledger.md"
---

# Investment Proposal

[Stock Assessment](./stock-assessment.md)の事業理解・価値・反証を、確認した価格・cash・保有・注文状態へ適用した提案を保存する。新規購入、指値待ち、経済的見送り、保有継続・売却、未調査による保留を区別する。提案公開で予約・注文・約定・人間の採用を作らない。

型は[`proposal.py`](../../engine/src/baibai_engine/research/proposal.py)、入力収集は[`proposal_inputs.py`](../../engine/src/baibai_engine/research/proposal_inputs.py)、保存検証は[`proposal_service.py`](../../engine/src/baibai_engine/research/proposal_service.py)、保存読取と現在再試算は[`proposal_query.py`](../../engine/src/baibai_engine/research/proposal_query.py)が正本である。操作順序は[Research skill](../../.agents/skills/research/SKILL.md)に従う。

## 行動と比較範囲

| 行動 | 意味 |
| --- | --- |
| buy | 買付の経済判断と、現在closeを上限とする指値または低い指値待ち |
| hold | 今回確認した仮説維持、または確認済みの十分な残存見返りによる保有継続 |
| sell | 重要な投資理由の不成立、または確認済みの残存見返り不足による全量回収 |
| no_action | 完成した評価を比較して購入しないという経済判断 |
| defer | 調査・入力・判断条件の不足。未調査は評価参照なしで理由と再開条件を残す |

三入口とも[Operationの固定選定](./analysis-operations.md#明示開始と固定対象)の全対象を被覆する。完成対象はexact Assessment ID/core hash/Review IDへ、未完了対象は理由と参照へ束縛する。未完了を経済的な見送りに混ぜず、new/holdingを混在させない。0対象では空Proposalを生成しない。

比較理由には[投資目的とリスク許容](../doctrine.md#investment-strategy)に沿って、価値を生む経済経路、重要な観測と個社への伝達、Base/Downside・反証・実現までの時間を現金維持や他の完成案と比べて書く。同じBase年率でも倍率上昇と業績回復への依存を分け、外部評価との重要な相違を採用・反証・保留または参考とした理由へ辿れるようにする。比較不能な観測や未完了対象を有利/不利な確定判断に変えず、Pmax内というだけで買付を決めない。企業別の根拠を比較欄へ全文コピーせず、[原価格の成立条件](./stock-assessment.md#valuation-context)と参照評価を示す。

## 条件付き購入

買付算術は[`evaluate_buy_terms`](../../engine/src/baibai_engine/research/entry_policy.py)が所有する。実quoteは観測、提案limitは試算価格であり、現在quoteを架空の安い値へ置換しない。pmaxはBase終価・今後分配・記録された期間と要求率から求め、Downside算術を別の購入閾値へしない。

最新reviewed評価がsupported/intact、valuationが当日起点で、現在要求率・実quote・仮説・権利単位・cash・既保有・予約・同Proposalの買約定等のgateを満たすことを確認する。現在closeがpmax超過でもpmax以下の低いlimitで待つことはできる。価格以外の不足を安い指値で迂回しない。数量は提案limitで計算し、guide上限とcashの単元床を使う。guide超でもcash内の1単元を許す現方針とwarningを維持する。

現在limitは現在closeを上限とする指値であり、成行や即約定の保証ではない。wait_limitには実quoteより低い価格と選択理由が必要である。価格・数量・対象日（session）・前提条件を保存し、株式単位、仮説、資金、注文状態に重要変化がないことを条件にする。未約定売却代金はcashへ加えない。

<a id="price-grid"></a>

### 共通呼値と条件未確定

[`position/price_grid.py`](../../engine/src/baibai_engine/position/price_grid.py)は[JPX公式「その他」表](https://www.jpx.co.jp/equities/trading/domestic/07.html)の保守的共通gridと、master原本の商品区分・銘柄コードから得る普通株判定と市場分類を使う。TOPIX500の最良呼値とは称さない。根拠・確認日・期間は同moduleの定数が正本で、`tse-common-conservative-20261014`は2026-10-14以上2027-03-01未満のsessionにだけ使う。

buyはpmax・作者希望上限・closeを超えない価格を下方丸め、丸め先の価格帯も検証する。sellは現在closeを上限とする範囲の案で、作者が指定した売却最低価格を勝手に下げる経路を持たない。分類不明・grid期間外等は評価や売買の経済判断を消さず、terms未確定として残す。termsなしのbuyは作者の希望価格をproposed_limit_yenに明示し、現在previewで同じ価格を再検証する。後日のquoteから希望価格を推測しない。

`order_readiness`は保存本文からの読取投影で、termsありは「提案条件確定」、なしは「条件未確定」、hold/no_action/deferは適用外である。条件未確定の売買判断は理由ある部分結果として公開できるが、価格・数量まで提案完了したと報告しない。日々の値幅制限、証券会社の取扱い、取引規制、注文受理・約定を保証せず、本人が実注文時に証券画面を確認する。

<a id="holding"></a>

## 保有継続と回収

保有の経済判断は[`holding_policy.py`](../../engine/src/baibai_engine/research/holding_policy.py)が所有する。当初評価を確認できるならそのexact参照を使い、不明なら理由を残す。現在評価を当初の投資理由へ付け替えない。

重要な一次根拠でinvestment caseの重大なbreakを確認した場合はsellを優先する。valuation・quote・数量basisの不足でholdへ変えず、具体売却条件を作れなければsell判断と不足理由を保存する。初期の売却提案は全量で、端株や数量basis未確認を黙って丸めない。

intactでmaterialな反証が無いことを今回確認した場合は、旧valuation日・quote欠測・算術未確定でもinvestment_case_intactとしてholdを許す。これは当日の期待収益が十分という主張ではない。sufficient_remaining_rewardによるholdとinsufficientによるsellには、当日起点valuation・利用可能quote・権利単位と現在算術を要求する。仮説維持を主理由にするholdでも、残存見返りを明示する場合は同日算術を検証する。既知のinsufficientとholdを同時に主張しない。

残存見返りは現在から保有して得る増分で判断する。受取済みcashと、権利確定済みで今売っても受け取れる未入金分を二重算入しない。旧horizonを残日数で割り直して年率を水増しせず、[差分refresh](./stock-assessment.md#assessment-refresh)で期間・今後分配を確認する。税・費用・遅延・下振れで結論が変わり得るなら不足と経済理由を示す。

価格下落、経過期間、単なる回復遅延、新規買付floor未達、Target到達、他候補や集中warning単独では売らない。新しい買付候補がなくても現金を回収できる。仮説・重要根拠・残存見返りの判断が不確実ならdeferとして必要確認を残す。

## 保存・独立Review・現在preview

提案の具体条件はschema 3とし、対象日（target_session）から重複する15:30の期限を保存しない。対象日は呼値と現在条件の確認に使う。実注文の任意期限は本人が報告した台帳上の事実であり、提案から補完しない。既存schema 2の提案がある場合は本番切替前に移行・退役方針を確定し、読込時に期限だけを削って旧Reviewや参照hashを新本文へ付け替えない。

scaffoldで入力snapshotとProposal/ReviewのIDを固定し、作者が行動・具体条件・比較理由を記す。初期draftの未記述比較はdeferであり、経済的no_actionへ自動変換しない。`proposal-check`は書込をせず、Review前にdraft_sha256を確認できる。作者とは別の役が内容Reviewを行い、変更した場合は取り直す。

Review hashにはreviewとpublished_at以外の全文を含め、cash、scope、価格・数量・対象日・前提、grid、warnings/exposureを束縛する。公開時はOperationの現在被覆と保存scope、exact評価pair、現在入力事実を再読照合する。checked_atの経過だけをdriftにせず、実入力値を比較する。入力が変わればsnapshotと独立Reviewを取り直す。

同ID同内容は保存済みpublicationを返し、異内容はimmutable conflictとなる。Proposal本文、item indexと具体buyのdecision bindingを同transactionで保存する。hold/sell/no_action/deferにbuy bindingを作らず、公開でledgerを変更しない。保存後は固定scopeが当時の正本であり、Operationの進捗や最新候補へ付け替えない。

`proposal-preview`は保存条件を保持したままcurrent_inputs・現在quote算術・保存limitの再検証・変化理由・現在warningを別に返す。保存termsやhashを現在値で上書きしない。現在gate不足はblocked理由を表示し、古い保存評価・当時の条件付き算術を悪い・失効と断定しない。

## 資本warningと確認済み取引

集中度、ADV、budget、dry powderの純粋projectionは[`proposal_warnings.py`](../../engine/src/baibai_engine/research/proposal_warnings.py)が単独で所有する。閾値と理由は[資本方針](../portfolio-management.md#reservation-and-warnings)に従う。予約分を含み、保有quote不足ではtotal NAVをunknownとする。common_factor_unclassified_tickersがある場合は既知factor比率が下限であり、coverageと集中warningを区別する。saved warningとcurrent preview warningを混ぜない。

本人の採否とbroker操作は別工程である。提案なしの直接取引、遅延報告、部分約定・部分売却も、人間が確認した事実として[Portfolio Ledger](./portfolio-ledger.md)へ記録する。旧判断参照はtickerを識別する最小bindingとして保持し、当時未保存の価格・数量・期限をFillから復元しない。
