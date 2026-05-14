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
python -m baibai_loop.screening.cli bootstrap-cache --asof YYYY-MM-DD
python -m baibai_loop.screening.cli select --asof YYYY-MM-DD [--outlook path] [--top N] [--profile PROFILE]
python -m baibai_loop.screening.cli select-sweep --asof YYYY-MM-DD [--outlook path] [--top N] [--profiles strict,balanced,loose]
python -m baibai_loop.screening.cli extract-edinet-metrics --asof YYYY-MM-DD [--lookback-days N]
python -m baibai_loop.screening.cli verify-cache-coverage --asof YYYY-MM-DD [--sqlite-path PATH] [--rules-path PATH]
```

`bootstrap-cache --asof` は `run --asof` が要求する source 別 window を自動で補完する。具体的には J-Quants master、asof まで 1200 日分の日次足、asof まで 730 日分の財務サマリー、asof から 90 日先までの決算予定 horizon、asof の営業日カレンダ、JPX 規制 snapshot を SQLite に書き込む。

`verify-cache-coverage` は SQLite が `run --asof` で必要な全入力をローカルに提供できるかを read-only で検証する。検証対象は J-Quants master、1200 日分の日次足、730 日分の財務サマリー、asof から 90 日先までの決算予定 horizon、asof の営業日カレンダ、asof の JPX 規制 snapshot と rules の `universe.required_jpx_flags` に含まれる source 名、EDINET metrics。master は common-stock universe が異常に小さくないこと、日次足と財務サマリーは source_coverage だけでなく SQLite 実データの ticker/date 密度も確認する。EDINET metrics は常に必須であり、raw JSON の読み込みや provider API 呼び出しは行わず、schema migration も行わない。不足があれば exit 1。

`extract-edinet-metrics` は EDINET documents list (`type=2`) から CSV 取得可能な有価証券報告書 / 四半期報告書 / 半期報告書を選び、EDINET document download (`type=5`) の CSV ZIP から screening 用 metrics を抽出して `data/screening/market.sqlite` に保存する。CSV ZIP 本体は再生成可能な cache として `.cache/screening/edinet/csv_zips/` に保存し、git には載せない。

`select` は最新 `records/04-candidates/<YYYY>/<MM>/<asof>.yaml` と `records/03-outlook/` を組み合わせて、`outlook` で `adverse` 判定された業種を除外し、queue 型の research triage を出力する。正本は `queues.recommended_research_queue` と `selection.diagnostics`。`fast_dislocation_queue` は価格下落 trigger と fundamental guard family を同時に要求し、出来高 spike / 52 週安値距離だけでは eligible にしない。`core_value_queue` は既存 playbook lane の分散候補、`long_hold_survivability_queue` は保有耐性の補助 queue。`recommendation_lane` は候補を拾った queue / lane、`selection_lane` は primary thesis として確認する screen で、複数 hit 銘柄では一致しないことがある。`select-sweep` は複数 profile を同じ candidates / outlook に replay し、recommended detail、fast total/emitted count、long-hold count、suppressed count、profile 間 diff、concentration / previous overlap / warnings を比較する。`--profile-config` の未知 key や未知 queue は fail-fast し、typo した profile を「検証済み」と誤認しない。`research` の選定プロセス ([`../components/research.md`](../components/research.md) §2.1) をスクリプトで支援する。

## 3. Required Env Vars

- `JQUANTS_REFRESH_TOKEN`

cache / SQLite の配置先は固定 (env override 廃止):

- disposable cache: `.cache/screening/`（gitignore。EDINET CSV ZIP や任意 disclosure title 入力など）
- SQLite 正本: `data/screening/market.sqlite`（gitignore。`run` 開始前に coverage を検証し、不足時は fail-fast）

任意 / 事前生成:

- `EDINET_API_KEY`: `extract-edinet-metrics` 実行時に必要。`run` は SQLite の EDINET metrics を必須入力として扱うため、標準運用では `run` 前に EDINET metrics を抽出しておく
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

## 5. EDINET Baseline

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
- `extract-edinet-metrics` は `documents.json` 取得後、`type=5` CSV ZIP（UTF-16 LE タブ区切り）を解凍し、EV/EBITDA / net cash / FCF 関連 metrics を SQLite に書く
- EDINET API key は `extract-edinet-metrics` 時だけ必要。`run` は provider API へ進まず、SQLite に asof の EDINET metrics が無い場合は fail-fast する。EDINET CSV ZIP 本体は disposable cache だが、抽出後の metrics は SQLite 正本の一部として扱う

EDINET CSV-derived metrics を更新してから run する標準手順:

```bash
python -m baibai_loop.screening.cli extract-edinet-metrics --asof YYYY-MM-DD --lookback-days 540
python -m baibai_loop.screening.cli verify-cache-coverage --asof YYYY-MM-DD
python -m baibai_loop.screening.cli run --asof YYYY-MM-DD
```

`EDINET_API_KEY` が無い場合、`extract-edinet-metrics` は fail-fast する。`run` は SQLite の EDINET metrics が無い状態では継続せず、事前 coverage 検証で fail-fast する。

## 6. JPX Policy

- 利用対象は JPX 公開情報（CSV / Excel / HTML）
- HTML は `https://www.jpx.co.jp/` 配下の許可済み URL に限定し、source-specific parser で fail-fast に扱う
- 特別注意銘柄は `JPX_SPECIAL_CAUTION_INDEX_URL` が設定されていれば、JPX の「個別銘柄信用取引残高表」index から最新の `mtdailyk*.xls` link を解決してから Excel を取得する。index 未設定時は `JPX_SPECIAL_CAUTION_URL` の固定 URL を使う
- 規制情報の取得失敗は fail-fast
- `universe.required_jpx_flags` の source が欠ける場合は fail-fast する。JPX 規制除外は universe 定義の一部であり、warning-only では扱わない
- JPX 公開規制情報は latest snapshot しか取得できないため、SQLite には `fetched_at_utc` を記録する。通常の coverage 検証では `asof` と `fetched_at_utc` が 7 weekday 超乖離していれば fail-fast する（祝日は引かない近似）
- `screening run` は JPX を取得しない。historical backfill で latest snapshot を過去 `asof` に固定するリスクを許容する場合は、先に `bootstrap-cache --asof` で SQLite に保存し、`verify-cache-coverage --allow-stale-jpx` と `run --allow-stale-jpx` を明示する

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
- EDINET metrics は `run` の必須 coverage。metrics 抽出済みでも個別 metric が `unavailable` になる場合だけ partial warning の対象にする
- 業績悪化フィルタ入力欠損が universe の 10% 以上

## 10. Exit Codes

- `0`: 全件成功
- `1`: fail-fast（YAML 未生成）
- `2`: partial warning（YAML 生成済み、欠損明記）

## 11. Cache Layout

- `data/screening/market.sqlite` は screening input の local canonical store。J-Quants / EDINET / JPX の provider fetch は normalized table と `source_coverage` を直接更新する
- `.cache/screening/edinet/csv_zips/` は EDINET `type=5` CSV ZIP の cache。削除しても SQLite の metric rows は残る
- `.cache/screening/disclosures/**/*.json` は SQLite 正本の対象外に残す任意の disclosure title cache。TDnet / 会社 IR 等から取得した `ticker` / `date` / `title` / `source` / `url` 相当の record を置くと、screening run が EDINET metrics 提出日以降の M&A・借入などの title keyword hit を `freshness_warnings` として candidates YAML に出す。cache が無い場合は `provider_status_lines` に optional unavailable を出す。cache が存在するが JSON 読み込み失敗・未対応 layout・必須 key 欠損がある場合は `provider_status_lines` と `fallback_lines` に件数を出す
- `records/` は履歴成果物だけを置く。通常運用の raw JSON cache、SQLite、CSV ZIP、一時 manifest は置かない
- `screening run` は開始時に `verify-cache-coverage --asof` 相当の coverage 検証を行う。SQLite が不完全な場合は fail-fast し、raw JSON cache や provider API へフォールバックしない
- `JQuantsProvider` / `EDINETProvider` / `JPXProvider` は bootstrap / extract 系コマンドでは SQLite miss 後に provider API へ進み、取得結果を SQLite に直接保存する。`screening run` では `cache_only` で構築され、run 中の追加取得を禁止する

### 11.1 SQLite Schema

SQLite は以下のテーブルを `data/screening/market.sqlite` に作成する。schema は `PRAGMA user_version` で現行版だけをサポートし、古い schema は migrate せず fail-fast する。

- `jquants_daily_bars(ticker, traded_at, open, high, low, close, volume, turnover_value, adjustment_*, upper_limit, lower_limit)` — 主キー `(ticker, traded_at)`、`traded_at` index 付。`is_common_stock=False` の record はスキップ
- `jquants_fin_summaries(ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, sales, operating_profit, ordinary_profit, profit, cfo, cash_eq, total_assets, equity, fiscal_period, fiscal_year_end, period_start, period_end)` — 主キー `(ticker, disclosed_at)`
- `jquants_master_snapshots(snapshot_date, ticker, name, market, sector_33, is_common_stock)` — 主キー `(snapshot_date, ticker)`
- `jquants_earnings_calendar(announcement_date, ticker)` — 主キー `(announcement_date, ticker)`
- `disclosures` raw JSON（SQLite 未収録）— 任意 cache。`Code` / `Date` / `Title` などの同義 key も reader 側で受け付ける。title keyword scan のみで金額や財務影響は解釈しない。読み取り coverage は `file_count` / `event_count` / `skipped_record_count` / `unsupported_record_count` / `load_errors` として candidates YAML の status / fallback に反映する
- `jquants_market_calendar(day, is_business_day)` — 主キー `(day)`。`HolidayDivision` "1" / "2" を business day=1、それ以外を 0 として記録
- `edinet_documents(doc_date, doc_id, sec_code, doc_type_code, csv_flag, xbrl_flag, legal_status, disclosure_status, withdrawal_status, submit_datetime, doc_description, period_start, period_end)` — 主キー `(doc_date, doc_id)`。`doc_date` はファイル名（`{date}.json`）から復元
- `edinet_metrics(asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, operating_profit_ttm, depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, net_cash, equity, total_assets, consolidation_basis, ttm_quality_*, source_doc_id, document_type, source_submit_datetime, source_period_start, source_period_end, capex_source, failure_reasons)` — 主キー `(asof_date, ticker)`。`asof_date` はファイル名から復元。Candidate YAML では EDINET raw `ocf_ttm` を `edinet_ocf_ttm` として出し、J-Quants 財務サマリー由来の `ocf_ttm` と区別する。`source_*` は research で一次資料へ戻るための traceability として保持する。`source_period_start/end` は EDINET documents metadata であり、半期報告書では実際の CF 測定期間と一致しないことがある
- `jpx_regulation_flags(asof_date, source_name, ticker, flag, fetched_at_utc)` — 主キー `(asof_date, source_name, ticker, flag)`。JPX cache の `flags_by_ticker` は source 別の起源を保持しないため、`source_name=flag` として記録
- `source_coverage(source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, status, error)` — provider/API request window の coverage 正本。`screening run` の事前検証はこの table と normalized rows を照合し、status 異常があれば fail-fast する。過去 raw JSON の hash / import audit は保持しない。

### 11.2 Volume と再生成の考え方

`data/screening/market.sqlite` は local store であり git 管理しない。容量増加は repo 履歴ではなくローカルディスクの問題として扱う。SQLite を作り直す場合は raw JSON からの migration ではなく、`bootstrap-cache --asof` と `extract-edinet-metrics --asof` で必要 window を provider から再取得して補完する。
