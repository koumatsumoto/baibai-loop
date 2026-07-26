---
title: "Workflow — macro analysis"
summary: "マクロ環境分析：L1 指標を毎営業日 L2 reading で機械読み値にし、人間が判断するときだけ L3 macro context report（core 環境評価 10 + connection 積立ループ接続 1）を書く。"
doc_type: workflow
status: active
last_reviewed: 2026-07-25
---

# Workflow — マクロ環境分析

マクロ環境分析は **独立した機能のまとまり**（データ取得層 + 機械読み値 + リサーチの実践）であり、形式化した独自ループにはしない。狙いは、個別銘柄の5年期待値を変え得る外部経路と共通riskを判断層へ供給すること。sector順位、相場方向、買い時、投入額を決めない。

扱うものは性質の異なる 4 種：**① データ（L1 の事実）／ ② 機械読み値（L2 macro reading）／ ③ 環境認識（L3 macro context report）／ ④ 知見（調べ方のメタ知識）**。①②は毎営業日 CI が機械で回し、③は人間が判断するときだけ書く。マクロは標本数がほぼ 1 の判断であり、優位性の数値・統計的有意性・自動の投入額倍率は出さない（§誠実性）。

## ① データ：indicator series を引く

指標データは `baibai-engine macro`（`src/baibai_engine/macro/indicators/`）で、再現可能かつ出所（provenance）付きで取得・キャッシュする。

```bash
uv run baibai-engine macro list --category rates       # 登録 series を見る
uv run baibai-engine macro search 失業率              # 名前/alias/category で検索
uv run baibai-engine macro get jp.nikkei225 --start 2026-05-20 --end 2026-06-22
uv run baibai-engine macro get jp.policy_rate --latest
uv run baibai-engine macro refresh us.10y --start 2026-06-20 --end 2026-07-02   # provider を強制再取得
uv run baibai-engine macro refresh us.10y --all-history --end 2026-07-20        # provider が提供する全履歴を同期
```

`get` は取得済み範囲のキャッシュを確認し、不足があるときだけ provider を呼ぶ。同じ入力には同じ出力を返す（決定論）。`get --latest` はJSTの運用日を `asof` とし、§② の reading rules が観測日から求める次回公表目安 + 猶予までは cache を返し、境界を超えた場合は provider を再取得するため、公表ラグと鮮度判定の知識は reading rules が一元的に持つ。再取得する期間幅は鮮度閾値とは別の契約であり、service の `LATEST_FETCH_LOOKBACK_DAYS`（daily 14 日・weekly 60 日・monthly 以下 370 日）を `get --latest` と日次batchが共用する。

`refresh --all-history` は provider ごとの取得可能な先頭日から強制再取得する。派生系列（`derived` provider）は外部ソースを持たず入力系列の重なりが履歴なので、どの base 系列よりも古い床から入力を読み直して全期間を再計算する（base 系列を先に同期してから回す）。月次整列は月内の各入力の最終観測を使う。market data を月末まで使う数式は選択入力の最終観測日を出力日とし、月初への backdate を防ぐ。数式変更で observation grid を置換する系列は provider spec で個別に宣言し、既存 period を欠く候補なら削除前に失敗して履歴を保持する。FRED 系列は現在の `fredgraph.csv` が返す先頭日を再現可能な境界とし、その日より前の観測を残さない。各系列の observation は registry の `source_url` と一致する cache だけを保持し、同内容の連続 vintage は provider run に取得記録を残して observation から除く。値・単位・期間・取得状態・source が変わる revision と、値が変化して同じ水準へ戻る revision は保持する。JP provider の契約期間や公表 archive が先頭日を制限する場合は、実際の取得範囲と制約を運用記録へ残す。

observation は `(series_id, observed_at, vintage_at)` を主キーに upsert する。多くの provider は取得時刻を vintage として刻むが、挿入前に vintage を除いた内容（値・単位・期間・取得状態・source）を既存最新 vintage と比較し、変化が無ければその再取得行を捨てる。したがって **同じ refresh を何度実行しても、ソースが改定した series の観測だけが新 vintage として増え、それ以外はテーブルが不変**になる（べき等）。ローカルで `--all-history` seed → cloud で日次 refresh、cloud の多重実行や手動 rerun も同じ性質で安全に収束する。cloud 正本の履歴を後から深くするときは、ローカルで `--all-history` を回してから `tools/cloud/r2_transfer.sh push-macro` で載せる（cloud copy を merge してから upload するので、日次 refresh が取った最新観測を失わない）。日次バッチは asof を終端とする frequency 別の窓（daily 14 日・weekly 60 日・monthly 以下 370 暦日）を毎回丸ごと再取得するため、窓内で起きた一時的な取得失敗は次の成功実行が同じ窓を引き直して自動でバックフィルする。窓を超える長期の取得断や旧 vintage の全面リベースが必要なときだけ `refresh --all-history` を運用レバーとして使う。

registry は系列定義の正本だが、DB を開く read 操作は登録外系列の facts・metadata・aliases を削除しない。open 時は現行 registry が知る系列の metadata / aliases だけを upsert し、`series` / `observations` / `provider_runs` の prune は、現行 registry の系列を 1 件以上指定した明示的な `macro refresh` の開始時だけ実行する。registry の series ID 集合には単調増加する generation を対応付け、store の generation が client より新しければ stale branch として refresh を拒否する。series ID を追加・削除するときは `definitions.py` の membership digest を次の generation として追記する。prune は `BEGIN IMMEDIATE` 内で件数集計から commit までを行い、commit 前の `registry-prune-pending` と commit 後の `registry-prune` を同じ transaction ID で出力する。pending を出力できなければ全削除を rollback し、pending だけが残った実行は未確定として扱う。schema v3 は無許可の series DELETE を trigger で拒否し、破壊的 open を実装する schema v2 client も version mismatch で拒否する。

全 provider の observation は insert 前に requested `series_id`・registry の unit・finite・series 固有の `plausible_min` / `plausible_max` を照合する。SQLite の INSERT / UPDATE 境界も unit と band を強制し、`foreign_keys=OFF` の直接writerでもunknown seriesを拒否する。複数行 insert は savepoint 単位で全件成功または全件 rollback するため、cloud merge を含む service 外の writer も部分取り込みや検証迂回を起こせない。schema v5 は `schema.sql` が定義する全 persistent triggerとregistry state tableを実storeへ完全一致させ、singleton generation・空のprune authorization stateも検証する。version番号だけ合う欠落・改変・予期しない追加triggerやstate driftは拒否する。migration は writer lock 内で現行registryに対する既存全履歴のunit/bandを先に検査し、失敗時はversion・column・trigger・dataを一切進めずrollbackする。`jp.foreign_flows` はJPX/J-Quantsのraw値を千円単位のまま保持するため、v5 migrationで数値をrescaleせずunit metadataだけを`jpy-thousand`へ補正する。band は直近 10 年の実績へ十分な桁余裕を持たせ、長期履歴も全件通るまで拡張した明白な列・桁・単位ずれの検出境界であり、景気急変を異常扱いする前回値ジャンプ判定ではない。band 内に残る scale 変更は source identity / header / metadata の provider 固有検証で守る。BOJ xlsx は値列番号・英語 header・metadata列番号・基準年または単位metadataを組にして検証し、隣列に同じ旧metadataが残っても代用しない。対象期間の date row があるのに選択列の数値が 0 件なら失敗する。1 点でも契約違反なら部分取り込みせず、その series の provider run を failed として残す。

registry の band を追加・変更する前後は、git 管理外の live store を read-only validator で全履歴・全 vintage 検査する。導入前の旧schemaは observation の必要列を capability check してscanし、現行schemaはcanonical trigger契約も検証するため、検査のためにlive storeを先にmigrateしない。登録外系列、band 未宣言、unit 不一致、非有限値、band 外値のいずれかがあれば observation identity を出して非 0 で終了する。

```bash
uv run python tools/validate_indicator_store.py --db data/indicators/macro.sqlite
```

### データソース registry

| Provider | 取得 | 担当ドメイン | 確認手順・既知の caveat |
| --- | --- | --- | --- |
| `fred_csv` | 無認証 CSV | 米マクロ・実質金利/期待インフレ・FX・原油・VIX・クレジット OAS・BTC・流動性・NFCI・JP 実質実効為替 | 系列 ID を `fredgraph.csv?id=<ID>` の header で実 fetch 確認。ICE BofA OAS は直近 3 年、S&P / Dow は直近 10 年が現在の配信範囲。**廃止系列あり**（JP OECD CPI は 2021 停止、金 LBMA は 2025/5 停止）。金・SOX は `yahoo`。relay の取り込み停止は store の上では系列自体の停止と区別できないので、publisher が機械可読な配信を持つ系列は publisher 直読を優先する |
| `frb_h15` | 無認証 CSV | 米国債金利・スプレッド | 1 package を series 横断に 1 回 DL |
| `ecb_fx` | 無認証 ZIP | JPY クロス（USD/EUR/AUD） | JPY と基軸通貨の比で算出 |
| `estat` | API（`ESTAT_APP_ID`） | JP 公式マクロ（CPI 総合・サービス、鉱工業生産、機械受注、景気ウォッチャー、消費者態度指数 等） | JP CPI の一次ソース。`statsDataId` と分類 code は e-Stat で確認。**e-Stat の DB 掲載は統計ごとに止まる**（毎月勤労統計は 2021-10 以降更新なし。月次結果は release 毎のファイル資源だけになる）ので、新規系列は `getStatsList` の `UPDATED_DATE` が現在かを先に確認する |
| `estat_dashboard` | 無認証 JSON API | JP 公式マクロのうち e-Stat DB が持たない系列（完全失業率 季節調整値・名目賃金指数） | 統計ダッシュボード（総務省統計局）の `getData`。1 IndicatorCode が月次/四半期/年 × 原数値/季節調整値を同時に返すため、**`source_url` に `IndicatorCode` と `Cycle=1` / `IsSeasonalAdjustment` / `RegionCode` を書いて 1 本に固定する**（observation に残る provenance が上流系列を名指すので、selector を直せば旧系列の観測が source 違いとして掃除される）。provider は全行の `@indicator` / `@cycle` / `@isSeasonal` / `@regionCode` / `@isProvisional` と宣言 total 行数を照合し、filter が効かなかった応答と速報を混入させない。読むのは月次のみで、要求窓に関わらず公表全履歴を取る（指数の基準改定が窓の境目で継ぎ足しにならないため） |
| `jquants_flows` | 認証（`JQUANTS_API_KEY`） | JP 市場内部（海外投資家フロー） | screening と同じ Light credential。`--all-history` は運用日から5年の契約窓を要求する。JPX/J-Quants の公表単位（千円）を `jpy-thousand` として保持し、集計週末を observation、公表日を vintage として同一公表日の複数週を保持する |
| `boj` | 無認証 xlsx | BOJ 長期時系列（マネタリーベース・実質輸出・消費活動指数） | 第1 sheetを openpyxl で読み、registry の `provider_series_id` が宣言する値列・英語header・metadata列・基準年または単位metadataを照合する |
| `boj_timeseries` | 無認証 JSON API | BOJ 無担保コール O/N 平均 | `FM01:STRDCLUCON` の日次値を一括取得する。公表タイミングは BOJ 時系列統計データ検索の更新日に従う |
| `mof_jgb` | 無認証 CSV | JP 国債金利（主要年限） | `jgbcm_all.csv` と当月 `jgbcm.csv` を CP932 で読み、和暦の基準日を ISO date に正規化する |
| `tsr_bankruptcies` | 無認証 JSON API | JP 企業倒産件数 | 東京商工リサーチの掲載ページが参照する公式 JSON から月次全履歴を取得 |
| `spglobal_pmi` | 無認証 PDF（requests→browser fallback） | S&P Global PMI（日本/米 製造業・サービス業） | free の data API が無い。`providers/pmi_release_urls.yaml` の月次 release URL から公式 PDF を取得し、headline 値を bounded context から抽出して diffusion index の定義域 0〜100 で検証する。WAF gated の月は headless browser（Playwright）で fetch する |
| `umich_sca` | 無認証 CSV | 米消費者態度指数（ミシガン大） | 公表元 Surveys of Consumers の月次表（`files/tbmics.csv`）を直読する。`provider_series_id` は値の列名（`ICS_ALL`）、`source_url` が表なので同じ公表元の別表は registry entry だけで足りる。行が「月名 + 年」なので読めない行は skip せず失敗させる（表の形が変わったのを黙って短い履歴にしない） |
| `yahoo` | 無認証 JSON | 金/銀/銅先物・MOVE・Russell2000・SOX 等 | **ブラウザ UA 必須**（default は 429）。`provider_series_id` は Yahoo シンボル |
| `multpl` | 無認証 HTML | S&P500 バリュエーション（CAPE・GAAP PER・益回り） | current page と public monthly table を機械的に parse する。取得・鮮度の契約は daily を保ち、reading rules の `sampling_cadence: monthly` で統計標本だけを月次化する。HTML 構造変更で壊れるため `--latest` と `--all-history` を live 確認 |

新ソース追加＝provider モジュールを 1 つ足して（`providers/` に 1 ファイル）`providers/registry.py` に 1 行登録し、series を registry（`indicators/registry/` の region 別 yaml）へ 1 entry 加える。provider の取得能力（all-history 起点・store 書き換え方針・refresh 可否・point-in-time vintage・必要 env）は各 provider の `ProviderSpec` が宣言し、service / store reader は provider 名で分岐しない。point-in-time replay を提供する provider の spec は fetch 実装を import しない read-safe module に置き、provider 実装と reader が同じ spec を参照する。registered provider との drift は test で検出する。1 series_id = 1 provider を厳守する。provider 取得は一時的な `IndicatorsProviderError` を 1 回 retry し、再失敗した場合は `provider_runs` に failed として記録する。

`macro refresh` は複数 series を 1 pass で取得し、1 series の失敗は他 series を止めない。失敗した series は最後にまとめて stderr へ列挙し、exit code は非 0 になる（1 つの壊れたソースが同一グループの残り全系列を stale にしない）。1 pass は 1 つの fetch context を共有するので、複数 series が同じ bulk ファイルを参照しても download は 1 回、browser fallback を要する provider の起動も 1 回で済む。取得値は store へ入る前に有限値であることを検証し、NaN / ±inf は取得失敗として扱う（派生計算・percentile・export を汚染させない）。

PMI は data API が無いため、月次 release URL の manifest（`src/baibai_engine/macro/indicators/providers/pmi_release_urls.yaml`、`schema_version: 2`、PMI stream ごとに `observed_at` → 公式 release URL）を正本とし、`spglobal_pmi` provider が各 URL の公式 PDF を live 取得して headline 値を抽出する。release URL の validator は `https://www.pmi.spglobal.com/Public/Home/PressRelease/<32 hex>` だけを許可する。

抽出は release が headline を述べる冒頭 statement（headline index を名指す文と、その statement を続ける次の文）だけを読む。値を採るのは、release がその値を対象月に結び付けているか（`posted 47.9 in December`、`in April to 51.3`、`October's 54.8`）、statement が reading として導入している（`posted 52.3`、`rose to 54.4`、`at 51.6 the index ...`）場合だけで、月を明示しない reading は release 自身の月にしか帰属させない。sub-index / composite index を名指す文は読まず、閾値との比較（`above the 50.0 no-change mark`）・flash 見積り・複数月平均は reading ではないので読む前に text から除く。抽出値は1〜3桁・小数1桁としてparseしてからdiffusion indexの定義域 0〜100 で検証し、複数候補が矛盾する月は取得を失敗させる（誤った値を store に入れない）。危機・再開局面の正当な30未満・70超も欠落させない。読めない phrasing は値を作らずに「その月の headline 値なし」として失敗するので、取り込み漏れは無音にならない。

1 月 = 1 PDF なので、通常の refresh は store に無い月と、manifest の release URL が store の値の出所と一致しない月だけを取得する。final headline は公表後に改定されないため、同じ URL から取り直した月は同じ値になる。URL を訂正すればその月は自動で取り直される。抽出規則の変更後など stream 全体を source から作り直すときは `refresh --all-history` を使う（全月を再取得する）。

**新しい月の追記は人手運用**: S&P Global の公式 press release ページ（`/Public/Release/PressReleases`）から該当月の release URL を特定して manifest へ追記し、`macro refresh` で該当月を取得して妥当域検証と公表値の照合を通してから commit する。**過去月の穴埋めは同じページの archive snapshot から辿る**：この index は最新 1 か月分しか列挙しないため、それより古い release id はサイトからは辿れない。Wayback の同 URL の snapshot が捕捉時点の一覧（公表日・タイトル・release id）を持つので、そこから月ごとの id を復元する。**復元した対応付けは、manifest に既にある月の id と一致するかで検算する**（release は前月分を報告するので、publish 月 − 1 が observed_at になる）。id が復元できない月でも、翌月の release が前月値を restate していればそこから読める：`observed_at` をその月、`release_observed_at` を翌月にして翌月の URL を書く（restate は「前月を名指した値」か「月を伴わない `down from X`」で書かれるため、後者は前月分としてだけ読む）。manifest が公表カレンダーに追いつかない（release 済みの月が manifest に無い）状態は取得の無音の停止になるため、provider が明示エラーで失敗させる: 要求 end の月に対して manifest 最新月が前月に達していないとき（当月 10 日以降）にエラーになり、日次バッチの繰延べ失敗として表面化する。このガードは取得（refresh / all-history）だけを止め、読み取りは store にある月をそのまま返す（manifest の追記漏れが既存データを隠さないため）。

```bash
uv run baibai-engine macro refresh jp.pmi_manufacturing --start 2026-05-01 --end 2026-06-30
uv run baibai-engine macro refresh jp.bankruptcies --all-history --end 2026-07-24
uv run baibai-engine macro get jp.bankruptcies --start 2003-01-01 --end 2026-07-24
uv run baibai-engine macro get jp.pmi_manufacturing --start 2023-01-01 --end 2026-07-24
```

`macro.sqlite` は schema / series registry と各 provider（PDF / API / CSV）から再構築する L1 store であり、定期 backup は持たない。ただし PMI 履歴のように publisher が古い URL を落とすと再取得できない部分があるため、R2 への push は上書き対象の 1 世代を `<key>.bak` として残す（[`tools/cloud/README.md`](../../tools/cloud/README.md)）。

### Baibai App で期間と粒度を読む

`baibai-app` の Macro ページは期間 `1y | 5y | 10y | max` と粒度 `daily | weekly | monthly | yearly` を全チャートへ適用する。`/api/macro` も同じ query parameter を受け、週次・月次・年次は各期間の最終観測値を返す。UIの既定は `max + monthly`、API parameterを省略した場合は `1y + daily` である。`series.yaml` に `tradingview_symbol` がある系列だけ、チャートカードから TradingView の該当 symbol を新規 tab で開く。

### 運用テスト（series / provider を変更したら必ず回す）

データ層の品質は **運用テスト** で担保する。すべて失敗 0 件で通す：(1) 全 series スイープ（`list | get --latest`）で error / stale を 0、(2) 桁・単位の妥当性、(3) provider ストレス（rate-limit 系を 1 プロセスで refresh し 429 が出ないか）、(4) 派生計算の単位整合（net liquidity = FRB総資産 − RRP − TGA、単位換算を明示）、(5) alias 解決、(6) 決定論、(7) `uv run pytest` とmacro model / config loaderのnegative test。

<a id="macro-reading"></a>

## ② 機械読み値：macro reading

macro reading は L1 store だけを入力に、登録全系列の **記述統計と鮮度** を決定論で出す L2 出力である。「今の VIX は歴史的に高いのか」を分析セッションごとに人が判断し直さないための共通の物差しであり、regime 分類・合成 score・売買 signal は出さない。

```bash
uv run baibai-engine macro reading --asof 2026-07-24                 # 表形式
uv run baibai-engine macro reading --asof 2026-07-24 --format json   # 機械読み
```

各系列について次を出す。

| 読み値 | 意味 |
| --- | --- |
| `latest_value` / `observed_at` | asof 以前で最も新しい観測とその観測日 |
| `staleness_days` / `stale` | asof − `observed_at`。次回公表目安 + 猶予を超えたら `stale` |
| `next_print_estimate` / `print_due_in_days` | `observed_at + 1 publication_cadence + publication_lag_days` で求める次回公表目安と asof からの日数。負値は公表済みのはずで取得待ち |
| `window_years` / `window_observations` | percentile / z-score を計算した実効窓と、その窓に入った観測数 |
| `insufficient_history` | 実効窓を履歴が満たさない。`percentile` / `z_score` は null になる（判定は下記） |
| `statistic` / `statistic_unit` / `statistic_value` | percentile / z-score が位置を測る対象（`level` = 水準そのもの、`yoy` = 前年比 %）と、その単位・最新値 |
| `percentile` | 実効窓の統計標本のうち `statistic_value` 以下の割合（0〜1） |
| `z_score` | (`statistic_value` − 標本平均) / 標本標準偏差。標本が定数なら null |
| `short_trend` / `long_trend` | 規則の月数だけ前の基準日以前で最新の観測に対する変化。`anchor_observed_at` を併記する |
| `flags` | 教科書的な閾値に触れていることの注記（PMI<50、curve 逆転、ERP≤0、VIX≥30 等） |

読み方の規律：

- **`insufficient_history` の系列は水準比較に使わない**。窓を満たさない履歴で percentile を出すと、その系列自身の短い生涯の中の順位を「歴史的な位置」と読み違える。reading は計算を拒否して null を返す。判定は 3 条件で、**先頭観測が窓の先頭 1/10 までに始まっている**（provider の rolling 窓のずれと、月次・四半期の観測日粒度を吸収する猶予）・**窓内の観測が 8 件以上**・**frequency が示す期数の 6 割以上が埋まっている**（週次・月次・四半期のみ。日次は「1 年に何営業日あるか」が frequency の性質ではないので件数を課さない）のいずれかを欠けば立つ。密度を見るのは、欠落が均等に散らないためである: 人手で埋めるソースは直近の月から埋まるので、穴の空いた 10 年窓は「直近の分布に 10 年のラベルを貼ったもの」になる。実際の件数と期待件数は `window_observations` / `expected_observations` に出る
- **`next_print_estimate` は公表予定日の目安であり、イベントカレンダーではない**。`publication_cadence` の次期と `publication_lag_days` だけから決定論で導出する。daily の既定は `business_daily` で推定日が土日なら翌平日へ送り、土日も観測を持つ系列は `calendar_daily` を明示する。cadence の既定は registry frequency だが、統計標本は月次でも当月値を営業日更新する `us.erp`、日次観測を週次バッチで公表する H.10 / EIA のような系列は override する。祝日や当局の個別日程は手維持しない。`print_due_in_days` が小さい正値なら公表が近く、負値なら公表済みのはずで取得待ちである。精密な会合・イベント日は L3 monitoring で一次情報を確認する
- **`stale` は provider の無音の停止を疑う合図**であって、値の否定ではない。観測の齢は公表ラグと週末を含むため、境界日は **`next_print_estimate + staleness_margin_days`** から同じ calendar arithmetic で導出する。`staleness_warn_days` はその観測日から境界日までの日数を表示する。次の公表を待っている平常時には出ず、1 回の公表落ちで出る水準である。lag / cadence が frequency default と構造的に違う source（H.4.1 の翌日公表、EIA の日次価格を週次でまとめる FRED、M+2 公表の JOLTS、OECD の中継、日次更新する月次派生値）は系列別に override する。**閾値が緩すぎると 2 公表分の欠落を通す**ので、公表が速い source ほど閾値も短くする
- **`flags` は signal ではなく注記**である。閾値に触れたことを見落とさないための機械的な指差しで、水準の解釈と行動は L3 の判断に属する
- **`z_score` の極端値は誤値と真の市場極値の両方で出る**。どちらかは reading では決めず、L3 が一次情報と突き合わせて判断する
- **percentile は `statistic` と一緒に読む**。水準の尺度が自らの履歴でしか決まらない系列（物価・数量の指数、名目の集計値、累積の雇用者数、株価指数）は水準の percentile が時間の経過を映すだけになるため、`statistic: yoy` として前年比 %の分布内の位置を出す。金利・スプレッド・比率・DI・ボラティリティ・為替・商品価格は水準自体に解釈があるので `level` を保つ。`yoy` の系列でも `latest_value`・`flags`・`short_trend` / `long_trend` は水準のままで、trend は系列自身の単位の絶対変化、percentile は %変化の位置を示す
- **統計標本の1点は reading rule の `sampling_cadence` に合わせる**。既定は registry frequency から解決し、monthly / quarterly 系列は同じ暦月・暦四半期の最終観測1点へ折ってから level / yoy を計算する。取得 cadence と統計 cadence が異なる source は系列 override で分離する。latest value・trend・flags は折る前の観測を読む
- **`statistic_value` が null なら位置は出ない**。前年比は 1 年前の観測を相手に取るので、その月が欠けている系列（相手が 380 日より前しかない）や相手が 0 以下の系列は該当点を標本から落とし、最新点が落ちれば percentile / z も null にする

計算規則は `method/macro-reading/<ISO8601>.yaml` の dated revision に置き、reading 出力は使った `rules_revision` を併記する。規則は **frequency 別の defaults + 系列別 override** で解決する。系列を registry へ追加しても規則の編集を要求しない（defaults が解決する）ことが設計要件であり、override は「default が事実に反する系列」だけに書く：provider の配信範囲が構造的に短い系列（J-Quants Light の 5 年 rolling、`spglobal_pmi` の manifest が持つ月だけ、ICE BofA OAS の 3 年）は percentile の実効窓を、水準に位置が無い系列は `statistic` を、公表 cadence / lag が frequency default と違う source（日次更新する月次派生値、H.4.1 の翌日公表、M+2 公表の月次、OECD 中継）は `publication_cadence` / `publication_lag_days` / `staleness_margin_days` を、教科書的な閾値を持つ系列は flags を override する。**全登録系列で規則解決が成立すること**は test が保証し、解決できない系列があれば fail する。1 つの系列は複数の理由で override されるため、**同じ series を 2 度書いた revision は load 時に失敗する**（YAML は後の entry だけを残すので、上の設定が黙って落ちて「適用済み」と読める）。

`schema_version: 1` の既発行 revision は引き続き load・再計算できる。その revision では当時存在しなかった `next_print_estimate` / `print_due_in_days` を null とし、既存の明示 `staleness_warn_days` をそのまま使う。`schema_version: 2` は publication lag / margin を契約とし、`staleness_warn_days` の明示を拒否する。系列固有の鮮度差は lag / cadence / margin を override して表す。両 shape の混在を load 時に拒否するため、過去 revision の意味を現在の lag 推定で書き換えない。

`statistic` の既定は `level` なので、水準に位置が無い系列を registry へ追加したら override を書く（書き忘れは percentile が 100% 近傍に張り付く形で reading 自身に現れる）。`yoy` の系列では窓の先頭より 13 か月前まで raw を読み、標本は窓の中だけを使う。履歴が窓の先頭で始まる系列（provider が rolling 窓を配信する場合）は先頭 1 年に相手が居ないため標本がその分薄くなるが、`insufficient_history` の判定は raw 履歴が窓を張るかで行うので percentile は出る。

percentile の実効窓を短縮した系列は、provider の履歴が伸びて default に届いたら override を外す（`window_years` が default と一致しているかを規則改版時に確認する）。

reading は L1 store を読むだけの純関数で、provider を呼ばず DB へ書かない。したがって過去日の asof でも同じ入力から同じ snapshot を再計算できる。CLI・read API・indicator chart は共通の store reader を使い、`ProviderSpec.point_in_time_vintage` を宣言する source だけを `vintage_at <= asof` へ clamp する。宣言のない bulk history の `vintage_at` は取得日時であって当時の公表日時ではないため、一律 clamp して取得前の過去 snapshot から既知だった履歴を消さない。専用 store は持たず、日次バッチが Baibai App 向けの serving view（`/api/macro/reading`・`views/macro-reading.json`）として export し、L3 レポートは引用した snapshot を `inputs.reading_snapshots` に記録する。

Baibai App の Macro タブは先頭にこの読み値をヒート面（系列ごとの方向・`statistic`・percentile・実効窓・flags）、data health（取得失敗・`stale`・`insufficient_history` の 3 分類。**3 つとも 0 件なら「問題なし」に畳む**）、分布の端（`|z_score|` ≥ 3 を強い順に列挙）として表示する。**極端な z は data health に混ぜない**：health の 3 分類は percentile の解釈可能性を壊すものだが、端にいることは reading が測った位置そのもので、panel の結論に最も近い情報である（誤値でないことの確認は L3 が一次情報と突き合わせて行う）。view が未生成のときはその区画だけを出さない（指標パネルとレポート index は通常表示する）。

## ③ 環境認識：macro context report を publish する

市場局面についての、日付と出所の明確な環境認識は application DB の immutable revision として残す。機械契約は `baibai_engine.macro.context.models.MacroContextDocument`、唯一の書き込み経路は `baibai-engine macro context publish` である。既存 head を読んで draft を作り、2件目以降は `--expected-head` にその ID を渡す。head が変わっていれば publish 全体が無変更で失敗する。

```bash
uv run baibai-engine macro context head
uv run baibai-engine macro context publish /tmp/macro-context-draft.yaml \
  --expected-head macro-context-2026-07-01-example
uv run baibai-engine macro context show --latest --asof 2026-07-19
```

レポートは **1 種類だけ**で、常に下記の深度契約を満たす full 深度で書く。軽い事実確認のための軽量版は持たない（その用途は §② が毎営業日 機械で果たす）。

**作成のきっかけは人間の判断だけ**である。定例義務・monitoring 発火時の更新義務・賞味期限の宣言は持たない。推奨リズムは (a) 米雇用統計の翌週、(b) スポットの資産運用判断の前、(c) opportunity cycle（OP3）の前で head が古いとき、の 3 つで、書かない月があっても壊れるものは無い。鮮度の判断は読む側が持つ（後述の consumer 側鮮度規則）。

### 2 部構造：core（環境評価）と connection（積立ループ接続）

レポートは **core 10 セクション + connection 1 セクション** で構成する。core は use-case agnostic な環境評価であり、日本株積立ループ固有の語彙（sector tilt・research 優先度・sizing caution）を持たない。connection はそれらを 1 か所へ隔離する。

この分離は書き手の注意ではなく **参照方向の機械契約** で守る：connection が引用できる series は core が引用済みのものだけで、connection は依拠する core セクション（その series を実際に引用しているセクション）を `core_section_ids` で明示する。core 側へ sector tilt / research 優先度ヒント / sizing caution を書いた draft は schema が拒否する。series 以外の input（`screening market-snapshot` の市場内部やループ固有の記事）は connection が自分の入力として持ってよい——バーゲン地形は connection の担当であり、core を日本株ループの語彙で汚さないためである。ただし **prose は機械では縛れない**（core の judgment に行動指示を書き込むことは schema では止まらない）ので、そこは skill の敵対的 self-check が受け持つ。core が単体で完結していることの構造的な証明になり、リポジトリ外のスポット資産運用判断の材料としてもそのまま読める。

| 部 | 順 | セクション（`section_id`） | 確認するfact | judgmentと接続 |
| --- | --- | --- | --- | --- |
| core | 1 | レジーム要約（`regime_summary`） | 成長・インフレ・金融条件の水準と方向、比較可能な時点からの変化 | 成長×インフレ×金融条件の共通座標で現局面を定め、以降の読み順を示す。前回 scorecard の採点結果を接続する |
| core | 2 | 金利・金融政策（`rates_policy`） | 政策金利、イールドカーブ、実質金利、主要中銀の方向 | discount rate経路とmaterial deltaを示す |
| core | 3 | 景気・需要（`growth_demand`） | PMI、雇用、消費、生産、景気breadth | 需要経路への接続を示す |
| core | 4 | インフレ・コスト（`inflation_costs`） | CPI、賃金、輸入物価、commodity、原油と通商政策 | 売価転嫁とmargin経路を示す |
| core | 5 | 流動性・信用・リスク選好（`liquidity_credit`） | net liquidity、credit OAS、VIX/MOVE、NFCI | funding条件と共通tail riskを示す |
| core | 6 | 為替（`fx`） | USD/JPY、金利差、実質実効為替 | 円水準の両側リスクを非対称ごと示す |
| core | 7 | 日本（`japan`） | BOJ政策、国内賃金物価、鉱工業生産、海外投資家フロー、日本の需要fact | 日本経済の需要・費用・為替感応度への接続を示す |
| core | 8 | バリュエーション（`valuation`） | 米 ERP / CAPE、日本 ERP（市場全体PERまたは益回り − JGB 10y）、金、BTC | 各資産の相対的な位置を示す |
| core | 9 | リスク選好環境の評価とシナリオ（`risk_environment`） | セクション2〜8を支持・反証する系列 | 攻め／守りどちらの環境かを `stance`・確度・**反証条件**付きで評価し、base / bear / bull を scorecard 条件付きで置く |
| core | 10 | 監視ポイント（`monitoring`） | 次の公表・会合と観測条件 | 何が出たらどの見方を変えるかを明記する |
| connection | 11 | 日本株積立ループ接続（`japan_equity_loop`） | core が引用済みの series のみ | research 優先度ヒント（効く候補タイプを `applies_to` で判別可能に）、sector tilt、sizing caution、バーゲン地形 |

共通 field：

- `context_id` / `as_of` / `published_at`。`as_of`は**市場データの最終完全営業日**にする（著述日ではない）。screening selectはpoint-in-time整合のため`as_of ≤ selection ASOF`のcontextだけをbindするので、週末・祝日に書くcontextの`as_of`を著述日にすると直近ASOFのselectへ恒常的にbindされない
- `inputs.articles`：外部記事の一意な`input_id`、source / title / url / published_at / accessed_at / status / used_for（記事本文や監査ログは保存しない）
- `inputs.indicator_series`：一意な`input_id`、`baibai-engine macro`で確認したprovider / series / window / observation_as_of / status / used_for
- `inputs.machine_snapshots`：引用した自前コマンドの決定論出力（`screening market-snapshot` 等）。一意な `input_id`、`command` / `snapshot_asof` / `observation_as_of` / `accessed_at` / `status` / `used_for`。**自前出力は記事ではない**ので `inputs.articles` へ入れない：発行者も URL も無く、コマンドと訊ねた日付が identity である（記事枠へ入れると定義 doc の URL が数値の出所として読まれる）。`snapshot_asof` は as_of より未来にできず、`observation_as_of`（実際に使った最終市場日）はその as_of を超えられない。scorecard は専用 snapshot 契約で context / rules revision / 両 store / result digest も固定し、観測を 1 件も使わない pending-only 結果だけ `observation_as_of: null` を許す
- `inputs.reading_snapshots`：引用した macro reading の `rules_revision` と `reading_asof`。**reading input を持たない draft は publish されない**。レジーム要約は reading input を引用する必要があり、共通座標を機械読み値から始めることを強制する。`reading_asof` は as_of より未来でも 7 日より古くてもならず（reading は任意の as_of で再計算できるので、レポートは自分の as_of の reading を引く）、`rules_revision` は `method/macro-reading/` に実在する revision でなければ publish されない
- core の各セクションは`series_ids`、source付き`fact_summary`、方向・確度・source付き`judgment`、source付き`economic_connection`を持つ。connection セクションは`economic_connection`を持たず、代わりに`core_section_ids`とループ固有の項目を持つ
- `material_deltas`：core セクション2〜8の判断として置く。channel / direction / materiality / used_forを持ち、レポート全体で最低1つ必要
- `sizing_cautions` / `sector_tilts` / `research_priority_hints`：connection セクションだけに置く。research 優先度ヒントは 1 件以上必須（着手順位を渡すことがこのセクションの存在理由）、sector tilt と sizing caution は該当が無ければ空でよい（core が支持しない tilt を埋めるために書かせない）

各`series_id`はaliasではなくseries定義のcanonical IDを使って`inputs.indicator_series`にも置き、各要約・判断・接続の`source_ids`をinputへ結ぶ。series定義にないID、inputにないseries参照、正常取得した同系列inputを引用しないセクション、failed inputを引用する判断はpublishされない。変化がmaterialでないセクションも省略せず、確認したfactと「見方を維持する条件」を記す。

<a id="depth-contract"></a>

### 深度契約（全レポート共通）

レポートは銘柄選定とスポット判断のリスクリワード判断の土台になるため、次の深度契約を常に満たす。

- **テーマ被覆**: 金利・政策 / インフレ・コスト / 需要・雇用 / 為替・流動性・credit / 日本の政策・金利 / 日本の需要 / energy・地政学・通商 / 市場内部・バリュエーション の8象限すべてにfactを置く。`inputs.articles`はTier-1中心に15本以上で、**数えるのは外部記事だけ**（`inputs.machine_snapshots` の自前出力と `inputs.reading_snapshots` は本数に数えない。自前出力を数えると外部の一次情報を集めた量を自分の計算で嵩上げできてしまう）。
- **日本の需要fact最低ライン**: セクション3または7に、実質賃金（毎月勤労統計）または実質消費、鉱工業生産を必ず含める。取得可能ならインバウンド（訪日外客数）・機械受注も置く。米国factだけで需要判断を組み立てない。
- **円水準の両側リスク**: セクション6に、円安継続と円反転（介入・利上げ）の両経路が輸出企業（為替換算益の剥落）と輸入コスト企業（margin回復）へ与える非対称を1つのjudgmentとして書く。片側の監視条件だけで済ませない。
- **バーゲン地形**: connection に`screening market-snapshot`のbenchmark 20d/60d・breadth・regimeを`inputs.machine_snapshots`の input としてfact引用し、「この局面でミスプライスがどこに出やすいか（全面安で広く出る / 回転相場で取り残しに出る / 全面高でプールが縮む）」をjudgmentとして書く。
- **日本株バリュエーションアンカー**: セクション8に市場全体のPERまたは益回り（日経・JPX公表の一次値、または全universeのin-house中央値）とJGB 10yの対比を置き、個別FVアンカーの妥当性を外側から検算できるようにする。
- **hintの識別力**: 全候補に等しく当てはまる助言（「net cash重視」等）はhintではない。各 research 優先度ヒントと sector tilt は、どの候補タイプ・sectorに効くかを`applies_to`で判別できる形で書く。
- **energy・通商・地政学**: セクション4または5に、原油と通商政策（関税）・地政学tailのfactを最低1つずつ置く。
- **reading 先読**: core を書く前に §② の reading を全系列読み、`stale` / `insufficient_history` / `flags` / 極端な `z_score` を確認する。機械読み値と自分の結論が矛盾する場合、どちらも盲信せず矛盾自体をjudgmentとして書く。

### scenario scorecard：見立てを後から採点できる形で書く

セクション9の base / bear / bull は、自由文の成立条件とは別に **機械照合可能な観測条件（scorecard）** を各シナリオ 2 つ以上持つ。条件は `series_id` + 比較演算（`below` / `at_or_below` / `above` / `at_or_above`）+ 閾値 + 期限日で書き、series はそのセクションが引用済みのものに限る。期限日は **as_of から「その系列がもう一度公表されるだけの日数」以上、かつ as_of から 18 か月以内**（四半期系列が 2 回公表される幅）で、近すぎる期限も遠すぎる期限も採点できないため publish されない。同じ条件を 2 回書いて 2 件にすることもできない（`series_id` + 比較演算 + 閾値の重複を拒否する）。

狙いは予測精度の測定ではなく、**機械照合できる条件でしか書けなくすることでシナリオの記述品質を事前に縛る**ことである。「金融環境が引き締まれば」のような採点不能な条件は書けなくなる。定例が無くても、次のレポートがいつになっても L1 履歴から遡って採点できる。

採点は次のレポート作成時に `baibai-engine macro context scorecard --context-id <前回id> --asof <今回asof> --format json` で L1 履歴と機械照合し、出力の `machine_snapshot` を inputs に引用して、その `input_id` をレジーム要約の `previous_scorecard_snapshot_id` へ置く。ただし **前回の採点は今回の解釈の前提にしない**：今回の評価をゼロベースで確定したあとに、採点結果を fact として接続する（後述の分析の独立性）。採点専用 ledger は持たず、発行済み revision の `previous_scorecard_review` と structured snapshot reference がその時点の latest-known-data による採点を固定する。将来の較正はこの固定済み review を横断集計し、コマンドの再実行は current store での再評価として区別する。

scorecard はレポート `as_of` の翌日から各条件の期限日までを評価する。期限内の最初の成立を `met`、期限後に公表待ちを含む保守的な settlement watermark を越えても不成立なら `not_met`、それまでは `pending` とし、`met` は最初の成立観測、`not_met` は期限内の最終観測を必ず出す。`met` は開始から成立観測まで、`not_met` は全評価窓を active provider が再取得した successful run（1 件以上）で裏付け、run は timezone-aware な完了時刻が採点 `asof` の JST 日末以前でなければならない。watermark は固定した reading rules revision の系列別 staleness 上限を期限へ加えた日であり、`not_met` の run は watermark 後の完了も要求する。未来 `asof`、必要な run の欠落、期限時点で staleness 上限を超える観測は、不成立と推測せず hard error にする。読み取りは通常の L1 reader と同じ latest eligible vintage を使い、観測日の上限は条件期限、publication-quality vintage の上限は採点 `asof` として分離する。JSON は実際に読んだ store path、rules revision、採用観測の unit / vintage / source、結果 digest を含み、後続 context の publish 時に read-only 再計算して digest を照合する。

### 鮮度は読む側が判断する

レポートは自分の賞味期限を宣言しない。鮮度の扱いは consumer が自分の規則として持つ。

- **screening select**: head レポートの `as_of` が判断 asof から 45 日より古ければ `macro_context_stale` warning を出す。warning は context-level summary の材料であり、E[r]順位・candidateの事実層・候補抽出のいずれも変えない。`as_of` が判断 asof より未来のときだけ hard error にする
- **opportunity cycle（OP3）/ スポット判断**: head が古い、または深度契約を満たさないと判断したら、shortlist 作成の前に書き直す。判断の前提が古いままかは判断する人が決める
- **Baibai App**: Macro タブが head の `as_of` を表示し、読む人が古さを目で確認できる

### 分析の独立性

環境認識の前提にしてよいのは過去の客観的事実（価格・指標・イベント）だけで、過去の macro context revision にある分析・結論・tilt は前提にしない。保有中の建玉も分析に持ち込まない。一次情報と指標から、解釈を毎回ゼロベースで組み立てる。比較可能な時点からの変化と前回 scorecard の採点は、結論を確定させた後にレジーム要約のfactとして接続する。

**revision は分析レイヤーであり、手順（作業の指示）を書かない**。「次回からこう調べる」といった手順の話は本 doc（workflow）に置く。revision には、screening / research / スポット判断の前提として使う環境認識と出所のメタデータだけを残す。

### 入門者向けの指標の読み方

指標は単独で結論にせず、方向・水準・市場予想との差・改定を分け、同じ経路の反証指標と組にして読む。系列の一次sourceと取得上の制約は[`../reference/data-sources.md`](../reference/data-sources.md)を参照する。

| 指標群 | 基本の読み方 | 必ず組み合わせる確認 |
| --- | --- | --- |
| 政策金利・国債金利 | 政策の現在地と市場が織り込む将来経路を分ける。長期金利上昇は割引率の上昇要因になりやすい | 実質金利、期待インフレ、イールドカーブ |
| PMI・生産・雇用・消費 | 50などの基準、水準の方向、雇用の遅行性を区別する | 新規受注、失業保険申請、生産、実質消費 |
| CPI・賃金・輸入物価 | 総合と基調、前年比と前月比を分ける。賃金上昇は需要とcostの両経路を持つ | service CPI、実質賃金、為替、原油・銅 |
| 為替・金利差 | 為替だけで因果を確定せず、金融政策差とrisk-offを分ける | 日米金利、VIX、trade-weighted dollar |
| 流動性・credit・volatility | net liquidityは構成系列を同じ単位にそろえる。OASやVIX/MOVEの上昇は資金調達・risk appetiteの悪化を示し得る | NFCI、HY/CCC OAS、株式breadth |
| 日本固有系列 | BOJ、賃金物価、海外需要、投資家フローを順に接続する | USD/JPY、実質実効為替、鉱工業生産 |

## ④ ナレッジ：8 分析レンズ

個別の指標は単体で読まず、以下のレンズに束ねて環境認識に使う（1枚のパネルとして横断的に読む）。操作routingはskill[`macro-analysis`](../../.agents/skills/macro-analysis/SKILL.md)、分析詳細とsource規律は本docを正本とする。

1. **グローバル流動性**：net liquidity ≈ `us.fed_assets` − `us.reverse_repo` − `us.tga`（単位換算注意）。`us.m2` 前年比はリスク資産に約 10 週先行。
2. **実質金利・store-of-value**：`us.real_10y` + `us.breakeven_10y` + `usd_index.broad` + `gold`。名目 = 実質 + 期待インフレに分解。日本側は `jp.real_10y_proxy`（月末10Y JGB − コアCPI前年比）で、名目金利の上昇が実質でも締まっているのか、インフレに食われて実質マイナスのままかを読む。
3. **金融環境の合成**：`us.nfci` を `vix`・`us.move`・クレジット OAS と突き合わせ、slow-burn（広範化前の局所ストレス）を読む。
4. **リスク選好の温度計**：`btc_usd` + `vix` + `credit.us_hy_oas`/`credit.us_ccc_oas` + `us.nfci`。BTC は先行温度計になりやすい（単独 driver にはしない）。
5. **景気サイクル・breadth**：`us.initial_claims` + `us.industrial_production` + `copper` + `us.russell2000` + `us.10y_3m_spread`。`us.sox` は AI/半導体サイクルと日本半導体株の先行ゲージ。
6. **バリュエーション・ERP**：`us.sp500_earnings_yield` − `us.10y` ＝ 米ERP。益回り < 名目金利（ERP≤0）は警戒域。`us.sp500_cape` で長期割高度。**日本側は市場全体PER/益回り（日経・JPX公表値またはin-house universe中央値）− JGB 10y** を同じ構図で読み、個別FVアンカーの外側検算に使う。
7. **グローバル中銀の同期**：`us.fed_funds.upper` + `jp.policy_rate` + `ecb.policy_rate`。1 国でなく同期を読む。
8. **エネルギー・地政学**：`wti`/`brent` + `gold`。日本はエネルギー輸入依存が高く（中東 ~95%・ホルムズ ~74%）原油 spike が通貨・スタグフレーションに直結するため `usd_jpy` と併読。

## Material deltaとAIの境界

macro contextはdiscount rate、需要、資金調達、共通tail risk、sizing cautionだけを表す。AIの役割と株主価値の獲得可能性はmacro contextに置かず、企業別thesisで評価する。

## 接続：判断層にだけ効かせる（screen は macro-blind）

マクロの読みは機械スクリーニングの `run` には接続しない（`run` は財務事実だけを扱う決定論的なエンジンのまま）。効かせるのは判断層だけ：

- **select**（[`./screening.md`](./screening.md)）：material deltaと`as_of`鮮度warningをcontext-level summaryとして出す。E[r]順位とcandidateの事実層は変えない。
- **research**：material deltaが個別5年期待値へ影響する場合だけ、thesisのjudgmentへその因果と根拠を残す。マクロを数値ドライバー、採用gate、投入額ルールにはしない。
- **connection セクション**：OP3 が research 優先度ヒントと sizing caution を消化する入口になる（[`../operations/decision-cycle.md`](../operations/decision-cycle.md)）。

行動指示（売買タイミング・現金比率・配分指示）はcore にもconnection にも書かない。sector tiltとresearch優先度ヒントは着手順位を判断するjudgment入力であり、機械ranking・hard gate・自動sizingへは接続しない。

## 誠実性（honesty firewall）

マクロは標本数がほぼ 1 であり、screening のように多数の銘柄を横断する統計検証ができない。この工程は優位性の数値・統計的有意性・自動売買スコアを出さない。ここで得られるのは再現性と、判断を事実に根付かせる基盤であって、統計的な厳密さではない。§② の reading も記述統計であり、regime の機械分類・合成 score・統計的 signal は作らない。

## 参考

- [`../doctrine.md`](../doctrine.md)：思想・柱 2（macroとAIの責務境界）
- [`./screening.md`](./screening.md)：material deltaとas_of鮮度warningを出すselect
- [`../reference/data-sources.md`](../reference/data-sources.md)：データソース Tier
