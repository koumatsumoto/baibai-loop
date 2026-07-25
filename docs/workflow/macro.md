---
title: "Workflow — macro analysis"
summary: "マクロ環境分析：指標と一次情報から、世界・日本の局面と投資接続を8セクションのmacro-context reportに残す。"
doc_type: workflow
status: active
last_reviewed: 2026-07-20
---

# Workflow — マクロ環境分析

マクロ環境分析は **独立した機能のまとまり**（データ取得層 + リサーチの実践）であり、形式化した独自ループにはしない。狙いは、個別銘柄の5年期待値を変え得る外部経路と共通riskを判断層へ供給すること。sector順位、相場方向、買い時、投入額を決めない。

扱うものは性質の異なる 3 種：**① データ（事実）／ ② 環境認識（macro-context record）／ ③ 知見（調べ方のメタ知識）**。マクロは標本数がほぼ 1 の判断であり、優位性の数値・統計的有意性・自動の投入額倍率は出さない（§誠実性）。

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

`get` は取得済み範囲のキャッシュを確認し、不足があるときだけ provider を呼ぶ。同じ入力には同じ出力を返す（決定論）。`get --latest` は frequency 別の鮮度窓（daily は 1 暦日、weekly は 14 日、monthly は 70 日）内の cache があればそれを返し、古い場合は最新確認用の短い窓（daily は 14 日、weekly は 60 日、monthly は 370 日）を provider で再取得する。環境認識を書く直前は、判断に使う主要 series を `refresh` で直近窓ごと再取得してから `get --latest` を読む。

`refresh --all-history` は provider ごとの取得可能な先頭日から強制再取得する。FRED 系列は現在の `fredgraph.csv` が返す先頭日を再現可能な境界とし、その日より前の観測を残さない。各系列の observation は registry の `source_url` と一致する cache だけを保持し、同内容の連続 vintage は provider run に取得記録を残して observation から除く。値・単位・期間・取得状態・source が変わる revision と、値が変化して同じ水準へ戻る revision は保持する。JP provider の契約期間や公表 archive が先頭日を制限する場合は、実際の取得範囲と制約を運用記録へ残す。

observation は `(series_id, observed_at, vintage_at)` を主キーに upsert する。多くの provider は取得時刻を vintage として刻むが、挿入前に vintage を除いた内容（値・単位・期間・取得状態・source）を既存最新 vintage と比較し、変化が無ければその再取得行を捨てる。したがって **同じ refresh を何度実行しても、ソースが改定した series の観測だけが新 vintage として増え、それ以外はテーブルが不変**になる（べき等）。ローカルで `--all-history` seed → cloud で日次 refresh、cloud の多重実行や手動 rerun も同じ性質で安全に収束する。日次バッチは asof を終端とする frequency 別の窓（daily 14 日・weekly 60 日・monthly 以下 370 暦日）を毎回丸ごと再取得するため、窓内で起きた一時的な取得失敗は次の成功実行が同じ窓を引き直して自動でバックフィルする。窓を超える長期の取得断や旧 vintage の全面リベースが必要なときだけ `refresh --all-history` を運用レバーとして使う。

### データソース registry

| Provider | 取得 | 担当ドメイン | 確認手順・既知の caveat |
| --- | --- | --- | --- |
| `fred_csv` | 無認証 CSV | 米マクロ・実質金利/期待インフレ・FX・原油・VIX・クレジット OAS・BTC・流動性・NFCI・JP 失業率/賃金/実質実効為替 | 系列 ID を `fredgraph.csv?id=<ID>` の header で実 fetch 確認。ICE BofA OAS は直近 3 年、S&P / Dow は直近 10 年が現在の配信範囲。**廃止系列あり**（JP OECD CPI は 2021 停止、金 LBMA は 2025/5 停止）。金・SOX は `yahoo`。 |
| `frb_h15` | 無認証 CSV | 米国債金利・スプレッド | 1 package を series 横断に 1 回 DL |
| `ecb_fx` | 無認証 ZIP | JPY クロス（USD/EUR/AUD） | JPY と基軸通貨の比で算出 |
| `estat` | API（`ESTAT_APP_ID`） | JP 公式マクロ（CPI 総合・サービス、鉱工業生産 等） | JP CPI の一次ソース。`statsDataId` と分類 code は e-Stat で確認 |
| `jquants_flows` | 認証（`JQUANTS_API_KEY`） | JP 市場内部（海外投資家フロー） | screening と同じ Light credential。`--all-history` は運用日から5年の契約窓を要求する。集計週末を observation、公表日を vintage として同一公表日の複数週を保持する |
| `boj` | 無認証 xlsx | BOJ 長期時系列（マネタリーベース 等） | `mblong.xlsx` を openpyxl で読む |
| `boj_timeseries` | 無認証 JSON API | BOJ 無担保コール O/N 平均 | `FM01:STRDCLUCON` の日次値を一括取得する。公表タイミングは BOJ 時系列統計データ検索の更新日に従う |
| `mof_jgb` | 無認証 CSV | JP 国債金利（主要年限） | `jgbcm_all.csv` と当月 `jgbcm.csv` を CP932 で読み、和暦の基準日を ISO date に正規化する |
| `tsr_bankruptcies` | 無認証 JSON API | JP 企業倒産件数 | 東京商工リサーチの掲載ページが参照する公式 JSON から月次全履歴を取得 |
| `spglobal_pmi` | 無認証 PDF（requests→browser fallback） | S&P Global PMI（日本/米 製造業・サービス業） | free の data API が無い。`providers/pmi_release_urls.yaml` の月次 release URL から公式 PDF を取得し、headline 値を bounded context から抽出して 30〜70 の妥当域で検証する。WAF gated の月は headless browser（Playwright）で fetch する |
| `yahoo` | 無認証 JSON | 金/銀/銅先物・MOVE・Russell2000・SOX 等 | **ブラウザ UA 必須**（default は 429）。`provider_series_id` は Yahoo シンボル |
| `multpl` | 無認証 HTML | S&P500 バリュエーション（CAPE・GAAP PER・益回り） | current page と public monthly table を機械的に parse する。HTML 構造変更で壊れるため `--latest` と `--all-history` を live 確認 |

新ソース追加＝provider モジュールを 1 つ足して（`providers/` に 1 ファイル）`providers/registry.py` に 1 行登録し、series を registry（`indicators/registry/` の region 別 yaml）へ 1 entry 加える。provider の取得能力（all-history 起点・store 書き換え方針・refresh 可否・point-in-time vintage・必要 env）は各 provider の `ProviderSpec` が宣言し、service / store は provider 名で分岐しない。1 series_id = 1 provider を厳守する。provider 取得は一時的な `IndicatorsProviderError` を 1 回 retry し、再失敗した場合は `provider_runs` に failed として記録する。

`macro refresh` は複数 series を 1 pass で取得し、1 series の失敗は他 series を止めない。失敗した series は最後にまとめて stderr へ列挙し、exit code は非 0 になる（1 つの壊れたソースが同一グループの残り全系列を stale にしない）。1 pass は 1 つの fetch context を共有するので、複数 series が同じ bulk ファイルを参照しても download は 1 回、browser fallback を要する provider の起動も 1 回で済む。取得値は store へ入る前に有限値であることを検証し、NaN / ±inf は取得失敗として扱う（派生計算・percentile・export を汚染させない）。

PMI は data API が無いため、月次 release URL の manifest（`src/baibai_engine/macro/indicators/providers/pmi_release_urls.yaml`、`schema_version: 2`、PMI stream ごとに `observed_at` → 公式 release URL）を正本とし、`spglobal_pmi` provider が各 URL の公式 PDF を live 取得して headline 値を抽出する。抽出値は 30〜70 の妥当域で検証し、複数候補が矛盾する月は取得を失敗させる（誤った値を store に入れない）。release URL の validator は `https://www.pmi.spglobal.com/Public/Home/PressRelease/<32 hex>` だけを許可する。

1 月 = 1 PDF なので、通常の refresh は store に無い月だけを取得する。final headline は公表後に改定されないため、store 済みの月を取り直しても同じ値になる。抽出規則の変更後など stream を source から作り直すときは `refresh --all-history` を使う（全月を再取得する）。

**新しい月の追記は人手運用**: S&P Global の公式 press release ページから該当月の release URL を特定して manifest へ追記し、`macro refresh` で該当月を取得して妥当域検証と公表値の照合を通してから commit する。manifest が公表カレンダーに追いつかない（release 済みの月が manifest に無い）状態は取得の無音の停止になるため、provider が明示エラーで失敗させる: 要求 end の月に対して manifest 最新月が前月に達していないとき（当月 10 日以降）にエラーになり、日次バッチの繰延べ失敗として表面化する。

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

## ② 環境認識：macro-context revision を publish する

市場局面についての、日付と出所の明確な環境認識は application DB の immutable revision として残す。機械契約は `baibai_engine.macro.models.MacroContextDocument`、唯一の書き込み経路は `baibai-engine macro context publish` である。既存 head を読んで draft を作り、2件目以降は `--expected-head` にその ID を渡す。head が変わっていれば publish 全体が無変更で失敗する。

```bash
uv run baibai-engine macro context head
uv run baibai-engine macro context publish /tmp/macro-context-draft.yaml \
  --expected-head macro-context-2026-07-01-example
uv run baibai-engine macro context show --latest --asof 2026-07-19
```

report の共通 field：

- `context_id` / `as_of` / `valid_until` / `published_at`。`as_of`は**市場データの最終完全営業日**にする（著述日ではない）。screening selectはpoint-in-time整合のため`as_of ≤ selection ASOF`のcontextだけをbindするので、週末・祝日に書くcontextの`as_of`を著述日にすると直近ASOFのselectへ恒常的にbindされない
- `inputs.articles`：外部記事の一意な`input_id`、source / title / url / published_at / accessed_at / status / used_for（記事本文や監査ログは保存しない）
- `inputs.indicator_series`：一意な`input_id`、`baibai-engine macro`で確認したprovider / series / window / observation_as_of / status / used_for
- `sections`：下表の固定順8セクション。各セクションは`series_ids`、source付き`fact_summary`、方向・確度・source付き`judgment`、source付き`investment_connection`を持つ
- `sections[0].change_since_previous`：Regime summaryで比較可能なpublished contextからの変化を示す。比較対象がない場合はその旨を示す
- `material_deltas` / `sizing_cautions`：セクション2〜7の判断として置く。material deltaはchannel / direction / materiality / used_for、sizing cautionはseverityを持つ
- `scenarios`：セクション7にbase / bear / bullの固定順で置き、成立条件と投資上の含意を分ける
- `monitoring_points`：セクション8にevent、条件、条件成立時の見方の変更を置く

### 8セクションの作成順

| 順 | セクション | 確認するfact | judgmentと投資接続 |
| --- | --- | --- | --- |
| 1 | Regime summary | 成長・インフレ・金融条件の水準と方向、比較可能な時点からの変化 | 現局面を一文で定め、以降の読み順を示す |
| 2 | 金利・金融政策 | 政策金利、イールドカーブ、実質金利、主要中銀の方向 | discount rate経路とmaterial deltaを示す |
| 3 | 景気・需要 | PMI、雇用、消費、生産、景気breadth | セクター需要とresearch着手順への含意を示す |
| 4 | インフレ・コスト | CPI、賃金、輸入物価、commodity | 売価転嫁とmargin経路を示す |
| 5 | 為替・流動性 | USD/JPY、金利差、net liquidity、credit OAS、VIX/MOVE | risk appetite、funding、共通tail riskを示す |
| 6 | 日本固有 | BOJ政策、国内賃金物価、鉱工業生産、海外投資家フロー | 日本企業の需要・費用・為替感応度への接続を示す |
| 7 | シナリオと接続 | セクション2〜6を支持・反証する系列、市場内部（`screening market-snapshot`のbenchmark 20d/60d・breadth・regime）、日本株バリュエーションアンカー | base / bear / bull、バーゲン地形、sector tilt、research優先度ヒント、sizing cautionを判断面に置く |
| 8 | 監視ポイント | 次の公表・会合と観測条件 | 何が出たらどの見方を変えるかを明記する |

### Decision-grade 深度契約（opportunity cycleの前提）

macro contextの用途は2つあり、深度要件が異なる。**delta更新**（monitoring condition発火やmaterial change時の部分的な見直し）は変化した経路の事実確認で足りる。**decision-grade context**（opportunity cycleのshortlist作成が前提にする環境認識）は、銘柄選定のリスクリワード判断の土台になるため、次の深度契約を満たす。

- **テーマ被覆**: 金利・政策 / インフレ・コスト / 需要・雇用 / 為替・流動性・credit / 日本の政策・金利 / 日本の需要 / energy・地政学・通商 / 市場内部・バリュエーション の8象限すべてにfactを置く。`inputs.articles`はTier-1中心に15本以上。
- **日本の需要fact最低ライン**: セクション3または6に、実質賃金（毎月勤労統計）または実質消費、鉱工業生産を必ず含める。取得可能ならインバウンド（訪日外客数）・機械受注も置く。米国factだけで需要判断を組み立てない。
- **円水準の両側リスク**: セクション6に、円安継続と円反転（介入・利上げ）の両経路が輸出企業（為替換算益の剥落）と輸入コスト企業（margin回復）へ与える非対称を1つのjudgmentとして書く。片側の監視条件だけで済ませない。
- **バーゲン地形**: セクション7に`screening market-snapshot`のbenchmark 20d/60d・breadth・regimeをfactとして引用し、「この局面でミスプライスがどこに出やすいか（全面安で広く出る / 回転相場で取り残しに出る / 全面高でプールが縮む）」をjudgmentとして書く。
- **日本株バリュエーションアンカー**: セクション7に市場全体のPERまたは益回り（日経・JPX公表の一次値、または全universeのin-house中央値）とJGB 10yの対比を置き、個別FVアンカーの妥当性を外側から検算できるようにする。
- **hintの識別力**: 全候補に等しく当てはまる助言（「net cash重視」等）はhintではない。各research_priority_hintとsector tiltは、どの候補タイプ・sectorに効くかを判別できる形で書く。
- **energy・通商・地政学**: セクション4または5に、原油と通商政策（関税）・地政学tailのfactを最低1つずつ置く。

published contextがこの契約を満たさない、またはas_of以降にmonitoring pointのdated eventを跨いだ場合、opportunity cycleはshortlist作成前にdecision-grade refreshを行う。

各`series_id`はaliasではなくseries定義のcanonical IDを使って`inputs.indicator_series`にも置き、各要約・判断・接続の`source_ids`をinputへ結ぶ。series定義にないID、inputにないseries参照、正常取得した同系列inputを引用しないセクション、failed inputを引用する判断はpublishされない。変化がmaterialでないセクションも省略せず、確認したfactと「見方を維持する条件」を記す。

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

**revision は分析レイヤーであり、手順（作業の指示）を書かない**。「次回からこう調べる」といった手順の話は本 doc（workflow）に置く。revision には、screening / research の前提として使う環境認識と出所のメタデータだけを残す。

**分析の独立性**：環境認識の前提にしてよいのは過去の客観的事実（価格・指標・イベント）だけで、過去のmacro-context revisionにある分析・結論は前提にしない。保有中の建玉も分析に持ち込まない。一次情報と指標から、解釈を毎回ゼロベースで組み立てる。比較可能な時点からの変化は、結論を確定させた後にRegime summaryのfactとして接続する。

**更新のきっかけ**：macro-contextは定期的には生成せず、(a) discount rate・需要・資金調達・共通tail riskにmaterial changeがあったとき、(b) published contextのmonitoring conditionが発火したとき、(c) opportunity cycleの前提となるcontextが[Decision-grade 深度契約](#decision-grade-深度契約opportunity-cycleの前提)を満たさないとき、のいずれかで更新する。unchanged専用recordは作らない。`valid_until`はwarningの材料であり、screeningの前提条件ではない。triggerの選択と全体導線は[`../operations/decision-cycle.md`](../operations/decision-cycle.md)を正本とする。

## ③ ナレッジ：8 分析レンズ

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

- **select**（[`./screening.md`](./screening.md)）：material deltaとwarningをcontext-level summaryとして出す。E[r]順位とcandidateの事実層は変えない。
- **research**：material deltaが個別5年期待値へ影響する場合だけ、thesisのjudgmentへその因果と根拠を残す。マクロを数値ドライバー、採用gate、投入額ルールにはしない。

## 誠実性（honesty firewall）

マクロは標本数がほぼ 1 であり、screening のように多数の銘柄を横断する統計検証ができない。この工程は優位性の数値・統計的有意性・自動売買スコアを出さない。ここで得られるのは再現性と、判断を事実に根付かせる基盤であって、統計的な厳密さではない。

## 参考

- [`../doctrine.md`](../doctrine.md)：思想・柱 2（macroとAIの責務境界）
- [`./screening.md`](./screening.md)：material deltaをwarningとして出すselect
- [`../reference/data-sources.md`](../reference/data-sources.md)：データソース Tier
