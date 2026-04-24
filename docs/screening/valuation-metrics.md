# screening/valuation-metrics.md

Baibai-Loop スクリーニングで使う valuation 指標の算出仕様とデータソース。`(b) screened/` と `(d) research/` の両方で参照される指標の前提を確定する。

## 1. 使用指標

| 指標 | 定義 | データ項目 |
| --- | --- | --- |
| PER (Forward) | 株価 / 会社予想 EPS | 株価、会社予想 EPS |
| PER (Trailing) | 株価 / 直近 4 四半期 EPS | 株価、EPS 直近 4Q 合算 |
| PBR | 株価 / 1 株純資産（BPS） | 株価、BPS |
| EV/EBITDA | (時価総額 + 有利子負債 - 現金) / EBITDA | 時価総額、有利子負債、現金、EBITDA |
| P/S | 株価 / 1 株売上高 | 株価、直近 4Q 売上 |
| PCFR | 株価 / 1 株営業 CF | 株価、直近 4Q 営業 CF |

## 2. Forward PER の取得方針（重要）

### 2.1 採用ソース

- **会社予想 EPS ベース**（会社が期初/修正後に開示した公式予想）
- 取得: J-Quants core（財務サマリー / 業績予想） + EDINET（補完）

### 2.2 却下したソース

- **アナリストコンセンサス forward EPS**: J-Quants core / EDINET のスコープ外。Bloomberg / IBES 等は有料で本計画の非スコープ。
- **期初予想のみ使用**: 期中の修正予想を無視すると精度低下、最新の修正予想を使う

### 2.3 会社予想未公表 or 予想レンジ提示銘柄の扱い

- **forward PER なし** として扱い、`per_forward: null`
- **trailing PER のみで判定**（mechanical-v1.md の閾値判定は trailing で代用）
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

EDINET の XBRL 構造から取得。J-Quants Light の財務サマリーで取れる項目は優先使用し、不足分を EDINET で補完する。

## 6. P/S の算出

- 直近 4 四半期の売上高合算
- 連結 / 単体の区別: **連結優先**

## 7. PCFR の算出

- 直近 4 四半期の営業 CF 合算
- 営業 CF マイナスの企業は `null` を採用（割安検出に意味を持たない）

## 8. 業種中央値の算出

### 8.1 業種分類粒度

- **東証 33 業種** を初期値として採用
- 17 業種はマクロ判定（view の sectors）で使うことも可能だが、スクリーニングは 33 業種基準
- 将来 retro で粒度変更する場合は本ファイルを更新

### 8.2 中央値算出

- 各業種内の銘柄の valuation 指標から中央値を算出
- 集計タイミング: screening 実行時（週次）
- 集計対象: universe（時価総額 300 億円以上 + 売買代金 2 億円以上を満たす銘柄のみ）

### 8.3 サンプル数下限

- **n < 10 の業種**: 市場全体中央値に fallback
- 中小規模業種で n が不安定な場合の判定歪みを防止
- 例: 東証 33 業種の「空運業」「鉱業」は銘柄数が少ない場合 fallback 対象

## 9. 過去自己比較（過去 3 年レンジ）

- **対象**: PER / PBR / EV-EBITDA
- **期間**: 直近 750 営業日（≒ 3 年）
- **パーセンタイル**: 下位 20% / 下位 50% / 上位 50% / 上位 80%
- **上場 3 年未満**: 上場来レンジで代替（universe-rules.md 参照）

## 10. データソース

### 10.1 Core（v1 必須）

- **J-Quants Light / ClientV2**:
  - `get_eq_master`: 上場銘柄一覧、普通株判定、市場区分、33 業種
  - `get_eq_bars_daily_range`: 日足（OHLC + 出来高 + 売買代金）
  - `get_fin_summary_range`: 財務サマリー、会社予想 EPS、利益系の概要値
  - `get_eq_earnings_cal`: 決算発表予定日
  - `get_mkt_calendar`: 営業日カレンダ
- **EDINET API v2**:
  - XBRL ベース財務諸表（EV/EBITDA / P/S / PCFR 計算用）
  - 2024 年以降の半期移行を踏まえた TTM 再構成用の確定値
- **JPX**:
  - 上場会社情報（業種分類、市場区分の補助確認）
  - 特別注意 / 整理 / 取引停止 / 上場廃止警告の除外判定

### 10.2 Optional（将来拡張）

- J-Quants Premium（財務諸表詳細、売買内訳、配当、指数系データ）
- TDnet API（5 年分の適時開示 / XBRL）
- JPX Corporate Action Data

## 11. 半期移行と TTM 品質

- 2024 年以降、EDINET 単体では旧来の四半期報告書に依存した TTM 再構成ができない期間がある
- v1 では TTM 品質を `exact` / `approximated` / `unavailable` で明示する
- `EV/EBITDA` は `ttm_quality = exact` のときのみ mechanical 判定に使用する
- `P/S` と `PCFR` は v1 では表示用とし、`ttm_quality` を front matter に残す

## 12. 営業利益相当の fallback

- 業績悪化フィルタに使う利益代表は以下の順で採用する
  - `OperatingProfit`
  - `OrdinaryProfit`
  - `Profit`
- すべて欠損のときは EPS / 売上の 2 項目だけで業績悪化フィルタを評価する

## 13. 算出エラー・欠損の扱い

- 取得不能・算出不能は **明示的に `null`**（省略しない）
- 決算期またぎの一時的欠損: 確報確定まで `null` 運用
- 会計方針変更・特損計上等で一時的歪み: research 側で「反対仮説」に記録、screened の指標値は素直に採用（事実層のため）

## 14. 参考

- [`principles.md`](./principles.md): スクリーニング原則
- [`universe-rules.md`](./universe-rules.md): universe 境界条件
- [`mechanical-v1.md`](./mechanical-v1.md): 機械的ふるい仕様（閾値 3 種 OR）
- [`../components/screened.md`](../components/screened.md): screened 運用仕様
- [`../data-sources.md`](../data-sources.md): データソース Tier 一覧
