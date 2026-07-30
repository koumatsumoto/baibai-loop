---
title: "Screening runtime"
summary: "screening CLI、provider、SQLite schema、cache coverage、runtime設定の実装仕様。"
doc_type: reference
status: active
last_reviewed: 2026-07-23
---

# screening-runtime — CLI / provider / SQLite の実装仕様

`data/screening/runs.sqlite` のtransactionalなrun cache publicationを担う screening CLI の実装正本。週次 screening の入力と実行条件、依存、失敗時の扱いを定義する。

## 1. Scope

- 対象は screening runの自動生成と、`research` 選定を補助する `select` まで
- `research` 自動採用判定は対象外
- `kabuステーション API` と `JPX Market Explorer` は source of truth に使わない

## 2. Runtime

- Python 3.14（[`./python-foundation.md`](./python-foundation.md)）
- package root: `src/baibai_engine/screening/`
- J-Quants client は `jquantsapi.ClientV2` 固定
- 実行コマンド:

```bash
uv run baibai-engine screening run --asof YYYY-MM-DD
uv run baibai-engine screening run --asof YYYY-MM-DD --allow-stale-jpx
uv run baibai-engine screening bootstrap-cache --asof YYYY-MM-DD
uv run baibai-engine screening backfill-master --asof YYYY-MM-DD [--asof YYYY-MM-DD ...]
uv run baibai-engine screening backfill-master --month-end-from YYYY-MM-DD --month-end-to YYYY-MM-DD
uv run baibai-engine screening select --asof YYYY-MM-DD --run-revision-id ID [--macro-context-id ID] [--top N] [--profile PROFILE] [--detail summary|full] [--longlist-top N]
uv run baibai-engine screening shortlist publish DRAFT.yaml [--db PATH]
uv run baibai-engine screening shortlist outcome [--db PATH] [--runs-db PATH] [--horizon 3m] [--out PATH]
uv run baibai-engine screening ticker-profile --ticker XXXX [--asof YYYY-MM-DD]
uv run baibai-engine screening market-snapshot [--asof YYYY-MM-DD] [--weeks N]
uv run baibai-engine screening extract-edinet-metrics --asof YYYY-MM-DD [--lookback-days N]
uv run baibai-engine screening verify-cache-coverage --asof YYYY-MM-DD [--sqlite-path PATH] [--rules-path PATH]
uv run baibai-engine screening prune [--keep N] [--runs-db PATH]
```

`select` の `--run-revision-id` は必須で、`screening run` が返した immutable revision を指す。`--macro-context-id` は published macro context の ID（省略時は as-of 以前の latest eligible）で path ではない。`--longlist-top N` は diversity/cap 切断前の上位 N 件を longlist として出す。

`backfill-master` は指定日の断面 master snapshot だけを取得する。較正 cohort が production evidence になるには population がその日の master から来る必要がある一方、`bootstrap-cache` は同時に 1200 日の bar 窓と 730 日の summary 窓も取り直すため 1 日あたり数時間かかる。snapshot 自体は 1 request なので、月末グリッドを埋める経路をここに分ける。`--month-end-from/--month-end-to` は較正グリッドと同じ導出（bar store の月末営業日）を使い、cohort 日以外の日付を埋めて非 exact-date のまま残すことを防ぐ。1 日の取得失敗は残りの日付を止めず、失敗件数を stderr に出して非 0 で終わる。

`bootstrap-cache --asof` は `run --asof` が要求する source 別 input を自動で補完する。具体的には J-Quants master、asof まで 1200 日分の日次足、asof まで 730 日分の財務サマリー、asof の営業日カレンダ、JPX の決算発表予定 snapshot と規制 snapshot を SQLite に書き込む。決算発表予定は固定 90 日 range ではなく、JPX 公式 index に現在掲載されている全 cohort file の既知日程を合成する snapshot である。

J-Quants master は `get_eq_master(date=asof)` で requested as-of と同日の response だけを受理する。response 全行の `Date`、必須 field、normalized ticker の一意性、普通株 population を SQLite transaction 前に検証し、空・部分・別日 response は保存しない。snapshot は `(snapshot_date, ticker)` の日付別履歴として保持し、同日再取得だけを原子的に置換する。coverage は `get_eq_master:YYYY-MM-DD..YYYY-MM-DD`、`coverage_start == coverage_end == asof`、同日 persisted row count を正本とする。

`verify-cache-coverage` は SQLite が `run --asof` で必要な全入力をローカルに提供できるかを read-only で検証する。検証対象は J-Quants master、1200 日分の日次足、730 日分の財務サマリー、asof の営業日カレンダ、JPX の決算発表予定 snapshot と規制 snapshot、rules の `universe.required_jpx_flags` に含まれる source 名、EDINET metrics。決算発表予定は論理 source `jpx_earnings_calendar` が `ok`、保存行数と coverage 件数が一致して 1 件以上、実データの最大日が asof 以後、取得が asof から 7 平日以内であることを要求する。`--allow-stale-jpx` は取得時刻だけを緩和し、空・部分保存・全件過去は許可しない。master は requested as-of のexact rowとcanonical coverageだけを照合し、range、status、row count、common-stock populationの一致を要求する。newer/prior snapshotを代用せず、別日snapshotの破損もrequested dateの判定へ混ぜない。日次足は SQLite 実データの行そのものから completeness を判定し（DB が SSOT、後述 §11.1）、財務サマリーは source_coverage の窓で判定する。いずれも ticker/date 密度を追加で確認する。EDINET metrics は常に必須であり、raw JSON の読み込みや provider API 呼び出しは行わず、schema migration も行わない。不足があれば exit 1。

`extract-edinet-metrics` は EDINET documents list (`type=2`) から CSV 取得可能な有価証券報告書 / 四半期報告書 / 半期報告書を選び、EDINET document download (`type=5`) の CSV ZIP から screening 用 metrics を抽出して `data/screening/market.sqlite` に保存する。CSV ZIP 本体は再生成可能な cache として `.cache/screening/edinet/csv_zips/` に保存し、git には載せない。

`select` は明示した`run_revision_id`のpublication viewからresearch recommendationsを出力する。macro contextはapplication DBからas-of以前のlatest eligible revisionを読む任意のcontext-level warningで、ranking、candidate facts、採用、投入額を変えない。不在時は`macro_context_missing`、stale時は`macro_context_stale`、future contextはerrorである。正本は `recommendations` と `selection.diagnostics`。default は daily triage 用 summary で、詳細は `--detail full` で出す。ranking の主キーは機械 E[r]（成分分解付き年率見積り）の降順（E[r] 欠損は ranking 対象外・従キーに evidence pattern の優先順 + 割安強度）で、`durability`（塩漬け耐性）annotation を採用の gate へ接続する。`selection_playbook` は evidence がある候補だけに付く primary thesis annotation で、evidence がない候補は `selection_playbook: null` のまま recommendation に入り得る。閾値変更は `method/screening-rules/` を編集して新しいrun/select revisionを作る。`research` の選定プロセス ([`../workflow/research.md`](../workflow/research.md)) を支援する。

`shortlist outcome` は published shortlist ごとに、その entry 集合を母集団として selected / rejected / 機械 E[r] 上位同数の forward return を母集団中央値と突き合わせ、選定時の `ploss` 別に実現ドローダウンを集計する。E[r] は shortlist が束縛した run から読むので、その run が prune 済みなら機械 cohort は `unresolved_pruned_run` として計算しない。割当は無作為化されていないので出力は記述比較であり、payload の `comparison_basis` がそれを明示する。

`prune --keep N` は as-of、run timestamp、revision ID の新しい順に N 世代を残し、対象 run のcandidateとmachine selectionをtransaction内で削除してから`VACUUM`する。既定は3世代。run storeは再生成可能なcacheであり、canonicalなshortlist、research、proposal、holding reviewはapplication DBのsnapshotを読む。

金融4業種（銀行業、証券・商品先物取引業、保険業、その他金融業）の `excluded_sectors` は、事業会社向け generic evidence playbook の適用だけを止める。金融4業種も liquidity を通過して E[r] が非 null なら、通常どおり ranking、recommendation、longlist の対象になる。

`ticker-profile` は任意の上場銘柄(universe 内外を問わない)について、価格・流動性・対 benchmark / sector 相対・regime・イベント(次回決算日、JPX 規制 flag)・直近screening runのcandidate record・prior research を 1 つの事実 profile として出力する。`next_earnings_date` は JPX snapshot に公表済みの asof 以後の最短日であり、`null` は snapshot 内に既知日程がない（未定を含む）ことを示す。決算が存在しないという意味ではない。valuation はそのcandidate recordから引用し、再計算しない(記録と矛盾する値を作らないため)。`--asof` 省略時は cache の最新営業日を使う。provider 認証は不要で、market.sqlite、run store（`data/screening/runs.sqlite`）、application DB（prior research 用）を読む。

`listing_span_days` は J-Quants 銘柄 master に上場日が無いため、cache 内の最古 daily bar からの経過日数を proxy にする。bars cache の窓は asof−1200 暦日なので、上場が古い銘柄は ~1200 日で頭打ちになる（新規上場は実日数）。上場年数の実値ではなく「最低これだけの履歴がある」下限として読む。

`market-snapshot` は週次の regime 履歴(benchmark trend・breadth・regime label)と asof 時点の sector 集計(20/60 営業日リターン中央値・sector 内 breadth)を出力する。regime の閾値・窓は regime module と同一の正本を共有する。macro context 作成時の機械入力としても使う。

`select` の ranking は機械 E[r] を主キーとし、market regime による中立化は行わない（期間ではなく valuation と耐性で判断する）。`market-snapshot` の regime 履歴は macro context の機械入力として使う。`--sqlite-path` で cache 位置を上書きできる。

## 3. Required Env Vars

- `JQUANTS_API_KEY`

cache / SQLite の配置先は固定 (env override 廃止):

- disposable cache: `.cache/screening/`（gitignore。EDINET CSV ZIP や任意 disclosure title 入力など）
- SQLite 正本: `data/screening/market.sqlite`（gitignore。`run` 開始前に coverage を検証し、不足時は fail-fast）

任意 / 事前生成:

- `EDINET_API_KEY`: `extract-edinet-metrics` 実行時に必要。`run` は SQLite の EDINET metrics を必須入力として扱うため、標準運用では `run` 前に EDINET metrics を抽出しておく
- `SCREENING_RULES_PATH`: `select` / `run` が使う screening rules / selection profile YAML の既定 path override。CLI の明示 `--rules-path` を最優先し、次に env、最後に `method/screening-rules/` の既定を解決する
- JPX 公開規制情報 URL（CSV / Excel / HTML）。`method/screening-rules/2026-06-19T000000+0900.yaml` の `universe.required_jpx_flags` に含まれる source は必須で、未ロード時は fail-fast し screening runをpublishしない:
  - `JPX_SPECIAL_CAUTION_INDEX_URL` 特別注意銘柄の個別銘柄信用取引残高表 index（推奨。日次で変わる `mtdailyk*.xls` を index から解決）
  - `JPX_SPECIAL_CAUTION_URL` 特別注意銘柄の固定 Excel URL
  - `JPX_REORGANIZATION_URL` 整理銘柄
  - `JPX_TRADING_HALT_URL` 取引停止
  - `JPX_DELISTING_WARNING_URL` 上場廃止警告
- env vars の雛形は repo root の `.env.sample` を参照。secret（API キー）は repository に commit せず、`.env` の値を docs / issue に貼らない。CI では GitHub Actions secret として渡し、docs には変数名と用途だけを書く

## 4. J-Quants ClientV2 Methods

使う method は次の 4 点に固定する。決算発表予定は JPX 公式 source から取得する。

| method | 用途 |
| --- | --- |
| `get_eq_master` | requested as-of時点の上場銘柄一覧、普通株判定、市場区分、33業種。`date=YYYY-MM-DD`を必須としresponse `Date`の一致を検証する |
| `get_eq_bars_daily_range` | 日次 OHLCV、20 営業日平均売買代金、60 営業日騰落率、750 営業日自己レンジ |
| `get_fin_summary_range` | 財務サマリー、会社予想 EPS、利益系概要値 |
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
uv run baibai-engine screening extract-edinet-metrics --asof YYYY-MM-DD --lookback-days 540
uv run baibai-engine screening verify-cache-coverage --asof YYYY-MM-DD
uv run baibai-engine screening run --asof YYYY-MM-DD
```

`EDINET_API_KEY` が無い場合、`extract-edinet-metrics` は fail-fast する。`run` は SQLite の EDINET metrics が無い状態では継続せず、事前 coverage 検証で fail-fast する。

## 6. JPX Policy

- 利用対象は JPX 公開情報（CSV / Excel / HTML）
- HTML は `https://www.jpx.co.jp/` 配下の許可済み URL に限定し、source-specific parser で fail-fast に扱う
- 特別注意銘柄は `JPX_SPECIAL_CAUTION_INDEX_URL` が設定されていれば、JPX の「個別銘柄信用取引残高表」index から最新の `mtdailyk*.xls` link を解決してから Excel を取得する。index 未設定時は `JPX_SPECIAL_CAUTION_URL` の固定 URL を使う
- 規制情報の取得失敗は fail-fast
- `universe.required_jpx_flags` の source が欠ける場合は fail-fast する。JPX 規制 flag は screening runのcandidate recordに記録される必須事実であり(除外判断は `selection.liquidity` が担う)、warning-only では扱わない
- JPX 公開規制情報は latest snapshot しか取得できないため、SQLite には `fetched_at_utc` を記録する。通常の coverage 検証では `asof` と `fetched_at_utc` が 7 weekday 超乖離していれば fail-fast する（祝日は引かない近似）
- `screening run` は JPX を取得しない。historical backfill で latest snapshot を過去 `asof` に固定するリスクを許容する場合は、先に `bootstrap-cache --asof` で SQLite に保存し、`verify-cache-coverage --allow-stale-jpx` と `run --allow-stale-jpx` を明示する

## 7. Date Semantics

- `--asof` は対象営業日を表す
- `run_date` は `asof_date` と同値にする
- 機械出力はrun storeのprunable revision cache。`--output-path`はAI連携用のephemeral YAML view
- 同一 path が既に存在する場合は fail-fast
- 非営業日の `--asof` は fail-fast

## 8. Rule Baselines

閾値の正本は `method/screening-rules/2026-06-19T000000+0900.yaml`。実装側の hardcode は parser default と型定義に留め、運用で変える閾値は YAML に寄せる。

- scope / 絞り込み: `universe.required_jpx_flags`(記録対象の規制 flag)と `selection.liquidity`(時価総額・平均売買代金・上場期間・JPX 規制の分析層パラメータ)
- evidence pattern (`playbook_id`) の screen 閾値: `valuation-reversion` / `cashflow-yield-discount` / `sales-discount-growth`
- TTM 期間一致基準: partial period の許容日数差、FY 期間長
- 品質条件: 売上 YoY、営業利益、営業 CF 悪化、赤字縮小条件
- `EV/EBITDA` は `ttm_quality = exact` かつ EV / EBITDA がどちらも正のときのみ判定に使う。EDINET が無い場合、または EV / EBITDA がゼロ以下の場合は `unavailable` / `null` として他 metric で degrade する

## 9. Partial Warning Thresholds

- 有効な evidence pattern が必須とする TTM metric の `ttm_quality != exact` が universe の 5% 以上、または 20 銘柄以上
- EDINET metrics は `run` の必須 coverage。metrics 抽出済みでも個別 metric が `unavailable` になる場合だけ partial warning の対象にする
- 業績悪化フィルタ入力欠損が universe の 10% 以上 <!-- drift: allow-unrelated-policy-literal -->

## 10. Exit Codes

- `0`: 全件成功
- `1`: fail-fast（YAML 未生成）
- `2`: partial warning（YAML 生成済み、欠損明記）

## 11. Cache Layout

- `data/screening/market.sqlite` は screening input の local canonical store。J-Quants / EDINET / JPX の provider fetch は normalized table と `source_coverage` を直接更新する
- `.cache/screening/edinet/csv_zips/` は EDINET `type=5` CSV ZIP の cache。削除しても SQLite の metric rows は残る
- `.cache/screening/disclosures/**/*.json` は SQLite 正本の対象外に残す任意の disclosure title cache。TDnet / 会社 IR 等から取得した `ticker` / `date` / `title` / `source` / `url` 相当の record を置くと、screening run が EDINET metrics 提出日以降の M&A・借入などの title keyword hit をYAML viewの`freshness_warnings`に出す。cache が無い場合は `provider_status_lines` に optional unavailable を出す。cache が存在するが JSON 読み込み失敗・未対応 layout・必須 key 欠損がある場合は `provider_status_lines` と `fallback_lines` に件数を出す
- `method/` は screening rules・macro panel・playbook だけを置く。通常運用の raw JSON cache、SQLite、CSV ZIP、一時 manifest は置かない
- `screening run` は開始時に `verify-cache-coverage --asof` 相当の coverage 検証を行う。SQLite が不完全な場合は fail-fast し、raw JSON cache や provider API へフォールバックしない
- `JQuantsProvider` / `EDINETProvider` / `JPXProvider` は bootstrap / extract 系コマンドでは SQLite miss 後に provider API へ進み、取得結果を SQLite に直接保存する。`screening run` では `cache_only` で構築され、run 中の追加取得を禁止する

### 11.1 SQLite Schema

SQLite は以下のテーブルを `data/screening/market.sqlite` に作成する。schema は `PRAGMA user_version` で版管理し、forward-only migration で進化する。v13 を初期基準とし、`open_connection` は既存 store が `13 <= version < 最新` なら再取得なしで in-place に前進 migrate し、`13` 未満（前進経路で復元できない）や最新超は「削除して再取得」で fail-fast する。新規 store は最新 DDL で直接作成する。migration 完走後の shape は DDL と一致するため、shape 検証は列順まで含めて厳密に照合する。列順が変わる変更（列の並べ替え・削除）は `ALTER TABLE ADD COLUMN` が末尾追加で列順検証に落ちるため、table 再作成 migration（`market.sqlite.rebuild_table`）で書く。schema を変える実装者は `SQLITE_SCHEMA_VERSION` を bump し（`migrations.py` に次の連番 `Migration` を追加すると `LATEST_VERSION` が追随する）migration を書く。

- `jquants_daily_bars(ticker, traded_at, open, high, low, close, volume, turnover_value, adjustment_*, upper_limit, lower_limit)` — 主キー `(ticker, traded_at)`、`traded_at` index 付。`is_common_stock=False` の record はスキップ。**この table は coverage の SSOT であり、completeness は行データから導出する**（全営業日が全市場分の行を持つので欠損は present date 間のギャップとして観測でき、`source_coverage` の bookkeeping に穴があっても行が揃っていれば re-fetch しない）。`source_coverage` は status / record_count の整合チェックにのみ併用する
- `jquants_fin_summaries(ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, sales, operating_profit, ordinary_profit, profit, forecast_profit, forecast_ordinary_profit, cfo, cash_eq, total_assets, equity, fiscal_period, fiscal_year_end, period_start, period_end, dps_actual_annual, dps_forecast_annual)` — 主キー `(ticker, disclosed_at)`。DPS は `DivAnn`（実績年間・FY 開示）と `FDivAnn`→`NxFDivAnn`（進行期の予想年間）から取る。`forecast_profit` / `forecast_ordinary_profit` は会社予想の当期純利益・経常利益で、`forecast_eps` と同一予想期のペア（当期予想 `FNP`/`FOdP`、本決算開示で FEPS 空なら翌期予想 `NxFNp`/`NxFOdP`）から取り、純利益>経常の一時益 data-quality flag（`forecast_special_gain`）の一次入力にする
- `jquants_master_snapshots(snapshot_date, ticker, name, market, sector_33, is_common_stock)` — 主キー `(snapshot_date, ticker)`。異なるrequested as-ofをappend-onlyに保持し、同日再取得だけを置換する。`screening run`はrequested as-ofと同日のsnapshotだけを使う。current-state補助出力の`ticker-profile` / `market-snapshot`とgeneric latest readerはDB全体の`MAX(snapshot_date)`に属するrowだけを読み、過去snapshotからtickerを補完しない。calibrationの`read_eq_master_asof`だけはcohort日以下の直前snapshotを`prior_snapshot`として返せる。cohort日以下にsnapshotが無い場合（snapshot収集開始前の歴史cohort）は最古snapshotへfallbackし`future_snapshot`とlabelする — authority契約が`exact_date`以外をproduction evidenceから除外するため、この近似はdiagnostic計測にのみ効く
- `jquants_earnings_calendar(announcement_date, ticker)` — 主キー `(announcement_date, ticker)`。schema version を変えずに既存 SQLite を読めるよう物理名だけを維持する互換 table で、論理 source と writer/reader の権威は JPX (`jpx_earnings_calendar`) にある
- `disclosures` raw JSON（SQLite 未収録）— 任意 cache。`Code` / `Date` / `Title` などの同義 key も reader 側で受け付ける。title keyword scan のみで金額や財務影響は解釈しない。読み取り coverage は `file_count` / `event_count` / `skipped_record_count` / `unsupported_record_count` / `load_errors` としてscreening runのYAML viewにあるstatus / fallbackへ反映する
- `jquants_market_calendar(day, is_business_day)` — 主キー `(day)`。`HolidayDivision` "1" / "2" を business day=1、それ以外を 0 として記録
- `edinet_documents(doc_date, doc_id, sec_code, doc_type_code, csv_flag, xbrl_flag, legal_status, disclosure_status, withdrawal_status, submit_datetime, doc_description, period_start, period_end)` — 主キー `(doc_date, doc_id)`。`doc_date` はファイル名（`{date}.json`）から復元
- `edinet_metrics(asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, operating_profit_ttm, depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, net_cash, equity, total_assets, consolidation_basis, ttm_quality_*, source_doc_id, document_type, source_submit_datetime, source_period_start, source_period_end, capex_source, failure_reasons)` — 主キー `(asof_date, ticker)`。`asof_date` はファイル名から復元。Candidate YAML では EDINET raw `ocf_ttm` を `edinet_ocf_ttm` として出し、J-Quants 財務サマリー由来の `ocf_ttm` と区別する。`source_*` は research で一次資料へ戻るための traceability として保持する。`source_period_start/end` は EDINET documents metadata であり、半期報告書では実際の CF 測定期間と一致しないことがある
- `jpx_regulation_flags(asof_date, source_name, ticker, flag, fetched_at_utc)` — 主キー `(asof_date, source_name, ticker, flag)`。JPX cache の `flags_by_ticker` は source 別の起源を保持しないため、`source_name=flag` として記録
- `source_coverage(source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, status, error)` — fetch-provenance source（master / 財務サマリー / 営業日カレンダ / 決算予定 / JPX 規制 / EDINET metrics）の coverage 正本。masterはrequested dateごとの単日keyと同日row countを持つ。その他は「未取得」と「対象なし」を行の有無から区別できないため source_coverage を gate にする。range fetch（日次足 / 財務サマリー / 営業日カレンダ）の coverage 記録は、新規取得 window を既存の overlapping / adjacent な `ok` window と union に merge して 1 本の連続 window にする。これは chunk 境界が asof（`asof - N 日`）ごとにずれるため、一部だけ重なる re-fetch が既存 window を delete-and-shrink して残りを孤立させ、行は揃っているのに `_range_covered` がギャップと判定する事故を防ぐためである。日次足だけは coverage gate を source_coverage ではなく行データから導出する（上記）。過去 raw JSON の hash / import audit は保持しない。

### 11.2 Volume と再生成の考え方

`data/screening/market.sqlite` は local store であり git 管理しない。容量増加は repo 履歴ではなくローカルディスクの問題として扱う。schema 変更は forward-only migration で in-place に進めるため、schema bump のたびに全再取得する必要はない。既存 row を migration で backfill できない変更（新 field を provider から埋め直す等）は、`screening invalidate-coverage --source <name> [--start --end]` で該当 `source_coverage` を削除して bootstrap 対象に戻し、`bootstrap-cache --asof` で該当 window を再取得する。store を丸ごと作り直す場合も raw JSON からの migration ではなく `bootstrap-cache --asof` と `extract-edinet-metrics --asof` で provider から補完する。`invalidate-coverage` は削除対象行数を表示してから削除する（cache は再生成可能なため確認プロンプトは無い）。既知でない source 名は既知一覧を示して拒否する。

## 12. J-Quants rate limit と bootstrap コスト

J-Quants Light プランの正確なレート制限は非公開で、挙動は実運用の観測から推測する（確定仕様ではない）。コード側の対処は `src/baibai_engine/screening/providers/jquants.py` の `_RATE_LIMIT_BACKOFF_SECONDS`（最大 600s の 429 backoff）と `_RANGE_CHUNK_DAYS`（range fetch を 31 日 chunk に分割）で扱う。

- `bootstrap-cache --asof <past>` の律速は **per-asof の長期履歴 re-fetch のボリューム** であり、「数分で回復する rate window」でも「日次クォータの枯渇」でもない。1 asof の日次足は asof−1200 暦日、財務サマリーは asof−730 暦日を範囲に取り、`_RANGE_CHUNK_DAYS=31` で 31 日 chunk に分割して ClientV2 内部の per-day API 呼び出しに fan-out する。throttling 下では 31 日 chunk あたり数分規模のスループットになり、1 asof の完全 bootstrap は数時間規模になる。429 backoff はこの volume に上乗せされる。
- chunk は resumable。`source_coverage` に chunk 単位で `status=ok` を記録し、中断しても完了済み chunk は再取得しない。複数 asof は履歴窓が大きく重複するため、最初の 1 asof の full bootstrap が高コストで、以降の週は非重複 chunk とその週の EDINET だけで安価になる。
- 既存 cache がある asof では長期履歴を再取得しない。日次足の coverage は行データから導出し（§11.1、DB が SSOT）、range fetch の coverage は overlapping / adjacent window と union merge する（§11.1）。chunk 境界が asof ごとにずれても、行が揃っていれば偽のギャップを作らず re-fetch しない。
- `run` は cache-only で、coverage が揃えば provider を叩かず高速。歴史 replay の律速は `run` ではなく `bootstrap-cache` / `extract-edinet-metrics` の coverage 充足にある。

過去 asof の cache 充足は「rate budget の回復を待つ」問題ではなく、**長期履歴 coverage を一度埋め切る wall-clock** の問題として扱う。1 asof ずつ長時間バックグラウンドで流し、resumable な性質を活かして複数セッションに跨いで充足させる。短い per-step timeout で kill するとその asof の coverage が未充足のまま `run` が fail-fast するため、kill せず完走させるか完了済み chunk から再開する。
