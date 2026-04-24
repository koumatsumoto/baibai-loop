# screening/automation-v1.md

Baibai-Loop の `screened/` を対象にした automation v1 の実装正本。週次 screening を機械的に再現するための実行方式、依存、失敗時の扱いを定義する。

## 1. Scope

- 対象は `screened` 自動生成まで
- `research` 自動選定、`view` 自動突合、CI 定期実行は対象外
- `kabuステーションAPI` と `JPX Market Explorer` は source of truth に使わない

## 2. Runtime

- Python 3.12 以上
- package root: `src/baibai_loop/screening/`
- J-Quants client は **`jquantsapi.ClientV2` 固定**
- 実行コマンド:

```bash
python -m baibai_loop.screening.cli run --asof YYYY-MM-DD
python -m baibai_loop.screening.cli bootstrap-cache --start YYYY-MM-DD --end YYYY-MM-DD
```

## 3. Required Env Vars

- `JQUANTS_REFRESH_TOKEN`
- `EDINET_API_KEY`

任意:

- `SCREENING_CACHE_DIR`
  - 既定値: `.cache/screening`

## 4. J-Quants ClientV2 Methods

v1 で使う method は次の 5 点に固定する。

| method | 用途 |
| --- | --- |
| `get_eq_master` | 上場銘柄一覧、普通株判定、市場区分、33 業種、信用銘柄区分 |
| `get_eq_bars_daily_range` | 日次 OHLCV、20 営業日平均売買代金、60 営業日騰落率、750 営業日自己レンジ |
| `get_fin_summary_range` | 財務サマリー、会社予想 EPS、利益系概要値 |
| `get_eq_earnings_cal` | 決算発表予定 |
| `get_mkt_calendar` | 営業日カレンダ |

`Client` (V1) は deprecated のため使わない。

## 5. EDINET Baseline

- API version: v2
- 仕様書参照日: `2026-01-29 / ESE140206.pdf`
- 認証方式: `Subscription-Key` を query parameter に付与
- 対象 docTypeCode:
  - `120`: 有報
  - `140`: 旧四半期報告書
  - `160`: 半期報告書
- CSV ZIP は UTF-16 LE タブ区切りとしてパースする

## 6. JPX Policy

- 利用対象は公開 CSV / Excel のみ
- HTML スクレイプは行わない
- 規制情報の取得失敗は fail-fast

## 7. Date Semantics

- `--asof` は対象営業日を表す
- `run_date` は `asof_date` と同値にする
- 出力 path は `screened/{YYYY}/{MM}/{asof_date}.md`
- 同一 path が既に存在する場合は fail-fast
- 非営業日の `--asof` は fail-fast

## 8. Rule Baselines

- `yoy_deterioration_threshold = -30%`
- 営業利益相当の fallback:
  - `OperatingProfit`
  - `OrdinaryProfit`
  - `Profit`
- `short_history_flag = true` の銘柄は条件 A を skip
- `EV/EBITDA` は `ttm_quality = exact` のときのみ判定に使う

## 9. Partial Warning Thresholds

- `ttm_quality != exact` が universe の 5% 以上、または 20 銘柄以上
- 業績悪化フィルタ入力欠損が universe の 10% 以上

## 10. Exit Codes

- `0`: 全件成功
- `1`: fail-fast（markdown 未生成）
- `2`: partial warning（markdown 生成済み、欠損明記）
