# screening/valuation-metrics.md

Baibai-Loop スクリーニングで使う valuation 指標の算出仕様とデータソース。`(b) records/03-candidates/` と `(d) records/04-research/` の両方で参照される指標の前提を確定する。

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
- **trailing PER のみで判定**（mechanical.md の閾値判定は trailing で代用）
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

EDINET `type=5` CSV から抽出する。raw XBRL 直接 parse は現時点の非スコープとし、EDINET API が返す CSV ZIP を deterministic な中間データとして使う。J-Quants Light の財務サマリーで取れる項目は優先使用し、不足分を EDINET CSV-derived metrics で補完する。

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

J-Quants 財務サマリー由来の `ocf_ttm` は OCF yield / PCFR 系の判定に使う。`fcf-yield-discount` では EDINET CFO / capex / FCF を同じ source family として扱い、J-Quants `ocf_ttm` と混ぜて FCF を再計算しない。

対象書類は有価証券報告書 / 四半期報告書 / 半期報告書と、それぞれの訂正書を扱う。訂正書は EDINET documents API 上で `periodStart` / `periodEnd` が欠損しやすいため、欠損時のみ `docDescription` の対象期間から fallback parse する。同一期間の訂正書は通常書類より優先するが、古い期間の訂正書が新しい半期 / 年次の通常書類を上書きしないよう、period end を submit time より先に比較する。

`strict-net-cash-discount` は `ttm_quality_net_cash != unavailable` かつ `failure_reasons` に `debt_assumed_zero` がないときだけ判定する。Debt tag が見つからない場合は debt を 0 と推定せず、net cash は unavailable として strict lane から除外する。一方で、CSV 上に debt element があり値が `0` / `－` などのゼロ表記で報告されている場合は、報告ゼロとして debt 0 を許容する。Net cash は balance sheet snapshot なので、半期・四半期の最新値も research で確認する前提で許容する。

`fcf-yield-discount` は `ttm_quality_fcf_yield = exact` のときだけ判定する。FCF は TTM 必須であり、半期・四半期の単一期間値を annualize して機械判定しない。tag 欠損、CSV parse 失敗、非連結 fallback は `failure_reasons` と coverage report に残し、候補判定では無理に推定しない。

## 8. 業種中央値の算出

### 8.1 業種分類粒度

- **東証 33 業種** を初期値として採用
- 17 業種はマクロ判定（outlook の sectors）で使うことも可能だが、スクリーニングは 33 業種基準
- 将来 retro で粒度変更する場合は本ファイルを更新

### 8.2 中央値算出

- 各業種内の銘柄の valuation 指標から中央値を算出
- 集計タイミング: screening 実行時（週次）
- 集計対象: universe（時価総額 100 億円以上 + 売買代金 1 億円以上を満たす銘柄のみ）

### 8.3 サンプル数下限

- **n < 10 の業種**: 市場全体中央値に fallback
- 中小規模業種で n が不安定な場合の判定歪みを防止
- 例: 東証 33 業種の「空運業」「鉱業」は銘柄数が少ない場合 fallback 対象

## 9. 過去自己比較（過去 3 年レンジ）

- **対象**: PER / PBR / EV-EBITDA
- **期間**: 直近 750 営業日（≒ 3 年）
- **パーセンタイル**: 下位 20% / 下位 50% / 上位 50% / 上位 80%
- **上場 3 年未満**: 上場来レンジで代替（universe-rules.md 参照）

Historical EV/EBITDA は、各日の split-adjusted close で時価総額だけを変化させ、最新の発行済株式数・有利子負債・現金・TTM EBITDA を全期間に適用する近似で算出する。式は `(historical_adjustment_close * latest_shares_outstanding + latest_debt - latest_cash) / latest_ebitda_ttm` とし、balance sheet / EBITDA の時系列が無くても EV/EBITDA の定義を保つ。`adjustment_close` が欠損する場合は raw `close` にフォールバックする。必要項目が欠損する場合は `null` とし、`ttm_quality_ev_ebitda = exact` の銘柄だけ mechanical 判定に使う。PBR / PER の history も同じ price 基準（adjustment_close 優先）で算出するため、株式分割があっても history は連続になる。

### 9.1 `adjustment_close` の中身（dividend / 配当の扱い）

J-Quants の `AdjustmentClose` は **株式分割・株式併合 (reverse split を含む)** を遡及
調整した price-only series であり、現金配当の支払いは price には反映しない (total
return ではない)。これ以外のコーポレートアクション (合併、株式交換、その他の無償交付
等) はサポート対象外として **公式 docs に明示** されている (J-Quants daily_quotes API
リファレンス: <https://jpx.gitbook.io/j-quants-ja/api-reference/daily_quotes>)。本システム
でも total return ベースには変換せず、`adjustment_close` をそのまま使う。理由:

- `valuation-reversion` playbook の主信号は「short-term の price
  decline」であり、配当落ちを含めた pure な price 系列で判定するのが thesis と整合
- 配当落ち分を加算した擬似 total return を使うと、配当利回り高銘柄 (鉄鋼 / 銀行 / 商社等)
  の `price_change_60d` が本来より small に見え、oversold 判定が遅れる方向にバイアスする
- 1-2 ヶ月 horizon の swing trade では現金配当の寄与は 0.3-0.5% / 60 日程度で、playbook
  の利確 / 損切 target (±10-20%) から見れば noise 範囲

トータルリターン視点での portfolio 評価が必要になった場合 (年次 retro 等) は `_ledger/`
側で配当落ちを別途加算するか、J-Quants Premium の配当 API 取得を検討する。

## 10. データソース

### 10.1 Core

- **J-Quants Light / ClientV2**:
  - `get_eq_master`: 上場銘柄一覧、普通株判定、市場区分、33 業種
  - `get_eq_bars_daily_range`: 日足（OHLC + 出来高 + 売買代金）
  - `get_fin_summary_range`: 財務サマリー、会社予想 EPS、利益系の概要値
  - `get_eq_earnings_cal`: 決算発表予定日
  - `get_mkt_calendar`: 営業日カレンダ
- **EDINET API v2**:
  - documents list (`type=2`): CSV 取得可能な提出書類の選定
  - document download (`type=5`): CSV ZIP から EV/EBITDA / Net cash / FCF 関連項目を抽出
  - raw XBRL (`type=1`) の直接 parser は将来拡張。CSV-derived metrics の coverage / precision が不十分な場合に検討する
- **JPX**:
  - 上場会社情報（業種分類、市場区分の補助確認）
  - 特別注意 / 整理 / 取引停止 / 上場廃止警告の除外判定

### 10.2 Optional（将来拡張）

- J-Quants Premium（財務諸表詳細、売買内訳、配当、指数系データ）
- TDnet API（5 年分の適時開示 / XBRL）
- JPX Corporate Action Data

## 11. 半期移行と TTM 品質

- 2024 年以降、EDINET 単体では旧来の四半期報告書に依存した TTM 再構成ができない期間がある
- TTM 品質を `exact` / `approximated` / `unavailable` で明示する
- `EV/EBITDA` は `ttm_quality_ev_ebitda = exact` のときのみ valuation-reversion 判定に使用する
- `P/S` / `PCFR` / `OCF yield` / `FCF yield` / `Net cash` は、それぞれ lane が要求する品質条件を満たすときのみ mechanical 判定に使う

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

- [`principles.md`](./principles.md): スクリーニング原則
- [`universe-rules.md`](./universe-rules.md): universe 境界条件
- [`mechanical.md`](./mechanical.md): 機械的ふるい仕様（閾値 3 種 OR）
- [`../components/candidates.md`](../components/candidates.md): candidates 運用仕様
- [`../reference/data-sources.md`](../reference/data-sources.md): データソース Tier 一覧
