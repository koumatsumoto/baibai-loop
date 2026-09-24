---
title: "Valuation metrics"
summary: "screeningで使うvaluation指標の定義、単位、欠損、算出仕様。"
doc_type: reference
status: active
---

# valuation-metrics — valuation 指標の算出仕様

Baibai Loop スクリーニングで使う valuation 指標の算出仕様とデータソース。screening run storeとapplication DBのresearch thesisで参照される指標の前提を確定する。

Candidate Discoveryのprimary authorityは4 Valuation Approachesのnative eligibility / orderである。機械E[r]はvaluation reversionとcarryをまとめたsecondary return priorで、Review Set membership、global rank、AI research priorityを所有しない。

Researchの判断見積りは[ThesisのBase/Downsideと共通算術](./thesis.md#scenario-arithmetic)に従う。通常12か月または理由付きの別期間について、sourceに基づく価値・累積分配・returnを計算し、[独立Review](./thesis.md#independent-second-pass)で反証する。

screening時点のE[r] / FV anchorは候補比較の文脈として表示・参照し、[Thesis payloadへ重複転記しない](./thesis.md#input-snapshot-and-lineage)。機械E[r]の5年priorと3y/5y calibrationは個別判断の期間を固定しない。

## 1. 使用指標

| 指標 | 定義 | データ項目 |
| --- | --- | --- |
| PER (Forward) | 株価 / 会社予想 EPS | 株価、会社予想 EPS |
| PER (Trailing) | 時価総額 / 直近 4 四半期の純利益 | 時価総額、純利益 直近 4Q 合算 |
| PER (3FY normalized) | 株価 / 直近 3 FY の分割補正後 EPS 平均 | 株価、FY EPS、株式分割・併合係数 |
| PBR | 株価 / 1 株純資産（BPS） | 株価、BPS |
| EV/EBITDA | (時価総額 + 有利子負債 - 現金) / EBITDA | 時価総額、有利子負債、現金、EBITDA |
| P/S | 株価 / 1 株売上高 | 株価、直近 4Q 売上 |
| PCFR | 株価 / 1 株営業 CF | 株価、直近 4Q 営業 CF |
| Net cash ratio | (現金 - 有利子負債) / 時価総額 | EDINET CSV-derived cash / debt、時価総額 |
| Asset-backed ratio | (Net cash + 投資有価証券) / 時価総額 | EDINET CSV-derived cash / debt / 投資有価証券、時価総額 |
| FCF yield | (営業 CF - 設備投資支出) / 時価総額 | EDINET CSV-derived CFO / capex、時価総額 |

## 2. Forward PER の取得方針（重要）

### 2.1 採用ソース

- **会社予想 EPS ベース**（会社が期初/修正後に開示した公式予想）
- 取得: J-Quantsの会社予想EPS。採用値は現行providerの取得契約に従う。

### 2.2 却下したソース

- **アナリストコンセンサスEPS**: 現行の取得経路では使用しない。
- **期初予想のみ使用**: 期中の修正予想を無視すると精度低下、最新の修正予想を使う

### 2.3 会社予想未公表 or 予想レンジ提示銘柄の扱い

有効な会社予想EPSを得られなければ`per_forward`はnullとする。Current Earnings Powerは正のforward PERを優先し、なければ正のtrailing PERを使う。選んだPERと対応gapを得られない場合は、そのApproachではnominateしない。他Approachの条件やThesisの評価方法へ、このfallbackを横展開しない。

### 2.4 会社予想の一時益 data-quality flag

**判定:** 同じ予想期の予想当期純利益と予想経常利益が両方あり、前者が後者を上回る場合に`forecast_special_gain`を立てる。大小関係だけでは差の原因や持続性は確定しない。特別損益・税等の影響は一次開示で確認する。

**用途:** 予想利益の質を調査するための注記である。flagや画面の「一時益予想」という名称を、原因が確定したことの証拠にはしない。

**非目標:** ranking、E[r]、既存指標の計算は変えない。これはdoctrineのwarning / annotation境界に従う。

### 2.5 会社予想の通期赤字 annotation

**判定:** 会社予想の**予想経常利益または予想当期純利益が負**なら、`forecast_full_year_loss` flagを立てる。片方しか開示されない期があるためORで判定する。予想が一つもない行はFalseに置き、欠損を黒字予想へ畳まない。

**用途:** 予想経常利益または予想純利益の赤字を示す注記であり、flagだけから予想EPSの符号や採用FV anchorを断定しない。実際のanchorは利用可能な入力とestimatorの条件で決まる。過去の価格レンジを現在の収益力の証明にしない。

**非目標:** 除外にも減衰にも使わず、annotationにする。一過性の赤字（引当・減損）と構造的な縮小を機械では区別できないため、一次開示を読むresearchが判定する。

## 3. Trailing PER の算出

- 直近 4 四半期の合算**純利益**（円）を使い、`時価総額 / 純利益 TTM` とする。P/S・PCFR・EV/EBITDA と同じ形になる
- 開示の利益は期中累計なので、`直近累計 + 前期通期 - 前年同期間累計` で 12 か月へ直す。合成できない期は `null`（単一四半期で割った偽の割高 PER を作らない）
- 配当・予想だけで実績anchorを持たない開示行は実績期間の選択から外す。直近累計は開示日ではなく会計期間の順序で最新の実績行を選ぶ。前期通期・前年同期間も先に選び、必要fieldが欠ける場合は`null`とする。過年度や同じ期間の古いrevisionへ補完しない
- 決算期またぎの場合、確報前期と確報後期の混在を避ける（確報確定後のみ更新）
- 赤字期（純利益マイナス）は `null` を採用（割安検出に意味を持たない）

**合成は円で行い、1 株当たりへの換算は最後に 1 回だけ行う。** 1 株当たりの各項は自分の期の株式数で割られているため、株式数が動いた会社では和・差が成立しない。買収の新株発行で株式数が 783M → 1,556M と動いた会社では、1 株当たりで合成すると `21.32 + 113.50 - 161.76 = -26.94` となり、黒字の会社が赤字に見えて収益 anchor を失う。**EPSの前年比も、比較期間と分割・株式basisを確認して読む。** 比であることだけで基準の一致は保証されない。

`FinancialSnapshot.eps` は `純利益 TTM / 自己株控除後株式数`で、提出者が開示する 1 株当たり当期純利益（分母は期中平均株式数）ではない。時価総額と同じ資本分母で組み直すことで `株価 / eps == per_trailing` が厳密に成立し、同じ語が 2 つの値を指さない。

### 3.1 3FY normalized PER の算出と用途

`normalized_per_3fy` は、as-of 以前に開示された直近 3 FY の EPS を現在の株式数基準へ分割補正し、その単純平均で現在株価を割る raw Derived Metric である。3 FY が揃わない、補正後 EPS 平均が正でない、または価格・分割係数を確定できない場合は `null` とし、別指標へのフォールバックは行わない。

`normalized_per_3fy`と同sector gapはNormalized Earnings Powerのnative eligibility/orderに使う。一方、FV/E[r] estimatorの入力には使わない。一時損益の中身を直接判定する指標ではなく、3FY earnings powerを単年利益と別座標で比較するDerived Metricである。

## 4. PBR の算出

PBRの普通株自己資本と株式basisは[資本の分母](#51-資本の分母)に従う。自己資本と非支配株主持分を含む純資産を代用しない。

## 5. EV/EBITDA の算出

- **時価総額**: 直近営業日終値 × **自己株式を除いた株式数**（発行済株式総数 − 期末自己株式数）。[§5.1](#51-資本の分母) が正本
- **有利子負債**: 短期借入金 + 長期借入金 + 社債
- **現金**: 現金及び現金同等物
- **EBITDA**: 営業利益 + 減価償却費 + のれん償却費（直近 4Q 合算）
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

自己株式は議決権も配当請求権も持たないので、時価総額に含めると過大になり、現金比率・利回りが薄く、倍率が割高に出る。**歪みが最大になるのは自己株式を積み上げた企業、つまり buyback を実行した企業**で、機械 E[r] の carry が上位へ押し上げる群と重なる。

**PBRはoutputを1つだけ持つ。** 普通株basisとの照合に通り、かつ最新`BPS`以上に新しい同一行の`TA × EqAR`があれば、その鮮度を使う。照合できない場合は`BPS × 自己株控除後株式数`へfallbackする。`EqAR`の小数第3位・`BPS`の小数第2位という公表精度は丸め区間として比較し、near-zero比率を相対誤差だけで拒否しない。Asset ValueはこのPBRを比較文脈と並び順の従キーに使う。eligibility/orderは[screening runtime](./screening-runtime.md#candidate-discovery)に従う。

**欠損を代用で埋めない。** 観測済みの資本状態から現在の自己株式数を安全に解決できない場合は**時価総額を出さない** — 発行済だけで代用すると、どれだけ過大か分からない値が現金比率・利回り・時価総額gateへ入る。発行済を超える自己株式数のような破損値も同じく答えない。自己資本比率が観測できない行は `equity_ratio` を `null` にし、純資産比率で代用しない（純資産は非支配株主持分を含み得るため、普通株自己資本と同じ量ではない）。時価総額が欠ける銘柄はcommon eligible母集団から外れるが、ADV欠損は外す理由にしない。

`TA × EqAR` で普通株自己資本の円経路を組むときは、両方を同時に観測した最新の開示行を使う。個々の最新値は staleness fact と表示には carry できるが、別開示日の `TA` と `EqAR` を掛けると、その間の資産変動を自己資本へ混入させる。両方を持つ行が無い場合、またはその同一行が最新`BPS`より古い場合は円経路を答えず、普通株基準の`BPS`経路を使う。同一状態へ揃えるために、より新しい資本状態を古い値へ巻き戻さない。

発行済株式総数と自己株式数は状態量なので、開示のない行へcarryできる。ただし、正の`TrShFY`を観測した後に、より新しい`ShOutFY`を持つ行が`TrShFY`を欠く場合は組み合わせない。J-Quantsでは自己株式0株が空欄になる行があり、空欄だけでは自己株式の消却・処分、発行済不変の処分、新株発行と処分の同時実施を区別できないためである。gross issuedの増加・減少・不変を安全条件にせず、新しい`TrShFY`を観測するまで答えない。明示的な`TrShFY = 0`は二重控除を起こさないためcarryでき、0株のsource issuedは要求しない。正の`TrShFY`の観測行に同時点の`ShOutFY`が無い場合も答えない。

`AvgSh` は EPS の期中平均株式数であり、gross issued の代替ではない。正の自己株式があり、入力上の `ShOutFY == AvgSh`、かつ`ShOutFY + TrShFY`が過去に観測したgross issuedへ戻る行は、期中平均が発行済欄へfallbackしたと識別できるためfail closedにする。`ShOutFY == AvgSh`だけなら、発行済が動かなかった1Qの正常行にも生じるので停止しない。負の自己株式数、非正の発行済、発行済以上の自己株式も有効な株式数へ変換しない。carry後の現在値だけでなく、正の`TrShFY`を観測したsource行の`ShOutFY > TrShFY`も必要である。正の`TrShFY`より後に`ShOutFY`だけを再観測した場合、欠損入力は自己株式0株と未報告を区別できず、旧値が現在も有効か判定できないためfail closedにする。これは自己株式が実際に変化したという断定ではなく、lossy inputに対するavailability policyである。新しい`TrShFY`の観測（0を含む）があれば、そのstateから再開する。これらの不整合は `capital_basis_failure_reason` に `indeterminate_positive_treasury_after_later_issued_observation`、`treasury_observation_without_issued_basis`、`invalid_treasury_source_capital_basis`、`issued_matches_average_with_positive_treasury`、`indeterminate_share_basis`、または`invalid_issued_or_treasury_shares`を記録し、時価総額とその派生倍率・利回りを `null` にする。

分割を跨ぐ行では自己株式数も発行済と同じ factor で換算する。片方だけ換算すると差である自己株控除後株式数が壊れる。


EV がゼロ以下、または EBITDA がゼロ以下の場合、EV/EBITDA は `null` として valuation-reversion から除外する。負の EV は net cash / cash-rich valuation approach で扱うべき balance sheet evidence であり、負の EBITDA は倍率が「低い」ほど割安という解釈が成立しないため。

## 6. P/S の算出

- 直近 4 四半期の売上高合算
- 連結 / 単体の区別: **連結優先**

## 7. PCFR の算出

- 直近 4 四半期の営業 CF 合算
- 営業 CF マイナスの企業は `null` を採用（割安検出に意味を持たない）

## 7.1 Net cash ratio / Asset-backed ratio / FCF yield

EDINET `type=5` CSV-derived metrics から以下を抽出する。

- `cash`: 現金及び現金同等物 / 現金及び預金
- `debt`: 短期借入金、1 年内返済予定長期借入金、社債、長期借入金、リース債務等の合算
- `investment_securities`: BS の `InvestmentSecurities` exact local name だけを連結優先・単体 fallback で抽出する帳簿価額。関係会社株式、営業投資有価証券、包括的な `Securities`、売却損益・CF、text block は合算しない。表示値が `－` 等の明示的な zero-like の場合だけ 0 とする
- `edinet_ocf_ttm`: EDINET CSV から抽出した営業活動によるキャッシュ・フロー
- `capex_ttm`: 有形固定資産・無形固定資産の取得支出。CFOと同じcontext・連結/単体の合算科目を優先し、合算がない場合は有形・無形の両科目が揃うときだけ足す。合算と内訳は二重計上せず、欠測をゼロにしない。符号は取得支出額の絶対値へ一度だけ正規化する
- `net_cash = cash - debt`
- `asset_backed_ratio = (net_cash + investment_securities) / market_cap`
- `fcf_ttm = edinet_ocf_ttm - capex_ttm`

`asset_backed_ratio` は投資有価証券の帳簿価額を加えた gross proxy である。上場株式だけでなく非上場・低流動性の保有を含み得て、含み損益、売却税、持合い・契約上の売却制約、事業上必要な保有を反映しない。このため marketable / liquid / fair value の指標とは呼ばない。非金融企業だけを対象とするCandidate DiscoveryのAsset Valueと較正panelの調査入口に限定し、E[r]、FV、Security Analysis全体の順位、warningには使わない。銀行業、保険業、その他金融業、証券・商品先物取引業は、cash / debt / securitiesが事業そのものなのでAsset Valueのeligibilityから除外する。`net_cash`、`investment_securities`、正の時価総額のいずれかが欠ける場合は`null`とし、net debtが投資有価証券を上回る場合の負値はそのまま保持する。`net_cash_to_market_cap`はReview analysis contextへ残すが、Asset Valueのeligibility / orderには使わない。

J-Quants 財務サマリー由来の `ocf_ttm` は OCF yield / PCFR 系の判定に使う。

**EDINET の値は、同じ実体の貸借対照表だと確かめられた行だけ使う。** 抽出器は 1 つの書類を連結・単体のどちらかの基準で読み、screening はその値を短信由来の時価総額・TTM 系列と組み合わせて比率にする。連結財務諸表を持つ会社の書類を単体基準で読むと、比率の分子と分母が別の会社を指す。両側が総資産を持つので照合できる — EDINET の総資産が短信の総資産から 2 倍を超えて外れる行は、EDINET 由来の値（`cash` / `debt` / `net_cash` / `ebitda_ttm` / `fcf_ttm` / `capex_ttm` / `investment_securities` / `edinet_ocf_ttm`）を出さず、`edinet_failure_reasons` に `entity_scale_mismatch` を載せる。

総資産を持たず照合できない行は、連結基準ならそのまま使い、単体基準・基準不明なら使わない。

落とすのは値だけで、`consolidation_basis` と書類の出所は残す。短信由来の指標（PBR・PER・`cash_to_market_cap`・自己資本比率）も残るので、**銘柄は universe に留まり screening され続ける**。必要なEDINET指標を欠くApproachではnominateしないが、他の指標・Approachまで一律に無効にしない。現行Asset Valueの`net_cash_to_market_cap`はanalysis contextであり、eligibility/orderには使わない。

対象書類は有価証券報告書 / 四半期報告書 / 半期報告書と、それぞれの訂正書を扱う。訂正書は EDINET documents API 上で `periodStart` / `periodEnd` が欠損しやすいため、欠損時のみ `docDescription` の対象期間から fallback parse する。書類選択は[EDINET provider](../../engine/src/baibai_engine/screening/providers/edinet.py)と[保存・選択処理](../../engine/src/baibai_engine/screening/edinet_store.py)を参照する。書類metadataの期間と、抽出したCF・BSの測定期間を同一視しない。

`edinet_source_period_start` / `edinet_source_period_end` は EDINET documents metadata 上の書類対象期間であり、必ずしも抽出 metric の測定期間そのものではない。特に半期報告書 / 訂正半期報告書では fiscal year 全体の period end が入ることがある。screening では source traceability と document selection に使い、research では対象書類の CF 計算書 / BS 表示期間を一次確認する。

## 7.2 配当（DPS・dividend_yield）

- `dps_actual_annual`: 直近実績の年間 1 株配当。J-Quants `DivAnn`（FY 開示にのみ記載）を、**開示行群の直近非 null 行から carry-forward** して使う（直近 FY の実績年間配当は次の FY 開示まで最新の実績であり続けるため。bps のような latest-row-only の季節欠損を避ける）。
- `dps_forecast_annual`: 進行期の予想年間 1 株配当。四半期開示の `FDivAnn`、本決算開示では進行期ガイダンスの `NxFDivAnn` を使う。分割を跨ぐ行は forecast EPS と同じく開示基準を機械判別できないため None に落とす。
- `dividend_yield` は、正の `dps_forecast_annual` を取得できる場合は原則として `dps_forecast_annual / 直近終値` とする。ただしsplit-safeな正の実績年間配当があり、予想が実績の2倍を超える場合は、実績年間配当 / 直近終値へ倒す。どちらも取れなければ `null` とする。

**予想は実績より優先するが、優先できるのは実績より新しく、かつ正の実績 DPS の2倍以下のときだけである。** 実績年間 DPS を持つ最新行より前に開示された予想は使わない。2倍を超える跳ねは特別配当か未実現の還元転換かを機械入力から区別できないため、5年反復するcarryには実績を使う。実績が0または未観測なら、無配からの初配当や実績開示前の新規上場を消さないため予想を使う。会社が予想を取り下げた後も過去の予想を引き当て続けると、無配化した会社に当時の配当額の利回りが付き、reversion 項の上限（5%/年）を単独で超える carry を作る。同じ規則が判断面の carry と較正 panel の両方で 1 つの実装から効く。

**年間 DPS の株式基準**。決算短信・有価証券報告書は 1 株当たり配当を各支払の基準日時点の株式基準で記載する一方、1 株当たり財務数値（EPS・BPS）は分割へ遡及修正される。したがって同じ開示行の中で per-share の基準が混在し、**会計期間が分割・併合を跨いだ年度は年間 DPS を単一の係数で asof の株式基準へ換算できない**。期末発行済株式数を遡及修正するかどうかも提出者ごとに割れており、開示 payload に判別できる field は無い。`dividend_basis` はどの経路で答えたかを持つ。

| `dividend_basis` | 意味 |
| --- | --- |
| `forecast_annual` | 予想 DPS から出した。正の実績 DPS の2倍以下、または実績が0・未観測の予想に使う |
| `actual_reported` | 会計期間に分割・併合が無く、短信の年間値をそのまま使った（厳密値） |
| `actual_record_date_resolved` | 期間内に分割があり、支払ごとにその基準日より後の調整を掛け直した |
| `unresolved_split_basis` | 掛け直せず**利回りを出していない**。E[r] も付かない |
| `unavailable` | 予想も実績も観測できない、または価格が無い |

`unresolved_split_basis` は無配（`dividend_yield = 0`）とも観測不能（`unavailable`）とも別の状態で、**判断面で読み替えない**。carry 支配型の銘柄でこの値が出たら、短信の配当表へ戻って基準を確認する。

支払ごとの換算は、配当の基準日と corporate action の権利落ち日が 5 日以内に並ぶ年度では行わない。日本の分割は権利落ちが基準日の前営業日、効力発生が基準日の翌日という形が定型で、store は権利落ち日しか持たないため、その配当が調整の前の株数で払われたのか後なのかを言えない。換算した値は、株式基準を持たない配当総額を自己株控除後株式数で割った値と突き合わせ、5% を超えて食い違えば答えない。その株数は提出者自身が EPS を出すのに使った期中平均株式数から 2 倍以上外れていれば per-share の分母に使わない（自己株式数の欄に株数そのものが入る開示があり、時価総額が桁で小さくなる）。

`dividend_split_factor` は会計期間に起きた累積 factor で、期間内に何も無ければ `null`。
- この配当利回りは機械E[r]の将来carry入力である。較正はprice-onlyの価格収束と、実績FY配当を含むtotal returnを分ける。entry時点の利回りを年数按分した値を実現配当としない。詳細は[見積り較正](./estimate-calibration.md)に従う。

## 7.3 過去の株数変化

機械 E[r] の carry は `dividend_yield + clip(-net_share_change_yoy, ±5%)` で、後半は過去 1 年の**グロス発行済株式数**（自己株式を含む）の変化である。日本の自社株買いは取得した株式を自己株式へ入れるだけなので、発行済株式総数は消却するまで減らない。したがってこの成分は過去の株数縮小・希薄化 signal であり、将来の自社株買い cash や未消化枠ではない。

将来の資本配分は機械 annotation で推定せず、research_triage で選ばれた銘柄だけを research で一次開示から確認する。

## 8. 業種中央値の算出

### 8.1 業種分類粒度

- **東証 33 業種** を初期値として採用
- macro contextのsector tiltはresearch着手順を考えるjudgment入力とし、スクリーニングの比較基準は33業種に固定する
- 粒度を変更する場合は本ファイルを更新

### 8.2 中央値算出

- 各業種内の銘柄の valuation 指標から中央値を算出
- 集計タイミング: screening 実行時（日次）
- 集計対象（比較母集団）: `candidate_discovery.common_eligibility` を満たす母集団（時価総額・上場期間・JPX規制の条件を満たす銘柄）。screenは全普通株を評価し、ADVは比較母集団のmembershipを変えない。sector relative strengthと市場全体fallbackも同じ母集団で算出する

### 8.3 サンプル数下限

- **n < 10 の業種**: 市場全体中央値に fallback（`metrics.MIN_SECTOR_MEDIAN_POPULATION`）
- 中小規模業種で n が不安定な場合の判定歪みを防止
- 下限は軸ごとに判定する。母集団は同じでも欠損の入り方が軸で違うので、同じ業種でも `pbr` は自業種、`ev_ebitda` は市場、という状態になりうる

### 8.4 fallback の素性

fallback した値も `sector_median_gap` / `sector_median_value` に入るため、同じ field が「業種との差」と「市場との差」の 2 つの量を指す。**どちらから作られたかは `DerivedMetrics.sector_median_basis` が軸ごとに `sector` / `market` で持つ。**

自業種中央値と市場中央値へのfallbackでは比較基準が異なる。後者には業種構成の違いも含まれるため、gapだけで「同業より割安」と解釈しない。fallbackの発生状況は入力断面と欠損に依存する。

素性の出口:

- `PanelRow.smg_market_fallback` — market から作られた軸を `|` で並べる。対応する `smg_*` が非 null の行でだけ意味を持つ
- 較正の `sector_median_basis` 座標 — `smg_*` 軸ごとに own_sector / market_fallback の効果量、cohort 勝率、screen 通過数を分けて出す

## 9. 過去自己比較（過去 3 年レンジ）

- **対象**: PER / PBR / P/S / EV-EBITDA
- **期間**: 直近 750 営業日（≒ 3 年）
- **パーセンタイル**: 下位 20% / 下位 50% / 上位 50% / 上位 80%
- **上場 3 年未満**: 上場来レンジで代替（[`./screening-runtime.md`](./screening-runtime.md) 参照）

Historical P/S と EV/EBITDA は、各日の raw close を `adjustment_factor` から as-of の株式分割基準へ揃え、最新の自己株式控除後株式数（`latest_shares_ex_treasury`）で時価総額だけを変化させる。P/S は `(historical_asof_basis_close * latest_shares_ex_treasury) / latest_sales_ttm`、EV/EBITDA は `(historical_asof_basis_close * latest_shares_ex_treasury + latest_debt - latest_cash) / latest_ebitda_ttm` とする。現在倍率と history の資本分母を揃えることで、自己株比率ではなく価格変化だけを自己レンジへ反映する。自己株式を含む発行済株式数・自己株式数のいずれかが欠損または破損していれば history は `null` とする。EV がゼロ以下、または EBITDA がゼロ以下の場合も `null` とし、`ttm_quality_ev_ebitda = exact` かつ正の EV/EBITDA だけ mechanical 判定に使う。PBR / PER の history も同じ as-of 株式分割基準の price を使うため、株式分割があっても history は連続になる。

### 9.0 価格履歴の連続性 fact（`price_history_sessions_750d` / `price_history_coverage_750d`）

自己レンジは、現在のfundamentalsを固定して価格だけを変化させた比較であり、各日の当時のfundamentalsから復元した倍率履歴ではない。価格比例の軸では自己レンジ中央値と現在倍率の比は価格中央値と現在価格の比に一致する。E[r]でこのanchorが採用される場合も、その意味を「過去の利益倍率の再現」としない。

自己レンジ / sigma gap は直近 750 本の bar（営業日ベース ≒ 3 年、§9）を代表的標本として前提にするが、上場が古くても bar 履歴に長期ギャップがある銘柄(上場区分変更・データ供給断など)では、レンジが実質それより短い期間で計算される。これを検出するため、Screening RunのSecurity Analysisには直近 **750 暦日窓**の bar 密度を以下の事実として記録する（窓が暦日なのは、取引カレンダーを fetch せず population 内の最大 bar 数を分母にして密度を出すため）。

- `price_history_sessions_750d`: 直近 750 暦日のうち bar が存在する営業日数
- `price_history_coverage_750d`: 上記 / 当日 scope 内の最大値(最も密な銘柄が取引カレンダーの近似)

`short_history_flag`(上場 750 暦日未満)は新規上場を扱い、本 fact は「上場は古いが履歴が疎」な銘柄を扱う。

### 9.1 `adjustment_close` の中身（dividend / 配当の扱い）

J-Quants の `AdjustmentClose` は **株式分割・株式併合 (reverse split を含む)** を遡及
調整した price-only series であり、現金配当の支払いは price には反映しない (total
return ではない)。これ以外のコーポレートアクション (合併、株式交換、その他の無償交付
等) はサポート対象外として **公式 docs に明示** されている (J-Quants daily_quotes API
リファレンス: <https://jpx.gitbook.io/j-quants-ja/api-reference/daily_quotes>)。本システム
でも screening の割安 percentile 判定は total return に変換せず、`adjustment_close`（price-only）で行う。理由:

- 割安判定の主信号は price に対する valuation（PBR / PER 等の percentile）であり、配当落ちを含めた pure な price 系列で percentile を出すのが一貫する
- 配当落ち分を加算した擬似 total return を percentile に使うと、高配当銘柄 (鉄鋼 / 銀行 / 商社等) の相対割安度が本来より small に見えるバイアスがかかる

**長期保有では配当を含む総リターンが重要**なため、配当は screen の price percentile ではなく、research の期待利回り見積り（[`./thesis.md`](./thesis.md)）と position の realized yield / calibration（[`./portfolio-ledger.md`](./portfolio-ledger.md)）で織り込む。

## 10. データソース

### 10.1 Core

- **J-Quants / ClientV2**: 取得の実装は[provider](../../engine/src/baibai_engine/screening/providers/jquants.py)、保存入力は[screening runtime](./screening-runtime.md#market-store-inputs)に従う。取得可能範囲は契約と実際のcoverageで確認する。
- **EDINET API v2**: documents listで書類を選び、`type=5` CSV ZIPから本書のEV/EBITDA・Net cash・Asset-backed・FCF項目を抽出する。このvaluation経路にはraw XBRLをfallbackしない。`type=1` raw XBRLを使う[Research facts](../../batch/OPERATIONS.md#edinet-research-facts)は別の抽出経路である。
- **JPX**:
  - 決算発表予定: 公式 financial-announcement index に掲載された全 cohort Excel の既知日程（file 間で日付が食い違う銘柄は、より current な view を持つ file を採る）
  - 上場会社情報（業種分類、市場区分の補助確認）
  - 特別注意 / 整理 / 取引停止 / 上場廃止警告の除外判定

## 11. 半期移行と TTM 品質

- 2024 年以降、EDINET 単体では旧来の四半期報告書に依存した TTM 再構成ができない期間がある
- TTM 品質を `exact` / `approximated` / `unavailable` で明示する
- EDINET CSV抽出では年次報告（書類種別120/130）の取得値を`exact`、それ以外の取得値を`approximated`、算出不能を`unavailable`とする。半期のCFO・CapExを取得しても完全TTMへ合成・年率化しない。書類の`source_period_end`はフローのcontext終点とは限らない。
- 品質は供給元の計算・期間の区別であり、継続利益、資金の経済的持続性、株主回収可能性、倍率全体の正しさの認定ではない。`approximated`を一律に半期と解釈しない。
- Research Triageの`analysis.data_quality.ttm_quality_ev_ebitda` / `ttm_quality_fcf`は、採用したSecurity Analysisの`ttm_quality.ev_ebitda` / `fcf_yield`をそのまま投影する。採用元の品質がなければ`null`で、別世代・別sourceから補わず、この注記だけでNominationを変えない。
- 新規snapshotは2項目を明示し、Triage ModelInputはschema version 2となる。2項目のない保存済みsnapshotはキー欠如を保ち、従来どおりversion 1として再構築する。明示的`null`とキー欠如をserializerで区別し、旧canonicalとhashを変更しない。
- `EV/EBITDA` は `ttm_quality_ev_ebitda = exact` かつ EV / EBITDA がどちらも正のときのみ valuation-reversion 判定に使用する
- 指標をeligibility/orderへ使う条件は各Approachに従う。`asset_backed_ratio`は非金融企業のAsset Valueで使い、E[r]・FV・全銘柄の単一順位やwarningには使わない。TTM品質の注記と、Approachが要求する入力条件を区別する。

## 12. 選択利益とTTM営業利益

`FinancialSnapshot.operating_profit`は選択した最新の該当会計期間内で`OperatingProfit`→`OrdinaryProfit`→`Profit`を選ぶ。表示と既存の`operating_profit_yoy`・`operating_profit_loss_narrowing`に使い、全欠損なら未評価とする。sourceと単期・累計の期間を伴う値であり、常に厳密な営業利益とは限らない。

Reinvestmentの`operating_profit_ttm`は営業利益そのもののTTMで、経常・純利益fallbackや単一四半期の単純年率換算で埋めない。営業利益率とoperating return on capital proxyの式・eligibility/orderは[Candidate Discovery](./screening-runtime.md#candidate-discovery)を正本とする。後者を税引後NOPAT・平均投下資本による厳密ROICと呼ばない。calibration専用列とproduction値の区別は[見積り較正](./estimate-calibration.md#methodとcacheの互換性)に従う。

## 13. 前年同期の決定ロジック

J-Quants の財務サマリーは四半期 disclosure の時系列として扱うため、直前 disclosure は YoY ではなく QoQ になる。 `eps_yoy` / `sales_yoy` / `operating_profit_yoy` の比較対象を、最新 summary と同じ `TypeOfCurrentPeriod` かつ `CurrentFiscalYearEndDate` が 1 年前の summary とする。該当する前年同期が無い場合、または period field が欠損している場合は `null` にする。nullは比較不能であり、改善や悪化なしを確認した値ではない。季節性によるQoQ変化をYoYへ読み替えない。

営業利益、経常利益、純利益の fallback は、選択対象になった会計期間の中でより具体的な
non-null field を選ぶ。部分訂正に営業利益が無いという理由で、同じ期間に観測済みの営業利益を
純利益へ置き換えない。forecast は source store が同日文書identityと対象期を保持しないため、
開示日順で保存された状態だけを使い、文書間の合成を推測しない。

## 14. 算出エラー・欠損の扱い

- 取得不能・算出不能は **明示的に `null`**（省略しない）
- 決算期またぎの一時的欠損: 確報確定まで `null` 運用
- 会計方針変更・特別損益等の経済的な解釈はResearchで行う。Screeningの観測・導出・見積りは既存の品質と欠損契約で出力し、AI解釈で上書きしない。

## 15. 参考

- [`./screening-runtime.md`](./screening-runtime.md): universe / valuation approach screen / screening run
- [`./data-sources.md`](./data-sources.md): データソース Tier 一覧
