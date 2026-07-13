---
title: "Valuation metrics"
summary: "screeningで使うvaluation指標の定義、単位、欠損、算出仕様。"
doc_type: reference
status: active
last_reviewed: 2026-07-13
---

# valuation-metrics — valuation 指標の算出仕様

Baibai-Loop スクリーニングで使う valuation 指標の算出仕様とデータソース。`records/02-candidates/` と `records/03-thesis/` の両方で参照される指標の前提を確定する。

## 1. 使用指標

| 指標 | 定義 | データ項目 |
| --- | --- | --- |
| PER (Forward) | 株価 / 会社予想 EPS | 株価、会社予想 EPS |
| PER (Trailing) | 株価 / 直近 4 四半期 EPS | 株価、EPS 直近 4Q 合算 |
| PBR | 株価 / 1 株純資産（BPS） | 株価、BPS |
| EV/EBITDA | (時価総額 + 有利子負債 - 現金) / EBITDA | 時価総額、有利子負債、現金、EBITDA |
| P/S | 株価 / 1 株売上高 | 株価、直近 4Q 売上 |
| PCFR | 株価 / 1 株営業 CF | 株価、直近 4Q 営業 CF |
| Net cash ratio | (現金 - 有利子負債) / 時価総額 | EDINET CSV-derived cash / debt、時価総額 |
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
- research packet の `primary_metric` には trailing を含める

## 3. Trailing PER の算出

- 直近 4 四半期の合算 EPS を使用
- 決算期またぎの場合、確報前期と確報後期の混在を避ける（確報確定後のみ更新）
- 赤字期（EPS マイナス）は `null` を採用（割安検出に意味を持たない）

## 4. PBR の算出

- 1 株純資産（BPS） = 純資産 / 発行済株式数
- 直近公表の四半期決算から取得（年次確報優先）

## 5. EV/EBITDA の算出

- **時価総額**: 直近営業日終値 × 発行済株式数
- **有利子負債**: 短期借入金 + 長期借入金 + 社債
- **現金**: 現金及び現金同等物
- **EBITDA**: 営業利益 + 減価償却費 + のれん償却費（直近 4Q 合算）
- **有効条件**: EV と EBITDA がどちらも正のときだけ倍率として採用する

EDINET `type=5` CSV から抽出する。raw XBRL 直接 parse は現時点の非スコープとし、EDINET API が返す CSV ZIP を deterministic な中間データとして使う。J-Quants Light の財務サマリーで取れる項目は優先使用し、不足分を EDINET CSV-derived metrics で補完する。

EV がゼロ以下、または EBITDA がゼロ以下の場合、EV/EBITDA は `null` として valuation-reversion から除外する。負の EV は net cash / cash-rich evidence pattern で扱うべき balance sheet evidence であり、負の EBITDA は倍率が「低い」ほど割安という解釈が成立しないため。

## 6. P/S の算出

- 直近 4 四半期の売上高合算
- 連結 / 単体の区別: **連結優先**

## 7. PCFR の算出

- 直近 4 四半期の営業 CF 合算
- 営業 CF マイナスの企業は `null` を採用（割安検出に意味を持たない）

## 7.1 Net cash ratio / FCF yield

EDINET `type=5` CSV-derived metrics から以下を抽出する。

- `cash`: 現金及び現金同等物 / 現金及び預金
- `debt`: 短期借入金、1 年内返済予定長期借入金、社債、長期借入金、リース債務等の合算
- `edinet_ocf_ttm`: EDINET CSV から抽出した営業活動によるキャッシュ・フロー
- `capex_ttm`: 有形固定資産・無形固定資産の取得支出。符号は絶対値に正規化する
- `net_cash = cash - debt`
- `fcf_ttm = edinet_ocf_ttm - capex_ttm`

J-Quants 財務サマリー由来の `ocf_ttm` は OCF yield / PCFR 系の判定に使う。

対象書類は有価証券報告書 / 四半期報告書 / 半期報告書と、それぞれの訂正書を扱う。訂正書は EDINET documents API 上で `periodStart` / `periodEnd` が欠損しやすいため、欠損時のみ `docDescription` の対象期間から fallback parse する。document selection は period end / period start を submit time より先に比較し、古い期間の訂正書が新しい半期 / 年次の通常書類を上書きしないようにする。同一期間では最新 submit time を優先し、同一 submit time の tie-break として訂正書を通常書類より優先する。

`edinet_source_period_start` / `edinet_source_period_end` は EDINET documents metadata 上の書類対象期間であり、必ずしも抽出 metric の測定期間そのものではない。特に半期報告書 / 訂正半期報告書では fiscal year 全体の period end が入ることがある。screening では source traceability と document selection に使い、research では対象書類の CF 計算書 / BS 表示期間を一次確認する。

## 7.2 配当（DPS・dividend_yield）

- `dps_actual_annual`: 直近実績の年間 1 株配当。J-Quants `DivAnn`（FY 開示にのみ記載）を、**開示行群の直近非 null 行から carry-forward** して使う（直近 FY の実績年間配当は次の FY 開示まで最新の実績であり続けるため。bps のような latest-row-only の季節欠損を避ける）。分割・併合を跨ぐ行は adjustment_factor 累積で asof-basis へ換算する。
- `dps_forecast_annual`: 進行期の予想年間 1 株配当。四半期開示の `FDivAnn`、本決算開示では進行期ガイダンスの `NxFDivAnn` を使う。分割を跨ぐ行は forecast EPS と同じく開示基準を機械判別できないため None に落とす。
- `dividend_yield` は、正の `dps_forecast_annual` を取得できる場合は `dps_forecast_annual / 直近終値`、取得できない場合は分割調整済みの正の `dps_actual_annual / 直近終値` とする。どちらも取れなければ `null` とする。
- この利回りは将来 carry の機械 E[r] anchor に使う。較正リプレイの実現値は price-only であり、entry 時点の利回りを保有年数で按分する疑似配当 accrual は加えない。

## 8. 業種中央値の算出

### 8.1 業種分類粒度

- **東証 33 業種** を初期値として採用
- macro contextは業種tiltを持たず、スクリーニングの比較基準は33業種に固定する
- 粒度を変更する場合は本ファイルを更新

### 8.2 中央値算出

- 各業種内の銘柄の valuation 指標から中央値を算出
- 集計タイミング: screening 実行時（週次）
- 集計対象（比較母集団）: `selection.liquidity` を満たす流動性母集団（時価総額・売買代金・上場期間・JPX 規制の条件を満たす銘柄）。screen は全普通株を評価するが、相対 valuation の基準は投資可能な比較対象に固定し、小型・低流動性銘柄の混入で判定が歪まないようにする。sector relative strength と市場全体 fallback も同じ母集団で算出する

### 8.3 サンプル数下限

- **n < 10 の業種**: 市場全体中央値に fallback
- 中小規模業種で n が不安定な場合の判定歪みを防止
- 例: 東証 33 業種の「空運業」「鉱業」は銘柄数が少ない場合 fallback 対象

## 9. 過去自己比較（過去 3 年レンジ）

- **対象**: PER / PBR / EV-EBITDA
- **期間**: 直近 750 営業日（≒ 3 年）
- **パーセンタイル**: 下位 20% / 下位 50% / 上位 50% / 上位 80%
- **上場 3 年未満**: 上場来レンジで代替（[`../workflow/screening.md`](../workflow/screening.md) 参照）

Historical EV/EBITDA は、各日の split-adjusted close で時価総額だけを変化させ、最新の発行済株式数・有利子負債・現金・TTM EBITDA を全期間に適用する近似で算出する。式は `(historical_adjustment_close * latest_shares_outstanding + latest_debt - latest_cash) / latest_ebitda_ttm` とし、balance sheet / EBITDA の時系列が無くても EV/EBITDA の定義を保つ。`adjustment_close` が欠損する場合は raw `close` にフォールバックする。必要項目が欠損する場合、EV がゼロ以下、または EBITDA がゼロ以下の場合は `null` とし、`ttm_quality_ev_ebitda = exact` かつ正の EV/EBITDA だけ mechanical 判定に使う。PBR / PER の history も同じ price 基準（adjustment_close 優先）で算出するため、株式分割があっても history は連続になる。

### 9.0 価格履歴の連続性 fact（`price_history_sessions_750d` / `price_history_coverage_750d`）

自己レンジ / sigma gap は直近 750 本の bar（営業日ベース ≒ 3 年、§9）を代表的標本として前提にするが、上場が古くても bar 履歴に長期ギャップがある銘柄(上場区分変更・データ供給断など)では、レンジが実質それより短い期間で計算される。これを検出するため、candidates には直近 **750 暦日窓**の bar 密度を以下の事実として記録する（窓が暦日なのは、取引カレンダーを fetch せず population 内の最大 bar 数を分母にして密度を出すため）。

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

**長期保有では配当を含む総リターンが重要**なため、配当は screen の price percentile ではなく、research の期待利回り見積り（[`../workflow/research.md`](../workflow/research.md)）と position の realized yield / calibration（[`../workflow/position.md`](../workflow/position.md)）で織り込む。銘柄の総リターン評価が要る場合は J-Quants Premium の配当 API 取得を検討する。

## 10. データソース

### 10.1 Core

- **J-Quants Light / ClientV2**:
  - `get_eq_master`: 上場銘柄一覧、普通株判定、市場区分、33 業種
  - `get_eq_bars_daily_range`: 日足（OHLC + 出来高 + 売買代金）
  - `get_fin_summary_range`: 財務サマリー、会社予想 EPS、利益系の概要値
  - `get_mkt_calendar`: 営業日カレンダ
- **EDINET API v2**:
  - documents list (`type=2`): CSV 取得可能な提出書類の選定
  - document download (`type=5`): CSV ZIP から EV/EBITDA / Net cash / FCF 関連項目を抽出
  - raw XBRL (`type=1`) の直接 parser は将来拡張。CSV-derived metrics の coverage / precision が不十分な場合に検討する
- **JPX**:
  - 決算発表予定: 公式 financial-announcement index に掲載された全 cohort Excel の既知日程
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
- `P/S` / `PCFR` / `OCF yield` / `FCF yield` / `Net cash` は、それぞれ evidence pattern が要求する品質条件を満たすときのみ mechanical 判定に使う

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
- 会計方針変更・特損計上等で一時的歪み: research 側で「反対仮説」に記録、candidates の指標値は素直に採用（事実層のため）

## 15. 参考

- [`../workflow/screening.md`](../workflow/screening.md): universe / evidence pattern screen / candidates
- [`./data-sources.md`](./data-sources.md): データソース Tier 一覧
