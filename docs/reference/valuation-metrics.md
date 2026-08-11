---
title: "Valuation metrics"
summary: "screeningで使うvaluation指標の定義、単位、欠損、算出仕様。"
doc_type: reference
status: active
---

# valuation-metrics — valuation 指標の算出仕様

Baibai Loop スクリーニングで使う valuation 指標の算出仕様とデータソース。screening run storeとapplication DBのresearch thesisで参照される指標の前提を確定する。

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
- 取得: J-Quants Light（財務サマリー / 業績予想） + EDINET（補完）

### 2.2 却下したソース

- **アナリストコンセンサス forward EPS**: J-Quants Light / EDINET のスコープ外。Bloomberg / IBES 等は有料で本計画の非スコープ。
- **期初予想のみ使用**: 期中の修正予想を無視すると精度低下、最新の修正予想を使う

### 2.3 会社予想未公表 or 予想レンジ提示銘柄の扱い

- **forward PER なし** として扱い、`per_forward: null`
- **trailing PER のみで判定**（screen の閾値判定は trailing で代用）
- research thesis の `primary_metric` には trailing を含める

### 2.4 会社予想の一時益 data-quality flag

- 会社予想で **予想当期純利益 > 予想経常利益**（両方存在時）なら `forecast_special_gain` flag を立てる。税負担が通常正である以上、純利益>経常は特別益（事業売却益など）の存在をほぼ確定する 1 行チェック。純利益/経常は `forecast_eps` と同一予想期のペアで比較する。
- 一時益で嵩上げされた forward PER・予想配当利回り・機械 E[r] carry の value trap を判断前に表面化させる **warning annotation** であり、ranking・E[r]・既存指標の計算は変えない（doctrine の warning/annotation 境界）。candidate metrics（`forecast_special_gain_flag`）・selection longlist の `event_warnings`・UI の `一時益予想` badge に出す。持続ベースへの補正（forecast 純利益を経常ベースへ丸める等）は方法変更のため [`estimate-calibration.md`](./estimate-calibration.md) の運用契約で事前登録して評価する。

### 2.5 会社予想の通期赤字 annotation

- 会社予想の**予想経常利益または予想当期純利益が負**なら `forecast_full_year_loss` flag を立てる。片方しか開示されない期があるので or で見る。予想が 1 つも無い行は False に置き、欠損を黒字予想へ畳まない。
- 赤字予想は `forecast_eps` を負にするため forward PER が引けず、FV アンカーが**自己履歴 PBR だけ**に落ちる。その PBR レンジは黒字だった時代に市場が許容した倍率なので、収益基盤が構造的に縮んだ銘柄では帳簿だけが残って implied upside が膨らむ。
- **除外でも減衰でもなく annotation にする。** 一過性の赤字（引当・減損）と構造的な縮小を機械では区別できないためであり、判定は一次開示を読む research が持つ。実測でも上位占有は起きていない — 2026-08-04 / 08-05 の longlist 20 件で該当は各 1 件（母集団 3,709 件中 119 件 = 3.2%）。
- reversion の機械的な減衰は E[r] を動かす方法変更なので、[`estimate-calibration.md`](./estimate-calibration.md) の運用契約で事前登録し、赤字予想 cohort の forward 成績を較正 panel で測ってから判断する（現行 panel は forecast 系列を持たないため再構築が要る）。
- candidate metrics（`forecast_full_year_loss_flag`）と selection longlist の `event_warnings` に出す。

## 3. Trailing PER の算出

- 直近 4 四半期の合算**純利益**（円）を使い、`時価総額 / 純利益 TTM` とする。P/S・PCFR・EV/EBITDA と同じ形になる
- 開示の利益は期中累計なので、`直近累計 + 前期通期 - 前年同期間累計` で 12 か月へ直す。合成できない期は `null`（単一四半期で割った偽の割高 PER を作らない）
- 決算期またぎの場合、確報前期と確報後期の混在を避ける（確報確定後のみ更新）
- 赤字期（純利益マイナス）は `null` を採用（割安検出に意味を持たない）

**合成は円で行い、1 株当たりへの換算は最後に 1 回だけ行う。** 1 株当たりの各項は自分の期の株式数で割られているため、株式数が動いた会社では和・差が成立しない。買収の新株発行で株式数が 783M → 1,556M と動いた会社では、1 株当たりで合成すると `21.32 + 113.50 - 161.76 = -26.94` となり、黒字の会社が赤字に見えて収益 anchor を失う。**1 株当たり同士の比（YoY）は分母の違いが希薄化を映すので正しく、和・差だけが誤りである。**

`FinancialSnapshot.eps` は `純利益 TTM / 自己株控除後株式数`で、提出者が開示する 1 株当たり当期純利益（分母は期中平均株式数）ではない。時価総額と同じ資本分母で組み直すことで `株価 / eps == per_trailing` が厳密に成立し、同じ語が 2 つの値を指さない。

### 3.1 3FY normalized PER の算出と用途

`normalized_per_3fy` は、as-of 以前に開示された直近 3 FY の EPS を現在の株式数基準へ分割補正し、その単純平均で現在株価を割る raw estimate である。3 FY が揃わない、補正後 EPS 平均が正でない、または価格・分割係数を確定できない場合は `null` とし、別指標へのフォールバックは行わない。

shortlist UI では trailing PER と並べて表示するが、warning、除外条件、ranking、FV、E[r] の入力には使わない。一時損益の中身を判定する指標ではなく、単年 EPS への依存度を人間が確認するための annotation として扱う。

## 4. PBR の算出

- 1 株純資産（BPS） = 自己資本 / 自己株式を除いた期末株式数。J-Quants が開示値を返すので導出しない
- 直近公表の決算から取得。BPS は本決算開示にしか載らないので直近の非 null 行から carry-forward する

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
| 時価総額の株式数 | `ShOutFY` − `TrShFY`（発行済 − 期末自己株式） | `ShOutFY` 単独（自己株式を含む） |
| 自己資本比率 | `EqAR`（開示値） | `Eq / TA`（`Eq` は非支配株主持分を含む純資産） |
| 株価 / 1 株純資産 | `pbr` = 終値 / `BPS`（`BPS` は自己資本 ÷ 自己株控除後株式数） | 時価総額 / 純資産 |
| trailing 収益 | 報告純利益の TTM 合成（円）。倍率は時価総額 ÷ それ | `EPS` × 株式数の再構成 |
| accruals の純利益 | 報告純利益の TTM 合成（円） | `EPS` × `ShOutFY` |

**per-share の値と株式数を掛けて総額を作らない。** `EPS` の分母は期中平均株式数、`BPS` の分母は期末の自己株控除後株式数で、`ShOutFY` は自己株式を含む。掛け合わせると分子と分母が別の概念になる。store の恒等式で確かめられる — 開示された自己資本比率と `BPS × 株数 ÷ 総資産` の一致は自己株控除後で 97.1%（発行済では 36.2%）、報告純利益と `EPS × 株数` の一致は期中平均で 94.5%（発行済では 32.4%）。

自己株式は議決権も配当請求権も持たないので、時価総額に含めると過大になり、現金比率・利回りが薄く、倍率が割高に出る。**歪みが最大になるのは自己株式を積み上げた企業、つまり buyback を実行した企業**で、機械 E[r] の carry が上位へ押し上げる群と重なる。

**「株価 / 1 株純資産」は `pbr` 1 つだけを持つ。** 時価総額 ÷ 自己資本でも同じ概念を作れるが、`BPS` は開示された 1 株あたり値なので `pbr` の方が導出段数が少なく、`EqAR` の小数第 3 位丸めも入らない。`cash-rich-asset-discount` playbook の gate（`pbr_max`）もこれを使う。

**欠損を代用で埋めない。** 自己株式数が観測できない行は**時価総額を出さない** — 発行済で代用すると、どれだけ過大か分からない値が現金比率・利回り・流動性 gate へ入る。発行済を超える自己株式数のような破損値も同じく答えない。自己資本比率が観測できない行は `equity_ratio` を `null` にし、純資産比率で代用しない（代用は少数株主持分の大きい銘柄で比率を数 pt 過大にし、`equity_ratio_min` の gate を通しやすくする向きに効く）。いずれも該当銘柄は流動性母集団から外れる。

分割を跨ぐ行では自己株式数も発行済と同じ factor で換算する。片方だけ換算すると差である自己株控除後株式数が壊れる。

EDINET `type=5` CSV から抽出する。raw XBRL 直接 parse は現時点の非スコープとし、EDINET API が返す CSV ZIP を deterministic な中間データとして使う。J-Quants Light の財務サマリーで取れる項目は優先使用し、不足分を EDINET CSV-derived metrics で補完する。

EV がゼロ以下、または EBITDA がゼロ以下の場合、EV/EBITDA は `null` として valuation-reversion から除外する。負の EV は net cash / cash-rich evidence pattern で扱うべき balance sheet evidence であり、負の EBITDA は倍率が「低い」ほど割安という解釈が成立しないため。

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
- `capex_ttm`: 有形固定資産・無形固定資産の取得支出。符号は絶対値に正規化する
- `net_cash = cash - debt`
- `asset_backed_ratio = (net_cash + investment_securities) / market_cap`
- `fcf_ttm = edinet_ocf_ttm - capex_ttm`

`asset_backed_ratio` は投資有価証券の帳簿価額を加えた gross proxy である。上場株式だけでなく非上場・低流動性の保有を含み得て、含み損益、売却税、持合い・契約上の売却制約、事業上必要な保有を反映しない。このため marketable / liquid / fair value の指標とは呼ばず、candidate context と較正 panel の調査入口に限定する。`net_cash`、`investment_securities`、正の時価総額のいずれかが欠ける場合は `null` とし、net debt が投資有価証券を上回る場合の負値はそのまま保持する。screening rule、E[r]、FV、ranking、warning は変更しない。

J-Quants 財務サマリー由来の `ocf_ttm` は OCF yield / PCFR 系の判定に使う。

対象書類は有価証券報告書 / 四半期報告書 / 半期報告書と、それぞれの訂正書を扱う。訂正書は EDINET documents API 上で `periodStart` / `periodEnd` が欠損しやすいため、欠損時のみ `docDescription` の対象期間から fallback parse する。document selection の期間比較と訂正書の tie-break は [`./screening-runtime.md`](./screening-runtime.md) §5 を正本とする。

`edinet_source_period_start` / `edinet_source_period_end` は EDINET documents metadata 上の書類対象期間であり、必ずしも抽出 metric の測定期間そのものではない。特に半期報告書 / 訂正半期報告書では fiscal year 全体の period end が入ることがある。screening では source traceability と document selection に使い、research では対象書類の CF 計算書 / BS 表示期間を一次確認する。

## 7.2 配当（DPS・dividend_yield）

- `dps_actual_annual`: 直近実績の年間 1 株配当。J-Quants `DivAnn`（FY 開示にのみ記載）を、**開示行群の直近非 null 行から carry-forward** して使う（直近 FY の実績年間配当は次の FY 開示まで最新の実績であり続けるため。bps のような latest-row-only の季節欠損を避ける）。
- `dps_forecast_annual`: 進行期の予想年間 1 株配当。四半期開示の `FDivAnn`、本決算開示では進行期ガイダンスの `NxFDivAnn` を使う。分割を跨ぐ行は forecast EPS と同じく開示基準を機械判別できないため None に落とす。
- `dividend_yield` は、正の `dps_forecast_annual` を取得できる場合は `dps_forecast_annual / 直近終値`、取得できない場合は asof の株式基準へ揃えた正の実績年間配当 / 直近終値とする。どちらも取れなければ `null` とする。

**予想は実績より優先するが、優先できるのは実績より新しいときだけである。** 実績年間 DPS を持つ最新行より前に開示された予想は使わない。会社が予想を取り下げた後も過去の予想を引き当て続けると、無配化した会社に当時の配当額の利回りが付き、reversion 項の上限（5%/年）を単独で超える carry を作る。上書きすべき実績が 1 つも無い銘柄（実績開示前の新規上場）は入力窓内の最新予想をそのまま使う。同じ規則が判断面の carry と較正 panel の増配判定の両方で 1 つの実装から効く。

**年間 DPS の株式基準**。決算短信・有価証券報告書は 1 株当たり配当を各支払の基準日時点の株式基準で記載する一方、1 株当たり財務数値（EPS・BPS）は分割へ遡及修正される。したがって同じ開示行の中で per-share の基準が混在し、**会計期間が分割・併合を跨いだ年度は年間 DPS を単一の係数で asof の株式基準へ換算できない**。期末発行済株式数を遡及修正するかどうかも提出者ごとに割れており、開示 payload に判別できる field は無い。`dividend_basis` はどの経路で答えたかを持つ。

| `dividend_basis` | 意味 |
| --- | --- |
| `forecast_annual` | 予想 DPS から出した。carry は将来利回りなのでこれを最優先する |
| `actual_reported` | 会計期間に分割・併合が無く、短信の年間値をそのまま使った（厳密値） |
| `actual_record_date_resolved` | 期間内に分割があり、支払ごとにその基準日より後の調整を掛け直した |
| `unresolved_split_basis` | 掛け直せず**利回りを出していない**。E[r] も付かない |
| `unavailable` | 予想も実績も観測できない、または価格が無い |

`unresolved_split_basis` は無配（`dividend_yield = 0`）とも観測不能（`unavailable`）とも別の状態で、**判断面で読み替えない**。carry 支配型の銘柄でこの値が出たら、短信の配当表へ戻って基準を確認する。

支払ごとの換算は、配当の基準日と corporate action の権利落ち日が 5 日以内に並ぶ年度では行わない。日本の分割は権利落ちが基準日の前営業日、効力発生が基準日の翌日という形が定型で、store は権利落ち日しか持たないため、その配当が調整の前の株数で払われたのか後なのかを言えない。換算した値は、株式基準を持たない配当総額を自己株控除後株式数で割った値と突き合わせ、5% を超えて食い違えば答えない。その株数は提出者自身が EPS を出すのに使った期中平均株式数から 2 倍以上外れていれば per-share の分母に使わない（自己株式数の欄に株数そのものが入る開示があり、時価総額が桁で小さくなる）。

`dividend_split_factor` は会計期間に起きた累積 factor で、期間内に何も無ければ `null`。
- この利回りは将来 carry の機械 E[r] anchor に使う。較正リプレイの実現値は price-only であり、entry 時点の利回りを保有年数で按分する疑似配当 accrual は加えない。

## 7.3 自己株式取得枠の状態（buyback_authorization_status）

機械 E[r] の carry は `dividend_yield + clip(-net_share_change_yoy, ±5%)` で、buyback 側は過去 1 年の株数変化である。取得枠を消化し終えた会社もこの成分を持つため、carry を「これから受け取る現金還元」と読むと過大評価になる。

EDINET の自己株券買付状況報告書（様式コード 220、訂正 230）は金商法 24 条の 6 第 1 項により取得期間中は毎月提出されるので、提出の有無と齢が、取得枠がいつまで在ったかの観測になる。`buyback_authorization_status` は次の 4 値を取り、併記する `buyback_status_latest_filing_date` / `buyback_status_filing_age_days` / `buyback_status_observed_from` を読み手が自分の閾値で使う。

値は**観測そのもの**を表し、枠が今も在るかの推論ではない。

| 値 | 意味 |
| --- | --- |
| `recent_filing` | 直近 45 日以内に提出がある。報告月の翌月 15 日までという提出期限に対し、月初の as-of で前月分が未提出でも前々月分が窓に入る幅である。**取得期間が終了した月の報告書もここに入る** |
| `stale_filing` | 観測窓に提出はあるが 45 日より古い |
| `no_filing` | 観測窓 365 日に提出が 1 件も無い |
| `unknown` | 提出が見つからず、store の提出観測も as-of から 365 日を覆えていない。観測済みの提出は no-filing 窓が未成熟でも `recent_filing` / `stale_filing` として残る |

**`recent_filing` は「今も枠が在る」を意味しない。** 提出は報告月の翌月に出るので、取得期間が終了した月の報告書も期間終了後に提出される。`buyback_remaining_share_ratio` と `buyback_authorization_window_end` が、直近報告月末の残枠と取得期間を別々に示す。提出日と報告月末の双方が as-of 以下の報告だけを使い、提出前の内容を historical 診断へ混ぜない。残枠・期間が欠損なら、使い切りとも継続中とも推定しない。

`no_filing` の観測窓は、EDINET の全様式を含む日次一覧について `is_final`、一覧 metadata 件数、永続行数、`source_coverage` の status・件数が一致し、as-of から日単位で連続する範囲だけを使う。Form 220 / 230 が1件ある日はその提出の positive evidence にはなるが、universe 全体の「提出なし」を証明しない。途中の欠落・partial・件数不一致・未確定日はそこで窓を切る。

**この annotation は ranking・gate・E[r] を変えない。** 3m / 6m の authorization 診断は正方向だが、1y 以上の同一母集団比較と 3y / 5y の完全な point-in-time evidence が存在しないため、終了済み枠を自動除外や carry 減衰へ接続しない（[診断](../../reports/studies/2026-08-09-buyback-authorization-calibration/report.md)）。shortlist は期間満了・残枠消化・取得目的・消却を一次開示で確認し、終了済み carry を forward 還元として narrative に残さない。

## 8. 業種中央値の算出

### 8.1 業種分類粒度

- **東証 33 業種** を初期値として採用
- macro contextのsector tiltはresearch着手順を考えるjudgment入力とし、スクリーニングの比較基準は33業種に固定する
- 粒度を変更する場合は本ファイルを更新

### 8.2 中央値算出

- 各業種内の銘柄の valuation 指標から中央値を算出
- 集計タイミング: screening 実行時（日次）
- 集計対象（比較母集団）: `selection.liquidity` を満たす流動性母集団（時価総額・売買代金・上場期間・JPX 規制の条件を満たす銘柄）。screen は全普通株を評価するが、相対 valuation の基準は投資可能な比較対象に固定し、小型・低流動性銘柄の混入で判定が歪まないようにする。sector relative strength と市場全体 fallback も同じ母集団で算出する

### 8.3 サンプル数下限

- **n < 10 の業種**: 市場全体中央値に fallback
- 中小規模業種で n が不安定な場合の判定歪みを防止
- 例: 東証 33 業種の「空運業」「鉱業」は銘柄数が少ない場合 fallback 対象

## 9. 過去自己比較（過去 3 年レンジ）

- **対象**: PER / PBR / P/S / EV-EBITDA
- **期間**: 直近 750 営業日（≒ 3 年）
- **パーセンタイル**: 下位 20% / 下位 50% / 上位 50% / 上位 80%
- **上場 3 年未満**: 上場来レンジで代替（[`./screening-runtime.md`](./screening-runtime.md) 参照）

Historical P/S と EV/EBITDA は、各日の raw close を `adjustment_factor` から as-of の株式分割基準へ揃え、最新の自己株式控除後株式数（`latest_shares_ex_treasury`）で時価総額だけを変化させる。P/S は `(historical_asof_basis_close * latest_shares_ex_treasury) / latest_sales_ttm`、EV/EBITDA は `(historical_asof_basis_close * latest_shares_ex_treasury + latest_debt - latest_cash) / latest_ebitda_ttm` とする。現在倍率と history の資本分母を揃えることで、自己株比率ではなく価格変化だけを自己レンジへ反映する。自己株式を含む発行済株式数・自己株式数のいずれかが欠損または破損していれば history は `null` とする。EV がゼロ以下、または EBITDA がゼロ以下の場合も `null` とし、`ttm_quality_ev_ebitda = exact` かつ正の EV/EBITDA だけ mechanical 判定に使う。PBR / PER の history も同じ as-of 株式分割基準の price を使うため、株式分割があっても history は連続になる。

### 9.0 価格履歴の連続性 fact（`price_history_sessions_750d` / `price_history_coverage_750d`）

自己レンジ / sigma gap は直近 750 本の bar（営業日ベース ≒ 3 年、§9）を代表的標本として前提にするが、上場が古くても bar 履歴に長期ギャップがある銘柄(上場区分変更・データ供給断など)では、レンジが実質それより短い期間で計算される。これを検出するため、screening runのcandidate recordには直近 **750 暦日窓**の bar 密度を以下の事実として記録する（窓が暦日なのは、取引カレンダーを fetch せず population 内の最大 bar 数を分母にして密度を出すため）。

- `price_history_sessions_750d`: 直近 750 暦日のうち bar が存在する営業日数
- `price_history_coverage_750d`: 上記 / 当日 scope 内の最大値(最も密な銘柄が取引カレンダーの近似)

`short_history_flag`(上場 750 暦日未満)は新規上場を扱い、本 fact は「上場は古いが履歴が疎」な銘柄を扱う。`select` では `listing_span_days >= 750` かつ coverage `< 0.8` の候補に risk tag `price_history_gap` を付ける(annotation のみ。事前固定閾値で、ranking / gate には使わない)。

### 9.1 `adjustment_close` の中身（dividend / 配当の扱い）

J-Quants の `AdjustmentClose` は **株式分割・株式併合 (reverse split を含む)** を遡及
調整した price-only series であり、現金配当の支払いは price には反映しない (total
return ではない)。これ以外のコーポレートアクション (合併、株式交換、その他の無償交付
等) はサポート対象外として **公式 docs に明示** されている (J-Quants daily_quotes API
リファレンス: <https://jpx.gitbook.io/j-quants-ja/api-reference/daily_quotes>)。本システム
でも screening の割安 percentile 判定は total return に変換せず、`adjustment_close`（price-only）で行う。理由:

- 割安判定の主信号は price に対する valuation（PBR / PER 等の percentile）であり、配当落ちを含めた pure な price 系列で percentile を出すのが一貫する
- 配当落ち分を加算した擬似 total return を percentile に使うと、高配当銘柄 (鉄鋼 / 銀行 / 商社等) の相対割安度が本来より small に見えるバイアスがかかる

**長期保有では配当を含む総リターンが重要**なため、配当は screen の price percentile ではなく、research の期待利回り見積り（[`./thesis.md`](./thesis.md)）と position の realized yield / calibration（[`./portfolio-ledger.md`](./portfolio-ledger.md)）で織り込む。銘柄の総リターン評価が要る場合は J-Quants Premium の配当 API 取得を検討する。

## 10. データソース

### 10.1 Core

- **J-Quants Light / ClientV2**: 使用 method（`get_eq_master` / `get_eq_bars_daily_range` / `get_fin_summary_range` / `get_mkt_calendar`）の用途と検証は [`./screening-runtime.md`](./screening-runtime.md) §4 を正本とする
- **EDINET API v2**:
  - documents list (`type=2`): CSV 取得可能な提出書類の選定
  - document download (`type=5`): CSV ZIP から EV/EBITDA / Net cash / Asset-backed / FCF 関連項目を抽出
  - raw XBRL (`type=1`) の直接 parser は将来拡張。CSV-derived metrics の coverage / precision が不十分な場合に検討する
- **JPX**:
  - 決算発表予定: 公式 financial-announcement index に掲載された全 cohort Excel の既知日程（file 間で日付が食い違う銘柄は、より current な view を持つ file を採る）
  - 上場会社情報（業種分類、市場区分の補助確認）
  - 特別注意 / 整理 / 取引停止 / 上場廃止警告の除外判定

### 10.2 Optional（将来拡張）

- J-Quants Premium（財務諸表詳細、売買内訳、配当、指数系データ）
- TDnet API（5 年分の適時開示 / XBRL）
- JPX Corporate Action Data

## 11. 半期移行と TTM 品質

- 2024 年以降、EDINET 単体では旧来の四半期報告書に依存した TTM 再構成ができない期間がある
- TTM 品質を `exact` / `approximated` / `unavailable` で明示する
- `EV/EBITDA` は `ttm_quality_ev_ebitda = exact` かつ EV / EBITDA がどちらも正のときのみ valuation-reversion 判定に使用する
- `P/S` / `PCFR` / `OCF yield` / `FCF yield` / `Net cash` は、それぞれ evidence pattern が要求する品質条件を満たすときのみ mechanical 判定に使う。`Asset-backed ratio` は mechanical 判定に使わない

## 12. 営業利益相当の fallback

- 業績悪化フィルタに使う利益代表は以下の順で採用する
  - `OperatingProfit`
  - `OrdinaryProfit`
  - `Profit`
- すべて欠損のときは EPS / 売上の 2 項目だけで業績悪化フィルタを評価する

## 13. 前年同期の決定ロジック

J-Quants の財務サマリーは四半期 disclosure の時系列として扱うため、直前 disclosure は YoY ではなく QoQ になる。 `eps_yoy` / `sales_yoy` / `operating_profit_yoy` の比較対象を、最新 summary と同じ `TypeOfCurrentPeriod` かつ `CurrentFiscalYearEndDate` が 1 年前の summary とする。該当する前年同期が無い場合、または period field が欠損している場合は `null` にする。`null` は業績悪化フィルタでは悪化なしとして扱い、季節性による QoQ 減少や不規則 disclosure の index shift を過剰棄却に使わない。

## 14. 算出エラー・欠損の扱い

- 取得不能・算出不能は **明示的に `null`**（省略しない）
- 決算期またぎの一時的欠損: 確報確定まで `null` 運用
- 会計方針変更・特損計上等で一時的歪み: research 側で「反対仮説」に記録、screening runの指標値は素直に採用（事実層のため）

## 15. 参考

- [`./screening-runtime.md`](./screening-runtime.md): universe / evidence pattern screen / screening run
- [`./data-sources.md`](./data-sources.md): データソース Tier 一覧
