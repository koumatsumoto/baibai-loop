---
title: "Stock Assessment reference"
summary: "事業理解・価値・反証を独立Reviewと一組で公開する評価契約。"
doc_type: reference
status: active
---

# Stock Assessment

<a id="purpose-and-activation"></a>

## 目的と適用

本書はStock Assessmentと独立したAssessment Reviewの内容・算術・参照契約を定める。企業評価は現在価格・cash・保有に対する行動判断から独立する。新規購入と保有継続・売却の行動、条件付き価格・数量・対象日は[Investment Proposal](./investment-proposal.md)、確認済み取引事実は[Portfolio Ledger](./portfolio-ledger.md)が所有する。

## Stock Assessment v1

| 内容 | 責務 |
| --- | --- |
| `input_snapshot` | ticker、企業名、正式評価基準日、採用した観測factとsource |
| `derived.metrics` | 必要な機械再計算値と入力・式・単位 |
| `investment_case` | 事業・資本配分が企業価値を生む経路と、市場での未織込み・価格差を区別した仮説、その成立性・重大な不成立条件と根拠 |
| `valuation` | 一つの期間、記録済み要求年率、BaseとDownside |
| `permanent_loss_risks` | 永久損失7軸の調査結果 |
| `findings` | 事業理解、重要観測の個社伝達、価値差と実現経路・遅延、依頼者/作者の検証仮説と重要な相違、未解決の問い |
| `conclusion` | `supported / uncertain / unsupported`、作者の評価時刻、最強反対仮説 |

`investment_case.status`は`intact / broken / uncertain`。brokenはunsupported、uncertainの投資仮説をsupportedへしない。supportedにはresolved valuationと重要な根拠の独立確認が必要だが、購入採用を意味しない。現在価格がPmaxを超えても企業評価を書き換えず、cash不足を評価結論へ混ぜない。 現在株価からの下落率と条件付き価値に対する余裕は別である。価格待ちを提案する際は、利益・倍率等の小さな変更で要求率を割るかを確かめ、価格低下と同時に仮説が悪化した場合の再確認条件を残す。

<a id="input-snapshot-and-lineage"></a>

## 観測・根拠・単位

Snapshotは必要な値を固定し、元run storeの存在を要求しない。ScreeningのE[r]とFV anchorは候補発見のsecondary machine priorであり、Stock Assessmentへ重複転記しない。Market Contextや外部評価の保存sourceがあるだけでは個社の利益、元評価者の対象期・株式basis、将来終価を確定できない。重要な材料だけを自己仮定へ接続し、重要でない観測、比較条件が未確認の観測、観測自体の未取得を分ける。

factは`fact_id / fact_kind / value / unit / as_of / source_ids`を持つ。外部sourceはHTTPS、local dataはprovider/datasetを持つ。ticker、source参照、取得・観測時刻、単位を検証する。判断後の資料を判断前の事実として混入しない。市場価格は未調整1株の`price_basis`を明示する。日次終値は原典の取引日`as_of`で扱い、観測時刻を捏造しない。実際に観測時刻がある場合だけ`observed_at`を付け、realtime価格では必須とする。終値の利用可能な取引日はJSTの立会終了境界で判定する。

resolved valuationは価格factを参照する。unresolvedでは価格もnullでよく、valuation_metricや正の起点利益を要求しない。財務根拠が未確定なら所見と理由を独立確認しuncertain/unsupportedの評価を完成させる。欠損を0やverifiedへ変換しない。

<a id="scenario-arithmetic"></a>

## Valuationと純粋算術

評価前提型は[`valuation/models.py`](../../engine/src/baibai_engine/valuation/models.py)、Projectionと純粋算術は[`valuation/scenario.py`](../../engine/src/baibai_engine/valuation/scenario.py)が所有する。Stock Assessmentはこれらを内包し、全体の検証・価格factとの合成・独立Review・store・調査進行はResearchに残る。厳密な新型は[`research/assessment.py`](../../engine/src/baibai_engine/research/assessment.py)、公開と再読は[`assessment_store.py`](../../engine/src/baibai_engine/research/assessment_store.py)が正本である。機械推計の意味と入口は[Valuation metrics](./valuation-metrics.md#machine-valuation)を参照する。

`valuation.status`はresolved/unresolved。resolvedは正の有限な`required_annual_return_pct`、正整数`horizon_months`、価格参照、Base/DownsideのProjectionを必須とする。unresolvedは期間・要求年率・両Projectionをnull、`unresolved_reason`を必須とする。

新しい通常評価では[要求年率方針](../portfolio-management.md#required-return-policy)に従い、作者が案件ごとの採用率を`required_annual_return_pct`へ明記する。その率を選ぶ経済的理由は既存のcalculationに記す。scaffoldは未評価の率をnullで出し、自動補完しない。保存済み評価は原率で読み、率を改める場合は新revisionと独立Reviewを作る。

夜間評価の注文案が翌営業日を対象日としていても、翌朝の現在判断には同日の企業評価を使う。元Stock Assessmentからの差分draftで、価値・期間・これから受け取るcashを確認し、新しい独立ReviewとInvestment Proposalへ束縛する。元資料の再取得や全Researchは一律に要求しないが、日付だけの更新で旧分配を今後分へ読み替えない。Operationの分析対象日、企業評価日、quote日は区別する。

Projectionは`terminal_value_per_share_yen / cash_distribution_per_share_yen / calculation / source_ids`。terminalとcashは0以上の有限値。calculationに入力、単位、式、数値代入、結果、重要仮定を記す。期間は銘柄とシナリオの比較窓で、保有期限や売却期限ではない。期間の根拠、重要な遅延・上振れは文章で扱う。

```text
W = terminal + cumulative_cash
return = W / price - 1
annualized_return = (W / price) ** (12 / horizon_months) - 1
Pmax_raw = W_base / (1 + required_annual_return_pct / 100) ** (horizon_months / 12)
required_total_value_at_P = price * (1 + required_annual_return_pct / 100) ** (horizon_months / 12)
```

Decimalで計算・比較し、Pmaxを呼値や整数円に丸めない。価格・期間は正、bool/NaN/Infinityは拒否。W=0の総return・年率は-100%。表示年率は小数4桁。この年率は条件付き・税費用控除前・分配再投資なしで、確率加重の期待利回りではない。

terminalは分配後に残る価値、cashは当該期間の累積分配である。配当・資産売却・買戻しを二重算入しない。終期が配当基準日の後で入金前なら、権利落ち後株価と未入金の受取権を別に数え、cashへ受取済みとしても重ねない。未公表の次期配当は維持条件と権利なしの保守条件を分け、確定値へ変換しない。起点の未調整1株に権利単位を揃え、分割、自己株控除、希薄化を説明する。NI×PERへ負債を再度控除せず、EVと株主価値を混同しない。回復可能な赤字を正の起点利益へ捏造しない。Downsideは経済的に不利な状態から組み立てる。

<a id="valuation-model-choice"></a>

### 主要モデルと期間を選ぶ

評価モデルは株価との比較より先に、価値獲得経路と根拠のある期間から選び、既存の`investment_case.explanation`と`calculation`で説明する。EPS×出口PERは選択肢の一つで、12か月を既定にしない。将来利益を機械的に延長できない場合、次のうち重要な対照を使う。全方式で値を出して平均したり、最も高い値を採用したりしない。

- 持続収益：価格・数量・費用から反復可能な利益/cashを見積もる。倍率に含む成長・再投資・リスクと、独立に認識する資産を区別する。
- 回復・再投資：回復に必要な支出・運転資金・損失期間を含む複数年経路を置く。過去のcapital proxyや会社のROIC目標を、新規投資の限界収益率にしない。費用計上済みの人材・研究開発投資を再控除しない。
- 有限寿命・資本解放：権利の残存収益または回収可能資産を、更新/撤退費、税、債権者・他の出資者への帰属、分配時期とともに評価する。継続価値を置くなら、その後の収益を維持する費用と根拠を示す。

倍率据置は、どの利益・資産に対する倍率かを明示する。利益一定でも留保で簿価が増えると、PBR一定はPER上昇を含み得る。「再評価なし」という言葉だけで同じcaseにしない。留保利益は自由cashと同じではなく、再投資・回収と将来収益への寄与を確認する。

多年度化には年度別の新schemaを要求せず、主要な中間年の利益/cash・投資・資金と終期の関係を`calculation`へ残す。最終出力は同じProjectionへ集約する。期間を伸ばす際は累積分配だけを増やさず、その間の事業変化・投資・調達と残余価値も更新する。期間内分配を再投資しない既存年率はIRRではなく、異なる分配時期の効果を精密に比較するものではない。早期回収の価値が採否を左右する場合は、時期を明示した別の感応度を説明し、保存年率の意味を変えない。

採用しなかった競合仮説が結論を覆し得る場合は、何が確認されれば主モデルを変えるかを`findings.hypotheses`または最強反対仮説に残す。例えば一時売却益を利益から除いても、手取りによる債務削減を消してよいわけではない。元の低PERが成立しないことと、残存事業・資産を含む投資機会がないことを分ける。

<a id="capital-bridge"></a>

### 期間内CFと終期資本のつながり

留保CFが重要なら、Base/Downsideの`calculation`に、期首・期末の現金、非営業資産、負債、非支配持分、期間中の営業CF・設備投資・運転資金・分配・元本返済と、期末株数へのつながりを同じ単位と期間で記す。CFに税・利息・維持/成長投資・運転資金増減を含めたか明示し、差引済み項目を再控除しない。以下は営業EV方式を選び、`operating_EV_end`が期末必要操業cash（`required_operating_cash_end`）を含む営業価値である場合の単純形。`nonoperating_assets_end`へ現金を重ねず、非営業資産売買・自己株買付け等は現金と対応資産・株数の両側へ反映する（下式ではそれらの変化なし）。

```text
cash_end = cash_start + CF_after_investment_before_distribution_and_principal
           - distribution - principal_repayment + equity_issuance_proceeds + new_borrowing
debt_end = debt_start - principal_repayment + new_borrowing
equity_end = operating_EV_end + nonoperating_assets_end
             + (cash_end - required_operating_cash_end) - debt_end - minority_claims_end
```

必要操業cashは営業EVに織り込んだ額だけを控除し、不足する差額も`max(0, ...)`で消さない。これは営業EV方式を全社へ強制する式ではない。NI×PER等で既に株主価値を求めた場合、同じ利益に対応する現金や全現金を機械的に足さず、別認識する余剰資産と利益の範囲を説明する。元本返済は現金と負債を同額減らす移動で、株主価値から再控除しない。通常賃借料を営業費用に含める評価とリース負債を控除する評価を混ぜず、会計基準変更によるEBIT・償却の見かけの変化を成長にしない。未コミットM&Aは既定で実行済みにせず、織り込むなら対価・調達と取得価値/利益を両側に反映する。利益の資本化率と、期間中の総価値を現在価格と比較する要求年率は別の仮定である。 PBRとROEを照合する場合は起点でなく終期の利益・簿価・分配の組合せを使い、留保で簿価が増える一方で利益を据え置けば終期ROEが低下することを反映する。金融機関の一般的なcash-debtを株主の余剰資産とみなさず、普通株主資本、規制資本、OCI、配当権利の範囲を区別する。

合成例（実銘柄・実証収益ではない）：期首・期末の必要操業cash、非営業資産、非支配持分は0。期末営業EV100、期首現金30・負債10（純余剰資産20）、税・利息後の営業CF13から設備投資3を差し引いたCF10、運転資金変化0、配当2、元本返済4、他の変化0なら、期末現金34・負債6・純余剰資産28、期末株主価値128、受取分配込み130。配当だけ0なら現金36・株主価値130・分配込み130。返済だけ0なら現金38・負債10・株主価値128・分配込み130。設備投資3は既にCF10へ反映済みで、省略すると営業価値も変わり得るため、現金増だけを利益としない。配当2を旧10株に払った後、期末に新2株を対価20で発行する例なら株主価値は148、期末12株の残余価値は1株当たり148/12、旧1株が受け取った分配は2/10。発行対価20は外部から入った資本で、留保CFや旧株主の価値創出ではない。分割・発行・転換の時期と分配権利に合わせ、terminalとcashを同じ起点1株へ換算する。

償却・減損などの非現金費用を正常化する際は、会計上の費用と資産の経済的な消耗を分ける。権利の寿命短縮、競合参入、更新投資が重要なら、単なる足し戻しで従来の利益・cashが継続すると扱わず、残る収益期間・代替投資・終価へ反映する。営業CFには既に足し戻された費用を重ねて足さない。会社予想の残り必要利益は達成条件の算術であり、達成可能性の結論ではない。季節性、比較期の一時損益、為替、事業別の進捗を確認し、四半期利益の単純年率化や不足額だけで回復・未達を断定しない。

倍率上昇には一次資料で確認した利益品質・事業構造等の根拠を記す。機械anchorや相対的割安さだけを根拠にせず、倍率が回復しない反対仮説も検算する。利益と倍率、業績と借換えが連動して悪化する場合は、一変数だけ動かして反証を済ませない。株数は会社EPSとの整合を確認し、自己株式込みのグロス株数を無条件に使わない。金融的妥当性は作者と独立Reviewの責務であり、productionで任意formulaを実行しない。

<a id="draft-calculation"></a>

### Draftの数値生成

`research calculate <INPUTS>`はYAML/JSONのtyped入力を一度読み、JSONをstdoutへ返す。ファイル・DBを書かず、ネット取得やAI実行も行わない。作者と独立Reviewがそれぞれ確認した入力で使い、保存・公開の前に`assessment-evaluate`へ渡す。

入力はStock Assessmentの`input_snapshot`、任意の`derived.metrics`と`valuation`に限定する。ratioの定義は既存DerivedMetricから`value`だけを除いた形で、`formula=ratio / formula_version=ratio-v1`は省略できる。factを参照して既存の単位対応で割算し、`derived.metrics`へ既存検証と同じ精度の値を生成する。例えば1/3は0.3333333333333333となり、表示用の0.3333を保存値に使わない。任意formulaや追加の事業モデルは実行しない。

`valuation`は既存のresolved/unresolved型で渡す。resolvedでは未調整価格fact、terminal/cash、期間、要求年率から既存Projection算術で総価値、総return、年率、`pmax_raw_yen`を計算し、価格・評価日・basisとともに`projection_numbers`へ返す。この出力は計算資料であり、`assessment-evaluate`の公開内容検証や`valuation_context`を代行しない。価格・単位が不適切、参照factがない場合はexit 2でstdoutを出さず、入力エラーをstderrへ返す。`valuation`省略/nullまたはunresolvedなら`projection_numbers=null / recalculated_projections=[]`で、欠測を0へ補わない。成功はexit 0である。

出力`derived`と`valuation`はStock Assessment欄へ、`recalculated_projections`は既存Review欄へ渡せる。terminal/cash自体は入力した値の再掲であり、売上・利益率・税・株数・倍率などから再構成しない。文章の`calculation`も解釈しないため、作者とReviewが同じ誤ったterminalを入力すれば一致する。一次資料・算式・数値代入の独立確認は必要なままであり、作者の入力をReviewへコピーして済ませない。この計算によって保存済み評価やReviewを書き換えない。

機械anchorとのgapは[Valuation metricsの保存済み照会](./valuation-metrics.md#machine-valuation)で原run・時点とともに参照する。企業評価の数値生成へ機械anchorを混ぜない。上流の事業算式を型として追加する必要性は実運用で確認してから検討する。

```bash
uv run baibai-engine research calculate /tmp/research-calculation-inputs.json
```

現金額面と転換価値の超過分を株式で交付するCBに限り、`research calculate --cash-share-cb <INPUTS>`で別の条件付き計算を行える。Stock Assessmentのschemaや`assessment-evaluate`には接続せず、JSONの計算資料だけを返す。額面・本数・通知ごとの対象本数、既存株数、社債債務を既に認識した株主価値、転換価額、各年度の契約上の「1株当たり特別配当」と調整参照終値を入力する。特別配当の年度累計・基準額への超過判定、決議・適用日と参照終値期間は作者が原契約と開示で確認する。算術は各調整の0.1円切捨て、1円未満の調整繰越し、通知単位の1株未満切捨てを適用する。

決済VWAPは評価時点の株価や配当調整用の参照終値とは別の入力である。端数の単元未満株の現金精算価額は**決済VWAPと等しいという明示的な試算**であり、実際の買取価格を確定しない。独立した決済VWAPでの価格と、VWAPを終期株価と同一視する条件付き固定点を並べる。整数株の切捨てによって厳密な固定点が存在しなければ、整数株の固定点はnullとし、連続近似と丸めギャップを示す。額面現金は既計上の社債債務消滅に対応し、株主価値から再控除しない。未転換は追加株式0として表示する。出力の金額は精度を保つ十進数文字列である。利益・倍率、行使時期・確率、実際の決済条件、Stock Assessmentの採否と注文は別途判断する。

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

`research assessment-evaluate`の`valuation_context`は、参照価格factの未調整1株価格P、原評価の期間hと記録済み要求年率rを使う読み取り専用の計算資料である。`price_context=assessment_snapshot`と評価日・価格日・basisを併記し、現在quoteやpolicyで置換しない。古いStock Assessmentも原価格・原期間で読み、当日の購入可否や保有の残存見返りには使わない。

```text
H = required_total_value_at_P
required_terminal_s = max(0, H - cumulative_cash_s)
total_value_surplus_s = terminal_s + cumulative_cash_s - H
fixed_value_delay: terminal_sと累積cash_sを固定し、h+12か月で年率を再計算
```

Base/Downsideの必要terminalは、その累積分配で要求年率以上に届く最低の非負価値である。分配だけでHを超える場合は0という下限であり、等号成立点ではない。総価値差額は符号付き差額で、安全余裕・損失許容額・買い可否ではない。Downsideの不足を追加の不合格条件にしない。Hは既存Pmaxの代数的な読み替えで、FV、市場の期待値、独立した割安signalではない。

固定価値の遅延は追加配当・利息・再投資を仮定せず、総価値と総returnを変えない。全損なら年率も-100%。損失caseで年率損失の絶対値が縮んでも総損失は減らない。実際の遅延では利益・分配・返済・資金調達・希薄化が変わり得るため、materialなら経済条件を一貫して組み直し、既存calculationとcountercaseへ記す。h+12の表示だけで遅延を検証済みとせず、必要年率未達だけを保有sellへ接続しない。

内容検証済みのresolvedな`ready / ready_with_warnings`と、唯一の未完了がReview未添付の`review_required`で表示する。後者は計算資料が読めても`not_ready`・exit 2のままで、公開前に独立Reviewが必要である。未作成の`--review`指定先も未添付として扱う。その他のincompleteとunresolvedはnullとなり、欠損を0や過去supportedで埋めない。既存5項目・終了コードは診断の有無で変わらず、診断の数値計算・変換だけが失敗した場合も全contextをnull、理由をstderrへ出す。parseや既存入力エラーは従来どおりerrorとなる。総return・年率は小数4桁、通貨は既存Decimal変換で表示し、表示丸めを判定に使わない。

### 判断理由への接続例

以下は架空の算術例であり、実銘柄や収益効果の検証ではない。事業の見積りを一次根拠から独立に構成した後、必要価値と比較する。必要額に合わせて利益・倍率を引き上げてはいけない。採用モデルに応じ、判断を左右する少数の仮定を確認する。一般に1〜2点で足りるが、件数をgateや調査打切り条件にしない。

| 例 | 算術と既存文章欄で扱う判断 |
| --- | --- |
| P=1000円、h=12、r=6%、D=30円 | H=1060円、必要terminal=1030円。EPS×PERモデルでPER10を固定するなら必要EPS=103円。数量・利益率・税・株数から独立に見積もったEPSの範囲と比較し、`valuation.base.calculation`へ前提と代入を書く。市場のEPS予想とは呼ばず、資産・EV評価へPER式を強制しない |
| 同じP/h/r、D=0円。AはEPS100×PER13、BはEPS130×PER10でBase1300円 | 両方の総returnは30%だが、PER10のままならAは0%、Bは30%。AがPER10でH=1060円に届くにはEPS106円が必要。Aは利益品質・事業構造による倍率上昇の根拠、Bは受注・利益率等によるEPS回復の根拠を吟味する。どちらの根拠が弱いかをInvestment Proposalの`comparison / forgone / items[].rationale`で説明し、倍率上昇なしだけでBを自動採用しない |
| Base V=1270円/D=30円、Downside V=680円/D=20円 | 総価値1300円/700円、12か月年率30%/-30%。固定価値で24か月なら14.0175%/-16.3340%となるが、総returnは30%/-30%のまま。返済期限が追加資金を要求するなら、その条件を再構成してcountercaseに記す |
| 返済条件不明と、将来EPSの幅 | materialな返済条件を確認できなければuncertain等を維持し、`nonmaterial_unknown_reason`で通さない。一方、一次根拠から将来EPSを幅で置くこと自体は自動排除の理由にしない。根拠のない回復予測や合理的に評価できないcaseはuncertain等とする |
| 企業評価はsupportedだが現在価格がPmax超過 | 同じ評価を参照してPmax以下の条件付きbuyを検討できる。実quoteを安い架空価格へ置換せず、仮説・cash・保有・権利単位等のgateを別に確認する |

事業・資本配分による価値創出と、その価値が市場価格に未織込みという仮説を分け、重要な観測・外部との相違は既存`findings.value_gap_explanation / hypotheses`へ書く。成立する経済経路と反証条件は`investment_case.explanation / status_reason / invalidation_conditions`へ、Base/Downsideの`calculation`には将来cash、終価の評価、分配、期間へ与える方向または感応度を記し、要求率は`valuation.required_annual_return_pct`とその経済的理由で扱う。市場環境がcashに影響しなくても割引率・市場の織込み・流動性を通じて判断に関わり得る。どの経路を採ったか示し、同じリスクを利益・倍率・要求率で無説明に重複計上しない。作者と独立Reviewの`strongest_countercase`には結論を覆す反例を記す。外部の元評価者、metric、対象期、株式basis、元更新日・前提が揃わないときは比較できない条件を明示し、値だけを自己終価へ採用しない。同じ候補の複数仮説も分けて検討し、戦略名や全入力の採否欄を固定しない。数値の引用は原価格・期間・仮定に対応させる。例えば公表済み分配だけの保守条件と、未公表の次期分配維持を含むBaseでは、採用した権利・分配条件が異なることをhypothesesや結論でも省略しない。異なる条件のreturnを同じcaseの裏付け・反証として混ぜない。全出力や新しい派生fieldをStock Assessment/Investment Proposalへ複写しない。

作成・評価・独立Review・公開・Proposalへの接続順序は[Research skill](../../.agents/skills/research/SKILL.md#company-research)が所有する。

<a id="permanent-loss-axes"></a>

## 永久損失7軸

`funding_liquidity / debt_repayment / cash_flow / dilution / customer_concentration / structural_decline / governance_accounting`を各1件持つ。評価はacceptable/adverse/unknown、証拠はverified/partially_verified/unverified。赤字や下方修正だけを永久損失としない。重要な根拠不足を小口購入やoverrideで通さない。非重要なunknownを許容する理由はReviewへ記す。

AIを含む技術・産業構造変化も、materialな場合だけ通常のcalculation、investment case、リスク、反対仮説へ接続する。専用のscoreやchecklistは設けない。

<a id="independent-second-pass"></a>

## 独立Review

全公開Stock Assessmentに、review_id、独立passのidentity/role/time、exact core hash、primary_source_check、checked_source_ids、review_scope、最強反証を持つReviewを組み合わせる。Base/Downside各1件のterminal/cashを独立に再計算する。原本の比較表では行・年度見出し・当期/前期列を合わせて照合し、期間内入金と終期の配当権利を別に検算する。作者値をscaffoldでコピーして検算としない。照合許容差は0.0001円。source・単位・hash不一致は公開できない。

外部評価との一致は、それだけで独立した割安の確認にはならない。同じ会社予想・価格・倍率・配信原典を共有する一致は重複した根拠になり得る。比較可能な対象期と株式basisを揃え、自己評価とは異なる根拠が主要仮定を支える範囲で確信を補強する。一致の票数や精密な確率へ変換しない。不一致も棄却せず、利益見通し・資本コスト・倍率・評価期間のどこが違うかを既存の仮説と反対仮説へ戻す。過去高値からの下落や外部目標値との乖離は、将来収益の悪化を織り込んだ結果かもしれず、単独で安全余裕と呼ばない。

materialな[資本のつながり](#capital-bridge)はReview側も一次資料と独立入力で追い、留保・投資・分配・返済・希薄化の片側だけを動かした仮定を確認する。重要な市場/マクロの観測が将来cash、終価の評価、要求率、市場の織込みのどこへ効くか、外部評価との重要な相違が比較可能か、不明ならその制約を確認する。配当停止なら残る現金、借入返済なら残る負債、増資なら調達額と分母株数も更新し、結論が変わる条件を既存の反対仮説・riskへ記す。最終terminal/cashの数値一致だけでは上流仮定の正しさを証明しない。

primary_source_check=verifiedは重要な判断根拠を確認した意味で、全非開示事項を解明した意味ではない。重要なunknownはsupportedを通さず、非重要なunknownは`nonmaterial_unknown_reason`で説明する。hashは整合性を保証するだけで、独立性や文章の真実性の証明ではない。

<a id="core-hash"></a>

## 公開・読込

`stock_assessment / stock_assessment_review`へ同一connection・短いtransactionで原子的に公開する。Review後付けの単独公開を持たない。`published_at`はwriterの公開時刻で、`conclusion.assessed_at`とは別である。

AssessmentとReviewのUUIDは各scaffoldで発行する。Assessmentの同一素材・同一supersedes先での再生成は公開IDを維持し、Reviewの再生成は新しいReview IDを発行する。同IDは両方の同内容なら既存pairを返し、異内容は拒否する。新revisionは同tickerの最新revisionをsupersedeし、評価日・公開順を逆行させない。最新評価を先に選び、その後に検証する。unsupported/unresolvedを理由に古いsupportedへfallbackしない。公開順はtimezoneを含む正確なinstantを比較する。

共通読込は公開時刻でexact pairを検証し、現在の価格・cash・保有を適用しない。参照はassessment_id/core_sha256/review_idの一組へ束縛する。新規の行動はentry_policy、保有の経済判断はholding_policy、提案の保存と現在再計算はInvestment Proposalが所有する。

<a id="assessment-refresh"></a>

## 差分refreshと現在価格

同日には同じimmutable pairをそのままProposalから参照してprice/cashを再計算できる。翌日以降の新規買付には当日起点のvaluationが必要であり、`assessment-scaffold --from-assessment-id`で指定した保存pairを素材に差分draftを作る。素材のprior refと、公開時に置き換える最新pairのsupersedes先は分けて固定する。原典sourceのas_of/retrieved_atとfactの観測日は変更しない。

新しい期間・今後分配とrefresh確認理由は未入力で出す。価値・materialな開示・投資仮説・株式単位・残る期間・受け取る分配を作者が再確認し、今回の全core hashへ新しい独立Reviewを取る。review_scopeに、新資料を再確認した範囲、以前の保存根拠を読み直した範囲と限界を記す。旧Reviewのhashや時刻を付け替えず、全原典の毎回再fetchを一律に要求しない。

例として旧12ヶ月、終価1200円、累積分配60円を3ヶ月後に使う際は、既に受け取った20円を除いた今後分配40円と、残る約9ヶ月・終価を維持する根拠を確認する。台帳所得だけから一株分配を自動推定しない。旧12ヶ月を日々延長せず、確認できなければvaluation unresolvedとする。horizon_monthsは整数月の比較窓であり、固定満期の正確な残日数を黙って丸めた値ではない。

古い評価は原価格・原期間でいつでも閲覧でき、現在projectionが不可でも評価を悪い・失効と断定しない。仮説維持を今回確認した保有継続と、当日の残る収益算術は[Proposalのholding契約](./investment-proposal.md#holding)で分ける。

`current_price_projection`は同日評価・利用可能なquote・同じ権利単位が揃う場合だけ現在の未調整価格によるBase/Downsideを返す。保存した原価格・予測を置換しない。この一時出力は条件付き税費用前・分配再投資なしの算術で、確率加重の期待利回りや売却閾値ではない。

## Workspaceとコマンド

[Researchの固定選定](./analysis-operations.md#明示開始と固定対象)が対象を所有する。newとholdingは共通の評価pairを公開し、Operationへassessment_refを記録する。保有はholdings/<ticker>子workspaceで現在holdingを確認し、入力正本は親の固定snapshotを共有する。無関係なledger更新で企業調査をやり直さない。

Caseのassessment-draft.yaml、assessment-review.yamlとassessment-publication.yamlを使う。最後のfileはscaffoldで固定する公開IDとsupersedes参照だけを持ち、企業評価本文の第二正本ではない。--forceだけでは公開IDを変えず、素材のprior参照とsupersedes先が同じなら再生成・再試行でも維持する。素材または置換先の最新評価が変わった場合は新しい公開IDを発行する。公開状態はexact document/Reviewとの一致で決まり、自由なchecklistを第二gateにしない。

scaffoldの--outは任意FILEへの追加exportを指定でき、同じpayloadをworkspaceのcanonical draftにも生成する。公開はworkspace側の本文と固定IDを使う。別exportを編集した場合はworkspace側へ明示的にコピーするかworkspace側を直接編集し、exportの変更を黙って採用しない。出力衝突は--forceの明示なしに上書きしない。作成・独立Review・公開・提案への順序は[Research skill](../../.agents/skills/research/SKILL.md#company-research)が所有する。
