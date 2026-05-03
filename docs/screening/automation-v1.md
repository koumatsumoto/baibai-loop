# screening/automation-v1.md

Baibai-Loop の `screened/` を対象にした automation v1 の実装正本。週次 screening の入力と実行条件を traceability として追跡するための実行方式、依存、失敗時の扱いを定義する。

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
python -m baibai_loop.screening.cli run --asof YYYY-MM-DD --allow-stale-jpx
python -m baibai_loop.screening.cli bootstrap-cache --start YYYY-MM-DD --end YYYY-MM-DD
python -m baibai_loop.screening.cli select --asof YYYY-MM-DD [--view path] [--top N]
python -m baibai_loop.screening.cli migrate-cache [--from PATH] [--to PATH] [--dry-run]
python -m baibai_loop.screening.cli rebuild-cache [--raw-dir PATH] [--sqlite-path PATH]
python -m baibai_loop.screening.cli verify-raw-cache [--raw-dir PATH] [--max-size-mb N] [--sqlite-path PATH]
```

`migrate-cache` は legacy の `.cache/screening/` 配下の raw JSON を git 管理対象の `data/raw/screening/` に移動する一回限りの helper。re-run しても既存ファイルは上書きしない（idempotent）。詳細は §11 を参照。

`rebuild-cache` は `data/raw/screening/` 配下の git 管理 raw JSON から派生 SQLite cache (`data/cache/screening/market.sqlite`) を再生成する。実行毎に出力ファイルを削除して書き直すため idempotent。schema は v1（jquants_daily_bars / jquants_fin_summaries / jquants_master_snapshots / raw_imports / cache_metadata）。詳細は §11 を参照。

`verify-raw-cache` は `data/raw/screening/` を再帰的に walk し、(1) 1 ファイル `--max-size-mb` 以上のものが無いこと、(2) SQLite (`data/cache/screening/market.sqlite`) が存在する場合は `raw_imports.sha256` と現状ファイルの SHA-256 が一致することを検証する。違反があれば exit 1。CI の `quality` job でも実行され、50MB 超過の commit を merge 前に弾く。

`select` は最新 `screened/<YYYY>/<MM>/<asof>.yaml` と `view/` を組み合わせて、`view` で `headwind` 判定された業種を除外し、`threshold_hit` の本数 → 時価総額の順で候補をランキングする。`research` の選定プロセス (`docs/components/research.md` §2.1) をスクリプトで支援する。

## 3. Required Env Vars

- `JQUANTS_REFRESH_TOKEN`
- `EDINET_API_KEY`

任意:

- `SCREENING_CACHE_DIR`
  - 既定値: `data/raw/screening`（git 管理対象）。issue #45 で `.cache/screening`（gitignore）から移行。
- `SCREENING_SQLITE_CACHE_DIR`
  - 既定値: `data/cache/screening`（gitignore）。raw JSON から再生成される SQLite cache 配置先。
- JPX 公開規制情報 URL（CSV / Excel / HTML。未設定時は該当 source のカバレッジなしで `fallback_lines` に `JPX source 未ロード` 明示）。URL 運用の日次変動は issue #16 を参照:
  - `JPX_SPECIAL_CAUTION_INDEX_URL` 特別注意銘柄の個別銘柄信用取引残高表 index（推奨。日次で変わる `mtdailyk*.xls` を index から解決）
  - `JPX_SPECIAL_CAUTION_URL` 特別注意銘柄の固定 Excel URL（後方互換）
  - `JPX_REORGANIZATION_URL` 整理銘柄
  - `JPX_TRADING_HALT_URL` 取引停止
  - `JPX_DELISTING_WARNING_URL` 上場廃止警告
- env vars の雛形は repo root の `.env.sample` を参照。

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
- CSV ZIP の解凍形式は仕様上 UTF-16 LE タブ区切り（v1 時点の CLI は `documents.json` の取得のみを行い、CSV ZIP 解凍・metric 抽出は本 CLI では未実装。`providers/edinet.py` の `load_metric_records` は前処理済み JSON cache を読み込む前提）

## 6. JPX Policy

- 利用対象は JPX 公開情報（CSV / Excel / HTML）
- HTML は `https://www.jpx.co.jp/` 配下の許可済み URL に限定し、source-specific parser で fail-fast に扱う
- 特別注意銘柄は `JPX_SPECIAL_CAUTION_INDEX_URL` が設定されていれば、JPX の「個別銘柄信用取引残高表」index から最新の `mtdailyk*.xls` link を解決してから Excel を取得する。index 未設定時は従来どおり `JPX_SPECIAL_CAUTION_URL` の固定 URL を使う。
- 規制情報の取得失敗は fail-fast
- 個別 source のうちロードできなかったものは `fallback_lines` に `JPX source 未ロード: ...` として明示される
- JPX 公開規制情報は latest snapshot しか取得できないため、cache には `fetched_at_utc` を記録する。cache 読み込み時に `asof` と `fetched_at_utc` が 7 weekday 超乖離していれば warning を出す（祝日は引かない近似）。
- `asof` が実行日から 7 weekday 超過去で、該当日の JPX cache が無い場合、`run` は fail-fast する。運用者が latest snapshot を過去 `asof` に固定するリスクを理解して許容する場合のみ `--allow-stale-jpx` を付ける。

## 7. Date Semantics

- `--asof` は対象営業日を表す
- `run_date` は `asof_date` と同値にする
- 出力 path は `screened/{YYYY}/{MM}/{asof_date}.yaml`
- 同一 path が既に存在する場合は fail-fast
- 非営業日の `--asof` は fail-fast

## 8. Rule Baselines

- `yoy_deterioration_threshold = -30%`
- 営業利益相当の fallback:
  - `OperatingProfit`
  - `OrdinaryProfit`
  - `Profit`
- `short_history_flag = true` の銘柄は条件 A を skip
- `EV/EBITDA` は `ttm_quality = exact` のときのみ判定に使う（**issue #15 の historical 近似バグ解決までは常時除外**。`_rule_metrics` は `per_trailing` / `pbr` のみを返す）

## 9. Partial Warning Thresholds

- `ttm_quality != exact` が universe の 5% 以上、または 20 銘柄以上
- 業績悪化フィルタ入力欠損が universe の 10% 以上

## 10. Exit Codes

- `0`: 全件成功
- `1`: fail-fast（YAML 未生成）
- `2`: partial warning（YAML 生成済み、欠損明記）

## 11. Cache Layout (issue #45)

- `data/raw/screening/` は J-Quants / EDINET / JPX から取得した raw JSON の **正本**。git 管理対象。1 ファイル 50MB 未満を維持し、別 PC で `git clone` 後に再取得なしで screening / ledger を再生成できる状態を目指す。
- `data/cache/screening/` は raw JSON から派生した SQLite cache や rebuild 中の一時ファイルの置き場。`.gitignore` 対象。安全に削除して再生成できる。
- `data/raw/screening/manifests/` は run 毎の lineage manifest 出力先。`.gitignore` 対象（`screened` YAML 側に `cache_manifest_hash` が記録されるため、manifest JSON 自体は git に載せない）。
- `JQuantsProvider` は SQLite (`data/cache/screening/market.sqlite`) が存在し、要求範囲を `raw_imports` の chunk window で覆える場合は SQLite から読む（read-through）。覆えない場合は従来通り raw JSON cache → API の順にフォールバックする。SQLite が古い場合は `rebuild-cache` を再実行する。
- `.cache/screening/` は legacy 配置で `.gitignore` のまま。新規ファイルは作られないが、既存の checkout には残っている。`migrate-cache` サブコマンドで `data/raw/screening/` に移動する。
- `migrate-cache` は冪等。`.cache/screening/` を空にした後に手動で `rmdir` して legacy ディレクトリを掃除してよい。

### 11.1 SQLite Schema v1

`rebuild-cache` は以下のテーブルを `data/cache/screening/market.sqlite` に作成する。

- `jquants_daily_bars(ticker, traded_at, open, high, low, close, volume, turnover_value, adjustment_*, upper_limit, lower_limit)` — 主キー `(ticker, traded_at)`、`traded_at` index 付。`is_common_stock=False` の record はスキップする。
- `jquants_fin_summaries(ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, sales, operating_profit, ordinary_profit, profit, fiscal_period, fiscal_year_end, period_start, period_end, raw_json)` — 主キー `(ticker, disclosed_at)`。
- `jquants_master_snapshots(snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json)` — 主キー `(snapshot_date, ticker)`。
- `raw_imports(source, path, sha256, imported_at_utc, record_count, min_date, max_date)` — `path` を主キーとし、import した raw JSON の SHA-256 と record 範囲を記録する監査用 table。
- `cache_metadata(key, value)` — `schema_version=v1` を含む KV ストア。

EDINET / JPX / earnings_calendar / market_calendar の SQLite 化、および provider 側の SQLite read-through / write-through 切替は follow-up の対象。issue #45 の TODO を参照。
