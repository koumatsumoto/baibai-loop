---
title: "Reference — macro analysis"
summary: "マクロ環境分析：L1 指標を毎営業日 L2 reading で機械読み値にし、人間が判断するときだけ L3 macro context report（core 環境評価 10 + synthesis 統合評価 + connection 日本株ループ接続）を書く。"
doc_type: reference
status: active
---

# Reference — マクロ環境分析

マクロ環境分析は **独立した機能のまとまり**（データ取得層 + 機械読み値 + リサーチの実践）であり、形式化した独自ループにはしない。狙いは、個別銘柄のinvestment caseと評価期間内の見返りを変え得る外部経路と共通riskを判断層へ供給すること。sector順位、相場方向、買い時、投入額を決めない。

扱うものは性質の異なる 4 種：**① データ（L1 の事実）／ ② 機械読み値（L2 macro reading）／ ③ 環境認識（L3 macro context report）／ ④ 知見（調べ方のメタ知識）**。①②は毎営業日 CI が機械で回し、③は人間が判断するときだけ書く。本工程の統計的な位置付けは末尾の「誠実性」に従う。

## ① データ：indicator series を引く

`list` / `search`はregistryだけを読み、観測storeの作成・更新・schema検査を行わない。取得・更新の手順は[`Macro履歴`](../../batch/OPERATIONS.md#macro履歴)、撤回と退役の操作は[`Macro観測とregistryの修正`](../../batch/OPERATIONS.md#macro観測とregistryの修正)を参照する。

指標データは `baibai-engine macro`（`engine/src/baibai_engine/macro/indicators/`）で、再現可能かつ出所（provenance）付きで取得・キャッシュする。

```bash
uv run baibai-engine macro list --category rates       # 登録 series を見る
uv run baibai-engine macro search 失業率              # 名前/alias/category で検索
uv run baibai-engine macro get jp.nikkei225 --start 2026-05-20 --end 2026-06-22
uv run baibai-engine macro get jp.policy_rate --latest
```

`get`は取得済み範囲を確認し、不足があればproviderを呼ぶ。同じ引数でもstore状態・source改定・実行日によって結果は変わる。計算の決定性は保存入力・採用vintage・規則・as-ofが固定された場合の性質である。`get --latest`はJSTの運用日を基準に、[reading rules](../../engine/src/baibai_engine/macro/reading/rules.py)の公表ラグと猶予でcacheの鮮度を判定する。再取得の窓幅は鮮度閾値とは別で、[indicator service](../../engine/src/baibai_engine/macro/indicators/service.py)が日次batchと共有する。

`refresh --all-history` は provider ごとの取得可能な先頭日から強制再取得する。派生系列（`derived` provider）は外部ソースを持たず入力系列の重なりが履歴なので、どの base 系列よりも古い床から入力を読み直して全期間を再計算する（base 系列を先に同期してから回す）。月次整列は月内の各入力の最終観測を使う。market data を月末まで使う数式は選択入力の最終観測日を出力日とし、月初への backdate を防ぐ。数式変更で observation grid を置換する系列は provider spec で個別に宣言し、既存 period を欠く候補なら削除前に失敗して履歴を保持する。FRED 系列は現在の `fredgraph.csv` が返す先頭日を再現可能な境界とし、その日より前の観測を残さない。各系列のobservationはregistryの`source_url`と一致するcacheだけを保持する。JP provider の契約期間や公表 archive が先頭日を制限する場合は、実際の取得範囲と制約を運用記録へ残す。

同内容の再取得ではobservationのvintageを増やさず、provider runに取得記録を残す。値・単位・期間・取得状態・sourceが変わるrevisionや、変化後に同じ値へ戻るrevisionは保持する。日次batchはrolling窓を引き直すため窓内の取得断は次回成功時に埋まるが、窓外の欠落は[Macro履歴の復旧](../../batch/OPERATIONS.md#macro履歴)が必要になる。保存・重複排除は[indicator DB](../../engine/src/baibai_engine/macro/indicators/db.py)を参照する。

誤って入った observation は削除では消えない。cloudとのmergeは双方のfactを戻すno-loss契約なので、localで消しても次のpushで復活する。撤回は対象日の最新vintageをCASで名指しし、その下の状態を新しいvintageとして記録する。同じ撤回を繰り返すと意図しない旧値を戻し得るため、期待vintageが変われば拒否する。derived系列の保存観測は入力の撤回だけでは消えない。操作順と失敗時の行き先は[運用手順](../../batch/OPERATIONS.md#macro観測とregistryの修正)に従う。

- 下に正しい観測があれば **その観測が再び読まれる**（誤った writer が正しい値の上に別の値を刻んだ場合。撤回のたびに 1 つずつ vintage を遡る）
- 下に何も無い、または下も撤回済みなら `fetch_status='retracted'` を書き、その日は reading / chart / scorecard / freshness のすべてから外れる

読みは「観測日ごとに `ok` と `retracted` の中から最大 vintage を採り、それが `ok` のときだけ返す」。`failed` / `unreleased` は取得の結果であって値についての主張ではないので、読めていた観測を隠さない。書かれた行は普通の fact なので merge が両方向へ運び、他方の store に残る誤った行より必ず新しいため上書きされない。`--all-history` を含む全ての store 書き換えは、**provider の最新の主張より新しい vintage の行を消さない**——書き換えが置換してよいのは provider が言い直すものだけで、その後に store が下した決定（撤回とそれが戻した観測）ではないからである。取得時刻を vintage に刻む provider では最新の主張が「今」なので、この規則は何も余分に残さない。provider が同じ観測日を再配信した場合だけ、新しい `ok` vintage が上に乗って復活する（「源泉が再び主張する事実は読む」で正しい）。

point-in-time provider（`jquants_flows`）では撤回の vintage が公表時刻ではなく操作時刻になるため、**撤回は撤回時刻以降の as-of にしか効かない**（それ以前の as-of での replay は撤回前の値を読み続ける。当時そう信じていたことの誠実な表現である）。同じ理由で、撤回時刻より前の公表 vintage を持つ改定は撤回の下に埋もれる。実際に撤回した観測は source が publish しない幽霊日なので改定の余地が無いが、real な観測日を撤回するときはこの境界を意識する。

registryは系列定義の正本である。通常のopenでは登録外のfactsを削除せず、現行系列を指定した明示的なrefreshが退役系列をpruneする。storeが新世代、または同世代でmembershipが異なる場合は拒否する。退役・改名は[registry修正手順](../../batch/OPERATIONS.md#macro観測とregistryの修正)に従い、過去の発行済みContextを書き換えない。

全providerの観測は系列identity・単位・有限値・系列固有のbandを満たす必要がある。bandは明白な列・桁・単位ずれを検出するもので、景気急変を前回値との差だけで拒否する条件ではない。band内に収まるscale変更はprovider固有のheader・metadata照合で守る。例えば`jp.foreign_flows`は原典の千円単位を保持する。検証違反はそのseriesの取得失敗とし、部分取り込みしない。DB境界でも同じ制約を守り、schemaやtriggerの不整合を拒否する。厳密な条件は[service](../../engine/src/baibai_engine/macro/indicators/service.py)・[DB](../../engine/src/baibai_engine/macro/indicators/db.py)・[schema](../../engine/src/baibai_engine/macro/indicators/schema.sql)が所有する。storeは空・直前schemaからの一段移行・現行schemaだけを受け付け、複数世代のmigrationは持たない。

registryのband変更時は、Git管理外のstoreをread-only validatorで全履歴・全vintage検査する。CIのfixtureだけでは既存観測との整合を証明できない。検査はstoreを書き換えず、観測とcanonical trigger契約の違反を報告する。

同じvalidatorはapplication storeの発行済みcurrent-contract Macro Contextも全件loadし、現行readerで読めないrevisionを失敗として報告する。退役系列の引用は読める限りwarningとし、scorecardが採点不能になる場合も明示する。Review Set発行はContextを読まないため、この検査失敗をReview Set停止と読み替えない。Contextを使うTriage・Research・scorecardの読取への影響を確認する。検証の実行条件とapplication store不在時の扱いは[完全local gate](./python-foundation.md#9-ci-and-local-parity)、系列退役の順序は[運用手順](../../batch/OPERATIONS.md#macro観測とregistryの修正)に従う。

### データソース registry

系列ID・source URL・unit・provider固有のselectorは[registry](../../engine/src/baibai_engine/macro/indicators/registry/)、取得能力と認証条件は[ProviderSpec](../../engine/src/baibai_engine/macro/indicators/provider_specs.py)と各[provider](../../engine/src/baibai_engine/macro/indicators/providers/)が所有する。下表はsourceを選ぶときの用途と制約を示す。

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

HTTP 429はclient内部の再試行が尽きた後、statusを持たない`RetryError`として返る。待避対象は例外の型で判定し、待機は1 processあたり20分を上限とする。1 passは3系列に対するserviceの2回再試行で最大6 fetchになる。fetchごとに20分を与えてはならない。上限が6倍になり、日次batchのjob timeoutによってscreening publishまで失うためである。

全営業日の取得後に1回だけstoreへ書く。途中で再試行しない失敗が起きた場合は、そのpassの取得結果をすべて破棄する。長い窓は年単位に分けて取得する。

#### 集計式の変更

新式が値を出さない日に旧式の観測が残り、`--all-history`でも定義が混在し得る。公開済み系列のlocal削除はno-loss mergeで戻るため、[registry修正手順](../../batch/OPERATIONS.md#macro観測とregistryの修正)の退役→cloud反映→新式再導入で切り替える。日次batch待ちや大量の日別retractで代行しない。

`iv_30d`と`iv_skew`はSQ直後に構造的に数日欠ける。鮮度判定はreading rulesを正本とし、過去studyの実測値を現行閾値として固定しない。

#### 購読窓と緊急取引証拠金日

購読窓は運用日から10年rollingで、窓外の要求はHTTP 400になる。`--all-history`の始点は窓の境界に置かず、数日内側から年単位で取得する。

緊急取引証拠金が発動した日はchainが2部返る。発動時の部は前営業日の原資産と平坦なIVを持つため、`EmMrgnTrgDiv`が名指す清算値算出時の部だけを読む。このfieldを持たない行は読まない。

### Providerの追加

新ソース追加＝provider モジュールを 1 つ足して（`providers/` に 1 ファイル）`providers/registry.py` に 1 行登録し、series を registry（`indicators/registry/` の region 別 yaml）へ 1 entry 加える。provider の取得能力（all-history 起点・store 書き換え方針・refresh 可否・point-in-time vintage・必要 env）は各 provider の `ProviderSpec` が宣言し、service / store reader は provider 名で分岐しない。point-in-time replay を提供する provider の spec は fetch 実装を import しない read-safe module に置き、provider 実装と reader が同じ spec を参照する。registered provider との drift は test で検出する。1 series_id = 1 provider を厳守する。provider 取得は一時的な `IndicatorsProviderError` を 1 回 retry し、再失敗した場合は `provider_runs` に failed として記録する。

`macro refresh` は複数 series を 1 pass で取得し、1 series の失敗は他 series を止めない。失敗した series は最後にまとめて stderr へ列挙し、exit code は非 0 になる（1 つの壊れたソースが同一グループの残り全系列を stale にしない）。1 pass は 1 つの fetch context を共有するので、複数 series が同じ bulk ファイルを参照しても download は 1 回、browser fallback を要する provider の起動も 1 回で済む。取得値は store へ入る前に有限値であることを検証し、NaN / ±inf は取得失敗として扱う（派生計算・percentile・export を汚染させない）。`provider_runs`は取得の成否と`record_count`を記録する。

### PMI release manifestの更新

PMI は data API が無いため、月次 release URL の manifest（`engine/src/baibai_engine/macro/indicators/providers/pmi_release_urls.yaml`、`schema_version: 2`、PMI stream ごとに `observed_at` → 公式 release URL）を正本とし、`spglobal_pmi` provider が各 URL の公式 PDF を live 取得して headline 値を抽出する。release URL の validator は `https://www.pmi.spglobal.com/Public/Home/PressRelease/<32 hex>` だけを許可する。

抽出は対象月のheadline値だけを読み、sub-index・composite・閾値比較・flash見積り・複数月平均を混入させない。未対応の表現や矛盾する候補は取得失敗とし、値を推測しない。具体的な英文の認識規則と数値検証は[PMI extractor](../../engine/src/baibai_engine/macro/indicators/providers/pmi_extraction.py)が所有する。

1 月 = 1 PDF なので、通常の refresh は store に無い月と、manifest の release URL が store の値の出所と一致しない月だけを取得する。final headline は公表後に改定されないため、同じ URL から取り直した月は同じ値になる。URL を訂正すればその月は自動で取り直される。抽出規則の変更後など stream 全体を source から作り直すときは `refresh --all-history` を使う（全月を再取得する）。

**新しい月の追記は `baibai_engine.macro.indicators.pmi_manifest` で行う**: S&P Global の公式 press release ページ（`/Public/Release/PressReleases`）が列挙する最新 release を stream ごとに拾い、release title の完全一致で系列を同定し、**release PDF が自分をその PMI と名乗り・自分の embargo 日が index の公表日と同じ月であり・headline をその月に結び付けていること**を 3 つとも確かめてから追記する。抽出には「その release 自身の月」として渡さず後の月を渡すので、月を明示しない reading（`posted 54.8`）は採られず、月を名指した reading（`posted 54.8 in June`）だけが通る。title を別の PMI に取り違えても月の証明だけは通ってしまうので（services も manufacturing と同じ形で headline を述べる）、系列は PDF 自身の名乗りで、公表月は PDF 自身の embargo 日で確かめる。**月が飛ぶ追記は拒否する**: provider の追いつき guard は manifest の最新月しか見ないため、穴を越えた entry を書くと飛ばした月が二度と報告されない。edge WAF が plain HTTP を challenge page（200）で返すときは、PDF 経路と同じ headless browser で index を読み直す。書き込みは copy を text 編集して provider の loader で読めることを確かめ、通ったものだけを正本へ move する（正本への書き込みは検証後の move 1 回だけ）。追記後は `macro refresh` で該当月を取得して公表値と照合してから commit する。

```bash
uv run python -m baibai_engine.macro.indicators.pmi_manifest --dry-run   # 何が追記されるかだけ見る
uv run python -m baibai_engine.macro.indicators.pmi_manifest             # 検証を通った entry を追記する
```

**過去月の穴埋めは同じページの archive snapshot から辿る**：この index は最新 1 か月分しか列挙しないため、それより古い release id はサイトからは辿れない。Wayback の同 URL の snapshot が捕捉時点の一覧（公表日・タイトル・release id）を持つので、そこから月ごとの id を復元する。**復元した対応付けは、manifest に既にある月の id と一致するかで検算する**（release は前月分を報告するので、publish 月 − 1 が observed_at になる）。id が復元できない月でも、翌月の release が前月値を restate していればそこから読める：`observed_at` をその月、`release_observed_at` を翌月にして翌月の URL を書く（restate は「前月を名指した値」か「月を伴わない `down from X`」で書かれるため、後者は前月分としてだけ読む）。manifest が公表カレンダーに追いつかない（release 済みの月が manifest に無い）状態は取得の無音の停止になるため、provider が明示エラーで失敗させる: 要求 end の月に対して manifest 最新月が前月に達していないとき（当月 10 日以降）にエラーになり、日次バッチの繰延べ失敗として表面化する。このガードは取得（refresh / all-history）だけを止め、読み取りは store にある月をそのまま返す（manifest の追記漏れが既存データを隠さないため）。

```bash
uv run baibai-engine macro refresh jp.pmi_manufacturing --start 2026-05-01 --end 2026-06-30
uv run baibai-engine macro refresh jp.bankruptcies --all-history --end 2026-07-24
uv run baibai-engine macro get jp.bankruptcies --start 2003-01-01 --end 2026-07-24
uv run baibai-engine macro get jp.pmi_manufacturing --start 2023-01-01 --end 2026-07-24
```

`macro.sqlite` は schema / series registry と各 provider（PDF / API / CSV）から再構築する L1 store であり、定期 backup は持たない。ただし PMI 履歴のように publisher が古い URL を落とすと再取得できない部分があるため、R2 への push は上書き対象の 1 世代を `<key>.bak` として残す（[`batch/OPERATIONS.md`](../../batch/OPERATIONS.md)）。

### Baibai Loop で現在局面と系列履歴を読む

`baibai-web` の `/api/macro` は最新 L3 Context の判断抜粋、指定基準日の L2 reading、全系列の現在読み値を返す非 canonical projection である。画面は Context の `summary`、`risk_environment`、`synthesis.dominant_forces`、`synthesis.interactions`、`scenarios` を現在局面として最初に示し、新しい要約やscoreを生成しない。Contextの更新日時は `published_at`、readingの基準は `asof`、系列の観測日は `observed_at` として別々に表示する。

初期 response は full chart history を持たず、一覧用に各系列の約1年・月次 period-end を最大13 pointだけ返す。これは方向を走査する固定windowで、期間・粒度selectorの対象ではない。行を開いた時だけ `/api/macro/series/<series_id>` がその 1 系列の daily 全履歴を返し、詳細chartの期間 `1y | 5y | 10y | max` と粒度 `daily | weekly | monthly | yearly` は browser 内で絞る。`series.yaml` に `tradingview_symbol` がある系列だけ TradingView の該当 symbol を新規 tab で開く。

### 変更時の検証

変更したprovider・計算・registryの契約を既存testで確認し、[Python foundation](./python-foundation.md#9-ci-and-local-parity)のmacro store検証を行う。外部の取得挙動を変えた場合は、その取得経路を確認する。任意の変更ごとに全系列のstaleをゼロにすることや、全providerのstress実行は要求しない。

<a id="macro-reading"></a>

## ② 機械読み値：macro reading

macro reading は L1 store だけを入力に、登録全系列の **記述統計と鮮度** を決定論で出す L2 出力である。「今の VIX は歴史的に高いのか」を分析セッションごとに人が判断し直さないための共通の物差しであり、regime 分類・合成 score・売買 signal は出さない。

```bash
uv run baibai-engine macro reading --asof 2026-07-24                 # 表形式
uv run baibai-engine macro reading --asof 2026-07-24 --format json   # 機械読み
```

### 解釈上の注意

- percentileとz-scoreは`statistic`が示すlevelまたはyoyの分布内位置である。latest valueとtrendまでyoyへ変わるわけではない。
- `insufficient_history`では位置統計を使わない。観測された最新値そのものまで無効にする意味ではなく、期間・鮮度を示して読む。
- `next_print_estimate`はcadenceとlagからの推定で、公式event calendarではない。`stale`は観測の古さであり、値の誤りの確定ではない。
- flagsと極端なz-scoreは確認の入口であり、売買signalや誤値判定ではない。
- 異なる実効窓とsampling cadenceを同じ物差しとして比較しない。`window_years`、`window_observations`と規則revisionを確認する。

規則は`method/macro/reading/`のdated revisionに置き、出力の`rules_revision`で識別する。frequencyの既定と系列固有のoverrideを使い、既定がsourceの性質に合わない箇所だけを上書きする。厳密なfield・閾値・旧revisionの読込条件はrule modelとloaderを参照する。過去revisionの意味を現在の規則で書き換えない。

reading は L1 store を読むだけの純関数で、provider を呼ばず DB へ書かない。したがって過去日の asof でも同じ入力から同じ snapshot を再計算できる。CLI・read API・indicator chart は共通の store reader を使い、`ProviderSpec.point_in_time_vintage` を宣言する source だけを `vintage_at <= asof` へ clamp する。宣言のない bulk history の `vintage_at` は取得日時であって当時の公表日時ではないため、一律 clamp して取得前の過去 snapshot から既知だった履歴を消さない。専用 store は持たず、日次バッチが `baibai-web` 向けの現在読み値（`/api/macro`・`views/macro.json`）へ投影し、L3 レポートは引用した snapshot を `inputs.reading_snapshots` に記録する。Web projectionは指定asofのreadingを1回組み立て、canonical snapshotを増やさない。

過去as-ofの再計算は、全sourceの改定前情報を当時のまま再現する保証ではない。取得時刻と公表vintageを区別する。

Baibai Loop の Macro タブは、上から **現在のマクロ局面 → マクロ経済指標 → 過去の経済分析レポート**の順に表示する。現在読み値は `web/config/macro-panel.yaml` の 7 group 順に `series_id` で join し、最新値・観測日・短期/長期トレンド・`statistic`・percentile・`z_score`・実効窓・注記を並べる。行を開いた後だけ拡大チャートを取得する。**チャートの期間・粒度と percentile の実効窓は別物**なので、列見出しの ⓘ で明示する。

取得失敗、stale、insufficient_history、分布の端は別の状態である。系列の開始時点や配信範囲による履歴不足は、再取得すれば必ず解消する障害ではない。現在読み値ではこれらを件数と絞り込みに使い、indicator storeがない場合は読み値をunavailableとして、保存済みContextとreport indexを表示する。

## ③ 環境認識：macro context report を publish する

環境認識はapplication DBのimmutable revisionとして保存する。機械契約は[MacroContextDocument](../../engine/src/baibai_engine/macro/context/models.py)、writerは`macro context publish`である。入力生成、前回比較、check、独立review、CAS発行と読戻しは[Macro Context skill](../../.agents/skills/macro-context/SKILL.md#手順)に集約する。

Contextは常にfull深度で作り、統合評価・確率付きシナリオ・機械見積りの歪み補正・バーゲン地形を含める。日々の機械的な状況確認にはReadingを使う。作成は人間の判断で始め、定例義務や監視条件による自動更新を設けない。鮮度は読む側が判断する。

<a id="3-層構成core環境評価synthesis統合評価connection積立ループ接続"></a>

### 3 層構成：core（環境評価）・synthesis（統合評価）・connection（日本株ループ接続）

coreはチャネル別の環境評価、synthesisはチャネルを横断する力、connectionは日本株ループへの含意を所有する。読む順はsummary → synthesis → core → connectionとする。

coreとsynthesisにはsector tilt・research優先度・sizing cautionを持ち込まない。connectionのseriesはcoreの引用に束縛し、市場snapshotやループ固有の記事はconnection自身の入力にできる。参照の機械検証はmodel、文章に行動指示が混入していないかは独立reviewが確認する。

### synthesis：支配的な力と相互作用

Readingの極値・flags・トレンド反転を8分析レンズと照合し、複数チャネルへ波及する力を選ぶ。力ごとに機序・伝達経路・方向・確度を説明し、支持と反証の一次情報を示す。相互作用が判断を変える場合に限り、その関係を書く。引用できるセクション・series・force間参照の厳密な制約はmodelを参照する。

### セクション別の問い

| 部 | 順 | セクション（`section_id`） | 確認するfact | judgmentと接続 |
| --- | --- | --- | --- | --- |
| synthesis | — | 統合評価（`synthesis`） | 名指ししたセクションが引用済みの series のみ | 支配的な力（機序・伝達経路・反証・方向・確度）と、判断に必要な力同士の相互作用 |
| core | 1 | レジーム要約（`regime_summary`） | 成長・インフレ・金融条件の水準と方向、比較可能な時点からの変化 | 成長×インフレ×金融条件の共通座標で現局面を定め、以降の読み順を示す。前回 scorecard の採点結果を接続する |
| core | 2 | 金利・金融政策（`rates_policy`） | 政策金利、イールドカーブ、実質金利、主要中銀の方向 | discount rate経路とmaterial deltaを示す |
| core | 3 | 景気・需要（`growth_demand`） | PMI、雇用、消費、生産、景気breadth | 需要経路への接続を示す |
| core | 4 | インフレ・コスト（`inflation_costs`） | CPI、賃金、輸入物価、commodity、原油と通商政策 | 売価転嫁とmargin経路を示す |
| core | 5 | 流動性・信用・リスク選好（`liquidity_credit`） | net liquidity、credit OAS、VIX/MOVE、NFCI | funding条件と共通tail riskを示す |
| core | 6 | 為替（`fx`） | USD/JPY、金利差、実質実効為替 | 円水準の両側リスクを非対称ごと示す |
| core | 7 | 日本（`japan`） | BOJ政策、国内賃金物価、鉱工業生産、海外投資家フロー、日本の需要fact | 日本経済の需要・費用・為替感応度への接続を示す |
| core | 8 | バリュエーション（`valuation`） | 米ERP / CAPE、日本の市場PER・益回りとJGB10y、金、BTC。益回りと金利の差は同じ尺度で比較する | 各資産の相対的な位置を示す |
| core | 9 | リスク選好環境の評価とシナリオ（`risk_environment`） | セクション2〜8を支持・反証する系列 | 攻め／守りどちらの環境かを `stance`・確度・**反証条件**付きで評価し、base / bear / bull を **確率**と scorecard 条件付きで置く |
| core | 10 | 監視ポイント（`monitoring`） | 次の公表・会合と観測条件 | 何が出たらどの見方を変えるかを prose で明記する |
| connection | 11 | 日本株ループ接続（`japan_equity_loop`） | core が引用済みの series のみ | research 優先度ヒント（効く候補タイプを `applies_to` で判別可能に）、sector tilt、sizing caution、**バーゲン地形（`bargain_topography`）**、**機械見積りの歪み補正（`estimate_caveats`）** |

### 入力と判断の束縛

| 入力 | 証拠として示すもの |
| --- | --- |
| articles | 実際に確認した外部資料とその公表・取得時点 |
| indicator_series | Readingが採用した観測source・系列・観測範囲・vintage。PIT系列の公表版と、bulk系列の取得版を区別する |
| machine_snapshots | 自前commandの出力と対象as-of。外部記事ではない |
| reading_snapshots | 採用したReadingのas-ofとrules revision |

Contextの`as_of`は市場データの最終完全営業日を基準にし、著述日や`published_at`と分ける。Research Triageが判断as-of以下のeligible Contextを束縛するのであって、Review SetがMacroを入力にするわけではない。

summaryは全体の結論、coreは各経路の根拠と判断、synthesisは経路を横断する力、connectionは日本株の調査への含意を所有する。connectionのseriesはcoreの根拠に結び、対象の異なるresearch hintは`applies_to`で区別する。bargain topographyは市場snapshot、estimate caveatは影響する見積り成分に接続する。

型・件数・ID・確率の刻み・引用参照の検証はmodelとpublish checkを正本とする。文書はschemaを写す代わりに、根拠が主張を支えるかを独立reviewで確認する。

published revisionの文書内部の整合は読取時にも検証する。変動するregistryへの所属と公表頻度は発行時に検証し、系列の退役で過去レポートを読めなくしない。退役系列を使うscorecardの採点可否は、保存レポートの閲覧とは別に扱う。

<a id="japanese-writing"></a>

### 日本語表現

判断内容を確定した後の文章編集は[judgment-writing](./judgment-writing.md)に従う。fieldごとの役割は前掲のContext構成に従う。

<a id="depth-contract"></a>

### 深度契約（全レポート共通）

レポートは銘柄選定とスポット判断のリスクリワード判断の土台になるため、次の深度契約を常に満たす。

- **統合が最上位の契約**: 支配的な力（synthesis）は、reading の極値・flags・トレンド反転を束ねて名指しし、伝達チャネルへの波及を説明する。力ごとに支持する一次情報と**反証する一次情報の両方**を読んでから書く。相互作用が判断を変える場合は joint risk を書く。この文章品質は独立 review が担い、件数で代理しない。
- **焦点fact**：各経路の判断を動かす観測を示し、Readingの全数値を本文へ転記しない。全系列の座標はsnapshotとUIで参照できるため、数値一覧で統合判断を代用しない。
- **テーマ被覆**: 金利・政策 / インフレ・コスト / 需要・雇用 / 為替・流動性・credit / 日本の政策・金利 / 日本の需要 / energy・地政学・通商 / 市場内部・バリュエーション の8象限すべてにfactを置く。`inputs.articles`は件数を品質の代理にせず、各 load-bearing claim と dominant force に有効な一次情報、支持 evidence、counter-evidence が解決することを独立 review で確認する。自前の `machine_snapshots` / `reading_snapshots` を外部 evidence と数えない。
- **日本の需要fact最低ライン**: セクション3または7に、実質賃金（毎月勤労統計）または実質消費、鉱工業生産を必ず含める。取得可能ならインバウンド（訪日外客数）・機械受注も置く。米国factだけで需要判断を組み立てない。
- **円水準の両側リスク**: セクション6に、円安継続と円反転（介入・利上げ）の両経路が輸出企業（為替換算益の剥落）と輸入コスト企業（margin回復）へ与える非対称を1つのjudgmentとして書く。片側の監視条件だけで済ませない。
- **バーゲン地形**: connection に`screening market-snapshot`のbenchmark 20d/60d・breadth・`benchmark_trend`をfact引用し、「この局面でミスプライスがどこに出やすいか（全面安で広く出る / 回転相場で取り残しに出る / 全面高でプールが縮む）」を`bargain_topography`として書く。機械 gate ではなく独立 review が接地を確認する。オプション IV は「市場が何をどれだけ恐れているか」の観測として併記できるが、買い時や投入判断には使わない。
- **日本株バリュエーションアンカー**: セクション8に市場全体のPERまたは益回り（日経・JPX公表の一次値、または全universeのin-house中央値）とJGB 10yの対比を置き、個別FVアンカーの妥当性を外側から検算できるようにする。
- **hintの識別力**: 全候補に等しく当てはまる助言（「net cash重視」等）はhintではない。各 research 優先度ヒントと sector tilt は、どの候補タイプ・sectorに効くかを`applies_to`で判別できる形で書く。
- **energy・通商・地政学**: セクション4または5に、原油と通商政策（関税）・地政学tailのfactを最低1つずつ置く。

### scenario scorecard：見立てを後から採点できる形で書く

base/bear/bullの主観ウェイトと機械照合可能な観測条件を組にする。条件の保存形式・件数・期限はmodel、実行とsnapshot identityの接続は[Macro Context skill](../../.agents/skills/macro-context/SKILL.md#手順)が所有する。成立数を予測精度の保証にしない。

観測日はContext as_of翌日からmin(条件期限, 採点asof)まで。最初の成立を`met`、未成立で期限前なら`pending`、期限後なら`not_met`とする。観測0件でも現実装は`not_met / observation=null`となるため、完全な履歴や現実の不成立を証明したとは読まず、採用観測と入力不足を併読する。

PITを宣言するproviderだけ採点asofでvintageを制限する。bulkの取得時刻を当時の公表時刻と同一視しない。current registryにない退役系列は採点できない場合があり、保存済みContextの閲覧とは別の制約である。任意の過去時点の完全な遡及再現は保証しない。

### 監視ポイント

セクション10の各監視ポイントは、観測する event、見方を変える condition、変わる judgment を prose で明記する。単一閾値の trigger subsystem は持たない。レポートの鮮度は consumer が `as_of` と内容を読んで判断し、機械的には 45 日の staleness warning だけを出す。

### 鮮度は読む側が判断する

レポートは自分の賞味期限を宣言しない。鮮度の扱いは consumer が自分の規則として持つ。

- **Research Triage**: scaffold は判断 asof 以下の最新 revision を束縛する。eligible revision があるのに `null`、未知 ID、future は publish error。古さは warning で、Review Set・ranking・machine snapshotを変えない。明示的に古い eligible revision を選ぶ場合は既存の判断文へ理由を残す
- **Fundamental Research**: 束縛 Context の estimate caveat を scenario / FV の前に読み、material なものを既存 assumption と source ID へ接続する。適用外・stale・low materiality は既存 note に理由を残す
- **Baibai Loop**: Macro タブは Context の更新日時 `published_at` を主要metaにし、Contextの基準日 `as_of`、readingの `asof`、系列の `observed_at`、staleness warningをそれぞれのsection/detailで分けて表示する

### 分析の独立性

前回の判断から隔離して今回の評価を作り、確定後にscorecardへ接続する。順序と発行条件は[Macro Context skill](../../.agents/skills/macro-context/SKILL.md)が所有する。Contextには環境認識を保存し、次回の作業指示を書かない。

## ④ ナレッジ：8 分析レンズ

以下は複数系列から仮説と反証を考える観点であり、固定の因果モデルや売買signalではない。

1. **グローバル流動性**：net liquidity ≈ `us.fed_assets` − `us.reverse_repo` − `us.tga`（単位換算注意）。`us.m2`とrisk assetの関係は対象期間・市場・政策局面ごとに検討し、固定の先行週数を仮定しない。
2. **実質金利・store-of-value**：`us.real_10y` + `us.breakeven_10y` + `usd_index.broad` + `gold`。名目 = 実質 + 期待インフレに分解。日本側は `jp.real_10y_proxy`（月末10Y JGB − コアCPI前年比）で、名目金利の上昇が実質でも締まっているのか、インフレに食われて実質マイナスのままかを読む。
3. **金融環境の合成**：`us.nfci` を `vix`・`us.move`・クレジット OAS と突き合わせ、slow-burn（広範化前の局所ストレス）を読む。
4. **リスク選好の温度計**：`btc_usd` + `vix` + `credit.us_hy_oas`/`credit.us_ccc_oas` + `us.nfci`。BTCは他のrisk指標と併読し、普遍的な先行指標とはしない。日本株の判断には `jp.n225_iv_30d` を併読する——`vix` は米国市場の恐怖で、判断対象が日本株なら代理変数になる。`jp.n225_iv_skew`（0.95 put − ATM put を 30 日満期へ補間したもの、大きいほど下落を恐れている）と `jp.n225_iv_term`（第 2 限月 − 手前限月、僅かな逆転は平常で、大きな負が目先のパニックが先の見通しより強い状態）を合わせて読む。水準の高低は他の系列と同じく **reading の `percentile` を正とする**——[option IV quantiles study](../../reports/studies/2026-07-31-option-iv-quantiles/report.md) は分位を凍結した第二の物差しではなく、この 3 系列が「いつ出ないか」「何に依存するか」を測った記録であり、store が 10 年窓を満たすまでの間だけ水準の当たりを付けるために読む。`iv_30d` と `iv_skew` は手前 2 限月が 30 日を挟む日にしか出ない（実測 83%）ので、SQ 直後に数日まとめて欠けるのは異常でなく、チェーンが 30 日を値付けしていないという事実である。同一行使価格のプットとコールの IV が大きく食い違う日は差を取る 2 つ（skew / term）を出さず `iv_30d` だけが残る——これも欠測でなく、その日は差を取れないという事実である。
5. **景気サイクル・breadth**：`us.initial_claims` + `us.industrial_production` + `copper` + `us.russell2000` + `us.10y_3m_spread`。`us.sox`は半導体関連の市場価格を読む補助であり、その上昇だけで日本企業の需要改善を確定しない。
6. **バリュエーション・ERP**：株式益回りと国債利回りを同じ尺度で比較する。米国は`us.sp500_earnings_yield`と`us.10y`、日本は市場の益回りとJGB10yを対比する。PERを使う場合は正のPERの逆数へ換算し、倍率のまま金利を引かない。実績/予想、指数集計/個別中央値のbasisを示し、この差を完全なrisk premium推計や売買signalとは扱わない。`us.sp500_cape`も長期valuationの文脈で読む。
7. **グローバル中銀の同期**：`us.fed_funds.upper` + `jp.policy_rate` + `ecb.policy_rate`。1 国でなく同期を読む。
8. **エネルギー・地政学**：原油、金、為替、物流、通商政策が調達費用・売価転嫁・需要へどう伝わるかを確認する。輸入依存率や航路比率は対象品目、数量/金額、分母、時点と一次sourceを示す。原油上昇だけで為替方向やスタグフレーションを確定しない。

## 意図的な境界（不足ではなく設計）

次の 3 点は「機能が足りない」ように読めるが、意図して引いた境界である。

**中国は proxy basket で読む。** 中国の直接系列は `usd_cny` の 1 本だけである。NBS 等の公式配信に機械可読で安定した無認証経路が無く、脆い scrape provider を足すと「取り込みの停止」と「系列自体の停止」を store の上で区別できない無音の失敗を増やす（§① の relay に関する注意と同じ理由）。代わりに **`copper`（中国の実需）・`aud_jpy`（資源国通貨として中国感応度が高い）・`em.equity`（EEM）・`usd_cny`** を横に読み、中国の需要と資金の向きを推す。安定した機械可読 source が現れたらこの方針を再評価する。境界は **L1 の系列取り込み**の側にあり、L3 のレポートでは NBS / 海関総署の公表値を `inputs.articles` の一次情報として引いてよい（[`./data-sources.md`](./data-sources.md)）。

**`next_print_estimate` は上端であり、entry timing には使えない。** これは「これ以降なら公表済みのはず」の線で、平常の公表待ちで負値や `stale` を出さないよう遅い側へ寄せてある。一方で「保有ウィンドウ内に CPI が落ちるか」のような事前確認には、**早い側に外れるイベントを見逃す**ので使えない。その用途には下端推定が要り、現状は L3 の monitoring と人手の暦確認が担う。

percentileの比較は[Readingの解釈上の注意](#macro-reading)に従う。

## Material deltaとAIの境界

macro contextはdiscount rate、需要、資金調達、共通tail risk、sizing cautionだけを表す。AIの役割と株主価値の獲得可能性はmacro contextに置かず、企業別thesisで評価する。

## 接続：判断層にだけ効かせる（screen は macro-blind）

マクロの読みは機械スクリーニングの `run` には接続しない（`run` は財務事実だけを扱う決定論的なエンジンのまま）。効かせるのは判断層だけ：

- **Research Triage**（[`./screening-runtime.md`](./screening-runtime.md)）：material deltaと`as_of`鮮度warningをcontext-level summaryとして出す。E[r]とSecurity Analysisの事実層は変えない。
- **research**：material deltaが個別企業のinvestment case、Base/Downsideまたは反証条件へ影響する場合だけ、Thesisのjudgmentへ因果とsourceを残す。判断期間と算術は[Thesis](./thesis.md#scenario-arithmetic)に従い、通常12か月と理由付き別期間のどちらでも、その期間に効く材料を扱う。マクロを数値ドライバー、採用gate、投入額ルールにはしない。
- **connection セクション**：Research Triageがresearch優先度ヒントとsizing cautionを消化する入口になる（skill `research-triage`）。

行動指示（売買タイミング・現金比率・配分指示）はcore にもconnection にも書かない。sector tiltとresearch優先度ヒントは着手順位を判断するjudgment入力であり、機械ranking・hard gate・自動sizingへは接続しない。

## 誠実性（honesty firewall）

この工程のContextと主観scenarioから、統計的な売買edgeや予測精度の保証は主張しない。Readingは記述統計であり、scenarioのウェイトとscorecardは見立てと条件成立を振り返るために使う。自動sizingの入力にはしない。これは本工程の根拠の範囲を限定する規則であり、macro全般の統計分析が不可能という主張ではない。

## 参考

- [`../doctrine.md`](../doctrine.md)：思想・柱 2（macroとAIの責務境界）
- [`./screening-runtime.md`](./screening-runtime.md)：material deltaとas_of鮮度warningを読むResearch Triage
- [`./data-sources.md`](./data-sources.md)：データソース Tier
