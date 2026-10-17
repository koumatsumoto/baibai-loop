---
title: "Market Context reference"
summary: "Market Contextの保存済み市場・個別相対の読取、経済観測・Readingと明示作成する市場環境判断、保存履歴の契約。"
doc_type: reference
status: active
---

# Market Context — 指標・Reading・判断・保存履歴

本書はMarket Contextの市場・個別相対の読取、経済指標・Reading、明示的assessment、過去に保存された判断本文の読取契約を所有する。個別企業の価値評価は[Valuation metrics](./valuation-metrics.md)と[Stock Assessment](./stock-assessment.md)が所有する。

Collectionの`economic_sources`・`economic_providers`・`economic_acquisition`がL1のsource定義と外部取得を所有する。Market Contextの`indicators`はL2系列の定義、`reading`は保存原本の選択・計算を所有し、`indicator_service.py`と`indicator_cli.py`は読取専用である。L3の統合判断と履歴は`research/market_context`が所有する。L1はL2の列選択・単位・数式・妥当範囲に依存しない。

日次工程はL1観測とL2 Readingを更新し、人間の明示開始でMarketContextAssessmentを作成する。新判断は下記assessmentの最小契約とreview手順に従う。Market Context全体へこのレポート作成を必須にせず、保存済み機械状態と発行済み判断は個別に読める。

## 現行配置と保全するidentity

経済データの原本storeはCollection、現在のL2定義は`indicators/registry/`、決定的な読取は`reading/query.py`、明示判断は`research/market_context`が所有する。Web/batchの公開読取は`read_api/market_context.py`へ統合する。Webの現在の内容は`AssessmentContent`、過去の章・確率・主張・引用は歴史的本文の表示であり、新writerの必須構造ではない。

runtimeの指標DBは`stores/market_context/indicators.sqlite`/R2 `indicators.sqlite`（schema12、経済sourceの原本・取得結果のみ）、判断はapplication DBの`market_context_assessment / market_context_assessment_head`に保存する。新規判断はschema6・`market-context-assessment-*` identityを使う。旧`macro-context-*` ID・JSON・引用・hashは変更せず、新しい`/api/market-context/assessments/{context_id}`から直接読む。schema5/6は同じコンパクト意味、旧3/4は保存された章立て履歴であり、新writerの構造とは分ける。Researchは保存された市場判断を固定入力へ取り込み、Investment Proposalがmarket_context_assessment_idへ束縛する。旧ResearchのTriage/CAAは通常runtimeで利用・再開せず、一回切替で必要な執筆材料と判断参照を退避する。保存名の切替は完了しており、通常の取得・反映・復旧は[運用手順](../../batch/OPERATIONS.md#store-identity-recovery)に従う。

Readingは`READING_RULES_PATH`が選ぶ一つのdated fileを使う。保存判断が引用する`method/macro/reading/2026-08-01T100000+0900.yaml`は数値・fieldの意味・dated pathを保全し、別のcurrent copyを作らない。計算を変えない説明訂正は可能とし、旧bytesはgit履歴で確認する。規則のidentityはdated stemであり、単なる配置整理で値・revision・schemaを変えない。

<a id="market-context-reads"></a>

価格騰落・標本volatility・流動性・安値乖離・信用需給の純粋算術は`market_context/{history,margin}`が所有する。保存価格の指定日からの騰落・期間内下落は`market_context/prices`が所有し、外向け`read_api/market`は同じ処理を公開する。価格読取も計算も保存された価格調整係数を使う共有算術を参照し、倍率の価格感応履歴/自己レンジ・母分散sigma gapはValuationに従う。観測窓と比較母集団の意味は変えない。

通常Researchでは保存済みReading・市場/業種/個別状態を[調査入力](./analysis-operations.md#明示開始と固定対象)へ接続し、materialな含意をAssessment/提案へ記録する。判断の新規作成は明示開始に限り、通常Researchは必要な環境上の含意を保存情報から接続する。

## Market Contextの保存済み読取

Market Contextは経済、市場全体、業種と個別株の相対状態を扱う分析領域である。L1観測、L2機械状態、L3環境判断を区別し、現行のMarketContextAssessmentと、旧Macro Contextを含む保存判断の履歴を参照する。領域読取は`market_context`、Web/batchの公開facadeは`read_api/market_context`が所有する。取得・書込・AI起動を行わず、欠測を別領域の障害や「変化なし」へ置き換えない。

市場・業種・個別の異なる統計をまとめて一つのscoreにはしない。

| 出口 | 統計・対象 | 意味と欠測 |
| --- | --- | --- |
| Stock Analysisの業種relative strength | Screeningの既存比較母集団。20 bar騰落の業種平均－市場平均と業種順位 | `market_context/sector`が計算し、Screeningが入力母集団・価格窓を供給する。同値の業種は平均順位を母集団数で割った同じ百分位を持つ。個別対業種の差ではない |
| market snapshot | benchmarkの週次20/60 bar騰落・20 bar平均超のbreadth、同じ観測日の銘柄による業種中央値 | 1321 proxyの±3%方向注記は広いリスク許容判断ではない。breadth・sector・業種peerは普通株masterに限定し、ETF等を混ぜない。週次breadthは各時点以下の最新masterを使い、将来の構成を遡及適用しない。最小標本数を満たさないbreadthは未評価 |
| security market context | 指定銘柄（候補外も可）の価格・benchmark比較と、同業peerの20 bar中央値 | 個別とbenchmark/peerは指定銘柄の同じ開始・終了日を使う。peerは自銘柄を除き、端点不足を補間・前日値で埋めない |
| Reading / MarketContextAssessment・保存履歴 | 保存済み経済観測・Reading規則、保存済み判断 | 観測/公表vintageの契約を維持する。判断は`as_of / known_at`で適格性を判定し実発行時刻で選ぶ。固定IDの履歴閲覧は旧schemaも本文のまま返す |

個別対業種の`relative_return_20d`は個別の価格騰落－peer中央値で、ratioの差（0.1＝10 percentage points）である。`comparison_start_date / comparison_end_date / master_snapshot_date`を併記し、peer数と比較基準を示す。市場・業種が上昇し個別が下落しても、乖離だけから割安と認定しない。財務・利益・企業価値の算術はValuationが所有する。

masterはrequested cutoff以下の最新の一つのsnapshotを集合ごと選ぶ。そのsnapshotにいないtickerを古いsnapshotから復活させず、未来分類を使わない。価格はcutoff以下のraw closeを保存済み価格調整係数の後方累積で揃え、closeがnullの価格調整eventも落とさない。現金配当込みreturnではない。価格の観測日、requested cutoff、master snapshotの日は同一とは限らないため、各基準日を見て読む。

既存CLIは次の領域計算を参照する。

```bash
uv run baibai-engine screening market-snapshot --asof YYYY-MM-DD --weeks 12
uv run baibai-engine screening ticker-profile --ticker <TICKER> --asof YYYY-MM-DD
uv run baibai-engine market-context reading --asof YYYY-MM-DD --format json
```

`ticker-profile`はMarket Context、保存済みStock Analysis、event、portfolioを合成する。市場・分類とrunのcutoffは要求日を使うが、portfolioは現在のledger documentを独立の`ledger_as_of`で表示し、要求日への保有履歴再生は行わない。profile全体を完全な過去時点再現とは扱わない。既存の`price.self_range_percentile`は価格観測の順位割合で、Valuationの`self_range_position`（min/max内位置）と別の量であり、窓は合成callerが既存methodから渡す。

日次の指標独立公開、有限AI調査・提案への接続、環境判断の深度は固定せず、materialな根拠・反証・含意に合わせる。以下にL1/Readingとassessmentの契約を示す。

## ① データ：indicator series を引く

`list` / `search`はL2 registryだけを読み、storeの作成・更新・schema検査を行わない。`get`も保存原本からの読取専用である。取得・更新はCollectionの明示操作とし、手順は[`指標履歴`](../../batch/OPERATIONS.md#指標履歴)、系列定義の変更は[`指標観測とregistryの修正`](../../batch/OPERATIONS.md#指標観測とregistryの修正)を参照する。

```bash
uv run baibai-engine market-context indicators list --category rates       # 登録 series を見る
uv run baibai-engine market-context indicators search 失業率              # 名前/alias/category で検索
uv run baibai-engine market-context indicators get jp.nikkei225 --start 2026-05-20 --end 2026-06-22
uv run baibai-engine market-context indicators get jp.policy_rate --latest
```

`get`は不足を外部取得せず、保存入力から計算できる値だけを返す。`get --latest`はJSTの運用日を終端とし、外部系列は日次14日・週次60日・その他370日の読取窓、派生系列は保存入力の全期間から最新値を選ぶ。これは[reading rules](../../engine/src/baibai_engine/market_context/reading/rules.py)の公表ラグ・鮮度閾値とは別である。計算の決定性は保存入力・採用revision・規則・as-ofが固定された場合の性質であり、取得時刻を歴史的な公開可能時刻とは扱わない。

`collection economic refresh <source_id> --all-history --end <date>`はsourceごとの取得可能な先頭日から明示取得する。通常のnative/document sourceは再取得し、オプションは保存済みの古い日を再利用するため[専用手順](#jquants-options-provider)に従う。全L1の新規取得と同じ意味ではない。正常に調べた要求窓の空日は空原本として保持し、失敗応答を空日にはしない。契約期間や公表archiveによる制限と実際の取得範囲を記録し、取得できない期間を旧scalarで補わない。

`derived`系列は[Readingの純粋な数式](../../engine/src/baibai_engine/market_context/reading/formulas.py)で保存入力から読む都度計算する。米10Y−2YスプレッドもH.15の利回りからL2で計算し、取得処理では差分を保存しない。派生系列は独立したsource取得の対象ではなく、`get`も入力を再取得しない。欠測や未定義の計算を古い派生値で補わず、式変更のために原本を再取得したり保存済み判断本文を書き換えたりしない。

ネット流動性の入力単位は[WALCL](https://fred.stlouisfed.org/series/WALCL)・[WTREGEN](https://fred.stlouisfed.org/series/WTREGEN)が百万USD、[RRPONTSYD](https://fred.stlouisfed.org/series/RRPONTSYD)が10億USDなので、`(assets - tga) / 1000 - rrp`とする。RRPを再度1,000で割らない。WALCLは水曜時点、WTREGENは水曜を期末とする週平均、RRPは日次であり、この組合せは近似指標であって同時点の厳密な残高や投資可能資金の測定ではない。

月次整列は月内の各入力の最終観測を使い、出力日は数式ごとの期間代表日または採用入力の最終日とする。これを実公表時刻や歴史的なAPI利用可能性の保証とみなさない。計算値のvintageは全入力に時点がある場合の最も遅い入力vintageであり、読んだ時刻を新しい公表として作らない。L2の数式変更に派生履歴の退役・再取得・置換を要求しない。オプションの3系列も保存済み原本をL2で計算する。取得は固定SDKの認証・再試行付きGETを使い、各ページのnative JSON recordをDataFrame化せず読む。空のdata配列と、dataの欠落・不正形状・途中ページ失敗を区別する。日次chainの全項目・欠測・識別子をgzip JSONとして保持し、現在の3指標が使わない項目も残す。HTTPの元bytesを保存する契約ではない。

`fred_csv`・`cftc`・`ecb_fx`・`jquants_flows`・`jquants_indices`・`estat_dashboard`・`estat`・`frb_h15`・`multpl`もnative recordを`economic_source_records`へ保存し、L2が必要な値を選択・計算する。CFTCのlong/short、ECBの全通貨、投資部門別の全項目、TOPIXのO/H/L/Cと欠測を保持し、未使用の値も現在の計算結果へ潰さない。正常なbulk応答は要求範囲の空日も記録するが、失敗した応答を空日として保存しない。計算不能でも取得した原本は残し、旧scalar観測へfallbackしない。

e-Stat dashboardのsource identityはIndicatorCodeだけではなく、頻度・季節調整・地域を固定するsource URLである。要求窓のVALUE行は速報・null記号・未使用属性もそのまま残し、L2が`@isProvisional=0`の確報だけを選ぶ。確報の欠測から旧値や速報へ戻らない。単位コードの混在は読み取りに使う期間の確報間で検証し、要求窓外の単位差だけで読み取りを止めない。取得時刻は公表時刻ではない。VALUEの単位コードだけで基準年を復元できるとは限らず、系列の意味はregistryと[公表元の系列メタデータ](https://dashboard.e-stat.go.jp/api/1.0/Json/getIndicatorInfo?Lang=JP&IndicatorCode=0302030202010090010)に従う。名目賃金の単位コード000は単位名が空欄であり、2020年平均=100という意味は系列注記から確認する。旧scalar値だけから失われた速報・属性は復元せず、マージ後に必要な原本を取得する。

multplは取得窓の長短でcurrent pageと履歴表を切り替えず、日付付き月次表を使う。最新行の日付は取得日ではなく公表元の表示日とし、日付・値のセル文字列、推定記号の注記、表のcaption、出典説明とリンクを保存する。L2は比率と百分率を区別し、同日で矛盾する値を行順だけで採用しない。現在の月次表から、過去に取得した日々のcurrent値までは再構成できない。PE・益回りの最新値には推定が含まれ、GAAP報告利益を基礎とすることは、その観測が確定値である保証ではない。

FREDはsource URLと列IDをsource identityとし、CSVの全列・文字列・`.`・空欄を残す。L2が登録列を数値化し、最新セルが欠測なら旧値へ戻らない。異なる数値の同日duplicateは計算不能として残す。旧scalar履歴から欠測セルや未使用列を原本として作り直さない。

H.15は利回りごとではなく、同じ公開endpointの共通packageをnative sourceとして保存する。日付別CSV行の全列・欠測記号・header前の説明行を保持し、L2が登録された年限の列をpercentとして読む。列の欠落や読めない値は保存後の読み取りで明示し、過去のscalar値へ戻らない。header前の説明は原形のまま残し、未確認の書式から単位や公表時刻を推定しない。

通常のe-Stat APIも、要求した表・分類条件に属する月次VALUEセルをnative保存する。年計など月次系列ではないセルを混ぜず、元の時点コードと単位・欠測・注記属性は保持する。source identityは公開endpointとstatsDataId・絞り込み条件で、認証appIdは保存しない。最新セルが欠測なら旧scalar値や同月の非欠測duplicateで補わない。異なる数値のduplicateやReading期間内の単位混在は計算できない入力として扱い、取得済みセル自体は残す。

投資部門別の取得範囲とsnapshotの日付は`PubDate`である。`StDate/EnDate`は集計期間として原本に残し、L2が要求した公表日cutoff以下の最新版を先に選ぶ。読み値の観測日は`EnDate`、vintageは`PubDate`のUTC日付表現であり、実際の配信時刻ではない。公表日と観測期間の終端は別なので、後日訂正を過去の集計期間へ反映できる。最新の`FrgnBal`が欠測なら買い−売りや旧公表値で埋めず、同一公表日に矛盾する値があれば任意の行を採らない。通常の範囲取得は終了日までの公表を対象とし、保存済みread-only照会は必要に応じて別の公表日cutoffを使う。取消の意味が不明な項目から、他の公表日の削除を推測しない。

経済storeは`economic_source_records`（native record JSON）、`economic_source_documents`（元PDF/JSON等のbodyとmetadata）、`economic_source_runs`（source単位の取得結果）の三表である。L2系列・alias・scalar観測・registry世代は複製しない。各writerは直前と同内容の原本を重複保存せず、内容が変わった取得は保持する。同じsource・日付またはrequest key・UTC取得時刻の再保存は同内容ならno-op、異内容なら拒否し、既存原本を上書きしない。[records](../../engine/src/baibai_engine/collection/economic_records.py)・[documents](../../engine/src/baibai_engine/collection/economic_documents.py)が保存identityを所有する。

読取は各日の最新取得原本を採る。最新原本が空・欠測・計算不能でも旧原本や旧scalarへ戻らない。取得失敗は既存原本を消さず、完了したdocumentやoption dayは後続失敗でも残る。nativeの要求窓はまとめて保存する。取得結果の記録に失敗しても、すでにcommitした原本を取り消さない。source取得の成功と、特定L2系列を計算できることは別の事実である。

registryは現在のL2定義の正本であり、追加・退役・改名でsource factsを削除しない。系列集合の世代台帳やscalar撤回操作は持たない。cloudとのmergeは三表の事実を統合し、同じidentityで異内容なら拒否する。誤った保存をlocalだけで削除してもmerge先には残るため、原本の訂正取得または明示的なstore再構築を行う。過去の発行済みContext本文は変更しない。

有限値・単位・系列固有の妥当範囲はL2の計算結果へ適用する。明白な列・桁・単位ずれを検出する条件であって、境界外だけで原資料が誤りとは確定しない。元の欠測記号や未使用項目は原本に保持する。例えば`jp.foreign_flows`のL2値は原典の千円単位を使う。計算できなければ原本・列選択・規則を確認し、source取得失敗へ読み替えない。

[store](../../engine/src/baibai_engine/collection/economic_store.py)と[schema](../../engine/src/baibai_engine/collection/economic_store.sql)は空の新規storeまたは現行schemaを受け付ける。通常openやtransferに旧schema移行を残さない。read-only validatorは全取得revisionのnative payload形状・件数とdocument metadataを検査し、L2の値域や計算結果は判定しない。式・band・source選択の変更時は、保存原本を使った該当L2読取とReadingを別途確認する。

同じvalidatorはapplication storeの判断履歴も別の契約として検証する。旧schemaの本文はstored_onlyとして保持し、退役系列の引用はwarningとする。候補一覧発行はContextを読まないため、その失敗を候補一覧停止と読み替えない。実行条件は[CIと必要な実store検証](./python-foundation.md#9-ci-and-local-parity)、変更手順は[運用手順](../../batch/OPERATIONS.md#指標観測とregistryの修正)に従う。

### データソース registry

外部provider・source URL・要求selectorは[Collection source catalog](../../engine/src/baibai_engine/collection/economic_sources.yaml)、L2系列ID・出力単位・読み取る列・数式は[registry](../../engine/src/baibai_engine/market_context/indicators/registry/)が所有する。取得能力と認証条件は[ProviderSpec](../../engine/src/baibai_engine/collection/economic_provider_specs.py)と各[provider](../../engine/src/baibai_engine/collection/economic_providers/)に従う。複数L2系列が一つのsourceを共有してよい。下表はsourceを選ぶときの用途と制約を示す。

| Provider | 用途 | 選択・運用上の制約 |
| --- | --- | --- |
| `fred_csv` | 米マクロ、金利、FX、信用、市場系列 | 系列ごとに配信開始・終了や履歴窓が異なる。relay停止とsource停止はstore上で区別できないため、公表元が機械可読配信を持つ場合は直読を優先する |
| `frb_h15` | 米国債金利・スプレッド | 遮断時だけbrowserで再取得する。サイズ上限・404・5xx・通信障害は別経路で迂回しない |
| `ecb_fx` | JPYクロス | 全通貨のnative値を保持し、JPYと基軸通貨の比をL2で算出する |
| `cftc` | 投機筋ポジション | 要求contractのAPI全項目を保持し、同じ原本行のlong−shortをL2で算出する |
| `estat` | 日本の公式マクロ | API認証が必要。登録系列の月次VALUEセルは単位・欠測記号・属性を保持し、L2で数値を選ぶ。DB掲載が途中で止まる統計もあるため、新規採用時は更新日と公表元を照合する |
| `estat_dashboard` | 失業率・名目賃金等 | 頻度・季節調整・地域を固定し、VALUEの全属性（速報・欠測・単位コードを含む）を保存する。L2のReadingだけが確報の数値を選ぶ。基準改定時は全履歴refreshで揃える |
| `jquants_flows` | 海外投資家フロー | screeningと同じcredential。公表日単位で全項目を保存し、集計期間と訂正の選択はL2で扱う。単位は千円 |
| `jquants_indices` | TOPIX | native Date/O/H/L/Cを保存し、L2でCを読み値にする。Closeという旧項目で補わない |
| `jquants_options` | 日経225のIV・スキュー・期間構造 | rate limit、式変更、購読窓、緊急取引証拠金日は[専用手順](#jquants-options-provider)を参照する |
| `boj` | 日本銀行の月次系列 | workbookの全列・元の日付・先頭metadata・欠測を保存し、L2で値列とheader・単位を照合して読む |
| `boj_timeseries` | 日銀の時系列統計 | `SURVEY_DATES`は対象期間。月次は月初、四半期は最終月の月初を内部の代表日とするが、実際の観測日や公表日時とは扱わない（[公式API仕様](https://www.stat-search.boj.or.jp/info/api_manual_en.pdf)） |
| `mof_jgb` | 日本国債金利 | 公表CSVの全列・和暦・欠測を保存する。L2が当月ファイルを履歴より優先し、欠測訂正も反映する |
| `tsr_bankruptcies` | 日本の企業倒産 | 公表ページが参照する月次履歴の既知値を取得する。欠月を補間せず、履歴の短さや古さはReadingで表示する。未解釈・矛盾する月次値は取得失敗とする |
| `spglobal_pmi` | 日本・米国の製造業/サービスPMI | 公式PDFを読む。release manifest更新は[下記](#pmi-release-manifestの更新)。遮断時だけbrowserを使う |
| `umich_sca` | 米消費者態度 | 公表元の月次表の全列・空欄を原本として保存し、L2で対象列を数値化する。欠測への訂正で古い数値を復活させない |
| `yahoo` | 金属先物・MOVE・Russell2000・SOX等 | 登録URLのchartを取得し、timestamp・OHLCV等の配列・metadata・nullを保存する。L2で終値を読む |
| `multpl` | 米株バリュエーション | current levelと確定済み月次標本を区別する。未公表月を合成せず、readingの統計標本だけ月次化する。HTML変更時は短期窓と全履歴の取得を確認する |

### GETの再試行

`jquants_flows`・`jquants_indices`・`jquants_options`・`fred_csv`は[GET transport](../../engine/src/baibai_engine/collection/economic_providers/read_retry.py)で本文読了までの再試行を所有する。個々のGETは初回＋最大5回で、status retryと本文の一時受信失敗を通算する。有効な`Retry-After`の秒数／HTTP-dateを優先し、不在・不正なら30・60・120・300・600秒で待つ。認証・403・解析失敗やproviderが終了と決めた取得をserviceで再開せず、後続ページの失敗で成功済みページへ戻らない。FREDの8MB上限とsession内のbytes cacheを維持する。他providerの取得方式はこの契約へ一律変更しない。

Collectionの取得処理が1回再試行するのは、停止を表す`ProviderFetchStoppedError`以外の`IndicatorsProviderError`に限る。再試行前に取得contextのbytes cacheを破棄し、再失敗は`economic_source_runs`へfailedとして記録する。GET transportの再試行終了・deadline・providerが確定した停止は`ProviderFetchStoppedError`で伝わり、このCollectionの取得処理再試行へ戻さない。

各providerの最初のGETから20分のdeadlineを同じ`FetchContext`の取得passで共有する。通信時間・本文受信・SDK内待機を数え、新しいGETや収まらない待機を開始しない。待機を短縮して再送せず、次passは新しい予算で始める。SDK/HTTPの通信timeoutは30秒のままで、進行中の通信を強制取消するものではない。日次job全体は90分上限だが、job残時間はcallerから渡されておらず、複数providerと他工程の総時間をこの局所予算だけで保証しない。job残時間の伝達は別の残件とし、永続cooldownや次passへの期限引継ぎは持たない。

<a id="jquants-options-provider"></a>

### `jquants_options`の取得と復旧

#### 取得とrate limit

`jquants_options`は営業日ごとのnative chainを取得し、`economic_source_records`へ取得時刻付きで保存する。空の正常応答も取得済み日であり、解析不能・途中ページ失敗は正常な空日にしない。原本は系列registryから独立したsource factで、同じ日・取得時刻のpayloadが異なるmergeは拒否する。応答内容が直前と同一なら重複payloadを増やさない。

GETの回数・待機は上の共通契約に従う。一回のsource取得passで対象日の予算を共有し、完了した日はその都度commitする。後日の取得停止や別途行うL2計算の失敗で原本を取り消さない。`--all-history`は保存済みの古い日を再利用し、直近14日と不足日を取得する。古い日の訂正は開始・終了を指定して明示取得する。現行購読窓より古い原本も削除しない。通常のrange/latest読取は外部取得せず、空日・不足は保存状態のまま返す。

#### 集計式の変更

L2は各日の最新の取得原本から3系列を計算する。新しい原本が空、または新式が値を出せない場合、過去の原本や旧scalar観測へフォールバックしない。式変更だけのための系列退役・generation変更・API再取得は不要である。入力不足がある場合だけ原本を補う。取得時刻は改訂の選択に使い、過去時点でのAPI公開可能性を保証しない。

同じread snapshot/passでは日ごとの小さな計算結果を共有する。全期間の展開済みJSONや永続的な計算cacheは持たない。L1は緊急証拠金時の別snapshotも含め保存し、どのsnapshot・満期・価格を計算に使うかはL2が決める。計算不能な原本自体を書き換えない。

`iv_30d`と`iv_skew`はSQ直後に構造的に数日欠ける。鮮度判定はreading rulesを正本とし、過去studyの実測値を現行閾値として固定しない。

#### 購読窓と緊急取引証拠金日

購読窓は運用日から10年rollingで、窓外の要求はHTTP 400になる。`--all-history`は運用日の10年前から未取得日を調べる。境界日が提供されない場合は開始日を明示して提供可能日から取得し、窓外を取得できたとは記録しない。

緊急取引証拠金が発動した日はchainが2部返る。発動時の部は前営業日の原資産と平坦なIVを持つため、`EmMrgnTrgDiv`が名指す清算値算出時の部だけを読む。このfieldを持たない行は読まない。

### Providerの追加

新しい外部sourceはCollection catalogへ追加し、必要なら`collection/economic_providers/`に取得処理を実装する。取得能力は`economic_provider_specs.py`に置き、L2側は必要な系列・列選択・数式を別途定義する。既存sourceから新たなL2値を計算できるなら、取得処理を重複させない。公表日を使う読取契約も、正確な歴史的API利用可能性を保証するものではない。

`collection economic refresh`は重複するsource IDを一度だけ処理し、sourceごとの成功・失敗と受領件数を返す。一つのsourceの失敗は他sourceを止めず、最後に失敗をstderrへ列挙して非0終了する。一回のpassではFetchContextを共有する。`economic_source_runs`の件数は原本record数またはdocument数であり、L2観測数ではない。取得の再試行と停止は[GETの再試行](#getの再試行)に従い、L2の値域違反を理由に取得を繰り返さない。

### PMI release manifestの更新

PMI は data API が無いため、月次 release URL の manifest（`engine/src/baibai_engine/collection/economic_providers/pmi_release_urls.yaml`、`schema_version: 2`、PMI stream ごとに `observed_at` → 公式 release URL）を正本とし、`spglobal_pmi` provider が各 URL の公式 PDF を live 取得して headline 値を抽出する。release URL の validator は `https://www.pmi.spglobal.com/Public/Home/PressRelease/<32 hex>` だけを許可する。

抽出は対象月のheadline値だけを読み、sub-index・composite・閾値比較・flash見積り・複数月平均を混入させない。未対応の表現や矛盾する候補は取得失敗とし、値を推測しない。具体的な英文の認識規則と数値検証は[PMI extractor](../../engine/src/baibai_engine/market_context/reading/pmi_extraction.py)が所有する。

1 月 = 1 PDF なので、通常の refresh は store に無い月と、manifest の release URL が store の値の出所と一致しない月だけを取得する。これは同一URLの内容が将来も不変である保証ではない。訂正・差替えや抽出規則の変更を確認する場合は、同じURLでも再取得して照合する。URL を訂正すればその月は自動で取り直される。抽出規則の変更後など stream 全体を source から作り直すときは `refresh --all-history` を使う（全月を再取得する）。

**新しい月の追記は `baibai_engine.market_context.indicators.pmi_manifest` で行う**: S&P Global の公式 press release ページ（`/Public/Release/PressReleases`）が列挙する最新 release を stream ごとに拾い、release title の完全一致で系列を同定し、**release PDF が自分をその PMI と名乗り・自分の embargo 日が index の公表日と同じ月であり・headline をその月に結び付けていること**を 3 つとも確かめてから追記する。抽出には「その release 自身の月」として渡さず後の月を渡すので、月を明示しない reading（`posted 54.8`）は採られず、月を名指した reading（`posted 54.8 in June`）だけが通る。title を別の PMI に取り違えても月の証明だけは通ってしまうので（services も manufacturing と同じ形で headline を述べる）、系列は PDF 自身の名乗りで、公表月は PDF 自身の embargo 日で確かめる。**月が飛ぶ追記は拒否する**: provider の追いつき guard は manifest の最新月しか見ないため、穴を越えた entry を書くと飛ばした月が二度と報告されない。edge WAF が plain HTTP を challenge page（200）で返すときは、PDF 経路と同じ headless browser で index を読み直す。書き込みは copy を text 編集して provider の loader で読めることを確かめ、通ったものだけを正本へ move する（正本への書き込みは検証後の move 1 回だけ）。追記後は `collection economic refresh` で該当月を取得して公表値と照合してから commit する。

```bash
uv run python -m baibai_engine.market_context.indicators.pmi_manifest --dry-run   # 何が追記されるかだけ見る
uv run python -m baibai_engine.market_context.indicators.pmi_manifest             # 検証を通った entry を追記する
```

**過去月の穴埋めは同じページの archive snapshot から辿る**：この index は最新 1 か月分しか列挙しないため、それより古い release id はサイトからは辿れない。Wayback の同 URL の snapshot が捕捉時点の一覧（公表日・タイトル・release id）を持つので、そこから月ごとの id を復元する。**復元した対応付けは、manifest に既にある月の id と一致するかで検算する**（release は前月分を報告するので、publish 月 − 1 が observed_at になる）。id が復元できない月でも、翌月の release が前月値を restate していればそこから読める：`observed_at` をその月、`release_observed_at` を翌月にして翌月の URL を書く（restate は「前月を名指した値」か「月を伴わない `down from X`」で書かれるため、後者は前月分としてだけ読む）。manifest が公表カレンダーに追いつかない（release 済みの月が manifest に無い）状態は取得の無音の停止になるため、provider が明示エラーで失敗させる: 要求 end の月に対して manifest 最新月が前月に達していないとき（当月 10 日以降）にエラーになり、日次バッチの繰延べ失敗として表面化する。このガードは取得（refresh / all-history）だけを止め、読み取りは store にある月をそのまま返す（manifest の追記漏れが既存データを隠さないため）。

```bash
uv run baibai-engine collection economic refresh spglobal-jp-pmi-mfg --start 2026-05-01 --end 2026-06-30
uv run baibai-engine collection economic refresh tsr-bankruptcies --all-history --end 2026-07-24
uv run baibai-engine market-context indicators get jp.bankruptcies --start 2003-01-01 --end 2026-07-24
uv run baibai-engine market-context indicators get jp.pmi_manufacturing --start 2023-01-01 --end 2026-07-24
```

`indicators.sqlite`は外部sourceの原本・取得記録を持つCollection storeであり、L2 registryや計算値の正本ではない。publisherが古いURLを落とす等で再取得不能な部分があり、取得できる範囲を過去の全情報と同一視しない。R2 pushは上書き対象の1世代を`<key>.bak`として残す（[運用手順](../../batch/OPERATIONS.md)）。

### Baibai Loop で現在局面と系列履歴を読む

`baibai-web`の`/api/market-context`は`as_of / known_at`で選んだL3 assessment、指定基準日のL2 Reading、全系列の読み値を返す非canonical projectionである。新判断は要約・重要変化・反証・不確実性・含意・入力出典を表示し、保存済みv3/v4は従来の詳細と原本文を読める。新しい要約やscoreを生成しない。判断の入力確定時刻は`known_at`、発行時刻は`published_at`、Readingの基準は`asof`、系列の観測日は`observed_at`として別々に表示する。

初期 response は full chart history を持たず、一覧用に各系列の約1年・月次 period-end を最大13 pointだけ返す。これは方向を走査する固定windowで、期間・粒度selectorの対象ではない。行を開いた時だけ `/api/market-context/indicators/<series_id>` がその 1 系列の daily 全履歴を返し、詳細chartの期間 `1y | 5y | 10y | max` と粒度 `daily | weekly | monthly | yearly` は browser 内で絞る。[region別registry](../../engine/src/baibai_engine/market_context/indicators/registry/)に `tradingview_symbol` がある系列だけ TradingView の該当 symbol を新規 tab で開く。

### 変更時の検証

変更したprovider・計算・registryの契約を既存testで確認し、[Python foundation](./python-foundation.md#9-ci-and-local-parity)のindicator store検証を行う。外部の取得挙動を変えた場合は、その取得経路を確認する。任意の変更ごとに全系列のstaleをゼロにすることや、全providerのstress実行は要求しない。

<a id="reading"></a>

## ② 機械読み値：Reading

ReadingはL1 store だけを入力に、登録全系列の **記述統計と鮮度** を決定論で出す L2 出力である。「今の VIX は歴史的に高いのか」を分析セッションごとに人が判断し直さないための共通の物差しであり、regime 分類・合成 score・売買 signal は出さない。

```bash
uv run baibai-engine market-context reading --asof 2026-07-24                 # 表形式
uv run baibai-engine market-context reading --asof 2026-07-24 --format json   # 機械読み
```

### 解釈上の注意

- percentileとz-scoreは`statistic`が示すlevelまたはyoyの分布内位置である。latest valueとtrendまでyoyへ変わるわけではない。
- `insufficient_history`では位置統計を使わない。観測された最新値そのものまで無効にする意味ではなく、期間・鮮度を示して読む。
- daily系列の位置統計は窓の開始近くまで届くことと標本8点以上を確認するが、窓内の日次観測密度は保証しない。疎な系列のpercentileを密な日次履歴と同列に扱わない。
- `next_print_estimate`はcadenceとlagからの推定で、公式event calendarではない。`stale`は観測の古さであり、値の誤りの確定ではない。
- flagsと極端なz-scoreは確認の入口であり、売買signalや誤値判定ではない。
- 異なる実効窓とsampling cadenceを同じ物差しとして比較しない。`window_years`、`window_observations`と規則revisionを確認する。

規則は`method/macro/reading/`のdated revisionに置き、出力の`rules_revision`で識別する。frequencyの既定と系列固有のoverrideを使い、既定がsourceの性質に合わない箇所だけを上書きする。実行対象はschema 2の規則に絞り、鮮度は公表周期・lag・marginから導く。旧schema 1の固定日数方式を再実行する互換分岐は持たない。旧dated fileと保存済み判断のrevision参照は当時の方針を示す履歴として残し、数値や意味を現在の規則へ書き換えない。`--rules`は現行schemaの別方針を試すために使える。厳密なfield・閾値はrule modelとloaderを参照する。現行dated fileの説明は実装と整合させる。当時の契約期間・公表遅延の測定を現在の操作や保証として使わない。操作と解釈は本書へ集約する。水準が下がらないという前提や、過去最大ラグが将来も超えないという保証は置かない。

reading は L1 store を読むだけの純関数で、provider を呼ばず DB へ書かない。したがって過去日の asof でも同じ入力から同じ snapshot を再計算できる。CLI・read API・indicator chart は共通の store reader を使い、`ProviderSpec.point_in_time_vintage` を宣言する source だけを `vintage_at <= asof` へ clamp する。宣言のない bulk history の `vintage_at` は取得日時であって当時の公表日時ではないため、一律 clamp して取得前の過去 snapshot から既知だった履歴を消さない。専用 store は持たず、日次バッチが `baibai-web` 向けの現在読み値（`/api/market-context`・`views/market-context.json`）へ投影し、L3判断は引用したsnapshotとmethod revisionを入力出典に記録する。Web projectionは指定asofのreadingを1回組み立て、canonical snapshotを増やさない。

過去as-ofの再計算は、全sourceの改定前情報を当時のまま再現する保証ではない。取得時刻と公表vintageを区別する。投資部門別の`PubDate`は公表日であり、APIでその改定を取得できた正確な時刻ではない。訂正の配信には時間差があり、古い期間は最新改定だけの場合もあるため、公表日cutoffだけで当時の利用可能情報を完全再現したとは扱わない（[提供元仕様](https://jpx-jquants.com/en/spec/eq-investor-types)）。

Baibai Loop の Market Context タブは、上から **市場・業種の価格状態 → 市場環境判断 → 経済指標のReading → 保存された判断履歴**の順に表示する。現在読み値は `web/config/market-context-panel.yaml` の 7 group 順に `series_id` で join し、最新値・観測日・短期/長期トレンド・`statistic`・percentile・`z_score`・実効窓・注記を並べる。行を開いた後だけ拡大チャートを取得する。**チャートの期間・粒度と percentile の実効窓は別物**なので、列見出しの ⓘ で明示する。

取得失敗、stale、insufficient_history、分布の端は別の状態である。系列の開始時点や配信範囲による履歴不足は、再取得すれば必ず解消する障害ではない。現在読み値ではこれらを件数と絞り込みに使い、indicator storeがない場合は読み値をunavailableとして、保存済みContextとreport indexを表示する。

<a id="assessment"></a>

保存storeからの公開Readingは一つの`store_snapshot_payload`で`read_semantics`を付け、Research・Web・CLIへ同じ説明を渡す。`as_of`は観測対象日であり、Researchの`known_at`はReadingに適用しない。既存ProviderSpecの公表vintage契約を持つ系列だけを対象日でclampし、その他は現在保存されている後日取得/改定履歴で再計算する。全体の当時点再現は保証しない。各系列は`vintage_selection / vintage_cutoff`を示す。任意のreaderを受ける純粋計算・serializerにはこのstore固有の保証や説明を付けない。数値・元時刻・vintage選択を変更する仕組みではない。

`read_semantics.mode: recomputed_from_current_store`は現在のstoreからの再計算を示すprovenanceであり、その日の`known_at`を満たす証明ではない。過去の判断や成績と突き合わせるときは、この利用可能時点の制約を残す。

## ③ 明示作成する市場環境判断

`research/market_context`は固定したL1/L2入力と基準時点、重要変化と根拠、反証/不確実性、調査・業種・リスクへの含意を扱う。新writerは小さな判断一本で、固定章数・シナリオ数・確率格子・指標条件・前回採点を必須にしない。変化が小さい判断や資料不足も表現でき、経済・金利を株価トレンドだけへ縮小しない。文章の品質は独立reviewで確認する。

作成・review・発行・読戻しは[assessment skill](../../.agents/skills/market-context-assessment/SKILL.md)、厳密な形は[model](../../engine/src/baibai_engine/research/market_context/models.py)が所有する。L1/L2の値は出典として参照し、AIによる別の数値正本を作らない。日次の機械更新から執筆を自動起動せず、売買の最終確認は人間が担う。

### 対象日と利用可能時刻

`as_of`は判断の対象日、`known_at`は入力を確定する利用可能時刻である。通常はcallerが入力確定時刻を一度固定し、当時の入力を調べる場合は過去cutoffを明示する。対象日以下で、その時刻までに発行された判断から実時刻順で選ぶ。selectorは現在時刻を読まず、timezone offsetの異なるISO文字列を辞書順で並べない。週末発行の前営業日評価も、利用可能時刻の範囲内なら選べる。

この判断の時刻制約を、全L1観測の改定前情報が再現可能である保証とは扱わない。ProviderSpecが宣言する公表vintageの能力と、bulk backfillの取得時刻の制約は上記Readingに従う。

### 保存履歴と現在の入力

保存済み`context_id`と`supersedes_id`は不変であり、新table/headへそのまま移す。旧schemaの本文、入力、supersedes、引用、保存済みscorecard参照は不変の履歴として取得し、新作成のvalidatorを適用しない。旧詳細の閲覧と新判断の表示は分け、共通projectionだけで過去本文を代替しない。ID閲覧では分析入力のcutoffを適用せず、分析用選択は日付と利用可能時刻を明示する。

Researchへ渡す共通projectionは要約・根拠・含意に加えて反証/不確実性を含む。旧Triageの当時の入力を調べる場合は、切替時に退避したrunnerの`input.json`を参照し、現行Researchの入力として再利用・再生成しない。退避ファイルがない場合に保存判断本文から抽出できる情報は、当時の全入力一致や再現済みを意味しない。

旧v4の共通projectionには支配的な力の主張・伝達経路・確信度・反証・出典と、force IDで対応する相互作用を小さな`synthesis`として引用する。これは保存済み判断の抽出であり、新判断の必須章や新しい評価ではない。Research workspaceのassessment選択は今回の固定`known_at`を使い、External Assessmentの受信cutoffとは分ける。

```bash
uv run baibai-engine research market-context history
uv run baibai-engine research market-context show --context-id macro-context-2026-07-17-example
uv run baibai-engine research market-context latest --asof 2026-07-17 --known-at 2026-07-18T13:00:00+09:00
```


### CFTC・ECBのnative入力

CFTCは要求contractのAPI record全項目、ECB FXはCSVの全通貨列を、source単位の日次native snapshotに保持する。CFTCの非商業long−short、ECBのJPY/USD・JPY/AUDはL2で同じ原本行から計算する。原値の片脚を独立に改訂したり、旧scalar cacheから計算結果を復活させたりしない。

正常なbulk responseは要求期間の全日を調べた結果として保存し、行のない日も空snapshotを残す。訂正で行が消えた日は新しい空snapshotが以前の値を隠す。取得時刻は取得・改訂の順序であり、当時の公表可能時刻の証明ではない。計算不能でも取得済みnative記録は残す。提供元record JSONであり、HTTP bytesそのものの保管を意味しない。

旧net値やcross値だけから元のlong/short・各通貨レートは復元できない。不足するnative履歴はマージ後に取得し、L2再計算と公開を確認する。実際の保存済み判断は書き換えない。
