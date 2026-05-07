# screening/automation.md

`records/04-candidates/` の自動生成を担う screening CLI の実装正本。週次 screening の入力と実行条件を traceability として追跡するための実行方式、依存、失敗時の扱いを定義する。

## 1. Scope

- 対象は `candidates` の自動生成と、`research` 選定を補助する `select` まで
- `research` 自動採用判定、`outlook` 自動突合は対象外
- `kabuステーション API` と `JPX Market Explorer` は source of truth に使わない

## 2. Runtime

- Python 3.14（[`../reference/python-foundation.md`](../reference/python-foundation.md)）
- package root: `src/baibai_loop/screening/`
- J-Quants client は `jquantsapi.ClientV2` 固定
- 実行コマンド:

```bash
python -m baibai_loop.screening.cli run --asof YYYY-MM-DD
python -m baibai_loop.screening.cli run --asof YYYY-MM-DD --allow-stale-jpx
python -m baibai_loop.screening.cli bootstrap-cache --start YYYY-MM-DD --end YYYY-MM-DD
python -m baibai_loop.screening.cli select --asof YYYY-MM-DD [--outlook path] [--top N]
python -m baibai_loop.screening.cli extract-edinet-metrics --asof YYYY-MM-DD [--lookback-days N]
python -m baibai_loop.screening.cli rebuild-cache [--raw-dir PATH] [--sqlite-path PATH]
python -m baibai_loop.screening.cli verify-raw-cache [--raw-dir PATH] [--max-size-mb N] [--sqlite-path PATH]
```

`rebuild-cache` は `records/_data/raw/screening/` 配下の git 管理 raw JSON から派生 SQLite cache (`records/_data/cache/screening/market.sqlite`) を再生成する。実行毎に出力ファイルを削除して書き直すため idempotent。詳細は §11 を参照。

`verify-raw-cache` は `records/_data/raw/screening/` を再帰的に walk し、(1) 1 ファイル `--max-size-mb` 以上のものが無いこと、(2) SQLite (`records/_data/cache/screening/market.sqlite`) が存在する場合は `raw_imports.sha256` と現状ファイルの SHA-256 が一致することを検証する。違反があれば exit 1。CI の `quality` job でも実行され、50MB 超過の commit を merge 前に弾く。

`extract-edinet-metrics` は EDINET documents list (`type=2`) から CSV 取得可能な有価証券報告書 / 四半期報告書 / 半期報告書を選び、EDINET document download (`type=5`) の CSV ZIP から screening 用 metrics を抽出する。出力は `records/_data/raw/screening/edinet/metrics/YYYY-MM-DD.json`。CSV ZIP 本体は再生成可能な derived cache として `records/_data/cache/screening/edinet/csv_zips/` に保存し、git には載せない。

`select` は最新 `records/04-candidates/<YYYY>/<MM>/<asof>.yaml` と `records/03-outlook/` を組み合わせて、`outlook` で `adverse` 判定された業種を除外し、lane-specific metric と macro status で候補をランキングする。Hit 数と時価総額だけでは並べない。出力には lane 別の `lane_toplists`、旧来のグローバル順位である `ranked_candidates`、research 着手用に lane 分散した `candidates` が含まれる。`candidates` は `output.research_selection_lane_order` の順に各 lane の上位を重複排除して選び、残枠を `ranked_candidates` で埋める。`recommendation_lane` は lane 分散で拾った枠、`selection_lane` は primary thesis として確認する screen で、複数 hit 銘柄では一致しないことがある。件数は CLI `--top` と `output.research_selection_target_max` の小さい方、`lane_toplists` は `records/_config/screening-rules/2026-05-01T000000+0900.yaml` の `output.lane_toplist_limit` で管理する。`research` の選定プロセス ([`../components/research.md`](../components/research.md) §2.1) をスクリプトで支援する。

## 3. Required Env Vars

- `JQUANTS_REFRESH_TOKEN`

raw cache / SQLite cache の配置先は固定 (env override 廃止):

- raw JSON: `records/_data/raw/screening/`（git 管理対象、hardcoded）
- SQLite cache: `records/_data/cache/screening/`（gitignore、`run` 開始時に raw JSON から自動 rebuild）

任意:

- `EDINET_API_KEY`: EV/EBITDA など EDINET 前処理済み metrics を使う場合のみ設定する。未設定でも screening は実行可能で、EV/EBITDA は `unavailable` として扱う
- JPX 公開規制情報 URL（CSV / Excel / HTML）。`records/_config/screening-rules/2026-05-01T000000+0900.yaml` の `universe.required_jpx_flags` に含まれる source は必須で、未ロード時は fail-fast し candidates YAML を生成しない:
  - `JPX_SPECIAL_CAUTION_INDEX_URL` 特別注意銘柄の個別銘柄信用取引残高表 index（推奨。日次で変わる `mtdailyk*.xls` を index から解決）
  - `JPX_SPECIAL_CAUTION_URL` 特別注意銘柄の固定 Excel URL
  - `JPX_REORGANIZATION_URL` 整理銘柄
  - `JPX_TRADING_HALT_URL` 取引停止
  - `JPX_DELISTING_WARNING_URL` 上場廃止警告
- env vars の雛形は repo root の `.env.sample` を参照

## 4. J-Quants ClientV2 Methods

使う method は次の 5 点に固定する。

| method | 用途 |
| --- | --- |
| `get_eq_master` | 上場銘柄一覧、普通株判定、市場区分、33 業種、信用銘柄区分 |
| `get_eq_bars_daily_range` | 日次 OHLCV、20 営業日平均売買代金、60 営業日騰落率、750 営業日自己レンジ |
| `get_fin_summary_range` | 財務サマリー、会社予想 EPS、利益系概要値 |
| `get_eq_earnings_cal` | 決算発表予定 |
| `get_mkt_calendar` | 営業日カレンダ |

## 5. EDINET Baseline（任意）

- API version: v2
- 仕様書: `2026-01-29 / ESE140206.pdf`
- 認証方式: `Subscription-Key` を query parameter に付与
- 対象 docTypeCode:
  - `120`: 有報
  - `130`: 訂正有報
  - `140`: 旧四半期報告書
  - `150`: 訂正四半期報告書
  - `160`: 半期報告書
  - `170`: 訂正半期報告書
- document selection は period end / period start を submit time より先に比較し、古い訂正書が新しい通常書類を上書きしない。同一期間では最新 submit time を優先し、同一 submit time の tie-break として訂正書を通常書類より優先する
- `extract-edinet-metrics` は `documents.json` 取得後、`type=5` CSV ZIP（UTF-16 LE タブ区切り）を解凍し、EV/EBITDA / net cash / FCF 関連 metrics を前処理済み JSON cache に書く
- EDINET cache / API key が無い場合も screening は fail-fast しない。`data_sources` には利用した source のみを記録し、`config_hash` には rules file hash と optional EDINET 設定有無を含める

EDINET CSV-derived metrics を更新してから run する標準手順:

```bash
python -m baibai_loop.screening.cli extract-edinet-metrics --asof YYYY-MM-DD --lookback-days 540
python -m baibai_loop.screening.cli rebuild-cache
python -m baibai_loop.screening.cli run --asof YYYY-MM-DD
```

`EDINET_API_KEY` が無い場合、`extract-edinet-metrics` は fail-fast する。`run` は EDINET metrics が無い状態でも継続し、EV/EBITDA / strict net-cash / FCF は `unavailable` として degrade する。

## 6. JPX Policy

- 利用対象は JPX 公開情報（CSV / Excel / HTML）
- HTML は `https://www.jpx.co.jp/` 配下の許可済み URL に限定し、source-specific parser で fail-fast に扱う
- 特別注意銘柄は `JPX_SPECIAL_CAUTION_INDEX_URL` が設定されていれば、JPX の「個別銘柄信用取引残高表」index から最新の `mtdailyk*.xls` link を解決してから Excel を取得する。index 未設定時は `JPX_SPECIAL_CAUTION_URL` の固定 URL を使う
- 規制情報の取得失敗は fail-fast
- `universe.required_jpx_flags` の source が欠ける場合は fail-fast する。JPX 規制除外は universe 定義の一部であり、warning-only では扱わない
- JPX 公開規制情報は latest snapshot しか取得できないため、cache には `fetched_at_utc` を記録する。cache 読み込み時に `asof` と `fetched_at_utc` が 7 weekday 超乖離していれば warning を出す（祝日は引かない近似）
- `asof` が実行日から 7 weekday 超過去で、該当日の JPX cache が無い場合、`run` は fail-fast する。運用者が latest snapshot を過去 `asof` に固定するリスクを許容する場合のみ `--allow-stale-jpx` を付ける

## 7. Date Semantics

- `--asof` は対象営業日を表す
- `run_date` は `asof_date` と同値にする
- 出力 path は `records/04-candidates/{YYYY}/{MM}/{asof_date}.yaml`
- 同一 path が既に存在する場合は fail-fast
- 非営業日の `--asof` は fail-fast

## 8. Rule Baselines

閾値の正本は `records/_config/screening-rules/2026-05-01T000000+0900.yaml`。実装側の hardcode は parser default と型定義に留め、運用で変える閾値は YAML に寄せる。

- universe 閾値: 時価総額、平均売買代金、上場日数、JPX 除外 flag
- playbook-linked screen 閾値: `valuation-reversion` / `strict-net-cash-discount` / `fcf-yield-discount` / `cash-rich-asset-discount` / `cashflow-yield-discount` / `sales-discount-growth`
- TTM 期間一致基準: partial period の許容日数差、FY 期間長
- 品質条件: 売上 YoY、営業利益、営業 CF 悪化、赤字縮小条件
- `EV/EBITDA` は `ttm_quality = exact` かつ EV / EBITDA がどちらも正のときのみ判定に使う。EDINET が無い場合、または EV / EBITDA がゼロ以下の場合は `unavailable` / `null` として他 metric で degrade する

## 9. Partial Warning Thresholds

- 有効な playbook-linked screen が必須とする TTM metric の `ttm_quality != exact` が universe の 5% 以上、または 20 銘柄以上
- EDINET optional による EV/EBITDA `unavailable` だけでは partial warning にしない
- 業績悪化フィルタ入力欠損が universe の 10% 以上

## 10. Exit Codes

- `0`: 全件成功
- `1`: fail-fast（YAML 未生成）
- `2`: partial warning（YAML 生成済み、欠損明記）

## 11. Cache Layout

- `records/_data/raw/screening/` は J-Quants / EDINET / JPX から取得した raw JSON の **正本**。git 管理対象。1 ファイル 50MB 未満を維持し、別 PC で `git clone` 後に再取得なしで screening / ledger を再生成できる
- `records/_data/cache/screening/` は raw JSON から派生した SQLite cache や rebuild 中の一時ファイルの置き場。`.gitignore` 対象。安全に削除して再生成できる
- `records/_data/cache/screening/edinet/csv_zips/` は EDINET `type=5` CSV ZIP の derived cache。metrics JSON を再生成するための一時物であり git 管理しない
- `records/_data/raw/screening/disclosures/**/*.json` は任意の disclosure title cache。TDnet / 会社 IR 等から取得した `ticker` / `date` / `title` / `source` / `url` 相当の record を置くと、screening run が EDINET metrics 提出日以降の M&A・借入などの title keyword hit を `freshness_warnings` として candidates YAML に出す。cache が無い場合は `provider_status_lines` に optional unavailable を出す。cache が存在するが JSON 読み込み失敗・未対応 layout・必須 key 欠損がある場合は `provider_status_lines` と `fallback_lines` に件数を出す
- `records/_data/raw/screening/manifests/` は run 毎の lineage manifest 出力先。`.gitignore` 対象（`candidates` YAML 側に `cache_manifest_hash` が記録されるため、manifest JSON 自体は git に載せない）
- `JQuantsProvider` は SQLite (`records/_data/cache/screening/market.sqlite`) が存在し、要求範囲を `raw_imports` の chunk window で覆える場合は SQLite から読む（read-through）。覆えない場合は raw JSON cache → API の順にフォールバックする。SQLite が古い場合は `rebuild-cache` を再実行する

### 11.1 SQLite Schema

`rebuild-cache` は以下のテーブルを `records/_data/cache/screening/market.sqlite` に作成する。`cache_metadata.schema_version` で schema version を管理する。

- `jquants_daily_bars(ticker, traded_at, open, high, low, close, volume, turnover_value, adjustment_*, upper_limit, lower_limit)` — 主キー `(ticker, traded_at)`、`traded_at` index 付。`is_common_stock=False` の record はスキップ
- `jquants_fin_summaries(ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, sales, operating_profit, ordinary_profit, profit, cfo, cash_eq, total_assets, equity, fiscal_period, fiscal_year_end, period_start, period_end, raw_json)` — 主キー `(ticker, disclosed_at)`
- `jquants_master_snapshots(snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json)` — 主キー `(snapshot_date, ticker)`
- `jquants_earnings_calendar(announcement_date, ticker, raw_json)` — 主キー `(announcement_date, ticker)`
- `disclosures` raw JSON（SQLite 未収録）— 任意 cache。`Code` / `Date` / `Title` などの同義 key も reader 側で受け付ける。title keyword scan のみで金額や財務影響は解釈しない。読み取り coverage は `file_count` / `event_count` / `skipped_record_count` / `unsupported_record_count` / `load_errors` として candidates YAML の status / fallback に反映する
- `jquants_market_calendar(day, is_business_day, raw_json)` — 主キー `(day)`。`HolidayDivision` "1" / "2" を business day=1、それ以外を 0 として記録
- `edinet_documents(doc_date, doc_id, sec_code, doc_type_code, raw_json)` — 主キー `(doc_date, doc_id)`。`doc_date` はファイル名（`{date}.json`）から復元
- `edinet_metrics(asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, operating_profit_ttm, depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, net_cash, equity, total_assets, consolidation_basis, ttm_quality_*, source_doc_id, document_type, source_submit_datetime, source_period_start, source_period_end, capex_source, failure_reasons)` — 主キー `(asof_date, ticker)`。`asof_date` はファイル名から復元。Candidate YAML では EDINET raw `ocf_ttm` を `edinet_ocf_ttm` として出し、J-Quants 財務サマリー由来の `ocf_ttm` と区別する。`source_*` は research で一次資料へ戻るための traceability として保持する。`source_period_start/end` は EDINET documents metadata であり、半期報告書では実際の CF 測定期間と一致しないことがある
- `jpx_regulation_flags(asof_date, source_name, ticker, flag, fetched_at_utc)` — 主キー `(asof_date, source_name, ticker, flag)`。JPX cache の `flags_by_ticker` は source 別の起源を保持しないため、`source_name=flag` として記録
- `raw_imports(source, path, sha256, imported_at_utc, record_count, min_date, max_date)` — `path` を主キーとし、import した raw JSON の SHA-256 と record 範囲を記録する監査用 table
- `cache_metadata(key, value)` — schema version などの KV ストア

### 11.2 Volume と rebuild 時間の目安

raw JSON 約 1.1GB / 44 ファイル（1 ファイル最大 32.8MB）規模で、`rebuild-cache` は数十秒程度で完了する。月次の追加見込みは raw JSON +30〜35MB / SQLite +10〜13MB の想定。GitHub 私有リポジトリ推奨上限 5GB に収まる範囲で運用する。
