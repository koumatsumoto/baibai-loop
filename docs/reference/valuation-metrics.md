---
title: "Valuation metrics"
summary: "機械ER/FV推計と読取、valuation指標の単位、欠損、入力basis。"
doc_type: reference
status: active
---

# valuation-metrics — valuation 指標の算出仕様

Baibai Loop スクリーニングで使う valuation 指標の算出仕様とデータソース。screening run storeとapplication DBのStock Assessmentで参照される指標の前提を確定する。

Candidate Discoveryのprimary authorityは4 Valuation Approachesのnative eligibility / orderである。機械E[r]はvaluation reversionとcarryをまとめたsecondary return priorで、候補一覧 membership、global rank、AI research priorityを所有しない。

Researchの判断見積りは[Stock AssessmentのBase/Downsideと共通算術](./stock-assessment.md#scenario-arithmetic)に従う。銘柄とシナリオに対応する比較期間について、sourceに基づく価値・累積分配・returnを計算し、[独立Review](./stock-assessment.md#independent-second-pass)で反証する。

screening時点のE[r] / FV anchorは候補比較の文脈として表示・参照し、[Stock Assessment payloadへ重複転記しない](./stock-assessment.md#input-snapshot-and-lineage)。機械E[r]の5年priorと3y/5y calibrationは個別判断の期間を固定しない。保存済み較正文脈の意味・解釈はValuationが所有し、生成とevidenceの条件は[見積り較正](./estimate-calibration.md#判断面へ渡す較正文脈)を参照する。

財務・倍率計算の正本は[valuation/metrics](../../engine/src/baibai_engine/valuation/metrics/)にある。`models`がFinancialSnapshot/4項目TTMRules/配当basisを所有し、`financial_snapshot`が生成本体、`periods`が会計期間/TTM、`capital`が株式/普通株自己資本basis、`dividends`が配当/還元、`profit`が利益、`history`が価格感応倍率履歴、`relative`が値で渡された業種/市場母集団と自己レンジの倍率比較を所有する。価格騰落・標本volatility・出来高・安値乖離・信用需給は`market_context/{history,margin}`、共有split算術は`market/bars`、対象/母集団選択とStock Analysis向けの結果組立は`screening/metrics/snapshot`が所有する。ScreeningRulesはTTMRulesを構成して計算へ渡し、計算領域へ設定読込や母集団選択を持ち込まない。本書は指標の意味と、異なるbasisを混ぜないための理由を扱う。

<a id="machine-valuation"></a>

## 機械ER/FVと保存済み照会

ERの計算は[`valuation/estimates.py`](../../engine/src/baibai_engine/valuation/estimates.py)が所有する。PBRと収益倍率（正の予想PERを優先し、なければ実績PER）の各軸で、業種中央値と自己レンジ中央値の低い方をanchorとする。片方欠測なら他方、軸が一つも取れなければ推計なし。配当の株式基準が未解決の場合も推計なしとする。

```text
upside_axis = min(available_sector_anchor, available_self_anchor) / current_multiple - 1
upside_blend = mean(available_upside_axes)
reversion_annual = 0.10 * clip(upside_blend, +/-0.50)
carry_annual = dividend_yield + clip(-net_share_change_yoy, +/-0.05)
er_annual = reversion_annual + carry_annual
sector_median_anchor = price * mean(available_sector_anchor / current_multiple)
self_range_anchor = price * mean(available_self_anchor / current_multiple)
```

carryの`dividend_yield / net_share_change_yoy`が欠測なら、その成分の計算上の寄与は0である。これは無配・株数不変を観測した意味ではない。Valuationの公開読取、Researchの機械anchor、新しい候補一覧とResearch入力は、この二つのnullable入力と`dividend_basis`、`carry_missing_assumption`を併記する。株数変化はclip前のグロス発行済株式数YoY（増加が正、減少が負）であり、将来の自社株買いcashではない。ER値・算式・既存の`er_assumptions`は変更しない。
新しい候補一覧とResearch入力は、同じStock Analysisの`dps_actual_annual / dps_forecast_annual`も欠測を保持して投影する。両値は配当計算の株式basisへ正規化した入力であり、原典の未加工値や予想配当の持続性の認定ではない。保存済みの旧publicationには後付けしない。

保存済み候補一覧とResearch snapshotに成分fieldが無い場合は「そのsnapshotに記録なし」であり、nullや0を補完して保存・再発行しない。Researchも欠測・field不在をゼロ観測の根拠にしない。直接のValuation/Research読取は元Stock Analysisに残る成分を引用できるが、凍結した判断入力へ後から混ぜない。

ERは年率ratio（0.1＝10%/年）の機械priorで、モデル・単位・成分・前提を併記する。業種中央値anchorはERの保守側blendそのものではなく、自己レンジは現在fundamentalsを固定した価格比較（[§9](#9-過去自己比較過去-3-年レンジ)）である。企業固有の調査FV・期間内分配・Projectionと同一の評価名へまとめない。4 Approachの推薦式・推薦枠は[Screening runtime](./screening-runtime.md#candidate-discovery)が所有する。

保存名`fv_sector_median_yen / fv_self_range_yen / er_*`とserializerの丸めは維持する。読取は再計算しない。`valuation.query.machine_valuation_from_analysis`が小さな機械評価結果へ投影し、Webの一覧・銘柄詳細・履歴は同じ保存値の業種中央値anchorと参考価格の乖離を表示する。候補一覧入りを照会条件にせず、候補外の保存済みStock Analysisも読める。

`read_api.stored_machine_valuation`はticker、cutoff、任意の固定run identityを受ける。通常はcutoff以下の最新runを`asof_date / run_at / run_revision_id`降順で選び、そのrun内でtickerを読む。最新全体が未来でも過去runを選び、固定runの未来as-ofや不明identityは拒否する。cutoffはrunの論理as-ofであり、後日に生成した過去as-ofのreplayも選択し得る。対象runなし、run内tickerなし、推計座標欠測を区別し、古いrunの値や候補一覧で穴を埋めない。取得・DB書込・AI起動は行わない。ticker profileの保存済み解析も同じrun解決を使う。

## 1. 使用指標

| 指標 | 定義 | データ項目 |
| --- | --- | --- |
| PER (Forward) | 株価 / 会社予想 EPS | 株価、会社予想 EPS |
| PER (Trailing) | 時価総額 / 直近 4 四半期の純利益 | 時価総額、純利益 直近 4Q 合算 |
| PER (3FY平均EPSベース) | 株価 / 直近 3 FY の分割補正後 EPS 算術平均 | 株価、FY EPS、株式分割・併合係数 |
| PBR | 株価 / 1 株純資産（BPS） | 株価、BPS |
| EV/EBITDA | (時価総額 + 有利子負債 - 現金) / EBITDA | 時価総額、有利子負債、現金、EBITDA |
| P/S | 株価 / 1 株売上高 | 株価、直近 4Q 売上 |
| PCFR | 株価 / 1 株営業 CF | 株価、直近 4Q 営業 CF |
| Net cash ratio | (現金 - 有利子負債) / 時価総額 | EDINET CSV-derived cash / debt、時価総額 |
| Asset-backed ratio | (Net cash + 投資有価証券) / 時価総額 | EDINET CSV-derived cash / debt / 投資有価証券、時価総額 |
| FCF yield | (営業 CF - 設備投資支出) / 時価総額 | EDINET CSV-derived CFO / capex、時価総額 |

`normalized_per_3fy`は連続3FYの実績EPS平均に基づく保存名であり、一時損益・景気循環を調整した正常利益ではない。企業固有の代表利益は一次資料と仮定から[Stock Assessment](./stock-assessment.md#scenario-arithmetic)で構成する。Current/Normalized Earningsの探索は業種gapとPERを優先し、CFは同順位時の補助であり、CF品質の選別済みとは扱わない。

### Researchへ渡すCF文脈

`valuation.query.cash_flow_from_analysis`は保存済みStock Analysisの金額・利回り・品質を出所別に読む。J-QuantsのOCF TTMと、選択EDINET書類のOCF/capex/FCFは別系列であり、同一期間・連結範囲を保証しない。異なる系列のOCFとcapexを減算してFCFを作らない。

Researchの選択済み銘柄入力の`cash_flow`は、機械anchorと同じrunの保存値を引用し、EDINETの書類ID・種類・提出日時・書類metadata期間・capex出所を残す。書類metadata期間はCF実測期間ではなく、最新財務開示日もOCF合成期間ではない。必要なら原典のCF計算書で期間・連結範囲・単位を確認する。欠測はnullのまま、明示0と負値は保持し、後日の資料で補完しない。Researchの合成入口と入力確定時刻は[Research進行](./analysis-operations.md#明示開始と固定対象)に従う。

## 2. Forward PER の取得方針（重要）

### 2.1 採用ソース

- **会社予想 EPS ベース**（会社が期初/修正後に開示した公式予想）
- 取得: J-Quantsの会社予想EPS。保存済み文書を[`market/financial_documents`](../../engine/src/baibai_engine/market/financial_documents.py)の共通純粋投影で読み、対象期・連結basis・指標sourceを解決する。EPSが有効なら純利益・経常利益の欠測だけでPERを落とさない。IFRS等の経常利益欠測も許す。

数値の有無より先に、as-ofに適用する最新の決算期状態を決める。当期・翌期とprimary・nonconsolidatedの全欄は保存し、異なる期間やbasisを混ぜない。単体会社のprimary欄に入った翌期予想も使うが、連結会社の単体補足予想で連結予想を上書きしない。利益3項目は同一対象期・basisの最新の同時開示候補からfield単位で読む。非競合の補完項目を単一文書へ限定せず、項目ごとのsource recordを残す。異なる対象期・basisや、順序が確定している古いrevisionの項目を混ぜない。年間DPSは独立した文書streamから明示0を保持して解決する。空欄はその文書の非掲載であり、撤回の確認に読み替えない。新しい決算期の空欄を古い対象期の数値で埋めない。

source文書のbasisと採用対象のbasisを別に保持する。採用対象は実績と同じ会計期間の順序で選び、短縮決算より後の日付を持つ旧年度末や、遅れて届いた過年度訂正で現在の区分を巻き戻さない。単体→連結・連結→単体の移行後に、旧primary予想を新basisへラベル替えしない。同時刻のstatementがbasisについて矛盾すれば、basis自体を未解決にする。

開示時刻不明は同日の既知時刻より古いとは決めず、候補として残す。開示日・時刻が同じ文書、または時刻不明で順序を確定できない文書の矛盾は対象fieldを`unresolved`とし、DocNoの大小や投入順で採否を決めない。一致しているEPS・NP・Sales等まで落とさず、利益予想の一部だけが矛盾した場合は`partially_unresolved`を記録する。Stock Analysisの`earnings_forecast_source` / `dividend_forecast_source`はJSON文字列で、status・文書番号・開示日/時刻・DocType・対象期間・sourceのbasis・採用対象basisを残す。現行の原本を文書単位で読み、対象期間が欠ける・逆転する文書から当期/翌期予想を値の有無で補完しない。旧date-only保存を読む互換経路は持たず、保存形式の切替時は保持原本から再構築する。

### 2.2 却下したソース

- **アナリストコンセンサスEPS**: 機械Forward PERには使用しない。External Assessmentで読む外部予測と区別する。
- **期初予想のみ使用**: 期中の修正予想を無視すると精度低下、最新の修正予想を使う

### 2.3 会社予想未公表 or 予想レンジ提示銘柄の扱い

有効な会社予想EPSを得られなければ`per_forward`はnullとする。Current Earnings Powerは正のforward PERを優先し、なければ正のtrailing PERを使う。選んだPERと対応gapを得られない場合は、そのApproachではnominateしない。他Approachの条件やStock Assessmentの評価方法へ、このfallbackを横展開しない。

### 2.4 予想純利益と予想経常利益の比較注記

**判定:** 同じ予想期の予想当期純利益と予想経常利益が両方あり、前者が後者を上回る場合に`forecast_special_gain`を立てる。大小関係だけでは差の原因や持続性は確定しない。特別損益・税等の影響は一次開示で確認する。

**用途:** 予想利益の質を調査するための注記である。画面では「予想純利益＞予想経常利益」と表示する。一時益や特別利益の発生を確定した表示にはしない。

**非目標:** ranking、E[r]、既存指標の計算は変えない。これはdoctrineのwarning / annotation境界に従う。

### 2.5 会社予想の通期赤字 annotation

**判定:** 会社予想の**予想経常利益または予想当期純利益が負**なら、`forecast_full_year_loss` flagを立てる。片方しか開示されない期があるためORで判定する。予想が一つもない行はFalseに置き、欠損を黒字予想へ畳まない。

**用途:** 予想経常利益または予想純利益の赤字を示す注記であり、flagだけから予想EPSの符号や採用FV anchorを断定しない。実際のanchorは利用可能な入力とestimatorの条件で決まる。過去の価格レンジを現在の収益力の証明にしない。

**非目標:** 除外にも減衰にも使わず、annotationにする。一過性の赤字（引当・減損）と構造的な縮小を機械では区別できないため、一次開示を読むresearchが判定する。

## 3. Trailing PER の算出

- 直近 4 四半期の合算**純利益**（円）を使い、`時価総額 / 純利益 TTM` とする。P/S・PCFR・EV/EBITDA と同じ形になる
- 開示の利益は期中累計なので、`直近累計 + 前期通期 - 前年同期間累計` で 12 か月へ直す。前年FY末と同期間の開始・終了は月末を保つ12か月前の日付で照合する。合成できない期は `null`（単一四半期で割った偽の割高 PER を作らない）
- 配当・予想だけで実績anchorを持たない開示行は実績期間の選択から外す。直近累計は開示日ではなく会計期間の順序で最新の実績行を選ぶ。前期通期・前年同期間も先に選び、必要fieldが欠ける場合は`null`とする。過年度や同じ期間の古いrevisionへ補完しない
- 合成する3項の連結・単体区分が異なれば`null`とし、区分を合わせるために古い訂正前の値へ戻らない。同一区分だけで連結範囲や会計方針の不変までは保証しない。
- 合成に必要な期間・値が揃わなければ`null`とする。別途の「確報確定」状態は判定しておらず、取得済みの実績開示と期間照合に基づく。
- 赤字期（純利益マイナス）は `null` を採用（割安検出に意味を持たない）

**合成は円で行い、1 株当たりへの換算は最後に 1 回だけ行う。** 1 株当たりの各項は自分の期の株式数で割られているため、株式数が動いた会社では和・差が成立しない。買収の新株発行で株式数が 783M → 1,556M と動いた会社では、1 株当たりで合成すると `21.32 + 113.50 - 161.76 = -26.94` となり、黒字の会社が赤字に見えて収益 anchor を失う。**EPSの前年比も、比較期間と分割・株式basisを確認して読む。** 比であることだけで基準の一致は保証されない。

`FinancialSnapshot.eps` は `純利益 TTM / 自己株控除後株式数`で、提出者が開示する 1 株当たり当期純利益（分母は期中平均株式数）ではない。時価総額と同じ資本分母で組み直すことで `株価 / eps == per_trailing` が厳密に成立し、同じ語が 2 つの値を指さない。

### 3.1 3FY normalized PER の算出と用途

`normalized_per_3fy` は、as-of 以前に開示された直近 3 FY の EPS を現在の株式数基準へ分割補正し、その単純平均で現在株価を割る raw Derived Metric である。FYの連続性は月末を保って前年へ移した末日で確認するため、2/28と閏年の2/29は連続する。各FYは原本の期間終了日が年度末に一致し、開示日までに終了した期間長が共通TTM規則の通期範囲内であることを確認する。最新FYと連結・単体区分が異なる年度や、期間が欠ける・短い・不正な年度を古い年で埋めない。3 FY が揃わない、選択済みFYのEPSが欠ける、補正後 EPS 平均が正でない、または価格・分割係数を確定できない場合は `null` とし、別指標へのフォールバックは行わない。

`normalized_per_3fy`と同sector gapはNormalized Earnings Powerのnative eligibility/orderに使う。一方、FV/E[r] estimatorの入力には使わない。一時損益の中身を直接判定する指標ではなく、3FY earnings powerを単年利益と別座標で比較するDerived Metricである。

## 4. PBR の算出

PBRの普通株自己資本と株式basisは[資本の分母](#51-資本の分母)に従う。自己資本と非支配株主持分を含む純資産を代用しない。

## 5. EV/EBITDA の算出

- **時価総額**: 直近営業日終値 × **自己株式を除いた株式数**（発行済株式総数 − 期末自己株式数）。[§5.1](#51-資本の分母) が正本
- **有利子負債・現金**: EDINET抽出値の定義と採用条件は[§7.1](#71-net-cash-ratio--asset-backed-ratio--fcf-yield)に従う。
- **EBITDA**: 選択したEDINET書類の営業利益に対応する減価償却・償却科目を加算した値。半期等の取得値を別書類からTTMへ合成せず、期間品質は[§11](#11-半期移行と-ttm-品質)に従う。
- **有効条件**: EV と EBITDA がどちらも正のときだけ倍率として採用する

### 5.1 資本の分母

時価総額と自己資本比率は、開示された概念と同じ分母で作る。J-Quants の field 名は会計概念と一対一でないので、素の値をそのまま使うと同じ概念を測る指標どうしが食い違う。

| 概念 | 使う値 | 使わない値 |
| --- | --- | --- |
| 時価総額の株式数 | 同じ資本状態の `ShOutFY` − `TrShFY`（発行済 − 期末自己株式） | `ShOutFY` 単独、または `AvgSh` − `TrShFY` |
| 自己資本比率 | `EqAR`（開示値） | `Eq / TA`（`Eq` は非支配株主持分を含む純資産） |
| PBR | 時価総額を、同一状態の条件を満たす`TA × EqAR`または`BPS × 自己株控除後株式数`による普通株自己資本で割る | 別開示日の`TA × EqAR`、非支配株主持分を含む純資産 |
| trailing 収益 | 報告純利益の TTM 合成（円）。倍率は時価総額 ÷ それ | `EPS` × 株式数の再構成 |
| accruals の純利益 | 報告純利益の TTM 合成（円） | `EPS` × `ShOutFY` |

報告利益等の総額が必要なら報告された円総額を優先する。EPSに期末の発行済株式数を掛けて利益を復元しない。EPSの期中平均株式数とBPSの期末普通株basisは別の概念であり、本節の条件を満たすBPS経路まで禁止するものではない。

自己株式は議決権も配当請求権も持たないので、時価総額に含めると過大になり、現金比率・利回りが薄く、倍率が割高に出る。自己株式を積み上げた企業ほど歪みが大きく、buybackを含む見返りの評価にも影響する。

**PBRはoutputを1つだけ持つ。** 普通株basisとの照合に通り、かつ最新`BPS`以上に新しい同一行の`TA × EqAR`があれば、その鮮度を使う。照合できない場合は`BPS × 自己株控除後株式数`へfallbackする。`EqAR`の小数第3位・`BPS`の小数第2位という公表精度は丸め区間として比較し、near-zero比率を相対誤差だけで拒否しない。Asset ValueはこのPBRを比較文脈と並び順の従キーに使う。eligibility/orderは[screening runtime](./screening-runtime.md#candidate-discovery)に従う。

**欠損を代用で埋めない。** 観測済みの資本状態から現在の自己株式数を安全に解決できない場合は**時価総額を出さない** — 発行済だけで代用すると、どれだけ過大か分からない値が現金比率・利回り・時価総額gateへ入る。発行済を超える自己株式数のような破損値も同じく答えない。自己資本比率が観測できない行は `equity_ratio` を `null` にし、純資産比率で代用しない（純資産は非支配株主持分を含み得るため、普通株自己資本と同じ量ではない）。時価総額が欠ける銘柄はcommon eligible母集団から外れるが、ADV欠損は外す理由にしない。

`TA × EqAR` で普通株自己資本の円経路を組むときは、両方を同時に観測した最新の開示行を使う。個々の最新値は staleness fact と表示には carry できるが、別開示日の `TA` と `EqAR` を掛けると、その間の資産変動を自己資本へ混入させる。両方を持つ行が無い場合、またはその同一行が最新`BPS`より古い場合は円経路を答えず、普通株基準の`BPS`経路を使う。同一状態へ揃えるために、より新しい資本状態を古い値へ巻き戻さない。

発行済株式総数と自己株式数は状態量なので、どちらも観測しない行へ既知の組をcarryできる。ただし、より新しい`ShOutFY`を持つ行が`TrShFY`を欠く場合、過去の自己株式数を組み合わせない。過去の明示`TrShFY = 0`も、現在の0を証明しない。欠測だけでは取得・処分・消却などを区別できず、gross issuedの増加・減少・不変や`AvgSh`との一致を安全条件にしない。新しい同一状態の組（自己株式0を含む）があれば、そのstateから再開する。これは自己株式が実際に変化したという断定ではなく、欠測入力から現在値を捏造しないためのavailability policyである。

`AvgSh` は EPS の期中平均株式数であり、gross issued の代替ではない。`ShOutFY == AvgSh`や`ShOutFY + TrShFY`と過去の発行済株式数の一致だけでは、誤転記や自己株式の二重控除を証明できない。この数値パターンによる除外は行わず、提供元が報告した各fieldを区別して計算する。負の自己株式数、非正の発行済、発行済以上の自己株式も有効な株式数へ変換しない。異なる観測を組み合わせようとした場合、自己株式数の観測行にも有効な`ShOutFY > TrShFY`が必要であり、発行済だけを後から再観測しても旧自己株式数は復活しない。不整合は`capital_basis_failure_reason`に`indeterminate_treasury_after_later_issued_observation`、`treasury_observation_without_issued_basis`、`invalid_treasury_source_capital_basis`、`conflicting_capital_document_sources`、`indeterminate_share_basis`、または`invalid_issued_or_treasury_shares`を記録し、時価総額とその派生倍率・利回りを`null`にする。

分割を跨ぐ行では自己株式数も発行済と同じ factor で換算する。片方だけ換算すると差である自己株控除後株式数が壊れる。


EV がゼロ以下、または EBITDA がゼロ以下の場合、EV/EBITDA は `null` として valuation-reversion から除外する。負の EV は net cash / cash-rich valuation approach で扱うべき balance sheet evidence であり、負の EBITDA は倍率が「低い」ほど割安という解釈が成立しないため。

## 6. P/S の算出

- 直近 4 四半期の売上高合算
- 原本のprimary欄の連結・単体区分を保持し、[共通TTM規則](#3-trailing-per-の算出)に従う。

## 7. PCFR の算出

- 直近 4 四半期の営業 CF 合算
- 営業 CF マイナスの企業は `null` を採用（割安検出に意味を持たない）

## 7.1 Net cash ratio / Asset-backed ratio / FCF yield

EDINET `type=5` CSV-derived metrics から以下を抽出する。金額は native の `ユニットID=JPY` を確認し、表示単位の「円」や数値だけから通貨を推測しない。[閲覧操作ガイド](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140133.pdf) の CSV 仕様に従い、値の ASCII `-` は明示ゼロとする。空欄・見た目の似た横棒はゼロへ置換せず、同じ項目・context の JPY 値が競合する場合も算出不能とする。欠測や通貨不明の当期行を除いて前期の数値へ戻る補完は行わない。元 ZIP の値は変更しない。

財務本表の当期contextを先に一つの期間・連結範囲へ絞る。年次は `CurrentYearInstant/Duration`、半期は `InterimInstant/Duration`、旧四半期報告は `CurrentQuarterInstant` と累計の `CurrentYTDDuration` を使い、四半期単独期間を累計値へ混ぜない。個別は対応する `_NonConsolidatedMember` で区別する。セグメント等の追加次元・前期・翌期を当期全社値の穴埋めに使わず、辞書順によるcontext選択を行わない。CSVの「連結・個別」が「その他」でも財務本表のcontextで範囲を判断し、非財務の記述箇所へこの規則を一般化しない。根拠は[現行インスタンスガイド](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140112.pdf#page=49)と[旧四半期のcontext定義](https://www.fsa.go.jp/search/20170228/2b.pdf#page=51)。

- `cash`: 現金及び預金を優先し、該当科目がない場合に現金及び現金同等物を使う。両者をCSV行順で選ばない
- `debt`: 短期借入金、1 年内返済予定長期借入金、社債、長期借入金、リース債務等の合算。意味を確認した標準科目の合算であり、名称だけが似た提出者独自の総額・別名を加えない。開示の全債務を網羅したことを保証する値ではない
- `investment_securities`: BS の `InvestmentSecurities` exact local name だけを連結優先・単体 fallback で抽出する帳簿価額。関係会社株式、営業投資有価証券、包括的な `Securities`、売却損益・CF、text block は合算しない。CSV仕様で定義された ASCII `-` または数値ゼロだけを 0 とし、空欄や似た横棒を補完しない
- `edinet_ocf_ttm`: EDINET CSV から抽出した営業活動によるキャッシュ・フロー
- 減価償却加算は `DepreciationAndAmortizationOpeCF` と `AmortizationOfGoodwillOpeCF` の観測値を使う。両者は標準間接法CFの別の加算子であり、同義名として片方を落とさない。のれん償却だけで減価償却費を代用せず、減価償却費が不明ならEBITDAを算出しない。根拠のない短縮名・提出者独自名をさらに足さない。[標準タクソノミ](https://disclosure2.edinet-fsa.go.jp/weee0020.aspx) の2016・2026年版を照合した範囲の対応であり、IFRSの異なる要素名を自動的な別名としない。
- `capex_ttm`: 有形固定資産・無形固定資産の取得支出。CFOと同じcontext・連結/単体の合算科目を優先し、合算がない場合は有形・無形の両科目が揃うときだけ足す。合算と内訳は二重計上せず、欠測をゼロにしない。符号は取得支出額の絶対値へ一度だけ正規化する
- `net_cash = cash - debt`
- `asset_backed_ratio = (net_cash + investment_securities) / market_cap`
- `fcf_ttm = edinet_ocf_ttm - capex_ttm`

`asset_backed_ratio` は投資有価証券の帳簿価額を加えた gross proxy である。上場株式だけでなく非上場・低流動性の保有を含み得て、含み損益、売却税、持合い・契約上の売却制約、事業上必要な保有を反映しない。このため marketable / liquid / fair value の指標とは呼ばない。非金融企業だけを対象とするCandidate DiscoveryのAsset Valueと較正panelの調査入口に限定し、E[r]、FV、Stock Analysis全体の順位、warningには使わない。銀行業、保険業、その他金融業、証券・商品先物取引業は、cash / debt / securitiesが事業そのものなのでAsset Valueのeligibilityから除外する。`net_cash`、`investment_securities`、正の時価総額のいずれかが欠ける場合は`null`とし、net debtが投資有価証券を上回る場合の負値はそのまま保持する。`net_cash_to_market_cap`はStock Analysisの比較文脈へ残すが、Asset Valueのeligibility / orderには使わない。

J-Quants 財務サマリー由来の `ocf_ttm` は OCF yield / PCFR 系の判定に使う。

**EDINET の値には、総資産による規模差の検査を適用する。** 抽出器は 1 つの書類を連結・単体のどちらかの基準で読み、screening はその値を短信由来の時価総額・TTM 系列と組み合わせて比率にする。連結財務諸表を持つ会社の書類を単体基準で読むと、比率の分子と分母の範囲がずれ得る。双方に正の総資産がある場合、EDINETと短信の比が0.5〜2.0の範囲を外れる行は、EDINET 由来の値（`cash` / `debt` / `net_cash` / `ebitda_ttm` / `fcf_ttm` / `capex_ttm` / `investment_securities` / `edinet_ocf_ttm`）を出さず、`edinet_failure_reasons` に `entity_scale_mismatch` を載せる。

双方の正の総資産を得られず照合できない行は、連結基準ならそのまま使い、単体基準・基準不明なら使わない。通過は同一時点・連結範囲の一致を証明せず、範囲外という結果だけでも原因は特定できない。原典での確認を省く条件ではない。

落とすのは値だけで、`consolidation_basis` と書類の出所は残す。短信由来の指標（PBR・PER・`cash_to_market_cap`・自己資本比率）も残るので、**銘柄は universe に留まり screening され続ける**。必要なEDINET指標を欠くApproachではnominateしないが、他の指標・Approachまで一律に無効にしない。現行Asset Valueの`net_cash_to_market_cap`はanalysis contextであり、eligibility/orderには使わない。

対象書類は有価証券報告書 / 四半期報告書 / 半期報告書と、それぞれの訂正書を扱う。訂正書は EDINET documents API 上で `periodStart` / `periodEnd` が欠損しやすいため、欠損時のみ `docDescription` の対象期間から fallback parse する。書類選択は[EDINET provider](../../engine/src/baibai_engine/market/providers/edinet.py)と[保存・選択処理](../../engine/src/baibai_engine/market/edinet_metrics/store.py)を参照する。書類metadataの期間と、抽出したCF・BSの測定期間を同一視しない。

[EDINET API仕様](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf)の書類情報訂正を畳む際は、保存したnative responseの同日・seqNumber・docIDに対応する操作行を使う。供給されたキーの明示nullは反映し、キーの省略は以前の値を変更しない。特定の4項目が揃うかから完全な置換を推定しない。native responseを持たない保存行ではSQL NULLから元のキー有無を復元できないため、NULLは明示的な消去の証拠にしない。通常の提出行は閲覧期限前に観測したidentityを保持し、同日の複数行は操作日時・seqNumberの順で選ぶ。

`edinet_source_period_start` / `edinet_source_period_end` は EDINET documents metadata 上の書類対象期間であり、必ずしも抽出 metric の測定期間そのものではない。特に半期報告書 / 訂正半期報告書では fiscal year 全体の period end が入ることがある。screening では source traceability と document selection に使い、research では対象書類の CF 計算書 / BS 表示期間を一次確認する。

## 7.2 配当（DPS・dividend_yield）

- `dps_actual_annual`: 直近実績の年間 1 株配当。J-Quants `DivAnn`（FY 開示にのみ記載）の非null行を会計期間順、同期間では改訂順に選び、carry-forwardして使う（直近FYの実績年間配当は次のFY開示まで最新の実績であり続けるため。bpsのようなlatest-row-onlyの季節欠損を避ける）。
- `dps_forecast_annual`: 進行期の予想年間 1 株配当。四半期開示の `FDivAnn`、本決算開示では進行期ガイダンスの `NxFDivAnn` を使う。分割を跨ぐ行では基準不明の正値を落とし、明示0は残す。
- `dividend_yield` は、鮮度を満たす非負の `dps_forecast_annual` があれば原則として `dps_forecast_annual / 直近終値` とする。ただしsplit-safeな正の実績年間配当があり、予想が実績の2倍を超える場合は実績へ倒す。明示0の実績も利回り0として観測済みと扱い、予想・実績とも欠ければ `null` とする。

予想DPSの対象期・文書選択は[§2.1](#21-採用ソース)の共通投影に従う。最新の適用対象期を先に確定し、同じ対象期の配当streamから読み、新期間の欠測を旧期間で埋めない。元payloadは当期・翌期slotと文書identityを保持する。空欄だけで取下げを確定せず、同時刻の矛盾は未解決にする。分割正規化で基準不明となった正予想も利用しない。


予想がsplit-safeな正の実績DPSの2倍を超える場合は、特別配当か未実現の還元転換かを機械入力から区別できないため、5年反復するcarryには実績を使う。実績が0または未観測なら、無配からの初配当や実績開示前の新規上場を消さないため予想を使う。同じ予想選択を判断面のcarryと較正panelのguidance signalで共有する。

**年間 DPS の株式基準**。[J-Quants公式仕様](https://jpx-jquants.com/ja/spec/fin-summary)では、配当実績は会社の開示値であり、基準日時点の値と会社が分割後へ換算した値が混在し得る。会計期間から比較日までに株数単位の変更があった正の実績は、四半期末を配当基準日と仮定して復元しない。明細合計や配当総額／期末株数の一致でも基準は確定しない。期中の自己株取得・増資だけでも総額を期末株数で割った値は支払別DPSと異なるため、その検算と専用の倍率guardを撤去する。原本の明細・総額・株数はL1に保持する。

正の実績DPSには原本の開始・終了日が揃い、開示日までに終了した正の会計期間を要求する。開始日や期間をFY末・開示日から推測して補わない。不明・逆転・未来終了の期間は未解決とし、明示0だけは株式基準に依存しない観測として残す。

実績 DPS は資本の正規化でも原本値を残し、配当側だけが比較日までの単位変更・未分類 action を判定する。開示後の変更も推定換算しない。観測された変更がない場合も、将来の発表済み分割を会社が先に反映していないという保証にはならず、個別判断では原典を確認する。明示0は株式基準に依存しない。累積factorが相殺する複数の変更も未解決として扱う。

| `dividend_basis` | 意味 |
| --- | --- |
| `forecast_annual` | 対象期・basisとspike条件を満たす非負の予想DPS。明示0を含む |
| `actual_reported` | 会計期間から比較日まで単位変更・未分類 action のない年間実績の原本値、または明示0 |
| `unresolved_split_basis` | 実績の基準が未解決で、独立して使える予想もないため利回りを出さない |
| `unresolved_actual_dividend` | 実績DPSが矛盾する、または正の実績の会計期間が不明・不正で、独立して使える予想もない |
| `unavailable` | 予想も実績も観測できない、または価格がない |

`unresolved_split_basis`は無配や観測自体の不在と区別し、利回りとE[r]を作らない。実績の基準が未解決でも、独立して利用できる予想は妨げない。較正では価格だけの成績を残し、配当込みの成績を未解決とする。未解決年度を飛ばして古い年度から増配率や連続増配を復活させない。

`dividend_split_factor`は配当計算窓に調整があれば累積factorを記録する。相殺して1でも記録する。変更なし、またはfactor自体が不明ならnull。factorがあるだけで配当換算に成功した意味ではない。
- この配当利回りは機械E[r]の将来carry入力である。較正はprice-onlyの価格収束と、実績FY配当を含むtotal returnを分ける。entry時点の利回りを年数按分した値を実現配当としない。詳細は[見積り較正](./estimate-calibration.md)に従う。

## 7.3 過去の株数変化

機械 E[r] の carry は `dividend_yield + clip(-net_share_change_yoy, ±5%)` で、後半は過去 1 年の**グロス発行済株式数**（自己株式を含む）の変化である。日本の自社株買いは取得した株式を自己株式へ入れるだけなので、発行済株式総数は消却するまで減らない。したがってこの成分は過去の株数縮小・希薄化 signal であり、将来の自社株買い cash や未消化枠ではない。

将来の資本配分は機械 annotation で推定せず、Researchで選定した対象を一次開示から確認する。

## 8. 業種中央値の算出

### 8.1 業種分類粒度

- **東証 33 業種** を初期値として採用
- Market Context Assessmentのsector tiltはresearch着手順を考えるjudgment入力とし、スクリーニングの比較基準は33業種に固定する
- 粒度を変更する場合は本ファイルを更新

### 8.2 中央値算出

- 各業種内の銘柄の valuation 指標から中央値を算出
- 集計タイミング: screening 実行時（日次）
- 集計対象（比較母集団）: `candidate_discovery.common_eligibility` の必須fact・JPX規制条件を満たす母集団。[規模と短い履歴の扱い](./screening-runtime.md#history-context)に従い、ADVはmembershipを変えない。業種relative strengthの母集団は[Market Context読取](./market-context.md#market-context-reads)に従い、市場全体fallbackもこの比較母集団で算出する

<a id="median-population-rationale"></a>

### 8.3 サンプル数下限

- **n < 10 の業種**: 市場全体中央値に fallback（[`MIN_SECTOR_MEDIAN_POPULATION`](../../engine/src/baibai_engine/valuation/metrics/relative.py)）
- 中小規模業種で n が不安定な場合の判定歪みを防止
- 下限は軸ごとに判定する。母集団は同じでも欠損の入り方が軸で違うので、同じ業種でも `pbr` は自業種、`ev_ebitda` は市場、という状態になりうる

10件は少数の業種中央値へ依存しすぎないための計算上の慣例で、経済的最適値を実証した下限ではない（導入[#12](https://github.com/koumatsumoto/baibai-loop/issues/12)）。NormalizedのgapとReinvestmentのquality比較も同じ下限を使い、fallbackでは比較基準の業種構成が変わる。Approachへの適用理由は[#1153](https://github.com/koumatsumoto/baibai-loop/issues/1153)、現在の役割は[ルールの目的](./screening-runtime.md#rule-purpose)に従う。

### 8.4 fallback の素性

fallback した値も `sector_median_gap` / `sector_median_value` に入るため、同じ field が「業種との差」と「市場との差」の 2 つの量を指す。**どちらから作られたかは `DerivedMetrics.sector_median_basis` が軸ごとに `sector` / `market` で持つ。**

自業種中央値と市場中央値へのfallbackでは比較基準が異なる。後者には業種構成の違いも含まれるため、gapだけで「同業より割安」と解釈しない。fallbackの発生状況は入力断面と欠損に依存する。

素性の出口:

- `PanelRow.smg_market_fallback` — market から作られた軸を `|` で並べる。対応する `smg_*` が非 null の行でだけ意味を持つ
- 較正の `sector_median_basis` 座標 — `smg_*` 軸ごとに own_sector / market_fallback の効果量、cohort 勝率、screen 通過数を分けて出す

## 9. 過去自己比較（過去 3 年レンジ）

対象は`per_forward`・`per_trailing`・`pbr`・`p_s`・`ev_ebitda`。as-of以前の直近750本の価格barを使い、足りなければ利用可能な履歴で計算する。

Valuationの`self_range_position`は自己レンジ内の位置であり、較正panelでは`srp_*`として保持する。式は`(current - min(history)) / (max(history) - min(history))`。履歴なし／現在値欠測は`null`、履歴の最大値と最小値が同じなら`0.0`とし、0〜1へのclipはしない。観測順位に基づく統計的percentileではない。採用・除外条件は[Candidate Discovery](./screening-runtime.md#candidate-discovery)に従う。

Historical P/S と EV/EBITDA は、各日の raw close を `adjustment_factor` から as-of の価格調整基準へ揃え、最新の自己株式控除後株式数（`latest_shares_ex_treasury`）で時価総額だけを変化させる。P/S は `(historical_asof_basis_close * latest_shares_ex_treasury) / latest_sales_ttm`、EV/EBITDA は `(historical_asof_basis_close * latest_shares_ex_treasury + latest_debt - latest_cash) / latest_ebitda_ttm` とする。現在倍率と history の資本分母を揃えることで、自己株比率ではなく価格変化だけを自己レンジへ反映する。自己株式を含む発行済株式数・自己株式数のいずれかが欠損または破損していれば history は `null` とする。EV がゼロ以下、または EBITDA がゼロ以下の場合も `null` とし、`ttm_quality_ev_ebitda = exact` かつ正の EV/EBITDA だけ mechanical 判定に使う。PBR / PER の history も同じ as-of 価格調整基準の price を使うため、株式分割があっても history は連続になる。

### 9.0 価格履歴の連続性 fact（`price_history_sessions_750d` / `price_history_coverage_750d`）

自己レンジは、現在のfundamentalsを固定して価格だけを変化させた比較であり、各日の当時のfundamentalsから復元した倍率履歴ではない。価格比例の軸では自己レンジ中央値と現在倍率の比は価格中央値と現在価格の比に一致する。E[r]でこのanchorが採用される場合も、その意味を「過去の利益倍率の再現」としない。

自己レンジ / sigma gap は直近 750 本の bar（営業日ベース ≒ 3 年、§9）を代表的標本として前提にするが、上場が古くても bar 履歴に長期ギャップがある銘柄(上場区分変更・データ供給断など)では、レンジが実質それより短い期間で計算される。これを検出するため、Screening RunのStock Analysisには直近 **750 暦日窓**の bar 密度を以下の事実として記録する（窓が暦日なのは、取引カレンダーを fetch せず population 内の最大 bar 数を分母にして密度を出すため）。

- `price_history_sessions_750d`: 直近 750 暦日のうち bar が存在する営業日数
- `price_history_coverage_750d`: 上記 / 当日 scope 内の最大値(最も密な銘柄が取引カレンダーの近似)

`short_history_flag`は観測された価格履歴が750暦日未満であることを示し、新規上場を確認したflagではない。相対密度はその履歴の疎密を示す別の量である。
Stock Analysisへの入力は有効価格1本から許すが、履歴を要する指標を埋めない。`avg_turnover_oku`は20本、1/5/20/60営業日騰落率はそれぞれ2/6/21/61本、自己価格中央値anchorは100本を必要とし、足りなければnullを保つ。1本だけの自己レンジ位置や52週安値gapは算術上0になり得るが、長期の割安さを示すものではない。`price_history_coverage_750d`は当日のscope内の相対密度なので、小さなfixtureでは1本でも1.0になり得る。必ず観測本数と期間proxyを合わせて読む。

### 9.1 価格調整と株数・配当の単位

Screeningの自己レンジと騰落率は、[`asof_basis_closes()`](../../engine/src/baibai_engine/price_basis.py)でraw `close`と`adjustment_factor`からas-of基準へ揃えた価格系列を使う。配信元の調整済みOHLCV列は保持しない。取得世代で基準が混ざる値を再生成せず、必要な価格基準をraw値とfactorから組み立てる。factorは価格日より後・as-of以前のものを適用し、権利落ち日当日のfactorを当日価格へ重ねて掛けない。
価格系列の換算に使うfactorは正の有限値に限定し、`0`を調整なしとして除外しない。係数は価格日の翌日からas-ofまでの範囲に適用する。価格調整と株数単位の変更は別であり、株数・配当の換算ではExRTによる分割/併合の確認が必要となる。

J-Quantsの価格factorには分割・併合だけでなくライツイシューも含まれる。`ExRT` が `1` / `2` のときだけ株数の逆数換算と1株当たり財務値・現金配当の換算に用いる。`3` は価格調整であり、既存株式の単位を変える係数として扱わない。種別を確定できない非1factorは分割と推定せず、影響する株数・per-share値を未解決にする。新株発行後の発行済株式総数を価格factorから推定することもしない。[配信元の調整仕様](https://jpx-jquants.com/en/spec/eq-bars-daily/adj)

この系列は現金配当を加算しないprice-onlyであり、配当込みのtotal returnとは別である。配当を含む評価との区別は[見積り較正](./estimate-calibration.md)に従う。

## 10. データソース

### 10.1 Core

- **J-Quants API v2**: 取得の実装は[HTTP collector](../../engine/src/baibai_engine/collection/jquants_http.py)、保存済み入力の読取は[offline reader](../../engine/src/baibai_engine/market/jquants_reader.py)に従う。保存入力は[screening runtime](./screening-runtime.md#market-store-inputs)、取得可能範囲は契約と実際のcoverageで確認する。
- **EDINET API v2**: documents listで書類を選び、`type=5` CSV ZIPから本書のEV/EBITDA・Net cash・Asset-backed・FCF項目を抽出する。このvaluation経路にはraw XBRLをfallbackしない。`type=1` raw XBRLを使う[Research facts](../../batch/OPERATIONS.md#edinet-research-facts)は別の抽出経路である。
- **JPX**:
  - 決算発表予定: 公式 financial-announcement index に掲載された全 cohort Excel の既知日程（file 間で日付が食い違う銘柄は、より current な view を持つ file を採る）
  - 上場会社情報（業種分類、市場区分の補助確認）
  - 特別注意 / 整理 / 取引停止 / 上場廃止警告の除外判定

## 11. 半期移行と TTM 品質

- 2024 年以降、EDINET 単体では旧来の四半期報告書に依存した TTM 再構成ができない期間がある
- TTM 品質を `exact` / `approximated` / `unavailable` で明示する
- J-Quantsの実績フローは開示日までに終了した正の期間だけをTTMへ合成する。原本に未来の終了日や逆転した期間があれば数値自体を保存しても、その期間を確定済み12か月として使わない。
- EDINET CSV抽出では年次報告（書類種別120/130）かつ書類metadataで12か月の期間を確認できるフローを`exact`、期間不明・短縮決算・それ以外の取得フローを`approximated`、算出不能を`unavailable`とする。時点残高のnet cashは12か月フローではなく、算出できる当期残高を`exact`、算出不能を`unavailable`とする。半期のCFO・CapExを取得しても完全TTMへ合成・年率化しない。既知の提出日より後に期間が終了する矛盾や解釈できない提出日時も`exact`へ昇格させない。書類の`source_period_end`はフローのcontext終点とは限らない。
- 品質は供給元の計算・期間の区別であり、継続利益、資金の経済的持続性、株主回収可能性、倍率全体の正しさの認定ではない。`approximated`を一律に半期と解釈しない。
- Webの同じStock Analysis rowにある`ttm_quality_ev_ebitda` / `ttm_quality_fcf`は、raw Stock Analysisの`ttm_quality.ev_ebitda` / `fcf_yield`をそのまま投影する。採用元の品質がなければ`null`で、別世代・別sourceから補わず、この注記だけで候補理由を変えない。
- Web DTOでは2項目を必須nullable fieldとして明示する。Researchは選択したraw Stock Analysisのnull・field欠落を維持する。保存rawとWeb表示のshapeを区別し、旧canonicalとhashを変更しない。
- `EV/EBITDA` は `ttm_quality_ev_ebitda = exact` かつ EV / EBITDA がどちらも正のときのみ valuation-reversion 判定に使用する
- 指標をeligibility/orderへ使う条件は各Approachに従う。`asset_backed_ratio`は非金融企業のAsset Valueで使い、E[r]・FV・全銘柄の単一順位やwarningには使わない。TTM品質の注記と、Approachが要求する入力条件を区別する。

## 12. 選択利益とTTM営業利益

`FinancialSnapshot.operating_profit`は選択した最新の該当会計期間内で`OperatingProfit`→`OrdinaryProfit`→`Profit`を選ぶ。表示と既存の`operating_profit_yoy`・`operating_profit_loss_narrowing`に使い、全欠損なら未評価とする。sourceと単期・累計の期間を伴う値であり、常に厳密な営業利益とは限らない。

Reinvestmentの`operating_profit_ttm`は営業利益そのもののTTMで、経常・純利益fallbackや単一四半期の単純年率換算で埋めない。営業利益率とoperating return on capital proxyの式・eligibility/orderは[Candidate Discovery](./screening-runtime.md#candidate-discovery)を正本とする。後者を税引後NOPAT・平均投下資本による厳密ROICと呼ばない。calibration専用列とproduction値の区別は[見積り較正](./estimate-calibration.md#methodとcacheの互換性)に従う。

## 13. 前年同期の決定ロジック

J-Quants の財務サマリーは四半期 disclosure の時系列として扱うため、直前 disclosure は YoY ではなく QoQ になる。`eps_yoy` / `sales_yoy` / `operating_profit_yoy` は原本の開始・終了日で前年同期間を照合し、必要な期間metadataが不足すれば比較しない。対応する前年値が無ければ`null`とする。nullは比較不能であり、改善や悪化なしを確認した値ではない。季節性によるQoQ変化をYoYへ読み替えない。

前年のFY末と同期間の開始・終了は、月末を保つ12か月前の日付で照合する。2/28と閏年の2/29は連続した月末として扱い、8月末決算の半期末が閏日でも前年半期を探す。実期間長と開始・終了日の既存許容差を検査する。開始・終了日による比較結果を採用し、欠測・不一致・不正期間をFY label/endの一致で救済しない。欠けた期間や年度を生成せず、fieldごとの候補選択と選択済み実績の欠測規則を維持する。

利益項目は当期の対象会計期間内で営業→経常→純利益の順に選び、前年も当期に採用した同じfieldで比較する。部分訂正では同期間に観測済みの同fieldを使う既存選択を維持する。採用可能な前年値がなければ、別の利益項目や別期間で補わず、YoYと赤字縮小をともに`null`とする。

財務値のYoYは、選択した当期・前年の連結・単体区分が異なれば比較しない。区分を合わせるために古い訂正前の値へ戻らない。発行済株式数は発行体の状態なので、この連結範囲の制約を適用しない。

YoYは`当期 / 前年 - 1`で、前年0なら`null`。赤字縮小は`前年 < 当期 < 0`のときだけ`true`、両値が観測済みでそれ以外なら`false`、欠測なら`null`とする。負の前年値に対するYoYの符号だけで改善・悪化を読み取らない。

実績の会計期間identityと文書revision（開示日・時刻）は別に扱う。同時刻のEPS・資本状態の矛盾を任意の一行へ潰さず、比較・分母を未解決にする。利益・配当の予想専用文書はactual FY系列へ入れない。実績DPSの矛盾はcarryにも適用し、使える予想DPSが無ければ`unresolved_actual_dividend`として利回りとERを未解決にする。NP/Salesが一致していればTTMは保持する。分割前の株数参照も同じfield単位の矛盾解決を使い、参照株数が未解決なら訂正文書の株式basisを確定しない。予想の対象期・source解決は§2.1に従う。

## 14. 算出エラー・欠損の扱い

- 取得不能・算出不能は **明示的に `null`**（省略しない）
- 決算期またぎを含む欠損: 必要な期間・値・basisを解決できるまで `null` とする。
- 会計方針変更・特別損益等の経済的な解釈はResearchで行う。Screeningの観測・導出・見積りは既存の品質と欠損契約で出力し、AI解釈で上書きしない。

## 15. 参考

- [`./screening-runtime.md`](./screening-runtime.md): universe / valuation approach screen / screening run
- [`./data-sources.md`](./data-sources.md): データソース Tier 一覧
