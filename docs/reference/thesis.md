---
title: "Thesis reference"
summary: "企業評価と独立検算を一組で公開するThesis v4の内容・算術・参照契約。"
doc_type: reference
status: active
---

# Thesis

<a id="purpose-and-activation"></a>

## 目的と適用

Researchは企業評価、新規資本配分、保有判断を所有する。Reviewed Thesisは企業評価と独立したThesis Reviewの一組であり、現在価格での購入やbroker操作を意味しない。型・内容検証は`research/thesis.py`、純粋算術は`research/valuation.py`、公開・読込は`research/thesis_store.py`が所有する。

## Thesis v4

| 内容 | 責務 |
| --- | --- |
| `input_snapshot` | ticker、企業名、正式評価基準日、採用した観測factとsource |
| `derived.metrics` | 必要な機械再計算値と入力・式・単位 |
| `investment_case` | 未織込み、価値の変化、実現経路、重大な不成立条件、成立性と根拠 |
| `valuation` | 一つの期間、記録済み要求年率、BaseとDownside |
| `permanent_loss_risks` | 永久損失7軸の調査結果 |
| `judgment` | `candidate / defer / reject`、作者の判断時刻、最強反対仮説 |

`investment_case.status`は`intact / broken / uncertain`。brokenはreject、uncertainはdefer/reject。candidateは「十分安ければ資本比較へ使える企業評価」で、resolved valuationと重要な根拠の独立確認が必要である。現在価格がPmaxを超えても企業評価を書き換えない。価格だけでallocateを決めない。

<a id="input-snapshot-and-lineage"></a>

## 観測・根拠・単位

Snapshotは必要な値を固定し、元run storeの存在を要求しない。ScreeningのE[r]とFV anchorは候補発見のsecondary machine priorであり、Thesisへ重複転記しない。

factは`fact_id / fact_kind / value / unit / as_of / source_ids`を持つ。外部sourceはHTTPS、local dataはprovider/datasetを持つ。ticker、source参照、取得・観測時刻、単位を検証する。判断後の資料を判断前の事実として混入しない。市場価格は`observed_at`と未調整1株の`price_basis`を明示する。

resolved valuationは価格factを参照する。unresolvedでは価格もnullでよく、valuation_metricや正の起点利益を要求しない。財務根拠が未確定なら理由付きdefer/rejectを完成させる。欠損を0やverifiedへ変換しない。

<a id="scenario-arithmetic"></a>

## Valuationと純粋算術

`valuation.status`はresolved/unresolved。resolvedは正の有限な`required_annual_return_pct`、正整数`horizon_months`、価格参照、Base/DownsideのProjectionを必須とする。unresolvedは期間・要求年率・両Projectionをnull、`unresolved_reason`を必須とする。

Projectionは`terminal_value_per_share_yen / cash_distribution_per_share_yen / calculation / source_ids`。terminalとcashは0以上の有限値。calculationに入力、単位、式、数値代入、結果、重要仮定を記す。通常12か月だが期限売却ではない。別期間の理由、重要な遅延・上振れは文章で扱う。

```text
W = terminal + cumulative_cash
return = W / price - 1
annualized_return = (W / price) ** (12 / horizon_months) - 1
Pmax_raw = W_base / (1 + required_annual_return_pct / 100) ** (horizon_months / 12)
required_total_value_at_P = price * (1 + required_annual_return_pct / 100) ** (horizon_months / 12)
```

Decimalで計算・比較し、Pmaxを呼値や整数円に丸めない。価格・期間は正、bool/NaN/Infinityは拒否。W=0の総return・年率は-100%。表示年率は小数4桁。この年率は条件付き・税費用控除前・分配再投資なしで、確率加重の期待利回りではない。

terminalは分配後に残る価値、cashは当該期間の累積分配である。配当・資産売却・買戻しを二重算入しない。起点の未調整1株に権利単位を揃え、分割、自己株控除、希薄化を説明する。NI×PERへ負債を再度控除せず、EVと株主価値を混同しない。回復可能な赤字を正の起点利益へ捏造しない。Downsideは経済的に不利な状態から組み立てる。

倍率上昇には一次資料で確認した利益品質・事業構造等の根拠を記す。機械anchorや相対的割安さだけを根拠にせず、倍率が回復しない反対仮説も検算する。株数は会社EPSとの整合を確認し、自己株式込みのグロス株数を無条件に使わない。金融的妥当性は作者と独立Reviewの責務であり、productionで任意formulaを実行しない。

<a id="permanent-loss-axes"></a>

## 永久損失7軸

`funding_liquidity / debt_repayment / cash_flow / dilution / customer_concentration / structural_decline / governance_accounting`を各1件持つ。評価はacceptable/adverse/unknown、証拠はverified/partially_verified/unverified。赤字や下方修正だけを永久損失としない。重要な根拠不足を小口購入やoverrideで通さない。非重要なunknownを許容する理由はReviewへ記す。

AIを含む技術・産業構造変化も、materialな場合だけ通常のcalculation、investment case、リスク、反対仮説へ接続する。専用のscoreやchecklistは設けない。

<a id="independent-second-pass"></a>

## 独立Review

全公開Thesisに、review_id、独立passのidentity/role/time、exact core hash、primary_source_check、checked_source_ids、最強反証を持つReviewを組み合わせる。Base/Downside各1件のterminal/cashを独立に再計算する。作者値をscaffoldでコピーして検算としない。照合許容差は0.0001円。source・単位・hash不一致は公開できない。

primary_source_check=verifiedは重要な判断根拠を確認した意味で、全非開示事項を解明した意味ではない。重要なunknownはcandidateを通さず、非重要なunknownは`nonmaterial_unknown_reason`で説明する。hashは整合性を保証するだけで、独立性や文章の真実性の証明ではない。

<a id="core-hash"></a>

## 公開・読込

schema20の既存thesis/thesis_reviewへ同一connection・短いtransactionで原子的に公開する。v4単独公開やReview後付けを持たない。`published_at`はwriterの公開時刻で、`judgment.proposed_at`とは別。DBのrecommendation列にはdispositionをそのまま保存する。

同IDはThesisとReview両方の同内容ならno-op、異内容は拒否。後続更新や現在価格で過去公開を再審査しない。新revisionは同tickerの直前revisionをsupersedeし、as_ofを遡らない。最新は`as_of DESC, published_at DESC, thesis_id DESC`で先に選び、その後に検証する。旧版・不利・未解決から過去candidateへfallbackしない。旧payload・ID・hash・ledgerは変更せず、履歴表示と取引事実のidentity参照に使う。

共通読込はexact pairの内容を検証し、現在の価格・cash・保有を評価しない。新規適格性はentry_policy、保有actionはposition_reviewが所有する。

<a id="planning-only-execution-pricing"></a>

## 現在価格とPlanning

正式な新規判断は企業評価基準日を当日に揃える。quoteは前営業日でよく、引け後は当日確定終値を使える。同日quote訂正だけで企業評価を再公開しない。日々のwatchはread-onlyで原評価日・期間・Pmaxを表示する。旧期間を縮めた残存年率を表示しない。

基準日更新は`thesis-scaffold --from-thesis-id`で元資料の日付・元予測を保持した差分draftを作り、成立性・残存分配・期間を確認して新Reviewを取る。受取済み・権利確定済み未入金の分配は現在からの将来増分に含めない。

PlanningはCAAのallocate候補だけに現在の価格、確認済みcash、保有・予約・同CAA約定履歴を確認する。既保有や同ticker予約、同CAA買約定済みはdefer。全売却後も旧CAAを再利用しない。数量はcashと20〜30万円guideの単元床を使い、cashで1単元を買えなければ0。1単元がguideを超える場合はcash内に限り1単元とwarningを出す。NAV自動sizingは行わない。

ADV参加率はTriageの観測日付き流動性contextを参照し、欠損は未評価とする。5%超をwarningとして伝える。

他保有quoteの欠損はNAV・集中・dry powderをunknownとして示し、架空のcash-only NAVを作らない。現在quoteと原評価の権利単位が確認できなければ購入提案をしない。詳細な資本規律は[portfolio management](../portfolio-management.md)。

## Workspaceとコマンド

admissionはexact Triageと人間のResearch SetをOperationへ固定する。Workspaceはdraft作成・再利用・promoteを所有し、注文式やchecklist全行completeという第二gateを持たない。公開状態はexact Thesis/Reviewとの一致で決まる。保有Workspaceは対象holdingを確認し、無関係なledger更新で調査をやり直さない。

```bash
uv run baibai-engine research evaluate /tmp/thesis-draft.yaml --review /tmp/thesis-review.yaml
uv run baibai-engine research status --workspace .cache/research/YYYY-MM-DD
uv run baibai-engine research plan-limit --help
```

PlanningとPosition Reviewは`current_price_projection`を共用し、現在の未調整価格でBase/Downsideの総return・年率、期間、累積分配、price basisを表示する。原評価の価格と予測は書き換えず、同日評価・利用可能なquote・同じ権利単位が揃わなければnullにする。この一時出力はThesisやPosition Reviewへ保存せず、売却閾値や確率加重期待値として使わない。
