---
title: "Screening runtime"
summary: "screening CLI、provider、SQLite schema、cache coverage、runtime設定の実装仕様。"
doc_type: reference
status: active
---

# screening-runtime — CLI / provider / SQLite の実装仕様

`stores/screening/runs.sqlite` のtransactionalなrun cache publicationを担う screening CLI の実装正本。screening の入力と実行条件、依存、失敗時の扱いを定義する。

## 1. Scope

- 対象は screening runの自動生成と、`research` 選定を補助する `select` まで
- `research` 自動採用判定は対象外
- `kabuステーション API` と `JPX Market Explorer` は source of truth に使わない

## 2. Runtime

- Python 3.14（[`./python-foundation.md`](./python-foundation.md)）
- package root: `engine/src/baibai_engine/screening/`
- J-Quants client は `jquantsapi.ClientV2` 固定
- 実行コマンド:

```bash
uv run baibai-engine screening run --asof YYYY-MM-DD
uv run baibai-engine screening run --asof YYYY-MM-DD --allow-stale-jpx
uv run baibai-engine screening bootstrap-cache --asof YYYY-MM-DD
uv run baibai-engine screening backfill-history --start YYYY-MM-DD --end YYYY-MM-DD
uv run baibai-engine screening backfill-master --asof YYYY-MM-DD [--asof YYYY-MM-DD ...]
uv run baibai-engine screening backfill-master --month-end-from YYYY-MM-DD --month-end-to YYYY-MM-DD
uv run baibai-engine screening select --asof YYYY-MM-DD --run-revision-id ID [--macro-context-id ID] [--top N] [--profile PROFILE] [--detail summary|full] [--longlist-top N]
uv run baibai-engine screening selection show --selection-id ID [--runs-db PATH] [--output-path PATH] [--force]
uv run baibai-engine screening shortlist publish DRAFT.yaml [--db PATH]
uv run baibai-engine screening shortlist outcome [--db PATH] [--runs-db PATH] [--horizon 3m] [--out PATH]
uv run baibai-engine screening ticker-profile --ticker XXXX [--asof YYYY-MM-DD]
uv run baibai-engine screening market-snapshot [--asof YYYY-MM-DD] [--weeks N]
uv run baibai-engine screening extract-edinet-metrics --asof YYYY-MM-DD [--lookback-days N]
uv run baibai-engine screening refresh-buyback-reports --asof YYYY-MM-DD [--lookback-days N] [--sqlite-path PATH]
uv run baibai-engine screening refresh-capital-control --asof YYYY-MM-DD [--sqlite-path PATH]
uv run baibai-engine screening backfill-edinet-identity --start YYYY-MM-DD --end YYYY-MM-DD
uv run baibai-engine screening build-control-event-exits --asof YYYY-MM-DD [--sqlite-path PATH]
uv run baibai-engine screening verify-cache-coverage --asof YYYY-MM-DD [--sqlite-path PATH] [--rules-path PATH]
uv run baibai-engine screening prune [--keep N] [--runs-db PATH]
```

`select` の `--run-revision-id` は必須で、`screening run` が返した immutable revision を指す。`--macro-context-id` は published macro context の ID（省略時は as-of 以前の latest eligible）で path ではない。`--longlist-top N` は diversity/cap 切断前の上位 N 件を longlist として出す。

`backfill-history` は日次足・財務サマリー・営業日カレンダ・週次信用残高・報告空売り残高を、明示した窓に対して 1 回で取得する。`bootstrap-cache` は窓を as-of から導き、通常指標用の日次足 1200 日・財務サマリー 730 日に加えて、`normalized_per_3fy` の分割基準と 3 FY を確定する両 source の 2200 日 coverage を要求する。窓を 1 度名指しすれば 1 パスで済み、coverage が既存の窓と繋がる。source ごとに独立に取得して行ごとに結果を出し、1 つの失敗が残りを止めない。日次足・財務サマリー・空売り残高報告は暦年で区切って要求する。窓全体を 1 度に読むと 10 年分の行をメモリに載せることになるうえ、区切りを `--start` でなく暦に置けば、開始日の違う実行どうしが同じ chunk を再利用できる。日次足・財務サマリーとも被覆済みの chunk は skip するので、中断した実行は chunk 単位で再開する。被覆の判定材料は違い、日次足は保存行そのもの（DB が SSOT、後述 §11.1）、財務サマリーと空売り残高報告は `source_coverage` を読む。空売り残高報告は訂正を取り込むため直近7日だけ被覆済みでも再取得する。営業日カレンダは provider 呼び出し 1 回なので分割せず、再開の単位にもならない。日次足を先に取るのは、`backfill-master` の月末グリッドが bar store から導出されるためで、bars の無い月の snapshot はまだ要求できない。

`backfill-master` は指定日の断面 master snapshot だけを取得する。較正 cohort が production evidence になるには population がその日の master から来る必要がある一方、`bootstrap-cache` は同時に最長 2200 日の bar / summary coverage も補完するため 1 日あたり数時間かかる。snapshot 自体は 1 request なので、月末グリッドを埋める経路をここに分ける。`--month-end-from/--month-end-to` は較正グリッドと同じ導出（bar store の月末営業日）を使い、cohort 日以外の日付を埋めて非 exact-date のまま残すことを防ぐ。1 日の取得失敗は残りの日付を止めず、失敗件数を stderr に出して非 0 で終わる。

`cloud-history-backfill` は pull 直後と backfill 終了後の `market.sqlite` SHA-256 を比較する。source failure があっても commit 済み chunk が増えた場合は `PRAGMA quick_check` 後に `push-market` で R2へ保存し、その後に元の非0を返す。storeが変わらないfailureはGB級objectを再uploadしない。再dispatchはR2へ保存済みのcoverage/rowsをpullするため、既存chunkを再取得しない。

`bootstrap-cache --asof` は `run --asof` が要求する source 別 input を自動で補完する。具体的には J-Quants master、通常指標用の日次足 1200 日・財務サマリー 730 日、`normalized_per_3fy` 用の両 source 2200 日 coverage、asof の営業日カレンダ、JPX の決算発表予定 snapshot と規制 snapshot を SQLite に書き込む。財務サマリーは日付 coverage に加え、exact as-of の普通株母集団に対する `shares_outstanding`、`treasury_shares`、`equity_to_asset_ratio` の 730 日窓内 population を検査する。market-cap / valuationの組合せは各fieldを開示日後の`adjustment_factor`でas-of株式基準へ揃え、自己株控除後株式数が正になるtickerだけを数える。各 field と market-cap 用2 field、valuation 用3 fieldの組合せが75%未満なら、欠損tickerが既に持つ開示日だけをrepair rangeとして再取得する。75% floorは銘柄固有の開示欠損を全履歴取得へ拡大せず、母集団規模の未投入を停止させる境界である。各 chunk は即時保存され、開始前に `resume_from` / `remaining_ranges`、保存完了ごとに `chunk i/n`、取得後に同じfield件数を表示するため、中断後は完了済み開示日を再計画から外せる。summary row自体が母集団の75%未満なら欠損tickerの開示日をSQLiteから特定できないため、730日窓を再取得する。

決算発表予定は固定 90 日 range ではなく、JPX 公式 index に現在掲載されている全 cohort file の既知日程を合成する snapshot である。発表日は上場会社の都合で動くので cohort file 間の食い違いは正常であり、より current な view を持つ file の日付を採る。asof 以後の日付を 1 件も持たない file は forward な予定を持たないので、rolling file であっても最下位へ落とす（更新の止まった rolling file の過去日が、まだ予定として生きている日付を潰さない）。残りは「毎営業日更新の rolling file（`kessan.xlsx`）> 掲載日の新しい dated cohort file（`YYYY年M月D日現在` stamp）> 掲載日を読めない file」の順で、rolling file は stamp の有無でなく file 名で同定する。stamp の書式が変わっても古い cohort が current を騙れない。順位を付けられない file 同士は URL で決め、index の掲載順に依存しない。rolling file が index から消えた場合・複数ある場合と、dated cohort file の stamp を読めなかった場合は警告に出す。採らなかった日付は件数を snapshot の `superseded` として数え、勝った側と負けた側の source を警告に出し、`bootstrap-cache jpx snapshots` 行の `earnings_calendar_superseded` に載せる。SQLite の cache から読み戻した snapshot は merge をしていないので、この key は fetch した run にだけ出る（合成ゼロを印字しない）。同一 file 内で 1 銘柄が別日を持つ場合だけは source 破損として exit 1。`superseded` は正常事象なので coverage の `rejected`（壊れ行 = `partial`）とは別物として扱う。

J-Quants master は `get_eq_master(date=asof)` で requested as-of と同日の response だけを受理する。response 全行の `Date`、必須 field、normalized ticker の一意性、普通株 population を SQLite transaction 前に検証し、空・部分・別日 response は保存しない。snapshot は `(snapshot_date, ticker)` の日付別履歴として保持し、同日再取得だけを原子的に置換する。coverage は `get_eq_master:YYYY-MM-DD..YYYY-MM-DD`、`coverage_start == coverage_end == asof`、同日 persisted row count を正本とする。

`verify-cache-coverage` は SQLite が `run --asof` で必要な全入力をローカルに提供できるかを read-only で検証する。検証対象は J-Quants master、通常指標用の日次足 1200 日・財務サマリー 730 日、`normalized_per_3fy` 用の両 source 2200 日 coverage、asof の営業日カレンダ、JPX の決算発表予定 snapshot と規制 snapshot、rules の `universe.required_jpx_flags` に含まれる source 名、EDINET metrics。財務サマリーは日付 coverage と独立にrequired-field populationを検証し、各field、`market_cap_required_fields`、`valuation_required_fields` の ticker件数を常に出力する。日付rangeがcompleteでもrequired-field floor未満なら `required-field:<name>@<asof>` を具体的な不足件数とともに返し、候補を全件nullのまま組み立てる前にexit 1とする。決算発表予定は論理 source `jpx_earnings_calendar` が `ok`、保存行数と coverage 件数が一致して 1 件以上、実データの最大日が asof 以後、取得が asof から 7 平日以内であることを要求する。`--allow-stale-jpx` は取得時刻だけを緩和し、空・部分保存・全件過去は許可しない。master は requested as-of のexact rowとcanonical coverageだけを照合し、range、status、row count、common-stock populationの一致を要求する。newer/prior snapshotを代用せず、別日snapshotの破損もrequested dateの判定へ混ぜない。日次足は SQLite 実データの行そのものから completeness を判定し（DB が SSOT、後述 §11.1）、財務サマリーは source_coverage の窓で判定する。`normalized_per_3fy` の追加窓も同じ authority で検証し、不足時に `null` や短い履歴へ黙って縮退しない。いずれも ticker/date 密度を追加で確認する。EDINET metrics は常に必須であり、raw JSON の読み込みや provider API 呼び出しは行わず、schema migration も行わない。不足があれば exit 1。

`extract-edinet-metrics` は EDINET documents list (`type=2`) から CSV 取得可能な有価証券報告書 / 四半期報告書 / 半期報告書を選び、EDINET document download (`type=5`) の CSV ZIP から screening 用 metrics を抽出して `stores/market/market.sqlite` に保存する。対象日以前の直近正常 snapshot と `(ticker, source_doc_id, document_type, source_submit_datetime, source_period_start, source_period_end, source_document_revision, extractor_revision)` が一致する row は解析済み metric を再利用し、新規・変更候補だけをdownloadする。`source_document_revision` は訂正・取下げ・開示状態を含むcanonical document eventのhashである。`extractor_revision` は抽出 entry point (`screening/cli/edinet_extract.py`) の import closure をfile単位で辿って自動導出する。導出なので、抽出経路が依存を得たり失ったりすると manifest がそれに追随し、依存の追加漏れでstale rowが生き残ることがない。entry point がこの1 commandだけを持つ moduleに居るのは、closureの広さがそのまま再構築の頻度になるためである — `bootstrap-cache` / `verify-cache-coverage` / `backfill-history` と同居していた頃は、それらが引く J-Quants・JPX・coverage の変更でも全件再取得が起きていた。manifestの実体は `tests/engine/test_edinet_revision.py` が両方向に固定する（値を決めうるmoduleが入っていること、決めえないmoduleが入っていないこと）。CSV ZIP 本体は再生成可能な cache として `.cache/screening/edinet/csv_zips/` に保存し、git には載せない。

`refresh-buyback-reports` は同じ document list から自己株券買付状況報告書（様式 220、訂正は 230）を選び、決議した株式数と価額・累計取得・当月取得・取得期間・報告月末の発行済株式総数と保有自己株式数を `edinet_buyback_reports` へ 1 銘柄 1 報告月で保存する。E[r] の carry は **trailing の株数変化**なので、取得を始めたばかりの会社はそこに現れず、終えた会社は現れ続ける。この表はその前を向いた側を持つ。**E[r]・ranking・gate は変えない**。

数値は typed な XBRL fact ではなく TextBlock 内に区切り無しで連結されているため、label 起点で読む。comma 区切りの整数は 3 桁 group が境界を与えるので連結されても一意に分解できるが、様式が併記する百分率は区切りも小数桁の固定も無いので読まず、進捗は整数から導出する。読めなかった項目は欠損のまま置く — 残枠が読めないのに 0 を返すと「枠を使い切った」と主張することになる。累計 > 決議、自己株 > 発行済、報告月末 > 提出日のような矛盾は label が別の行に噛んだ証拠なので、その読みを丸ごと捨てる（報告月末は主キーの半分なので、その場合は行ごと保存しない）。実測（1,215 銘柄 6,067 行）で残枠が計算できたのは 80.0%、取得期間の終了日 91.6%、保有状況 98.3% である。取れない分は「取締役会決議による取得」節を持たない提出で、枠そのものが無い。

1 件の取得失敗はその提出だけを飛ばし、rate limit は pass を終えて成果を残す（保存済みの提出は二度と取りに行かないので、次の run が続きから進む）。annotation であり判断入力ではないため、部分的な pass で日次 batch を落とさない。

EDINET の当日分 document list は日中に更新されるため、`bootstrap-cache` は対象日を
毎回取得し、未確定のまま保存した日付を次回実行時に再取得してから確定済みにする。
raw row は file date と `seqNumber` で保持し、書類情報修正、取下げ、不開示開始・解除を
operation event として origin filing へ適用してから候補を選ぶ。`legalStatus="2"` は
延長閲覧期間中であり利用可能として扱う。

operation event の origin filing が取得窓内に無い場合は、その event を docID・種別つきで
quarantine し、件数を extraction summary と daily batch metrics へ出す。screening 対象の
書類種別かつ secCode を特定できる event は該当 ticker の当日 EDINET metrics を null にして
fail-closed とするが、他 ticker の抽出と daily publish は継続する。event 以外の status 不正、
書類関係の循環、API / rate-limit、CSV hard failure は batch 全体を止める。daily batch は
同日再実行でも差分抽出を行い、mutable な document state と quarantine counter を同期する。

EDINET は過去日の origin row 自体を後日上書きするため、document cache は取得時点の
current source state であり point-in-time ledger ではない。既存の
`edinet_metrics(asof_date, ticker)` snapshot が過去 as-of の正本である。snapshot が
ない過去 as-of の再抽出は実行時点の EDINET current source state を使い、観測前の
修正前・取下げ前状態を再現するものではない。

`select` は明示した`run_revision_id`のpublication viewからresearch recommendationsを出力する。macro contextはapplication DBからas-of以前のlatest eligible revisionを読む任意のcontext-level warningで、ranking、candidate facts、採用、投入額を変えない。不在時は`macro_context_missing`、stale時は`macro_context_stale`、future contextはerrorである。正本は `recommendations` と `selection.diagnostics`。default は daily triage 用 summary で、詳細は `--detail full` で出す。ranking の主キーは機械 E[r]（成分分解付き年率見積り）の降順（E[r] 欠損は ranking 対象外・従キーに evidence pattern の優先順 + 割安強度）で、`durability`（塩漬け耐性）annotation を採用の gate へ接続する。`selection_playbook` は evidence がある候補だけに付く primary thesis annotation で、evidence がない候補は `selection_playbook: null` のまま recommendation に入り得る。閾値変更は `method/screening/rules/` を編集して新しいrun/select revisionを作る。`research` の選定プロセス（skill `shortlist` / `research`）を支援する。

`shortlist outcome` は published shortlist ごとに、その entry 集合を母集団として selected / rejected / 機械 E[r] 上位同数の forward return を母集団中央値と突き合わせ、選定時の `ploss` 別に実現ドローダウンを集計する。E[r] は shortlist が束縛した run から読むので、その run が prune 済みなら機械 cohort は `estimate_missing` として計算しない。割当は無作為化されていないので出力は記述比較であり、payload の `comparison_basis` がそれを明示する。

`screening shortlist preflight` は shortlist cycle が run を作る前の read-only gate である。cloud workflow summary が指す run / selection、対象 as-of、run と selection の `application_git_commit`、checked-out HEAD、worktree clean、local run store の束縛を同時に照合し、`reuse` / `resume-current-code` / `rerun-current-code` / `blocked` を 1 つだけ返す。`resume-current-code` は同一 as-of・同一 HEAD の run だけがあり selection が未作成の状態を示すため、その run へ select だけを 1 回行う。実行しても run / selection は増えない。greatest prior as-of は全 run と application DB の canonical shortlist の和集合から決める。複数 run は `--previous-run-revision-id` で preflight 自体を解決し、select へ `previous.selection_arguments` を渡す。canonical run が retention で失われた場合は同日別 revision へ代替せず、`--previous-shortlist-id` で canonical shortlist の焼き込み entries を使う。

`prune --keep N` は as-of、run timestamp、revision ID の新しい順に N 世代を残し、対象 run のcandidateとmachine selectionをtransaction内で削除してから`VACUUM`する。既定は3世代。run storeは再生成可能なcacheであり、canonicalなshortlist、research、proposal、holding reviewはapplication DBのsnapshotを読む。

金融4業種（銀行業、証券・商品先物取引業、保険業、その他金融業）の `excluded_sectors` は、事業会社向け generic evidence playbook の適用だけを止める。金融4業種も liquidity を通過して E[r] が非 null なら、通常どおり ranking、recommendation、longlist の対象になる。

`ticker-profile` は任意の上場銘柄(universe 内外を問わない)について、価格・流動性・対 benchmark / sector 相対・regime・イベント(次回決算日、JPX 規制 flag)・直近screening runのcandidate record・prior research を 1 つの事実 profile として出力する。`next_earnings_date` は JPX snapshot に公表済みの asof 以後の最短日であり、`null` は snapshot 内に既知日程がない（未定を含む）ことを示す。決算が存在しないという意味ではない。valuation はそのcandidate recordから引用し、再計算しない(記録と矛盾する値を作らないため)。`--asof` 省略時は cache の最新営業日を使う。provider 認証は不要で、market.sqlite、run store（`stores/screening/runs.sqlite`）、application DB（prior research 用）を読む。

`listing_span_days` は J-Quants 銘柄 master に上場日が無いため、通常 run が指標計算へ渡す最古 daily bar からの経過日数を proxy にする。この入力窓は asof−1200 暦日なので、物理 cache がより長い履歴を持っていても、上場が古い銘柄は ~1200 日で頭打ちになる（新規上場は実日数）。上場年数の実値ではなく「最低これだけの履歴がある」下限として読む。

`market-snapshot` は日次運用の任意の asof で、7日間隔の regime 履歴(benchmark trend・breadth・regime label)と asof 時点の sector 集計(20/60 営業日リターン中央値・sector 内 breadth)を出力する。regime の閾値・窓は regime module と同一の正本を共有する。macro context 作成時の機械入力としても使う。

`select` の ranking は機械 E[r] を主キーとし、market regime による中立化は行わない（期間ではなく valuation と耐性で判断する）。`market-snapshot` の regime 履歴は macro context の機械入力として使う。`--sqlite-path` で cache 位置を上書きできる。

## 3. Required Env Vars

- `JQUANTS_API_KEY`

cache / SQLite の配置先は固定 (env override 廃止):

- disposable cache: `.cache/screening/`（gitignore。EDINET CSV ZIP や任意 disclosure title 入力など）
- SQLite 正本: `stores/market/market.sqlite`（gitignore。`run` 開始前に coverage を検証し、不足時は fail-fast）

任意 / 事前生成:

- `EDINET_API_KEY`: `extract-edinet-metrics` 実行時に必要。`run` は SQLite の EDINET metrics を必須入力として扱うため、標準運用では `run` 前に EDINET metrics を抽出しておく
- `SCREENING_RULES_PATH`: `select` / `run` が使う screening rules / selection profile YAML の既定 path override。CLI の明示 `--rules-path` を最優先し、次に env、最後に `method/screening/rules/` の既定を解決する
- JPX 公開規制情報 URL（CSV / Excel / HTML）。現行 rules の `universe.required_jpx_flags` に含まれる source は必須で、未ロード時は fail-fast し screening runをpublishしない:
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
| `get_eq_master` | requested as-of時点の上場銘柄一覧、市場区分、33業種。`date=YYYY-MM-DD`を必須としresponse `Date`の一致を検証する。証券種別のfieldは返らないので、普通株かどうかはこのresponseからは分からない |
| `get_eq_bars_daily_range` | 日次 OHLCV、20 営業日平均売買代金、60 営業日騰落率、750 営業日自己レンジ、3FY normalized PER の分割基準 |
| `get_fin_summary_range` | 財務サマリー、会社予想 EPS、利益系概要値、3FY normalized PER の FY EPS |
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

閾値の正本は `method/screening/rules/` の現行 revision で、その実 path は `rule_config.DEFAULT_RULES_PATH` が持つ（`--rules-path` / `SCREENING_RULES_PATH` で override した場合はそちら）。rules は dated revision で増えるので、file 名の実値をここへ書かない。実装側の hardcode は parser default と型定義に留め、運用で変える閾値は YAML に寄せる。

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

- `stores/market/market.sqlite` は screening input の local canonical store。J-Quants / EDINET / JPX の provider fetch は normalized table と `source_coverage` を直接更新する
- `.cache/screening/edinet/csv_zips/` は EDINET `type=5` CSV ZIP の cache。削除しても SQLite の metric rows は残る
- `.cache/screening/disclosures/**/*.json` は SQLite 正本の対象外に残す任意の disclosure title cache。TDnet / 会社 IR 等から取得した `ticker` / `date` / `title` / `source` / `url` 相当の record を置くと、screening run が EDINET metrics 提出日以降の M&A・借入などの title keyword hit をYAML viewの`freshness_warnings`に出す。cache が無い場合は `provider_status_lines` に optional unavailable を出す。cache が存在するが JSON 読み込み失敗・未対応 layout・必須 key 欠損がある場合は `provider_status_lines` と `fallback_lines` に件数を出す
- `method/` は screening rules・macro panel・playbook だけを置く。通常運用の raw JSON cache、SQLite、CSV ZIP、一時 manifest は置かない
- `screening run` は開始時に `verify-cache-coverage --asof` 相当の coverage 検証を行う。SQLite が不完全な場合は fail-fast し、raw JSON cache や provider API へフォールバックしない。通常指標は 1200 日の日次足と 730 日の財務行を読み、`normalized_per_3fy` は 2200 日窓から FY 行と分割・併合 event だけを疎に読むため、全日次足を追加でメモリへ載せない
- `JQuantsProvider` / `EDINETProvider` / `JPXProvider` は bootstrap / extract 系コマンドでは SQLite miss 後に provider API へ進み、取得結果を SQLite に直接保存する。`screening run` では `cache_only` で構築され、run 中の追加取得を禁止する

### 11.1 SQLite Schema

SQLite は以下のテーブルを `stores/market/market.sqlite` に作成する。schema は `PRAGMA user_version` で版管理し、forward-only migration で進化する。v13 を初期基準とし、`open_connection` は既存 store が `13 <= version < 最新` なら再取得なしで in-place に前進 migrate し、`13` 未満（前進経路で復元できない）や最新超は「削除して再取得」で fail-fast する。新規 store は最新 DDL で直接作成する。migration 完走後の shape は DDL と一致するため、shape 検証は列順まで含めて厳密に照合する。列順が変わる変更（列の並べ替え・削除）は `ALTER TABLE ADD COLUMN` が末尾追加で列順検証に落ちるため、table 再作成 migration（`market.sqlite.rebuild_table`）で書く。schema を変える実装者は `SQLITE_SCHEMA_VERSION` を bump し（`migrations.py` に次の連番 `Migration` を追加すると `LATEST_VERSION` が追随する）migration を書く。

- `jquants_daily_bars(ticker, traded_at, open, high, low, close, volume, turnover_value, adjustment_*, upper_limit, lower_limit)` — 主キー `(ticker, traded_at)`、`traded_at` index 付。取引所が値付けする銘柄をそのまま持ち、証券種別では絞らない。benchmark に使う ETF (`1306`) の系列がここに要るためで、instrument type の判別は universe 構築で行う。**この table は coverage の SSOT であり、completeness は行データから導出する**（全営業日が全市場分の行を持つので欠損は present date 間のギャップとして観測でき、`source_coverage` の bookkeeping に穴があっても行が揃っていれば re-fetch しない）。`source_coverage` は status / record_count の整合チェックにのみ併用する
- `jquants_fin_summaries(ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, sales, operating_profit, ordinary_profit, profit, forecast_profit, forecast_ordinary_profit, cfo, cash_eq, total_assets, equity, fiscal_period, fiscal_year_end, period_start, period_end, dps_actual_annual, dps_forecast_annual, treasury_shares, equity_to_asset_ratio, dividend_q1, dividend_interim, dividend_q3, dividend_year_end, dividend_total_annual, average_shares)` — 主キー `(ticker, disclosed_at)`。DPS は `DivAnn`（実績年間・FY 開示）と `FDivAnn`→`NxFDivAnn`（進行期の予想年間）から取る。支払ごとの `Div1Q`/`Div2Q`/`Div3Q`/`DivFY` と配当総額 `DivTotalAnn`（円）、期中平均株式数 `AvgSh` も持つ。年間 DPS は各支払の基準日時点の株式基準で記載されるので、会計期間が分割・併合を跨いだ年度は支払ごとに換算し直し、総額を自己株控除後株式数で割った値で照合する（[valuation-metrics.md §7.2](./valuation-metrics.md#72-配当dpsdividend_yield)）。`AvgSh` は提出者が EPS を出すのに使った株数で、その株数が壊れていないかを同じ行の中で確かめるアンカーとして独立に持つ。`forecast_profit` / `forecast_ordinary_profit` は会社予想の当期純利益・経常利益で、`forecast_eps` と同一予想期のペア（当期予想 `FNP`/`FOdP`、本決算開示で FEPS 空なら翌期予想 `NxFNp`/`NxFOdP`）から取り、純利益>経常の一時益 data-quality flag（`forecast_special_gain`）と、どちらかが負のときの通期赤字予想 annotation（`forecast_full_year_loss`）の一次入力にする。`treasury_shares`（`TrShFY`）と `equity_to_asset_ratio`（`EqAR`）は時価総額と自己資本比率の分母を開示概念へ揃えるために持つ（[valuation-metrics.md §5.1](./valuation-metrics.md#51-資本の分母)）
- `jquants_master_snapshots(snapshot_date, ticker, name, market, sector_33, is_common_stock)` — 主キー `(snapshot_date, ticker)`。異なるrequested as-ofをappend-onlyに保持し、同日再取得だけを置換する。`screening run`はrequested as-ofと同日のsnapshotだけを使う。current-state補助出力の`ticker-profile` / `market-snapshot`とgeneric latest readerはDB全体の`MAX(snapshot_date)`に属するrowだけを読み、過去snapshotからtickerを補完しない。calibrationの`read_eq_master_asof`だけはcohort日以下の直前snapshotを`prior_snapshot`として返せる。cohort日以下にsnapshotが無い場合（snapshot収集開始前の歴史cohort）は最古snapshotへfallbackし`future_snapshot`とlabelする — authority契約が`exact_date`以外をproduction evidenceから除外するため、この近似はdiagnostic計測にのみ効く
- `jquants_earnings_calendar(announcement_date, ticker)` — 主キー `(announcement_date, ticker)`。schema version を変えずに既存 SQLite を読めるよう物理名だけを維持する互換 table で、論理 source と writer/reader の権威は JPX (`jpx_earnings_calendar`) にある
- `disclosures` raw JSON（SQLite 未収録）— 任意 cache。`Code` / `Date` / `Title` などの同義 key も reader 側で受け付ける。title keyword scan のみで金額や財務影響は解釈しない。読み取り coverage は `file_count` / `event_count` / `skipped_record_count` / `unsupported_record_count` / `load_errors` としてscreening runのYAML viewにあるstatus / fallbackへ反映する
- `jquants_market_calendar(day, is_business_day)` — 主キー `(day)`。`HolidayDivision` "1" / "2" を business day=1、それ以外を 0 として記録
- `jquants_weekly_margin(week_end, ticker, long_vol, short_vol, long_std_vol, long_neg_vol, short_std_vol, short_neg_vol, issue_type)` — 主キー `(week_end, ticker)`。2026-09-18 残高までの全銘柄週次開示。既存 `margin_*` はこの table だけを読む
- `jquants_margin_alerts(publication_date, ticker, ...)` — 主キー `(publication_date, ticker)`。日々公表銘柄等に限定された日次残高・公表理由・規制分類。全銘柄系列ではない
- `jquants_all_issues_daily_margin(balance_date, ticker, ...)` — 主キー `(balance_date, ticker)`。2026-09-25 残高からの全銘柄日次開示。公式移行確認と ClientV2 実 payload 検証まで batch 取込は無効で、週次 table と union しない。境界と運用契約は [`margin-publication-transition.md`](./margin-publication-transition.md)
- `jquants_short_sale_reports(disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name, discretionary_investment_contractor_name, investment_fund_name, short_ratio, short_shares, short_trading_units, previous_reported_at, previous_short_ratio, is_cancellation, notes)` — 主キー `(disclosed_at, source_ordinal)`。0.5%以上の報告空売り残高を開示rowのまま保持し、PIT集約は disclosure / calculation の両日とreporter identityを使う。1 disclosure date のresponseは完全snapshotで、`source_ordinal`はprovider応答順のprovenanceとしてlosslessに保存する。cloud/local mergeはrow集合が同じならordinal差を無視し、集合が異なる訂正では新しい`fetched_at_utc`を持つ完全snapshotで日全体を置換する
- `jpx_regulation_sources(asof_date, source_name, fetched_at_utc)` — 主キー `(asof_date, source_name)`。`jpx_regulation_flags` は指定された ticker の行しか持たないため、「その日その source を取得したが該当 0 件だった」と「そもそも取得していない」を行の有無では区別できない。この table が取得の事実を持つ
- `edinet_document_lists(doc_date, process_datetime, result_count, fetched_at_utc, is_final)` — 主キー `(doc_date)`。その日の document list を取得した事実と、EDINET が返した件数を持つ。当日分は日中に増えるため `is_final` が立つまで再取得の対象になる
- `edinet_documents(doc_date, sequence_number, doc_id, sec_code, doc_type_code, csv_flag, xbrl_flag, legal_status, disclosure_status, withdrawal_status, doc_info_edit_status, parent_doc_id, operation_datetime, submit_datetime, doc_description, period_start, period_end, edinet_code, issuer_edinet_code, subject_edinet_code)` — 主キー `(doc_date, sequence_number)`。`sec_code` は提出者の証券コードであり、大量保有・公開買付では提出者と対象会社が別なので、対象会社は `issuer_edinet_code` / `subject_edinet_code` で持つ。identity 3 列を持たない日の行は「観測していない」として扱い、提出が無かった日と区別する（後述「資本配分・支配権イベントの typed fact」）。列は 3 つに分かれる。**記述列**（`sec_code` / `doc_type_code` / `parent_doc_id` / `submit_datetime` / `doc_description`）は提出そのものの主張で、EDINET は縦覧期間が満了すると entry を返したままこの 5 列を伏せる（`legal_status='0'` の行は全てこの 5 列が NULL）。満了は「その属性が無かった」ではなく「もう配信されない」ことなので、再 list は保存済みの非 NULL をこの 5 列で上書きせず、cloud/local merge は片側だけが観測できた値を残す。両側に値があって食い違う場合は従来どおり publish を拒否する。**lifecycle 列**（`legal_status` / `withdrawal_status` / `csv_flag` / `xbrl_flag`）は読取時点で EDINET がその書類をどう扱っているかであり、新しい読取をそのまま採り、merge では比較しない。残りの列は厳密比較のままで、`doc_id` の不一致は行 identity の破損として拒否する
- `tse_capital_policy_snapshots(snapshot_month_end, ticker, status, status_change, updated_on, contact_requested, first_disclosed_month_end, first_disclosure_left_censored)` — 主キー `(snapshot_month_end, ticker)`。東証「資本コストや株価を意識した経営」開示企業一覧の月次 point-in-time 展開。`first_disclosure_left_censored` は「最古シートに既に載っていたので初回開示月を特定できない」ことを表し、初回開示月をその月と主張しない
- `jpx_delistings(delisted_on, ticker, name, market, reason)` — 主キー `(delisted_on, ticker)`。JPX 上場廃止銘柄一覧と過去分アーカイブの合成。上場廃止は起きたら変わらない事実なので、アーカイブ頁が過去年を落としても既存行を消さず accumulate する
- `tender_offer_exit_values(ticker, delisted_on, offer_price_yen, offer_doc_id, result_doc_id, filed_on)` — 主キー `(ticker, delisted_on)`。成立した現金公開買付けの 1 株買付価格。較正 forward の未解決 exit を実値へ置換する唯一の入力で、2 つの一次 source（JPX 上場廃止理由と EDINET 公開買付書類）が同じ案件を指すときだけ行が立つ
- `edinet_buyback_reports(ticker, report_month_end, doc_id, filed_on, window_start, window_end, resolved_shares, resolved_amount_yen, cumulative_shares, cumulative_amount_yen, month_shares, month_amount_yen, issued_shares, treasury_shares)` — 主キー `(ticker, report_month_end)`。自己株券買付状況報告書（様式 220、訂正は 230）の月次読み。同じ月を訂正が上書きするので、1 銘柄 1 報告月につき残るのは 1 行だけである（§5）
- `edinet_metrics(asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, investment_securities, ebitda_ttm, operating_profit_ttm, depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, net_cash, equity, total_assets, consolidation_basis, ttm_quality_*, source_doc_id, document_type, source_submit_datetime, source_period_start, source_period_end, capex_source, failure_reasons, source_document_revision, extractor_revision)` — 主キー `(asof_date, ticker)`。`asof_date` はファイル名から復元。Candidate YAML では EDINET raw `ocf_ttm` を `edinet_ocf_ttm`、投資有価証券の帳簿価額を `investment_securities` として出し、J-Quants 財務サマリー由来の `ocf_ttm` と区別する。`source_*` は research で一次資料へ戻るための traceability として保持する。`source_period_start/end` は EDINET documents metadata であり、半期報告書では実際の CF 測定期間と一致しないことがある。いずれかのrevisionが`NULL`のlegacy rowまたはcurrent candidate/revisionと異なるrowは差分抽出で再利用しない
- `jpx_regulation_flags(asof_date, source_name, ticker, flag, fetched_at_utc)` — 主キー `(asof_date, source_name, ticker, flag)`。JPX cache の `flags_by_ticker` は source 別の起源を保持しないため、`source_name=flag` として記録
- `source_coverage(source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, status, error)` — fetch-provenance source（master / 財務サマリー / 営業日カレンダ / 空売り残高報告 / 決算予定 / JPX 規制 / EDINET metrics）の coverage 正本。masterと空売り残高報告はrequested dateごとの単日keyと同日row countを持つ。空売り残高報告は訂正snapshotの新旧を`fetched_at_utc`で解決するため、隣接日を1 rangeへ畳まない。その他は「未取得」と「対象なし」を行の有無から区別できないため source_coverage を gate にする。range fetch（日次足 / 財務サマリー / 営業日カレンダ）の coverage 記録は fetch の status で分かれる。`ok` fetch は新規取得 window を既存の overlapping / adjacent な `ok` window と union に merge し、その merged window に完全に含まれる非 `ok` row を削除する（clean な再取得はその範囲についての品質の訴えを supersede する）。chunk 境界が asof（`asof - N 日`）ごとにずれるため、一部だけ重なる re-fetch が既存 window を delete-and-shrink して残りを孤立させ、行は揃っているのに `range_covered` がギャップと判定する事故をこの merge が防ぐ。`partial` fetch（正規化で reject された record を含む）は取得範囲だけを自身の row として記録し、重なる `ok` window は削除せずその範囲を切り出して trim する。trim で残った両側は元の `fetched_at_utc` を保ち row 数を数え直す。partial が言えるのはその範囲の中だけで、外側は再取得しておらず主張は生きているためである。したがって 1 source が非連続な複数 window を持つ状態は正常で、`range_covered` は失敗した範囲にだけギャップを見る。日次足だけは coverage gate を source_coverage ではなく行データから導出する（上記）。過去 raw JSON の hash / import audit は保持しない。

### 11.2 Volume と再生成の考え方

`stores/market/market.sqlite` は local store であり git 管理しない。容量増加は repo 履歴ではなくローカルディスクの問題として扱う。schema 変更は forward-only migration で in-place に進めるため、schema bump のたびに全再取得する必要はない。既存 row を migration で backfill できない変更（新 field を provider から埋め直す等）は、`screening invalidate-coverage --source <name> [--start --end]` で該当 `source_coverage` を削除して bootstrap 対象に戻し、`bootstrap-cache --asof` で該当 window を再取得する。store を丸ごと作り直す場合も raw JSON からの migration ではなく `bootstrap-cache --asof` と `extract-edinet-metrics --asof` で provider から補完する。`invalidate-coverage` は削除対象行数を表示してから削除する（cache は再生成可能なため確認プロンプトは無い）。既知でない source 名は既知一覧を示して拒否する。

## 12. J-Quants rate limit と bootstrap コスト

J-Quants の正確なレート制限は非公開で、挙動は実運用の観測から推測する（確定仕様ではない）。コード側の対処は `engine/src/baibai_engine/screening/providers/jquants.py` の `_RATE_LIMIT_BACKOFF_SECONDS`（最大 600s の 429 backoff）と `_RANGE_CHUNK_DAYS`（range fetch を 31 日 chunk に分割）で扱う。

- `bootstrap-cache --asof <past>` の律速は **per-asof の長期履歴 re-fetch のボリューム** であり、「数分で回復する rate window」でも「日次クォータの枯渇」でもない。1 asof の最長窓は `normalized_per_3fy` が要求する日次足・財務サマリー各 2200 暦日で、`_RANGE_CHUNK_DAYS=31` の chunk から ClientV2 内部の per-day API 呼び出しへ fan-out する。throttling 下では 31 日 chunk あたり数分規模のスループットになり、初回の完全 bootstrap は数時間規模になる。429 backoff はこの volume に上乗せされる。
- chunk は resumable。`source_coverage` に chunk 単位で `status=ok` を記録し、中断しても完了済み chunk は再取得しない。複数 asof は履歴窓が大きく重複するため、最初の 1 asof の full bootstrap が高コストで、以降の週は非重複 chunk とその週の EDINET だけで安価になる。
- 既存 cache がある asof では長期履歴を再取得しない。日次足の coverage は行データから導出し（§11.1、DB が SSOT）、range fetch の coverage は overlapping / adjacent window と union merge する（§11.1）。chunk 境界が asof ごとにずれても、行が揃っていれば偽のギャップを作らず re-fetch しない。
- 被覆済みと判定された窓の末尾を読み直すのは **鮮度のための機構であり、穴の修復機構ではない**。窓の終端が store の最新取引日以降のときだけ発火し、そうでない過去窓は読み直さない。したがって「端の許容（10 日）より短い、過去窓の末尾の穴」を埋める経路は無い。これは coverage 判定が休場と区別できない gap 幅と同じ範囲で、それより広い欠けは被覆判定が落として chunk 経路が取り直す。
- range 取得の store は要求範囲を置換するので、空の応答が来たときに行を保持している範囲は置換を拒否する（`EmptyRangeReplacementError`）。日次足・財務・カレンダは市場の実績なので、一度行があった範囲が後から空になることはなく、成功扱いの空応答で断面を消さないための防御である。
- `run` は cache-only で、coverage が揃えば provider を叩かず高速。歴史 replay の律速は `run` ではなく `bootstrap-cache` / `extract-edinet-metrics` の coverage 充足にある。

過去 asof の cache 充足は「rate budget の回復を待つ」問題ではなく、**長期履歴 coverage を一度埋め切る wall-clock** の問題として扱う。1 asof ずつ長時間バックグラウンドで流し、resumable な性質を活かして複数セッションに跨いで充足させる。短い per-step timeout で kill するとその asof の coverage が未充足のまま `run` が fail-fast するため、kill せず完走させるか完了済み chunk から再開する。

## 資本配分・支配権イベントの typed fact

価値実現の経路（いつ・誰が乖離を閉じるか）を機械 fact として持つ層。**ranking・E[r]・gate のいずれにも接続しない。** イベント delta 系の指標は 3y / 5y で negative であり（`reports/studies/2026-08-02-share-return-components-production/`）、東証開示率は Prime 94% / Standard 56% で開示の有無自体の弁別力は既に低い。判断は人間に残し、機械は日付つきの事実だけを供給する。

`refresh-capital-control --asof` は 2 つの JPX source を読み直す。東証「資本コストや株価を意識した経営」開示企業一覧（`list.xlsx`）は当月シートと過去分シートを持つので、月次 point-in-time 系列として全シートを展開する。列位置はシートによって動く（開示内容の列が後から入り、コンタクト希望の列が右へずれた）ため、列は見出し語で解決する。JPX 上場廃止銘柄一覧は index と過去分アーカイブを合成する。両 source とも同じ月・同じ廃止日を再取得すれば同じ行になるので、繰り返し実行してよい。

### EDINET 様式コードと対象会社

| 様式 | 書類 | 対象会社を持つ列 |
| --- | --- | --- |
| 350 | 大量保有報告書・変更報告書 | `issuerEdinetCode` |
| 360 | 大量保有報告書（特例対象株券等） | `issuerEdinetCode` |
| 240 | 公開買付届出書 | `subjectEdinetCode` |
| 250 | 公開買付届出書の訂正届出書 | `subjectEdinetCode` |
| 260 | 公開買付撤回届出書 | `subjectEdinetCode` |
| 270 | 公開買付報告書 | `subjectEdinetCode` |
| 280 | 公開買付報告書の訂正報告書 | `subjectEdinetCode` |

EDINET code から ticker への解決は、同じ document list 履歴が観測した `(edinetCode, secCode)` 対応だけを使う。1 つの code が複数 ticker に対応する場合は解決しない。identity 3 列は後から加わったため、`backfill-edinet-identity --start --end` が保持窓を再取得して既存行へ埋める。coverage を消さずに再取得するのは、coverage を消すと自己株券買付状況報告書の観測窓（§11.1 `edinet_document_lists`）まで同時に失われ、全銘柄の `buyback_authorization_status` が unknown へ落ちるためである。

### candidate annotation

`screening run` は universe の各 ticker へ次を付ける。いずれも「観測できていない」と「観測して該当なし」を別の値で表す。

- `tse_capital_policy_status` — `disclosed` / `considering` / `none`（as-of 以前の最新月次スナップショットに居ない）/ `null`（参照できる月次スナップショットが無い）。`tse_capital_policy_updated_on` は開示内容のアップデート日
- `large_holding_event_recent` / `large_holding_event_latest_on` — 対象会社として直近 183 日に観測した大量保有系提出の有無と最新日
- `tender_offer_event_recent` / `tender_offer_event_latest_on` — 同じく公開買付系提出

`*_recent` の `null` は「言えない」状態であり、`false`（窓を観測して提出が無かった）と違う。言えないのは 3 つの場合で、(a) 窓の全日が identity 込みで観測されていない、(b) その銘柄の EDINET code が過去の提出から解決できない、(c) 対象会社を名指さない提出がその種別で窓にある。(c) は種別ごとに効き、もう一方の種別は答えられる。

### 支配権イベントの実現 exit 値

`build-control-event-exits --asof` は、JPX が上場廃止理由に公開買付けを名指しした銘柄について、成立した現金公開買付けの 1 株買付価格を導出する。届出書（240、訂正 250）と報告書（270、訂正 280）を提出者 EDINET code ごとに束ね、撤回（260）が無く、報告書が買付けの実行を述べ、届出書が普通株式 1 株あたりの円建て価格を一意に示す案件だけを採る。同一対象へ複数の提出者が届出している場合、および 1 提出者が二段階公開買付け（応募合意株主向けと少数株主向けで価格が違う）を出している場合は実値化しない。導出規則と較正への影響は [`reports/studies/2026-08-11-capital-control-exit-values/`](../../reports/studies/2026-08-11-capital-control-exit-values/) に事前登録している。

較正 forward はこの表を使い、市場終値で閉じられなかった窓（`unresolved_missing_exit` / `unresolved_stale_exit`）を `resolved_control_event_exit` へ置換する。置換した行は `resolved` になるが status は通常の `resolved` と別で、settled takeover consideration と観測 quote を混同しない。市場終値で閉じられた行は上書きしない。`calibration-build --without-control-event-exits` は置換前の baseline を再生成する。

## select の判断境界

ranking を変えるのは versioned screening rules と estimate component だけである。held / reserved・月次予算・cash・集中・macro material delta / staleness は annotation / warning であり、rank・候補抽出を変えない。corporate action が unresolved の候補は rank を都合よく変えず、research / plan-limit を block する。candidate に AI 解釈・因果・採用結論を書かない（observed / derived / estimate の区分を維持する）。

### 供給×機会幅の read-only scorecard

`python -m tools.experiments.measure_supply_context --selection-id <ID>` は、shortlist の判断時に
供給と候補集合の幅を別々の座標で出す。calibration panel・run store・application DB は
read-only で開き、E[r]・FV・rank・gate・selection payload・schema を変更しない。

供給軸は current selection 上位 5 の平均 E[r] と、最新月末 panel の screen・流動性・E[r]
hurdle 通過件数である。機会幅は次の座標を持つ。

| 座標 | 定義 | 主な読み方 |
| --- | --- | --- |
| `temporal_jaccard` | current top-20 と前 cycle top-20 の ticker 集合 Jaccard。歴史分布は連続する月次 panel 同士 | 高いほど候補が持続する。daily の前回側は run store、retention 外では `--longlist-history-dir` の R2 longlist history |
| `trailing_12m_unique_top20` | 連続する直近 12 月次 panel の top-20 に現れた unique ticker 数 | slot 240 件に対する銘柄の広がり。月欠損があれば未計測 |
| `carry_dominant_share` | top-20 のうち `er_carry_annual > er_reversion_annual` の比率 | 高いほど E[r] の経済成分が carry 側へ集中する |
| `sector_hhi` | top-20 の `sector_33` share の二乗和 | 高いほど sector 集中が強い |
| `max_cluster_share` | `sector_33 × (carry / reversion 支配)` の最大 cluster 比率 | sector と E[r] 成分を組み合わせた最大の同一 economic bet |
| `event_wait_share` | 最新 canonical shortlist の rejected 中 `reject_class: event_wait` の比率 | 深掘り済み候補が再評価 trigger 待ちである度合い。歴史分布は application DB の prior shortlist cycle |

panel 由来座標の percentile は同一 rules hash の月次 panel 分布、`event_wait_share` は prior
shortlist cycle の分布に置く。異なる母集団の percentile を横並びの score へ合成しない。
前 cycle の run と longlist history が無い、連続月 panel が欠ける、shortlist または rejected
entry が無い場合は `status: unmeasured`・`value: null` と理由を返す。未計測を 0 や
「異常なし」へ補完しない。

供給 2 座標が同じ方向を示し、幅の各座標も同じ結論を支える場合にだけ、低供給×狭い幅を
市場側の枯渇、十分な供給×狭い幅を集中した供給、十分な供給×広い幅を広い供給、低供給×
広い幅を現行 value 軸外の機会として読む。供給内または幅内で方向が割れた場合は四象限を
断定せず、割れた座標と `research / discovery` の優先判断が未解決であることを報告する。
percentile に新しい二値閾値を置かず、raw 座標を単一 regime label へ変換しない。ticker の
新しさ自体を KPI にしない。carry は較正上有効な予測成分でもある
（[`2026-08-06-bargain-capture-diagnosis`](../../reports/studies/2026-08-06-bargain-capture-diagnosis/report.md)
§6.3）ため、carry 集中も単独で悪化や除外と読まない。

**判断時の機械行焼き込み**: run store は 3 世代 retention で、ある shortlist を rank した selection はその run と一緒に消える。shortlist は cycle の正本判断記録（selected 0 件のときは唯一の記録）なので、レビュー面が比較する機械座標は判断側へ持たせる。`shortlist publish` は source selection の longlist 行（`rank` / `fair_value_anchor_yen` / `market_price_yen` / `expected_return_pct` / `fv_convergence` / `event_warnings` / `durability_warnings` / `selection_reasons` / `screening_playbook` / `liquidity_status`）を entry の `machine_snapshot` へ焼き込む。`er_annual` と同じ規律で、**表示専用・publish 後に再計算も上書きもしない**（内容が違う再 publish は conflict）。read model は焼き込みを longlist 行と同じ view 型で返し、レビュー面は生きている selection を優先しつつ prune 後は焼き込みへ落ちる。`select` を `--longlist-top` 無しで publish した selection には焼き込む行が無く、その shortlist は prune 後に機械値を失う。

**決算ラグ annotation**: 決算開示と as-of 財務のラグを判断面へ出す。`jquants_earnings_calendar` は ticker あたり 1 行の**予定**表で、`next_earnings_date` は as-of 以降の最短予定日しか持たないため、日付だけでは「これから」と「もう出た」が区別できない。`next_earnings_status` が 4 状態で答える — `announced`（予定日が as-of 以前）/ `scheduled`（予定日が as-of より後）/ `estimated`（カレンダー行が無く、過去の開示周期から推定できる）/ `unknown`（材料なし）。会社が予定日より前に開示してもカレンダー行は残るので、その銘柄は `scheduled` のまま見える —— 予定日直前の開示は前倒しの実績より業績予想修正・再開示であることが多く（実 store の retrospective で前倒し判定は 89% が誤り）、誤って `announced` にすると読み手が目前の決算を event risk から外すため、判定しない側へ倒している。前倒しかどうかは隣の `fin_latest_disclosed_date` が予定日の直前を指すことで読む。`fin_latest_disclosed_date` は機械行の財務が含む最後の開示日。`stale_fin_flag` は「予定日が as-of 以前なのに、その発表に対応する開示が行に無い」で立ち、**原因は区別しない**（延期・決算期変更・provider 欠落のいずれでも立つ。読み手のすべきことはどれでも同じで、一次開示で切り分ける）。判定材料が無い場合は `null` で、`false`（照合して食い違わなかった）と同じ値にしない。`next_earnings_estimated_date` は前年同期の次の開示日を 1 年ずらした推定で、確定日を上書きしない。周期の刻みに数えるのは実績を伴う開示だけで、来期ガイダンス行（`period_end` が開示日より後）・同一期の再開示・実績列を持たない業績予想/配当予想の修正は除く。実 store の過去 4 as-of で**実際の次回開示日**と突合すると誤差 7 日以内 88〜95%・誤差の中央値 1 日（全件）。次回開示までの距離や決算期の分布で帯ごとに 75〜96% まで振れるので、確定日の代わりには使わない —— event risk 判定は確定日だけで行い、推定は着手順の目安に留める。いずれも annotation で、screen pass・自動除外・E[r]・rank・recommendation を変更しない。カレンダー行を持たない universe ticker 数は `run` の進捗行に出す（実測で universe の約 18%。個別企業の未公表を含むので閾値は置かず、急増を provider 欠落として読む）。

**historical backfill では読めない**: カレンダーは fetch 日を持たない単一 snapshot なので、過去 as-of の run は「今日の予定表」を読む。backfill run の `next_earnings_status` / `stale_fin_flag` は as-of 時点の状態ではない。

**FV convergence warning**: selection longlist の調査入口だけに置く。入力は candidate 保存済みの `market_price_yen`・`fv_sector_median_yen`・`fv_self_range_yen`・`er_reversion_annual`。有限かつ正の価格・anchor と有限な reversion だけを使い、anchor 2 本なら現値が両方以上（1 本ならその 1 本以上）かつ reversion ≤ 0 のとき `price_at_or_above_all_fv_anchors` を warning とする（等値は上値余地がないため warning 側）。anchor 0 本・無効値は `not_evaluable` とし、欠損を 0 へ補完しない。使った anchor 名と値・参考価格・reversion を provenance として payload に残す。warning は screen pass・自動除外・E[r]・rank・recommendation を変更しない。
