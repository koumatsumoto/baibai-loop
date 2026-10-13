---
title: "Market Context reference"
summary: "Market Contextの保存済み市場・個別相対の読取、経済観測・Readingと明示作成する市場環境判断、保存履歴の契約。"
doc_type: reference
status: active
---

# Market Context — 指標・Reading・判断・保存履歴

本書はMarket Contextの市場・個別相対の読取、経済指標・Reading、明示的assessment、過去に保存された判断本文の読取契約を所有する。個別企業の価値評価は[Valuation metrics](./valuation-metrics.md)と[Thesis](./thesis.md)が所有する。

Market Contextの`indicators`はL1のregistry・取得・DB adapter、`reading`はL2の決定的算術・保存済みreaderを所有する。観測契約は`indicators/observations.py`に置き、計算からDB adapterを参照しない。L3は`market_context/assessment`が所有し、旧reportは履歴として読む。

日次工程はL1観測とL2 Readingを更新し、人間の明示開始でMarketContextAssessmentを作成する。新判断は下記assessmentの最小契約とreview手順に従う。Market Context全体へこのレポート作成を必須にせず、保存済み機械状態と発行済み判断は個別に読める。

## 現行配置と保全するidentity

`indicators`はL1の取得/registry/観測DB、`reading/query.py`は保存観測からのL2読取、`assessment`は明示判断と保存履歴を所有する。Web/batch/Owner MCPの公開読取は`read_api/market_context.py`へ統合し、旧moduleを転送する入口は置かない。Webの現在の内容は`AssessmentContent`、過去の章・確率・主張・引用は歴史的本文の表示であり、新writerの必須構造ではない。

runtimeの指標DBは`stores/market_context/indicators.sqlite`/R2 `indicators.sqlite`（schema7）、判断はapplication schema21の`market_context_assessment / market_context_assessment_head`に保存する。新規判断はschema6・`market-context-assessment-*` identityを使う。旧`macro-context-*` ID・JSON・引用・hashは変更せず、新しい`/api/market-context/assessments/{context_id}`から直接読む。schema5/6は同じコンパクト意味、旧3/4は保存された章立て履歴であり、新writerの構造とは分ける。新Triage4・CAA2は`market_context_assessment_id`を参照し、保存Triage3・CAA1は元payload/hashを保つ読取で利用・再開する。保存名の切替は完了しており、通常の取得・反映・復旧は[運用手順](../../batch/OPERATIONS.md#store-identity-recovery)に従う。

Readingは`READING_RULES_PATH`が選ぶ一つのdated fileを使う。13の保存判断が引用する`method/macro/reading/2026-08-01T100000+0900.yaml`は元bytes/pathのまま保全し、別のcurrent copyを作らない。規則のidentityはdated stemであり、単なる配置整理で値・revision・schemaを変えない。

<a id="market-context-reads"></a>

価格騰落・標本volatility・流動性・安値乖離・信用需給の純粋算術は`market_context/{history,margin}`が所有する。価格読取も計算も共有L1 split算術を使い、倍率の価格感応履歴/自己レンジ・母分散sigma gapはValuationに従う。観測窓と比較母集団の意味は変えない。

通常Researchでは保存済みReading・市場/業種/個別状態を[調査入力](./analysis-operations.md#明示開始と固定対象)へ接続し、materialな含意をThesis/提案へ記録する。判断の新規作成は明示開始に限り、通常Researchは必要な環境上の含意を保存情報から接続する。

## Market Contextの保存済み読取

Market Contextは経済、市場全体、業種と個別株の相対状態を扱う分析領域である。L1観測、L2機械状態、L3環境判断を区別し、現行のMarketContextAssessmentと、旧Macro Contextを含む保存判断の履歴を参照する。領域読取は`market_context`、Web/batchの公開facadeは`read_api/market_context`が所有する。取得・書込・AI起動を行わず、欠測を別領域の障害や「変化なし」へ置き換えない。

市場・業種・個別の異なる統計をまとめて一つのscoreにはしない。

| 出口 | 統計・対象 | 意味と欠測 |
| --- | --- | --- |
| Security Analysisの業種relative strength | Screeningの既存比較母集団。20 bar騰落の業種平均－市場平均と業種順位 | `market_context/sector`が計算し、Screeningが入力母集団・価格窓を供給する。個別対業種の差ではない |
| market snapshot | benchmarkの週次20/60 bar騰落・20 bar平均超のbreadth、同じ観測日の銘柄による業種中央値 | 1321 proxyの±3%方向注記は広いリスク許容判断ではない。breadthは既存最小標本数、sectorは既存対象を維持し、平均対平均と統合しない |
| security market context | 指定銘柄（候補外も可）の価格・benchmark比較と、同業peerの20 bar中央値 | 個別とbenchmark/peerは指定銘柄の同じ開始・終了日を使う。peerは自銘柄を除き、端点不足を補間・前日値で埋めない |
| Reading / MarketContextAssessment・保存履歴 | 保存済み経済観測・Reading規則、保存済み判断 | 観測/公表vintageの契約を維持する。判断は`as_of / known_at`で適格性を判定し実発行時刻で選ぶ。固定IDの履歴閲覧は旧schemaも本文のまま返す |

個別対業種の`relative_return_20d`は個別の価格騰落－peer中央値で、ratioの差（0.1＝10 percentage points）である。`comparison_start_date / comparison_end_date / master_snapshot_date`を併記し、peer数と比較基準を示す。市場・業種が上昇し個別が下落しても、乖離だけから割安と認定しない。財務・利益・企業価値の算術はValuationが所有する。

masterはrequested cutoff以下の最新の一つのsnapshotを集合ごと選ぶ。そのsnapshotにいないtickerを古いsnapshotから復活させず、未来分類を使わない。価格はcutoff以下のraw closeを保存済み分割factorの後方累積で揃え、closeがnullの分割eventも落とさない。現金配当込みreturnではない。価格の観測日、requested cutoff、master snapshotの日は同一とは限らないため、各基準日を見て読む。

既存CLIは次の領域計算を参照する。

```bash
uv run baibai-engine screening market-snapshot --asof YYYY-MM-DD --weeks 12
uv run baibai-engine screening ticker-profile --ticker <TICKER> --asof YYYY-MM-DD
uv run baibai-engine market-context reading --asof YYYY-MM-DD --format json
```

`ticker-profile`はMarket Context、保存済みSecurity Analysis、event、portfolioを合成する。市場・分類とrunのcutoffは要求日を使うが、portfolioは現在のledger documentを独立の`ledger_as_of`で表示し、要求日への保有履歴再生は行わない。profile全体を完全な過去時点再現とは扱わない。既存の`price.self_range_percentile`は価格観測の順位割合で、Valuationのmin/max内位置と別の量であり、窓は合成callerが既存methodから渡す。

日次の指標独立公開、有限AI調査・提案への接続、環境判断の深度は固定せず、materialな根拠・反証・含意に合わせる。以下にL1/Readingとassessmentの契約を示す。

## ① データ：indicator series を引く

`list` / `search`はregistryだけを読み、観測storeの作成・更新・schema検査を行わない。取得・更新の手順は[`指標履歴`](../../batch/OPERATIONS.md#指標履歴)、撤回と退役の操作は[`指標観測とregistryの修正`](../../batch/OPERATIONS.md#指標観測とregistryの修正)を参照する。

指標データは `baibai-engine market-context indicators`（`engine/src/baibai_engine/market_context/indicators/`）で、再現可能かつ出所（provenance）付きで取得・キャッシュする。

```bash
uv run baibai-engine market-context indicators list --category rates       # 登録 series を見る
uv run baibai-engine market-context indicators search 失業率              # 名前/alias/category で検索
uv run baibai-engine market-context indicators get jp.nikkei225 --start 2026-05-20 --end 2026-06-22
uv run baibai-engine market-context indicators get jp.policy_rate --latest
```

`get`は取得済み範囲を確認し、不足があればproviderを呼ぶ。同じ引数でもstore状態・source改定・実行日によって結果は変わる。計算の決定性は保存入力・採用vintage・規則・as-ofが固定された場合の性質である。`get --latest`はJSTの運用日を基準に、[reading rules](../../engine/src/baibai_engine/market_context/reading/rules.py)の公表ラグと猶予でcacheの鮮度を判定する。再取得の窓幅は鮮度閾値とは別で、[indicator service](../../engine/src/baibai_engine/market_context/indicators/service.py)が日次batchと共有する。

`refresh --all-history` は provider ごとの取得可能な先頭日から強制再取得する。派生系列（`derived` provider）は外部ソースを持たず入力系列の重なりが履歴なので、どの base 系列よりも古い床から入力を読み直して全期間を再計算する（base 系列を先に同期してから回す）。月次整列は月内の各入力の最終観測を使う。market data を月末まで使う数式は選択入力の最終観測日を出力日とし、月初への backdate を防ぐ。数式変更で observation grid を置換する系列は provider spec で個別に宣言し、既存 period を欠く候補なら削除前に失敗して履歴を保持する。FRED 系列は現在の `fredgraph.csv` が返す先頭日を再現可能な境界とし、その日より前の観測を残さない。HTTP providerの全履歴source訂正では、再取得期間とvintageの条件に該当し、registryの`source_url`と異なる旧sourceの観測をcacheから除く。JP provider の契約期間や公表 archive が先頭日を制限する場合は、実際の取得範囲と制約を運用記録へ残す。

local derivedの式の出典は現行配置の固定commit URLで示す。出典リンクの変更を外部source訂正として扱わず、旧観測の値・vintage・source URLは保持する。新URLの観測はprovenanceが異なるため初回は新vintageとなり、同じURL・同内容の反復では増えない。HTTP providerの全履歴取得で行う外部source訂正の扱いは維持する。

`us.erp --all-history`の旧grid/vintage置換は、[providerの明示的な再構築契約](../../engine/src/baibai_engine/market_context/indicators/providers/derived.py)として別に存在する。出典URLの変更を理由に起動せず、このURL切替を全履歴再構築のno-loss保証と扱わない。

同内容の再取得ではobservationのvintageを増やさず、provider runに取得記録を残す。値・単位・期間・取得状態・sourceが変わるrevisionや、変化後に同じ値へ戻るrevisionは保持する。日次batchはrolling窓を引き直すため窓内の取得断は次回成功時に埋まるが、窓外の欠落は[指標履歴の復旧](../../batch/OPERATIONS.md#指標履歴)が必要になる。保存・重複排除は[indicator DB](../../engine/src/baibai_engine/market_context/indicators/db.py)を参照する。

誤って入った observation は削除では消えない。cloudとのmergeは双方のfactを戻すno-loss契約なので、localで消しても次のpushで復活する。撤回は対象日の最新vintageをCASで名指しし、その下の状態を新しいvintageとして記録する。同じ撤回を繰り返すと意図しない旧値を戻し得るため、期待vintageが変われば拒否する。derived系列の保存観測は入力の撤回だけでは消えない。操作順と失敗時の行き先は[運用手順](../../batch/OPERATIONS.md#指標観測とregistryの修正)に従う。

- 下に正しい観測があれば **その観測が再び読まれる**（誤った writer が正しい値の上に別の値を刻んだ場合。撤回のたびに 1 つずつ vintage を遡る）
- 下に何も無い、または下も撤回済みなら `fetch_status='retracted'` を書き、その日は reading / chart / freshness のすべてから外れる

読みは「観測日ごとに `ok` と `retracted` の中から最大 vintage を採り、それが `ok` のときだけ返す」。`failed` / `unreleased` は取得の結果であって値についての主張ではないので、読めていた観測を隠さない。書かれた行は普通の fact なので merge が両方向へ運び、他方の store に残る誤った行より必ず新しいため上書きされない。`--all-history` を含む全ての store 書き換えは、**provider の最新の主張より新しい vintage の行を消さない**——書き換えが置換してよいのは provider が言い直すものだけで、その後に store が下した決定（撤回とそれが戻した観測）ではないからである。取得時刻を vintage に刻む provider では最新の主張が「今」なので、この規則は何も余分に残さない。provider が同じ観測日を再配信した場合だけ、新しい `ok` vintage が上に乗って復活する（「源泉が再び主張する事実は読む」で正しい）。

point-in-time provider（`jquants_flows`）では撤回の vintage が公表時刻ではなく操作時刻になるため、**撤回は撤回時刻以降の as-of にしか効かない**（それ以前の as-of での replay は撤回前の値を読み続ける。当時そう信じていたことの誠実な表現である）。同じ理由で、撤回時刻より前の公表 vintage を持つ改定は撤回の下に埋もれる。実際に撤回した観測は source が publish しない幽霊日なので改定の余地が無いが、real な観測日を撤回するときはこの境界を意識する。

registryは系列定義の正本である。通常のopenでは登録外のfactsを削除せず、現行系列を指定した明示的なrefreshが退役系列をpruneする。storeが新世代、または同世代でmembershipが異なる場合は拒否する。退役・改名は[registry修正手順](../../batch/OPERATIONS.md#指標観測とregistryの修正)に従い、過去の発行済みContextを書き換えない。

全providerの観測は系列identity・単位・有限値・系列固有のbandを満たす必要がある。bandは明白な列・桁・単位ずれを検出するもので、景気急変を前回値との差だけで拒否する条件ではない。band内に収まるscale変更はprovider固有のheader・metadata照合で守る。例えば`jp.foreign_flows`は原典の千円単位を保持する。検証違反はそのseriesの取得失敗とし、部分取り込みしない。DB境界でも同じ制約を守り、schemaやtriggerの不整合を拒否する。厳密な条件は[service](../../engine/src/baibai_engine/market_context/indicators/service.py)・[DB](../../engine/src/baibai_engine/market_context/indicators/db.py)・[schema](../../engine/src/baibai_engine/market_context/indicators/schema.sql)が所有する。storeは空・直前schemaからの一段移行・現行schemaだけを受け付け、複数世代のmigrationは持たない。

registryのband変更時は、Git管理外のstoreをread-only validatorで全履歴・全vintage検査する。CIのfixtureだけでは既存観測との整合を証明できない。検査はstoreを書き換えず、観測とcanonical trigger契約の違反を報告する。

同じvalidatorはapplication storeの判断履歴のmetadataと現在の作成型を検証する。旧schemaの本文はstored_onlyとして保持し、旧writerの構造条件で拒否しない。退役系列の引用はwarningとして記録する。Review Set発行はContextを読まないため、この検査失敗をReview Set停止と読み替えない。判断を使うTriage・Researchと履歴閲覧への影響を確認する。検証の実行条件とapplication store不在時の扱いは[完全local gate](./python-foundation.md#9-ci-and-local-parity)、系列退役の順序は[運用手順](../../batch/OPERATIONS.md#指標観測とregistryの修正)に従う。

### データソース registry

系列ID・source URL・unit・provider固有のselectorは[registry](../../engine/src/baibai_engine/market_context/indicators/registry/)、取得能力と認証条件は[ProviderSpec](../../engine/src/baibai_engine/market_context/indicators/provider_specs.py)と各[provider](../../engine/src/baibai_engine/market_context/indicators/providers/)が所有する。下表はsourceを選ぶときの用途と制約を示す。

| Provider | 用途 | 選択・運用上の制約 |
| --- | --- | --- |
| `fred_csv` | 米マクロ、金利、FX、信用、市場系列 | 系列ごとに配信開始・終了や履歴窓が異なる。relay停止とsource停止はstore上で区別できないため、公表元が機械可読配信を持つ場合は直読を優先する |
| `frb_h15` | 米国債金利・スプレッド | 遮断時だけbrowserで再取得する。サイズ上限・404・5xx・通信障害は別経路で迂回しない |
| `ecb_fx` | JPYクロス | JPYと基軸通貨の比で算出する |
| `estat` | 日本の公式マクロ | API認証が必要。DB掲載が途中で止まる統計もあるため、新規採用時は更新日と公表元を照合する |
| `estat_dashboard` | 失業率・名目賃金等 | 頻度・季節調整・地域を固定し、速報は採らない。確報前はreading rulesの公表待ちとして扱う。基準改定を窓の境目で継ぎ足さないため全履歴を取得する |
| `jquants_flows` | 海外投資家フロー | screeningと同じcredential。購読窓内を取得し、集計週末をobservation、公表日をvintage、原典の千円を単位として保持する |
| `jquants_options` | 日経225のIV・スキュー・期間構造 | rate limit、式変更、購読窓、緊急取引証拠金日は[専用手順](#jquants-options-provider)を参照する |
| `boj` | マネタリーベース・実質輸出・消費活動 | xlsxの値列とheader・基準年・単位を照合する。隣列のmetadataで代用しない |
| `boj_timeseries` | 無担保コールO/N | BOJ時系列統計の公表に従う |
| `mof_jgb` | 日本国債金利 | 公表CSVの和暦・文字コードを正規化する |
| `tsr_bankruptcies` | 日本の企業倒産 | 公表ページが参照する月次履歴を取得する |
| `spglobal_pmi` | 日本・米国の製造業/サービスPMI | 公式PDFを読む。release manifest更新は[下記](#pmi-release-manifestの更新)。遮断時だけbrowserを使う |
| `umich_sca` | 米消費者態度 | 公表元の月次表を直読する。読めない行をskipして短い履歴として扱わない |
| `yahoo` | 金属先物・MOVE・Russell2000・SOX等 | providerが要求するbrowser UAを使う |
| `multpl` | 米株バリュエーション | current levelと確定済み月次標本を区別する。未公表月を合成せず、readingの統計標本だけ月次化する。HTML変更時は短期窓と全履歴の取得を確認する |

<a id="jquants-options-provider"></a>

### `jquants_options`の取得と復旧

#### 取得とrate limit

`jquants_options`は1営業日につき1回呼び出すため、10年の`--all-history`は約2,600回の逐次取得になる。1日最大約1万行のchainは保存せず、取得時に3系列へ集計する。

再試行はこのproviderが生成するClientV2のHTTPS transportが所有し、個々のGETは本文読了まで初回＋最大5回とする。HTTP200本文の一時的な受信失敗もstatus retryと同じ回数・deadlineで扱う。有効な`Retry-After`の秒数／HTTP-dateを優先し、不在・不正なら30・60・120・300・600秒で待つ。認証・403・解析失敗、再試行の終了をserviceで再開しない。ページと営業日のGETはそれぞれ独立した回数予算を持ち、後続ページの失敗で成功済みページを再取得しない。同じ営業日の失敗も3系列で共有し、失敗を正常nullへ変えない。

最初の取得から20分のprocess内deadlineを全営業日・3系列で共有する。通信時間とSDK内部の待機も経過時間に数え、新しいGETやdeadlineに収まらない待機を開始しない。待機を短縮して再送せず、失敗として独立工程へ戻す。通常のSDK通信timeoutは30秒のままで、進行中の通信をdeadlineで強制取消するものではない。job残時間は現行callerから渡されておらず、この20分予算とは別の未実装範囲である。deadlineや失敗memoを次processへ永続化しない。

全営業日の取得後に1回だけstoreへ書く。途中で再試行しない失敗が起きた場合は、そのpassの取得結果をすべて破棄する。長い窓は年単位に分けて取得する。

#### 集計式の変更

新式が値を出さない日に旧式の観測が残り、`--all-history`でも定義が混在し得る。公開済み系列のlocal削除はno-loss mergeで戻るため、[registry修正手順](../../batch/OPERATIONS.md#指標観測とregistryの修正)の退役→cloud反映→新式再導入で切り替える。日次batch待ちや大量の日別retractで代行しない。

`iv_30d`と`iv_skew`はSQ直後に構造的に数日欠ける。鮮度判定はreading rulesを正本とし、過去studyの実測値を現行閾値として固定しない。

#### 購読窓と緊急取引証拠金日

購読窓は運用日から10年rollingで、窓外の要求はHTTP 400になる。`--all-history`の始点は窓の境界に置かず、数日内側から年単位で取得する。

緊急取引証拠金が発動した日はchainが2部返る。発動時の部は前営業日の原資産と平坦なIVを持つため、`EmMrgnTrgDiv`が名指す清算値算出時の部だけを読む。このfieldを持たない行は読まない。

### Providerの追加

新ソース追加＝provider モジュールを 1 つ足して（`providers/` に 1 ファイル）`providers/registry.py` に 1 行登録し、series を registry（`indicators/registry/` の region 別 yaml）へ 1 entry 加える。provider の取得能力（all-history 起点・store 書き換え方針・refresh 可否・point-in-time vintage・必要 env）は各 provider の `ProviderSpec` が宣言し、service / store reader は provider 名で分岐しない。point-in-time replay を提供する provider の spec は fetch 実装を import しない read-safe module に置き、provider 実装と reader が同じ spec を参照する。registered provider との drift は test で検出する。1 series_id = 1 provider を厳守する。provider 取得は一時的な `IndicatorsProviderError` を 1 回 retry し、再失敗した場合は `provider_runs` に failed として記録する。

`market-context indicators refresh` は複数 series を 1 pass で取得し、1 series の失敗は他 series を止めない。失敗した series は最後にまとめて stderr へ列挙し、exit code は非 0 になる（1 つの壊れたソースが同一グループの残り全系列を stale にしない）。1 pass は 1 つの fetch context を共有するので、複数 series が同じ bulk ファイルを参照しても download は 1 回、browser fallback を要する provider の起動も 1 回で済む。取得値は store へ入る前に有限値であることを検証し、NaN / ±inf は取得失敗として扱う（派生計算・percentile・export を汚染させない）。`provider_runs`は取得の成否と`record_count`を記録する。

### PMI release manifestの更新

PMI は data API が無いため、月次 release URL の manifest（`engine/src/baibai_engine/market_context/indicators/providers/pmi_release_urls.yaml`、`schema_version: 2`、PMI stream ごとに `observed_at` → 公式 release URL）を正本とし、`spglobal_pmi` provider が各 URL の公式 PDF を live 取得して headline 値を抽出する。release URL の validator は `https://www.pmi.spglobal.com/Public/Home/PressRelease/<32 hex>` だけを許可する。

抽出は対象月のheadline値だけを読み、sub-index・composite・閾値比較・flash見積り・複数月平均を混入させない。未対応の表現や矛盾する候補は取得失敗とし、値を推測しない。具体的な英文の認識規則と数値検証は[PMI extractor](../../engine/src/baibai_engine/market_context/indicators/providers/pmi_extraction.py)が所有する。

1 月 = 1 PDF なので、通常の refresh は store に無い月と、manifest の release URL が store の値の出所と一致しない月だけを取得する。final headline は公表後に改定されないため、同じ URL から取り直した月は同じ値になる。URL を訂正すればその月は自動で取り直される。抽出規則の変更後など stream 全体を source から作り直すときは `refresh --all-history` を使う（全月を再取得する）。

**新しい月の追記は `baibai_engine.market_context.indicators.pmi_manifest` で行う**: S&P Global の公式 press release ページ（`/Public/Release/PressReleases`）が列挙する最新 release を stream ごとに拾い、release title の完全一致で系列を同定し、**release PDF が自分をその PMI と名乗り・自分の embargo 日が index の公表日と同じ月であり・headline をその月に結び付けていること**を 3 つとも確かめてから追記する。抽出には「その release 自身の月」として渡さず後の月を渡すので、月を明示しない reading（`posted 54.8`）は採られず、月を名指した reading（`posted 54.8 in June`）だけが通る。title を別の PMI に取り違えても月の証明だけは通ってしまうので（services も manufacturing と同じ形で headline を述べる）、系列は PDF 自身の名乗りで、公表月は PDF 自身の embargo 日で確かめる。**月が飛ぶ追記は拒否する**: provider の追いつき guard は manifest の最新月しか見ないため、穴を越えた entry を書くと飛ばした月が二度と報告されない。edge WAF が plain HTTP を challenge page（200）で返すときは、PDF 経路と同じ headless browser で index を読み直す。書き込みは copy を text 編集して provider の loader で読めることを確かめ、通ったものだけを正本へ move する（正本への書き込みは検証後の move 1 回だけ）。追記後は `market-context indicators refresh` で該当月を取得して公表値と照合してから commit する。

```bash
uv run python -m baibai_engine.market_context.indicators.pmi_manifest --dry-run   # 何が追記されるかだけ見る
uv run python -m baibai_engine.market_context.indicators.pmi_manifest             # 検証を通った entry を追記する
```

**過去月の穴埋めは同じページの archive snapshot から辿る**：この index は最新 1 か月分しか列挙しないため、それより古い release id はサイトからは辿れない。Wayback の同 URL の snapshot が捕捉時点の一覧（公表日・タイトル・release id）を持つので、そこから月ごとの id を復元する。**復元した対応付けは、manifest に既にある月の id と一致するかで検算する**（release は前月分を報告するので、publish 月 − 1 が observed_at になる）。id が復元できない月でも、翌月の release が前月値を restate していればそこから読める：`observed_at` をその月、`release_observed_at` を翌月にして翌月の URL を書く（restate は「前月を名指した値」か「月を伴わない `down from X`」で書かれるため、後者は前月分としてだけ読む）。manifest が公表カレンダーに追いつかない（release 済みの月が manifest に無い）状態は取得の無音の停止になるため、provider が明示エラーで失敗させる: 要求 end の月に対して manifest 最新月が前月に達していないとき（当月 10 日以降）にエラーになり、日次バッチの繰延べ失敗として表面化する。このガードは取得（refresh / all-history）だけを止め、読み取りは store にある月をそのまま返す（manifest の追記漏れが既存データを隠さないため）。

```bash
uv run baibai-engine market-context indicators refresh jp.pmi_manufacturing --start 2026-05-01 --end 2026-06-30
uv run baibai-engine market-context indicators refresh jp.bankruptcies --all-history --end 2026-07-24
uv run baibai-engine market-context indicators get jp.bankruptcies --start 2003-01-01 --end 2026-07-24
uv run baibai-engine market-context indicators get jp.pmi_manufacturing --start 2023-01-01 --end 2026-07-24
```

`indicators.sqlite` は schema / series registry と各 provider（PDF / API / CSV）から再構築する L1 store であり、定期 backup は持たない。ただし PMI 履歴のように publisher が古い URL を落とすと再取得できない部分があるため、R2 への push は上書き対象の 1 世代を `<key>.bak` として残す（[`batch/OPERATIONS.md`](../../batch/OPERATIONS.md)）。

### Baibai Loop で現在局面と系列履歴を読む

`baibai-web`の`/api/market-context`は`as_of / known_at`で選んだL3 assessment、指定基準日のL2 Reading、全系列の読み値を返す非canonical projectionである。新判断は要約・重要変化・反証・不確実性・含意・入力出典を表示し、保存済みv3/v4は従来の詳細と原本文を読める。新しい要約やscoreを生成しない。判断の入力確定時刻は`known_at`、発行時刻は`published_at`、Readingの基準は`asof`、系列の観測日は`observed_at`として別々に表示する。

初期 response は full chart history を持たず、一覧用に各系列の約1年・月次 period-end を最大13 pointだけ返す。これは方向を走査する固定windowで、期間・粒度selectorの対象ではない。行を開いた時だけ `/api/market-context/indicators/<series_id>` がその 1 系列の daily 全履歴を返し、詳細chartの期間 `1y | 5y | 10y | max` と粒度 `daily | weekly | monthly | yearly` は browser 内で絞る。`series.yaml` に `tradingview_symbol` がある系列だけ TradingView の該当 symbol を新規 tab で開く。

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
- `next_print_estimate`はcadenceとlagからの推定で、公式event calendarではない。`stale`は観測の古さであり、値の誤りの確定ではない。
- flagsと極端なz-scoreは確認の入口であり、売買signalや誤値判定ではない。
- 異なる実効窓とsampling cadenceを同じ物差しとして比較しない。`window_years`、`window_observations`と規則revisionを確認する。

規則は`method/macro/reading/`のdated revisionに置き、出力の`rules_revision`で識別する。frequencyの既定と系列固有のoverrideを使い、既定がsourceの性質に合わない箇所だけを上書きする。厳密なfield・閾値・旧revisionの読込条件はrule modelとloaderを参照する。過去revisionの意味を現在の規則で書き換えない。

reading は L1 store を読むだけの純関数で、provider を呼ばず DB へ書かない。したがって過去日の asof でも同じ入力から同じ snapshot を再計算できる。CLI・read API・indicator chart は共通の store reader を使い、`ProviderSpec.point_in_time_vintage` を宣言する source だけを `vintage_at <= asof` へ clamp する。宣言のない bulk history の `vintage_at` は取得日時であって当時の公表日時ではないため、一律 clamp して取得前の過去 snapshot から既知だった履歴を消さない。専用 store は持たず、日次バッチが `baibai-web` 向けの現在読み値（`/api/market-context`・`views/market-context.json`）へ投影し、L3判断は引用したsnapshotとmethod revisionを入力出典に記録する。Web projectionは指定asofのreadingを1回組み立て、canonical snapshotを増やさない。

過去as-ofの再計算は、全sourceの改定前情報を当時のまま再現する保証ではない。取得時刻と公表vintageを区別する。

Baibai Loop の Market Context タブは、上から **現在の市場環境判断 → 経済指標のReading → 保存された判断履歴**の順に表示する。現在読み値は `web/config/market-context-panel.yaml` の 7 group 順に `series_id` で join し、最新値・観測日・短期/長期トレンド・`statistic`・percentile・`z_score`・実効窓・注記を並べる。行を開いた後だけ拡大チャートを取得する。**チャートの期間・粒度と percentile の実効窓は別物**なので、列見出しの ⓘ で明示する。

取得失敗、stale、insufficient_history、分布の端は別の状態である。系列の開始時点や配信範囲による履歴不足は、再取得すれば必ず解消する障害ではない。現在読み値ではこれらを件数と絞り込みに使い、indicator storeがない場合は読み値をunavailableとして、保存済みContextとreport indexを表示する。

<a id="assessment"></a>

保存storeからの公開Readingは一つの`store_snapshot_payload`で`read_semantics`を付け、Research・Owner MCP・Web・CLIへ同じ説明を渡す。`as_of`は観測対象日であり、Researchの`known_at / received_cutoff`はReadingに適用しない。既存ProviderSpecの公表vintage契約を持つ系列だけを対象日でclampし、その他は現在保存されている後日取得/改定履歴で再計算する。全体の当時点再現は保証しない。各系列は`vintage_selection / vintage_cutoff`を示す。任意のreaderを受ける純粋計算・serializerにはこのstore固有の保証や説明を付けない。数値・元時刻・vintage選択を変更する仕組みではない。

## ③ 明示作成する市場環境判断

`market_context/assessment`は固定したL1/L2入力と基準時点、重要変化と根拠、反証/不確実性、調査・業種・リスクへの含意を扱う。新writerは小さな判断一本で、固定章数・シナリオ数・確率格子・指標条件・前回採点を必須にしない。変化が小さい判断や資料不足も表現でき、経済・金利を株価トレンドだけへ縮小しない。文章の品質は独立reviewで確認する。

作成・review・発行・読戻しは[assessment skill](../../.agents/skills/market-context-assessment/SKILL.md)、厳密な形は[model](../../engine/src/baibai_engine/market_context/assessment/models.py)が所有する。L1/L2の値は出典として参照し、AIによる別の数値正本を作らない。日次の機械更新から執筆を自動起動せず、売買の最終確認は人間が担う。

### 対象日と利用可能時刻

`as_of`は判断の対象日、`known_at`は入力を確定する利用可能時刻である。通常はcallerが入力確定時刻を一度固定し、当時の入力を調べる場合は過去cutoffを明示する。対象日以下で、その時刻までに発行された判断から実時刻順で選ぶ。selectorは現在時刻を読まず、timezone offsetの異なるISO文字列を辞書順で並べない。週末発行の前営業日評価も、利用可能時刻の範囲内なら選べる。

この判断の時刻制約を、全L1観測の改定前情報が再現可能である保証とは扱わない。ProviderSpecが宣言する公表vintageの能力と、bulk backfillの取得時刻の制約は上記Readingに従う。

### 保存履歴と現在の入力

保存済み`context_id`と`supersedes_id`は不変であり、新table/headへそのまま移す。旧schemaの本文、入力、supersedes、引用、保存済みscorecard参照は不変の履歴として取得し、新作成のvalidatorを適用しない。旧詳細の閲覧と新判断の表示は分け、共通projectionだけで過去本文を代替しない。ID閲覧では分析入力のcutoffを適用せず、分析用選択は日付と利用可能時刻を明示する。

Research/Triageへ渡す共通projectionは要約・根拠・含意に加えて反証/不確実性を含む。旧Triageの実際の入力は保存されたrunner `input.json`が正本であり、上書き再生成しない。ファイルがない場合の照会は保存本文からの抽出で、当時の全入力一致や再現済みとは言わない。

旧v4の共通projectionには支配的な力の主張・伝達経路・確信度・反証・出典と、force IDで対応する相互作用を小さな`synthesis`として引用する。これは保存済み判断の抽出であり、新判断の必須章や新しい評価ではない。Research workspaceのassessment選択は今回の固定`known_at`を使い、External Assessmentの受信cutoffとは分ける。

```bash
uv run baibai-engine market-context assessment history
uv run baibai-engine market-context assessment show --context-id macro-context-2026-07-17-example
uv run baibai-engine market-context assessment latest --asof 2026-07-17 --known-at 2026-07-18T13:00:00+09:00
```
