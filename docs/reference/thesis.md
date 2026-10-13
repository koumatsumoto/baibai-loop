---
title: "Thesis reference"
summary: "企業評価と独立検算を一組で公開するThesis v4の内容・算術・参照契約。"
doc_type: reference
status: active
---

# Thesis

<a id="purpose-and-activation"></a>

## 目的と適用

本書はThesisと独立したThesis Reviewの内容・算術・参照契約を定める。一組の企業評価は、資本配分、保有売却、broker操作そのものではない。新規配分は[CAA](./capital-allocation-assessment.md)、保有判断は[Position Review](./position-review.md)、取引事実は[Portfolio Ledger](./portfolio-ledger.md)が所有する。

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

評価前提型は[`valuation/models.py`](../../engine/src/baibai_engine/valuation/models.py)、Projectionと純粋算術は[`valuation/scenario.py`](../../engine/src/baibai_engine/valuation/scenario.py)が所有する。Thesisはこれらを内包し、全体の検証・価格factとの合成・独立Review・store・調査進行はResearchに残る。保存済みThesis JSONとcore hashの形は変わらない。機械推計の意味と入口は[Valuation metrics](./valuation-metrics.md#machine-valuation)を参照する。

`valuation.status`はresolved/unresolved。resolvedは正の有限な`required_annual_return_pct`、正整数`horizon_months`、価格参照、Base/DownsideのProjectionを必須とする。unresolvedは期間・要求年率・両Projectionをnull、`unresolved_reason`を必須とする。

新しい通常評価では[要求年率方針](../portfolio-management.md#required-return-policy)を出発点に、作者が採用率を`required_annual_return_pct`へ明記する。個別の上乗せ理由は既存のcalculationに記す。scaffoldは未評価の率をnullで出し、自動補完しない。保存済み評価は原率で読み、率を改める場合は新revisionと独立Reviewを作る。

夜間評価の注文案が翌営業日を期限としていても、翌朝の現在判断には同日の企業評価を使う。元Thesisからの差分draftで、価値・期間・これから受け取るcashを確認し、新しい独立ReviewとCAAへ束縛する。元資料の再取得や全Researchは一律に要求しないが、日付だけの更新で旧分配を今後分へ読み替えない。Operationの分析対象日、企業評価日、quote日は区別する。

Projectionは`terminal_value_per_share_yen / cash_distribution_per_share_yen / calculation / source_ids`。terminalとcashは0以上の有限値。calculationに入力、単位、式、数値代入、結果、重要仮定を記す。期間は銘柄とシナリオの比較窓で、保有期限や売却期限ではない。期間の根拠、重要な遅延・上振れは文章で扱う。

```text
W = terminal + cumulative_cash
return = W / price - 1
annualized_return = (W / price) ** (12 / horizon_months) - 1
Pmax_raw = W_base / (1 + required_annual_return_pct / 100) ** (horizon_months / 12)
required_total_value_at_P = price * (1 + required_annual_return_pct / 100) ** (horizon_months / 12)
```

Decimalで計算・比較し、Pmaxを呼値や整数円に丸めない。価格・期間は正、bool/NaN/Infinityは拒否。W=0の総return・年率は-100%。表示年率は小数4桁。この年率は条件付き・税費用控除前・分配再投資なしで、確率加重の期待利回りではない。

terminalは分配後に残る価値、cashは当該期間の累積分配である。配当・資産売却・買戻しを二重算入しない。起点の未調整1株に権利単位を揃え、分割、自己株控除、希薄化を説明する。NI×PERへ負債を再度控除せず、EVと株主価値を混同しない。回復可能な赤字を正の起点利益へ捏造しない。Downsideは経済的に不利な状態から組み立てる。

倍率上昇には一次資料で確認した利益品質・事業構造等の根拠を記す。機械anchorや相対的割安さだけを根拠にせず、倍率が回復しない反対仮説も検算する。利益と倍率、業績と借換えが連動して悪化する場合は、一変数だけ動かして反証を済ませない。株数は会社EPSとの整合を確認し、自己株式込みのグロス株数を無条件に使わない。金融的妥当性は作者と独立Reviewの責務であり、productionで任意formulaを実行しない。

<a id="draft-calculation"></a>

### Draftの数値生成

`research calculate <INPUTS>`はYAML/JSONのtyped入力を一度読み、JSONをstdoutへ返す。ファイル・DBを書かず、ネット取得やAI実行も行わない。作者と独立Reviewがそれぞれ確認した入力で使い、保存・公開の前に従来の`evaluate`へ渡す。

入力は既存Thesisの`input_snapshot`、任意の`derived.metrics`と`valuation`に限定する。ratioの定義は既存DerivedMetricから`value`だけを除いた形で、`formula=ratio / formula_version=ratio-v1`は省略できる。factを参照して既存の単位対応で割算し、`derived.metrics`へ既存検証と同じ精度の値を生成する。例えば1/3は0.3333333333333333となり、表示用の0.3333を保存値に使わない。任意formulaや追加の事業モデルは実行しない。

`valuation`は既存のresolved/unresolved型で渡す。resolvedでは未調整価格fact、terminal/cash、期間、要求年率から既存Projection算術で総価値、総return、年率、`pmax_raw_yen`を計算し、価格・評価日・basisとともに`projection_numbers`へ返す。この出力は計算資料であり、`evaluate`の公開内容検証や`valuation_context`を代行しない。価格・単位が不適切、参照factがない場合はexit 2でstdoutを出さず、入力エラーをstderrへ返す。`valuation`省略/nullまたはunresolvedなら`projection_numbers=null / recalculated_projections=[]`で、欠測を0へ補わない。成功はexit 0である。

出力`derived`と`valuation`は既存Thesis欄へ、`recalculated_projections`は既存Review欄へ渡せる。terminal/cash自体は入力した値の再掲であり、売上・利益率・税・株数・倍率などから再構成しない。文章の`calculation`も解釈しないため、作者とReviewが同じ誤ったterminalを入力すれば一致する。一次資料・算式・数値代入の独立確認は必要なままであり、作者の入力をReviewへコピーして済ませない。保存済みJSON/core hash、Review許容差、Planning判断・数量式は変えない。

機械anchorとのgapは[Valuation metricsの保存済み照会](./valuation-metrics.md#machine-valuation)で原run・時点とともに参照する。企業評価の数値生成へ機械anchorを混ぜない。上流の事業算式を型として追加する必要性は実運用で確認してから検討する。

```bash
uv run baibai-engine research calculate /tmp/research-calculation-inputs.json
```

現金額面と転換価値の超過分を株式で交付するCBに限り、`research calculate --cash-share-cb <INPUTS>`で別の条件付き計算を行える。既存Thesisのschemaや`evaluate`には接続せず、JSONの計算資料だけを返す。額面・本数・通知ごとの対象本数、既存株数、社債債務を既に認識した株主価値、転換価額、各年度の契約上の「1株当たり特別配当」と調整参照終値を入力する。特別配当の年度累計・基準額への超過判定、決議・適用日と参照終値期間は作者が原契約と開示で確認する。算術は各調整の0.1円切捨て、1円未満の調整繰越し、通知単位の1株未満切捨てを適用する。

決済VWAPは評価時点の株価や配当調整用の参照終値とは別の入力である。端数の単元未満株の現金精算価額は**決済VWAPと等しいという明示的な試算**であり、実際の買取価格を確定しない。独立した決済VWAPでの価格と、VWAPを終期株価と同一視する条件付き固定点を並べる。整数株の切捨てによって厳密な固定点が存在しなければ、整数株の固定点はnullとし、連続近似と丸めギャップを示す。額面現金は既計上の社債債務消滅に対応し、株主価値から再控除しない。未転換は追加株式0として表示する。出力の金額は精度を保つ十進数文字列である。利益・倍率、行使時期・確率、実際の決済条件、Thesisの採否と注文は別途判断する。

```bash
uv run baibai-engine research calculate --cash-share-cb /tmp/cb-inputs.json
```

`/tmp/cb-inputs.json`の最小例（架空の単位・価格）は次のとおり。通知単位の本数と単元株数も仮定として指定する。

```json
{"valuation_as_of":"2026-10-13","terminal_as_of":"2028-10-13",
 "face_value_yen_per_bond":"100","bond_count":1,"settlement_group_bond_counts":[1],
 "share_unit":1,"odd_lot_cash_price_basis":"settlement_vwap",
 "conversion_price_yen":"10.0","existing_shares":10,
 "equity_value_yen_with_bond_liability":"1000",
 "special_dividend_adjustments":[{"special_dividend_per_share_yen":"2.5",
                                   "reference_close_yen":"20.0"}],
 "settlement_vwap_yen":"20"}
```

<a id="valuation-context"></a>

### 原価格の成立条件と時間感度

`research evaluate`の`valuation_context`は、参照価格factの未調整1株価格P、原評価の期間hと記録済み要求年率rを使う読み取り専用の計算資料である。`price_context=thesis_snapshot`と評価日・価格日・basisを併記し、現在quoteやpolicyで置換しない。古いThesisも原価格・原期間で読み、当日の購入可否や保有の残存見返りには使わない。

```text
H = required_total_value_at_P
required_terminal_s = max(0, H - cumulative_cash_s)
total_value_surplus_s = terminal_s + cumulative_cash_s - H
fixed_value_delay: terminal_sと累積cash_sを固定し、h+12か月で年率を再計算
```

Base/Downsideの必要terminalは、その累積分配で要求年率以上に届く最低の非負価値である。分配だけでHを超える場合は0という下限であり、等号成立点ではない。総価値差額は符号付き差額で、安全余裕・損失許容額・買い可否ではない。Downsideの不足を追加の不合格条件にしない。Hは既存Pmaxの代数的な読み替えで、FV、市場の期待値、独立した割安signalではない。

固定価値の遅延は追加配当・利息・再投資を仮定せず、総価値と総returnを変えない。全損なら年率も-100%。損失caseで年率損失の絶対値が縮んでも総損失は減らない。実際の遅延では利益・分配・返済・資金調達・希薄化が変わり得るため、materialなら経済条件を一貫して組み直し、既存calculationとcountercaseへ記す。h+12の表示だけで遅延を検証済みとせず、必要年率未達だけを保有exitへ接続しない。

内容検証済みのresolvedな`ready / ready_with_warnings`と、唯一の未完了がReview未添付の`review_required`で表示する。後者は計算資料が読めても`not_ready`・exit 2のままで、公開前に独立Reviewが必要である。未作成の`--review`指定先も未添付として扱う。その他のincompleteとunresolvedはnullとなり、欠損を0や過去candidateで埋めない。既存5項目・終了コードは診断の有無で変わらず、診断の数値計算・変換だけが失敗した場合も全contextをnull、理由をstderrへ出す。parseや既存入力エラーは従来どおりerrorとなる。総return・年率は小数4桁、通貨は既存Decimal変換で表示し、表示丸めを判定に使わない。

### 判断理由への接続例

以下は架空の算術例であり、実銘柄や収益効果の検証ではない。事業の見積りを一次根拠から独立に構成した後、必要価値と比較する。必要額に合わせて利益・倍率を引き上げてはいけない。採用モデルに応じ、判断を左右する少数の仮定を確認する。一般に1〜2点で足りるが、件数をgateや調査打切り条件にしない。

| 例 | 算術と既存文章欄で扱う判断 |
| --- | --- |
| P=1000円、h=12、r=8.5%、D=30円 | H=1085円、必要terminal=1055円。EPS×PERモデルでPER10を固定するなら必要EPS=105.5円。数量・利益率・税・株数から独立に見積もったEPSの範囲と比較し、`valuation.base.calculation`へ前提と代入を書く。市場のEPS予想とは呼ばず、資産・EV評価へPER式を強制しない |
| 同じP/h/r、D=0円。AはEPS100×PER13、BはEPS130×PER10でBase1300円 | 両方の総returnは30%だが、PER10のままならAは0%、Bは30%。AがPER10でH=1085円に届くにはEPS108.5円が必要。Aは利益品質・事業構造による倍率上昇の根拠、Bは受注・利益率等によるEPS回復の根拠を吟味する。どちらの根拠が弱いかをCAAの`comparison / forgone / alternatives[].rationale`で説明し、倍率上昇なしだけでBを自動採用しない |
| Base V=1270円/D=30円、Downside V=680円/D=20円 | 総価値1300円/700円、12か月年率30%/-30%。固定価値で24か月なら14.0175%/-16.3340%となるが、総returnは30%/-30%のまま。返済期限が追加資金を要求するなら、その条件を再構成してcountercaseに記す |
| 返済条件不明と、将来EPSの幅 | materialな返済条件を確認できなければdefer等を維持し、`nonmaterial_unknown_reason`で通さない。一方、一次根拠から将来EPSを幅で置くこと自体は自動排除の理由にしない。根拠のない回復予測や合理的に評価できないcaseはdefer等とする |
| 企業はcandidateだが当日価格がPmax超過 | CAAで配分を見送り、または既存allocateに対するPlanningをdeferとする。価格だけを理由に企業rejectや保有exitへ変換しない |

必要な業績・倍率・分配・感度は既存Base/Downsideの`calculation`、実現経路と成立条件は`investment_case.explanation / status_reason / invalidation_conditions`、作者と独立Reviewの反証はそれぞれの`strongest_countercase`に書く。数値の引用は原価格・期間・仮定に対応させ、全出力や新しい派生fieldをThesis/CAAへ複写しない。

作成・evaluate・独立Review・promote・CAAへの接続順序は[Research skill](../../.agents/skills/research/SKILL.md#company-research)が所有する。

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

現行application schemaのthesis/thesis_reviewへ同一connection・短いtransactionで原子的に公開する。v4単独公開やReview後付けを持たない。`published_at`はwriterの公開時刻で、`judgment.proposed_at`とは別。DBのrecommendation列にはdispositionをそのまま保存する。

同IDはThesisとReview両方の同内容ならno-op、異内容は拒否。後続更新や現在価格で過去公開を再審査しない。新revisionは同tickerの直前revisionをsupersedeし、as_ofを遡らない。最新は`as_of DESC, published_at DESC, thesis_id DESC`で先に選び、その後に検証する。旧版・不利・未解決から過去candidateへfallbackしない。旧payload・ID・hash・ledgerは変更せず、履歴表示と取引事実のidentity参照に使う。

共通読込はexact pairの内容を検証し、現在の価格・cash・保有を評価しない。新規適格性はentry_policy、保有actionはposition_reviewが所有する。

<a id="planning-only-execution-pricing"></a>

## 現在価格とPlanning

正式な新規判断は企業評価基準日を当日に揃える。quoteは前営業日でよく、引け後は当日確定終値を使える。同日quote訂正だけで企業評価を再公開しない。日々のwatchはread-onlyで原評価日・期間・Pmaxを表示する。旧期間を縮めた残存年率を表示しない。

基準日更新は`thesis-scaffold --from-thesis-id`で元資料の日付・元予測を保持した差分draftを作り、成立性・残存分配・期間を確認して新Reviewを取る。受取済み・権利確定済み未入金の分配は現在からの将来増分に含めない。

PlanningはCAAのallocate候補だけに現在の価格、確認済みcash、保有・予約・同CAA約定履歴を確認する。既保有や同ticker予約、同CAA買約定済みはdefer。全売却後も旧CAAを再利用しない。数量はcashと20〜30万円guideの単元床を使い、cashで1単元を買えなければ0。1単元がguideを超える場合はcash内に限り1単元とwarningを出す。NAV自動sizingは行わない。

ADV参加率は[開始sourceに固定した流動性context](./capital-allocation-assessment.md#research-source)を参照し、欠損は未評価とする。5%超をwarningとして伝える。

他保有quoteの欠損はNAV・集中・dry powderをunknownとして示し、架空のcash-only NAVを作らない。現在quoteと原評価の権利単位が確認できなければ購入提案をしない。詳細な資本規律は[portfolio management](../portfolio-management.md)。

## Workspaceとコマンド

新規admissionは[Researchの開始sourceと固定選択](./analysis-operations.md#explicit-research-source)をOperationへ保存し、保有は同じ有限対象の別purposeとする。Workspaceはdraft作成・再利用・promoteを所有し、注文式やchecklist全行completeという第二gateを持たない。公開状態はexact Thesis/Reviewとの一致で決まる。保有Workspaceは対象holdingを確認し、無関係なledger更新で調査をやり直さない。

```bash
uv run baibai-engine research evaluate /tmp/thesis-draft.yaml --review /tmp/thesis-review.yaml
uv run baibai-engine research status --workspace .cache/research/YYYY-MM-DD
uv run baibai-engine research plan-limit --help
```

PlanningとPosition Reviewは`current_price_projection`を共用し、現在の未調整価格でBase/Downsideの総return・年率、期間、累積分配、price basisを表示する。原評価の価格と予測は書き換えず、同日評価・利用可能なquote・同じ権利単位が揃わなければnullにする。この一時出力はThesisやPosition Reviewへ保存せず、売却閾値や確率加重期待値として使わない。
