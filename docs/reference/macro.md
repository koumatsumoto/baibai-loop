---
title: "Reference — macro analysis"
summary: "マクロ環境分析：L1 指標を毎営業日 L2 reading で機械読み値にし、人間が判断するときだけ L3 macro context report（core 環境評価 10 + synthesis 統合評価 + connection 積立ループ接続）を書く。"
doc_type: reference
status: active
---

# Reference — マクロ環境分析

マクロ環境分析は **独立した機能のまとまり**（データ取得層 + 機械読み値 + リサーチの実践）であり、形式化した独自ループにはしない。狙いは、個別銘柄の5年期待値を変え得る外部経路と共通riskを判断層へ供給すること。sector順位、相場方向、買い時、投入額を決めない。

扱うものは性質の異なる 4 種：**① データ（L1 の事実）／ ② 機械読み値（L2 macro reading）／ ③ 環境認識（L3 macro context report）／ ④ 知見（調べ方のメタ知識）**。①②は毎営業日 CI が機械で回し、③は人間が判断するときだけ書く。マクロは標本数がほぼ 1 の判断であり、優位性の数値・統計的有意性・自動の投入額倍率は出さない（§誠実性）。

## ① データ：indicator series を引く

指標データは `baibai-engine macro`（`engine/src/baibai_engine/macro/indicators/`）で、再現可能かつ出所（provenance）付きで取得・キャッシュする。

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

observation は `(series_id, observed_at, vintage_at)` を主キーに upsert する。多くの provider は取得時刻を vintage として刻むが、挿入前に vintage を除いた内容（値・単位・期間・取得状態・source）を既存最新 vintage と比較し、変化が無ければその再取得行を捨てる。したがって **同じ refresh を何度実行しても、ソースが改定した series の観測だけが新 vintage として増え、それ以外はテーブルが不変**になる（べき等）。ローカルで `--all-history` seed → cloud で日次 refresh、cloud の多重実行や手動 rerun も同じ性質で安全に収束する。cloud 正本の履歴を後から深くするときは、ローカルで `--all-history` を回してから `batch/scripts/r2_transfer.sh push-macro` で載せる（cloud copy を merge してから upload するので、日次 refresh が取った最新観測を失わない）。日次バッチは asof を終端とする frequency 別の窓（daily 14 日・weekly 60 日・monthly 以下 370 暦日）を毎回丸ごと再取得するため、窓内で起きた一時的な取得失敗は次の成功実行が同じ窓を引き直して自動でバックフィルする。窓を超える長期の取得断や旧 vintage の全面リベースが必要なときだけ `refresh --all-history` を運用レバーとして使う。

誤って入った observation は削除では消えない。cloud との merge は双方の fact を必ず戻す no-loss 契約なので、ローカルで消しても次の push で復活する。この契約は本物の履歴を守るためのものなので緩めず、代わりに **今わかっていることを新しい vintage として上に積む**: `baibai-engine macro retract <series_id> --observed-at <date> --expected-vintage <ts>` が対象 observation 日の**最新 vintage を撤回**し、その 1 つ下にあった状態を現在時刻の vintage で書き直す。撤回する vintage を名指すのは publish の `--expected-head` と同じ compare-and-swap で、これが無いと同じコマンドの 2 回目が「復元した行の下にある誤値」を読んで書き戻してしまう。名指してあれば 2 回目は拒否になる。**撤回対象が derived 系列の入力なら、その derived 系列の同じ日も先に撤回する必要があり、コマンドが書き込み前に拒否して対象を印字する**（derived は自分の観測を持つので、入力を撤回しても計算済みの値は消えず、再計算でも直らない）。

- 下に正しい観測があれば **その観測が再び読まれる**（誤った writer が正しい値の上に別の値を刻んだ場合。撤回のたびに 1 つずつ vintage を遡る）
- 下に何も無い、または下も撤回済みなら `fetch_status='retracted'` を書き、その日は reading / chart / scorecard / freshness のすべてから外れる

読みは「観測日ごとに `ok` と `retracted` の中から最大 vintage を採り、それが `ok` のときだけ返す」。`failed` / `unreleased` は取得の結果であって値についての主張ではないので、読めていた観測を隠さない。書かれた行は普通の fact なので merge が両方向へ運び、他方の store に残る誤った行より必ず新しいため上書きされない。`--all-history` を含む全ての store 書き換えは、**provider の最新の主張より新しい vintage の行を消さない**——書き換えが置換してよいのは provider が言い直すものだけで、その後に store が下した決定（撤回とそれが戻した観測）ではないからである。取得時刻を vintage に刻む provider では最新の主張が「今」なので、この規則は何も余分に残さない。provider が同じ観測日を再配信した場合だけ、新しい `ok` vintage が上に乗って復活する（「源泉が再び主張する事実は読む」で正しい）。

point-in-time provider（`jquants_flows`）では撤回の vintage が公表時刻ではなく操作時刻になるため、**撤回は撤回時刻以降の as-of にしか効かない**（それ以前の as-of での replay は撤回前の値を読み続ける。当時そう信じていたことの誠実な表現である）。同じ理由で、撤回時刻より前の公表 vintage を持つ改定は撤回の下に埋もれる。実際に撤回した観測は source が publish しない幽霊日なので改定の余地が無いが、real な観測日を撤回するときはこの境界を意識する。

registry は系列定義の正本だが、DB を開く read 操作は登録外系列の facts・metadata・aliases を削除しない。open 時は現行 registry が知る系列の metadata / aliases だけを upsert し、`series` / `observations` / `provider_runs` の prune は、現行 registry の系列を 1 件以上指定した明示的な `macro refresh` の開始時だけ実行する。registry の series ID 集合には単調増加する generation を対応付け、store の generation が client より新しければ stale branch として refresh を拒否する。series ID を追加・削除するときは `definitions.py` の membership digest を次の generation として追記し、退役・改名では後述の validator を前後で回して発行済みレポートへの影響を確認する。prune は `BEGIN IMMEDIATE` 内で件数集計から commit までを行い、commit 前の `registry-prune-pending` と commit 後の `registry-prune` を同じ transaction ID で出力する。pending を出力できなければ全削除を rollback し、pending だけが残った実行は未確定として扱う。production の prune 入口はこの service 経路 1 本であり、SQLite への直接 DELETE を別の authorization 機構では包まない。

generation は refresh のたびに store へ書かれるので、**series を追加した registry は、その store がクラウドへ渡るより先に main へ入っていなければならない**。日次バッチは main のコードで動くため、main が古い generation のままだと macro の全系列が毎日拒否される。同じ理由で、store を触る checkout は main を取り込んでから refresh する。

全 provider の observation は insert 前に requested `series_id`・registry の unit・finite・series 固有の `plausible_min` / `plausible_max` を照合する。SQLite の INSERT / UPDATE 境界も unit と band を強制し、`foreign_keys=OFF` の直接writerでもunknown seriesを拒否する。複数行 insert は savepoint 単位で全件成功または全件 rollback するため、cloud merge を含む service 外の writer も部分取り込みや検証迂回を起こせない。store を開くたびに、`schema.sql` が定義する persistent trigger と singleton の registry state を実 store へ完全一致させる。version 番号だけ合う欠落・改変・予期しない追加 trigger や state drift は拒否する。**store は空、直前 schema からの一段移行、現行 schema のいずれかだけを受け付ける**。過去へ戻る通路や複数世代の migration は持たず、schema を進めるときは実在 store に必要な 1 段だけを書く。`jp.foreign_flows` は JPX/J-Quants の raw 値を千円単位のまま保持するので unit は `jpy-thousand` である。band は直近 10 年の実績へ十分な桁余裕を持たせ、長期履歴も全件通るまで拡張した明白な列・桁・単位ずれの検出境界であり、景気急変を異常扱いする前回値ジャンプ判定ではない。band 内に残る scale 変更は source identity / header / metadata の provider 固有検証で守る。BOJ xlsx は値列番号・英語 header・metadata列番号・基準年または単位metadataを組にして検証し、隣列に同じ旧metadataが残っても代用しない。対象期間の date row があるのに選択列の数値が 0 件なら失敗する。1 点でも契約違反なら部分取り込みせず、その series の provider run を failed として残す。

registry の band を追加・変更する前後は、git 管理外の live store を read-only validator で全履歴・全 vintage 検査する。validator は store を書き込みで開かず、canonical trigger 契約も併せて検証する。登録外系列、band 未宣言、unit 不一致、非有限値、band 外値のいずれかがあれば observation identity を出して非 0 で終了する。

同じ validator が **application store の発行済み macro context revision を全件 load** する。code / registry が不変の発行済みレポートより先へ進む drift は CI では検出できず（workflow には application store が無い）、両 store が揃うのは local だけなので、この検査は push 前の運用計器として置く。現行契約の revision が 1 件でも read 経路で load できなければ非 0 で終了する（`screening review-set publish` と scorecard が使うのと同じ経路であり、落ちれば日次バッチが止まる）。**registry の系列を退役・改名する前後は必ず回す**。退役系列を引用するレポートは load できる限り warning で報告し、fail にはしない——退役は正常な運用であり、履歴の書き換えは選択肢に無い。scorecard 条件の系列が退役している場合はそのレポートが今後採点不能になるため、warning でその旨を明示する。application store が無い checkout（fresh clone）は skip する。

```bash
uv run baibai-batch validate-macro-stores \
  --db stores/macro/macro.sqlite --app-db stores/application/baibai.sqlite
```

### データソース registry

| Provider | 取得 | 担当ドメイン | 確認手順・既知の caveat |
| --- | --- | --- | --- |
| `fred_csv` | 無認証 CSV | 米マクロ・実質金利/期待インフレ・FX・原油・VIX・クレジット OAS・BTC・流動性・NFCI・JP 実質実効為替 | 系列 ID を `fredgraph.csv?id=<ID>` の header で実 fetch 確認。ICE BofA OAS は直近 3 年、S&P / Dow は直近 10 年が現在の配信範囲。**廃止系列あり**（JP OECD CPI は 2021 停止、金 LBMA は 2025/5 停止）。金・SOX は `yahoo`。relay の取り込み停止は store の上では系列自体の停止と区別できないので、publisher が機械可読な配信を持つ系列は publisher 直読を優先する |
| `frb_h15` | 無認証 CSV（requests→browser fallback） | 米国債金利・スプレッド | 1 package を series 横断に 1 回 DL。edge の bot mitigation が datacenter IP へ CSV を出さず block page（200）/ 空 body / 403 challenge を返すので、ブラウザ UA を送り、それでも CSV が来なければ headless browser（Playwright）で取り直す。取り直した CSV も同じ fetch context に載るので DL は 1 回のまま。**遮断以外の失敗（サイズ上限・404・5xx・ネットワーク）は fallback しない**（別経路で取り直しても解決せず、上限ガードを迂回するだけのため） |
| `ecb_fx` | 無認証 ZIP | JPY クロス（USD/EUR/AUD） | JPY と基軸通貨の比で算出 |
| `estat` | API（`ESTAT_APP_ID`） | JP 公式マクロ（CPI 総合・サービス、鉱工業生産、機械受注、景気ウォッチャー、消費者態度指数 等） | JP CPI の一次ソース。`statsDataId` と分類 code は e-Stat で確認。**e-Stat の DB 掲載は統計ごとに止まる**（毎月勤労統計は 2021-10 以降更新なし。月次結果は release 毎のファイル資源だけになる）ので、新規系列は `getStatsList` の `UPDATED_DATE` が現在かを先に確認する |
| `estat_dashboard` | 無認証 JSON API | JP 公式マクロのうち e-Stat DB が持たない系列（完全失業率 季節調整値・名目賃金指数） | 統計ダッシュボード（総務省統計局）の `getData`。1 IndicatorCode が月次/四半期/年 × 原数値/季節調整値を同時に返すため、**`source_url` に `IndicatorCode` と `Cycle=1` / `IsSeasonalAdjustment` / `RegionCode` を書いて 1 本に固定する**（observation に残る provenance が上流系列を名指すので、selector を直せば旧系列の観測が source 違いとして掃除される）。provider は全行の `@indicator` / `@cycle` / `@isSeasonal` / `@regionCode` と宣言 total 行数を照合し、filter が効かなかった応答を混入させない。`@isProvisional` が速報の行は要求した系列そのものなので照合の対象外とし、その月を書かずに skip して series と月を警告に出す。確報が出た日の refresh がその月を埋める（reading rules の `publication_lag_days` は確報基準なので、速報しか無い期間はそもそも公表待ちで stale にならない）。読むのは月次のみで、要求窓に関わらず公表全履歴を取る（指数の基準改定が窓の境目で継ぎ足しにならないため） |
| `jquants_flows` | 認証（`JQUANTS_API_KEY`） | JP 市場内部（海外投資家フロー） | screening と同じ Light credential。`--all-history` は運用日から5年の契約窓を要求する。JPX/J-Quants の公表単位（千円）を `jpy-thousand` として保持し、集計週末を observation、公表日を vintage として同一公表日の複数週を保持する |
| `jquants_options` | 認証（`JQUANTS_API_KEY`） | 日経225オプションの恐怖観測（30日IV・スキュー・期間構造） | 1営業日ごとに取得して3系列へ集計する。rate limit、式変更、購読窓、緊急取引証拠金日の契約は[専用手順](#jquants-options-provider)に従う |
| `boj` | 無認証 xlsx | BOJ 長期時系列（マネタリーベース・実質輸出・消費活動指数） | 第1 sheetを openpyxl で読み、registry の `provider_series_id` が宣言する値列・英語header・metadata列・基準年または単位metadataを照合する |
| `boj_timeseries` | 無認証 JSON API | BOJ 無担保コール O/N 平均 | `FM01:STRDCLUCON` の日次値を一括取得する。公表タイミングは BOJ 時系列統計データ検索の更新日に従う |
| `mof_jgb` | 無認証 CSV | JP 国債金利（主要年限） | `jgbcm_all.csv` と当月 `jgbcm.csv` を CP932 で読み、和暦の基準日を ISO date に正規化する |
| `tsr_bankruptcies` | 無認証 JSON API | JP 企業倒産件数 | 東京商工リサーチの掲載ページが参照する公式 JSON から月次全履歴を取得 |
| `spglobal_pmi` | 無認証 PDF（requests→browser fallback） | S&P Global PMI（日本/米 製造業・サービス業） | free の data API が無い。`providers/pmi_release_urls.yaml` の月次 release URL から公式 PDF を取得し、headline 値を bounded context から抽出して diffusion index の定義域 0〜100 で検証する。WAF gated の月は headless browser（Playwright）で fetch する。**遮断以外の失敗（サイズ上限・404・5xx・ネットワーク）は fallback しない**（frb_h15 と同じ契約） |
| `umich_sca` | 無認証 CSV | 米消費者態度指数（ミシガン大） | 公表元 Surveys of Consumers の月次表（`files/tbmics.csv`）を直読する。`provider_series_id` は値の列名（`ICS_ALL`）、`source_url` が表なので同じ公表元の別表は registry entry だけで足りる。行が「月名 + 年」なので読めない行は skip せず失敗させる（表の形が変わったのを黙って短い履歴にしない） |
| `yahoo` | 無認証 JSON | 金/銀/銅先物・MOVE・Russell2000・SOX 等 | **ブラウザ UA 必須**（default は 429）。`provider_series_id` は Yahoo シンボル |
| `multpl` | 無認証 HTML | S&P500 バリュエーション（CAPE・GAAP PER・益回り） | current page と public monthly table を機械的に parse する。取得・鮮度の契約は daily を保ち、reading rules の `sampling_cadence: monthly` で統計標本だけを月次化する。HTML 構造変更で壊れるため `--latest` と `--all-history` を live 確認 |

<a id="jquants-options-provider"></a>

### `jquants_options`の取得と復旧

#### 取得とrate limit

`jquants_options`は1営業日につき1回呼び出すため、10年の`--all-history`は約2,600回の逐次取得になる。1日最大約1万行のchainは保存せず、取得時に3系列へ集計する。

HTTP 429はclient内部の再試行が尽きた後、statusを持たない`RetryError`として返る。待避対象は例外の型で判定し、待機は1 processあたり20分を上限とする。1 passは3系列に対するserviceの2回再試行で最大6 fetchになる。fetchごとに20分を与えてはならない。上限が6倍になり、日次batchのjob timeoutによってscreening publishまで失うためである。

全営業日の取得後に1回だけstoreへ書く。途中で再試行しない失敗が起きた場合は、そのpassの取得結果をすべて破棄する。長い窓は年単位に分けて取得する。

#### 集計式の変更

集計式は最初のcloud push前に確定する。新しい式が値を出さない日には古い式の観測が残り、`--all-history`でも上書きされないため、同じ系列へ2つの定義が混ざり得る。

cloudへ渡す前なら、localの対象行を削除して全期間を入れ直せる。cloudへ渡した後は、mergeのno-loss契約がlocalで削除した行を戻すため、この方法を使えない。式を変える場合は次の順序を守る。

1. registryから3系列を外し、generationを上げる。
2. 日次batchがcloud側の旧系列をpruneするまで待つ。
3. 新しい定義で3系列をregistryへ戻し、入れ直す。

日単位の`retract`は数百日の操作になるため、この移行には使わない。`iv_30d`と`iv_skew`はSQ直後に構造的に数日欠けるので、reading rulesにstaleness overrideを持つ。

欠測率、最長空白、緊急取引証拠金日の2部返しを測定した履歴は[option IV quantiles study](../../reports/studies/2026-07-31-option-iv-quantiles/report.md)に残す。active contractの閾値はstudyで固定せず、reading rulesを正本とする。

#### 購読窓と緊急取引証拠金日

購読窓は運用日から10年rollingで、窓外の要求はHTTP 400になる。`--all-history`の始点は窓の境界に置かず、数日内側から年単位で取得する。

緊急取引証拠金が発動した日はchainが2部返る。発動時の部は前営業日の原資産と平坦なIVを持つため、`EmMrgnTrgDiv`が名指す清算値算出時の部だけを読む。このfieldを持たない行は読まない。

新ソース追加＝provider モジュールを 1 つ足して（`providers/` に 1 ファイル）`providers/registry.py` に 1 行登録し、series を registry（`indicators/registry/` の region 別 yaml）へ 1 entry 加える。provider の取得能力（all-history 起点・store 書き換え方針・refresh 可否・point-in-time vintage・必要 env）は各 provider の `ProviderSpec` が宣言し、service / store reader は provider 名で分岐しない。point-in-time replay を提供する provider の spec は fetch 実装を import しない read-safe module に置き、provider 実装と reader が同じ spec を参照する。registered provider との drift は test で検出する。1 series_id = 1 provider を厳守する。provider 取得は一時的な `IndicatorsProviderError` を 1 回 retry し、再失敗した場合は `provider_runs` に failed として記録する。

`macro refresh` は複数 series を 1 pass で取得し、1 series の失敗は他 series を止めない。失敗した series は最後にまとめて stderr へ列挙し、exit code は非 0 になる（1 つの壊れたソースが同一グループの残り全系列を stale にしない）。1 pass は 1 つの fetch context を共有するので、複数 series が同じ bulk ファイルを参照しても download は 1 回、browser fallback を要する provider の起動も 1 回で済む。取得値は store へ入る前に有限値であることを検証し、NaN / ±inf は取得失敗として扱う（派生計算・percentile・export を汚染させない）。`provider_runs`は取得の成否と`record_count`を記録する。

PMI は data API が無いため、月次 release URL の manifest（`engine/src/baibai_engine/macro/indicators/providers/pmi_release_urls.yaml`、`schema_version: 2`、PMI stream ごとに `observed_at` → 公式 release URL）を正本とし、`spglobal_pmi` provider が各 URL の公式 PDF を live 取得して headline 値を抽出する。release URL の validator は `https://www.pmi.spglobal.com/Public/Home/PressRelease/<32 hex>` だけを許可する。

抽出は release が headline を述べる冒頭 statement（headline index を名指す文と、その statement を続ける次の文）だけを読む。値を採るのは、release がその値を対象月に結び付けているか（`posted 47.9 in December`、`in April to 51.3`、`October's 54.8`）、statement が reading として導入している（`posted 52.3`、`rose to 54.4`、`at 51.6 the index ...`）場合だけで、月を明示しない reading は release 自身の月にしか帰属させない。sub-index / composite index を名指す文は読まず、閾値との比較（`above the 50.0 no-change mark`）・flash 見積り・複数月平均は reading ではないので読む前に text から除く。抽出値は1〜3桁・小数1桁としてparseしてからdiffusion indexの定義域 0〜100 で検証し、複数候補が矛盾する月は取得を失敗させる（誤った値を store に入れない）。危機・再開局面の正当な30未満・70超も欠落させない。読めない phrasing は値を作らずに「その月の headline 値なし」として失敗するので、取り込み漏れは無音にならない。

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

### Baibai Loop で期間と粒度を読む

`baibai-web` の Macro ページは期間 `1y | 5y | 10y | max` と粒度 `daily | weekly | monthly | yearly` を全チャートへ適用する。`/api/macro` も同じ query parameter を受け、週次・月次・年次は各期間の最終観測値を返す。UIの既定は `max + monthly`、API parameterを省略した場合は `1y + daily` である。`series.yaml` に `tradingview_symbol` がある系列だけ、チャートカードから TradingView の該当 symbol を新規 tab で開く。

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
- **`next_print_estimate` は公表予定日の目安であり、イベントカレンダーではない**。`publication_cadence` の次期と `publication_lag_days` だけから決定論で導出する。daily の既定は `business_daily` で推定日が土日なら翌平日へ送り、土日も観測を持つ系列は `calendar_daily` を明示する。cadence の既定は registry frequency だが、統計標本は月次でも当月値を営業日更新する `us.erp`、日次観測を週次バッチで公表する H.10 / EIA のような系列は override する。祝日や当局の個別日程は手維持しない。`print_due_in_days` が小さい正値なら公表が近く、負値なら公表済みのはずで取得待ちである。精密な会合・イベント日は L3 monitoring で一次情報を確認する（推定は上端であり entry timing には使えない。後述の「意図的な境界」）
- **`stale` は provider の無音の停止を疑う合図**であって、値の否定ではない。観測の齢は公表ラグと週末を含むため、境界日は **`next_print_estimate + staleness_margin_days`** から同じ calendar arithmetic で導出する。`staleness_warn_days` はその観測日から境界日までの日数を表示する。次の公表を待っている平常時には出ず、1 回の公表落ちで出る水準である。lag / cadence が frequency default と構造的に違う source（H.4.1 の翌日公表、EIA の日次価格を週次でまとめる FRED、M+2 公表の JOLTS、OECD の中継、日次更新する月次派生値）は系列別に override する。**閾値が緩すぎると 2 公表分の欠落を通す**ので、公表が速い source ほど閾値も短くする
- **`flags` は signal ではなく注記**である。閾値に触れたことを見落とさないための機械的な指差しで、水準の解釈と行動は L3 の判断に属する
- **`z_score` の極端値は誤値と真の市場極値の両方で出る**。どちらかは reading では決めず、L3 が一次情報と突き合わせて判断する。政策正常化のような構造変化では正しい極値が何か月も端に居座るので、続いて見えることを慣れの理由にしない——居座り自体が「現行水準がまだ履歴に無い」という読みである
- **percentile は `statistic` と一緒に読む**。水準の尺度が自らの履歴でしか決まらない系列（物価・数量の指数、名目の集計値、累積の雇用者数、株価指数）は水準の percentile が時間の経過を映すだけになるため、`statistic: yoy` として前年比 %の分布内の位置を出す。金利・スプレッド・比率・DI・ボラティリティ・為替・商品価格は水準自体に解釈があるので `level` を保つ。`yoy` の系列でも `latest_value`・`flags`・`short_trend` / `long_trend` は水準のままで、trend は系列自身の単位の絶対変化、percentile は %変化の位置を示す
- **統計標本の1点は reading rule の `sampling_cadence` に合わせる**。既定は registry frequency から解決し、monthly / quarterly 系列は同じ暦月・暦四半期の最終観測1点へ折ってから level / yoy を計算する。取得 cadence と統計 cadence が異なる source は系列 override で分離する。latest value・trend・flags は折る前の観測を読む
- **`statistic_value` が null なら位置は出ない**。前年比は 1 年前の観測を相手に取るので、その月が欠けている系列（相手が 380 日より前しかない）や相手が 0 以下の系列は該当点を標本から落とし、最新点が落ちれば percentile / z も null にする

計算規則は `method/macro/reading/<ISO8601>.yaml` の dated revision に置き、reading 出力は使った `rules_revision` を併記する。規則は **frequency 別の defaults + 系列別 override** で解決する。系列を registry へ追加しても規則の編集を要求しない（defaults が解決する）ことが設計要件であり、override は「default が事実に反する系列」だけに書く：provider の配信範囲が構造的に短い系列（J-Quants Light の 5 年 rolling、`spglobal_pmi` の manifest が持つ月だけ、ICE BofA OAS の 3 年）は percentile の実効窓を、水準に位置が無い系列は `statistic` を、公表 cadence / lag が frequency default と違う source（日次更新する月次派生値、H.4.1 の翌日公表、M+2 公表の月次、OECD 中継）は `publication_cadence` / `publication_lag_days` / `staleness_margin_days` を、教科書的な閾値を持つ系列は flags を override する。**全登録系列で規則解決が成立すること**は test が保証し、解決できない系列があれば fail する。1 つの系列は複数の理由で override されるため、**同じ series を 2 度書いた revision は load 時に失敗する**（YAML は後の entry だけを残すので、上の設定が黙って落ちて「適用済み」と読める）。

`schema_version: 1` の既発行 revision は引き続き load・再計算できる。その revision では当時存在しなかった `next_print_estimate` / `print_due_in_days` を null とし、既存の明示 `staleness_warn_days` をそのまま使う。`schema_version: 2` は publication lag / margin を契約とし、`staleness_warn_days` の明示を拒否する。系列固有の鮮度差は lag / cadence / margin を override して表す。両 shape の混在を load 時に拒否するため、過去 revision の意味を現在の lag 推定で書き換えない。

`statistic` の既定は `level` なので、水準に位置が無い系列を registry へ追加したら override を書く（書き忘れは percentile が 100% 近傍に張り付く形で reading 自身に現れる）。`yoy` の系列では窓の先頭より 13 か月前まで raw を読み、標本は窓の中だけを使う。履歴が窓の先頭で始まる系列（provider が rolling 窓を配信する場合）は先頭 1 年に相手が居ないため標本がその分薄くなるが、`insufficient_history` の判定は raw 履歴が窓を張るかで行うので percentile は出る。

percentile の実効窓を短縮した系列は、provider の履歴が伸びて default に届いたら override を外す（`window_years` が default と一致しているかを規則改版時に確認する）。

reading は L1 store を読むだけの純関数で、provider を呼ばず DB へ書かない。したがって過去日の asof でも同じ入力から同じ snapshot を再計算できる。CLI・read API・indicator chart は共通の store reader を使い、`ProviderSpec.point_in_time_vintage` を宣言する source だけを `vintage_at <= asof` へ clamp する。宣言のない bulk history の `vintage_at` は取得日時であって当時の公表日時ではないため、一律 clamp して取得前の過去 snapshot から既知だった履歴を消さない。専用 store は持たず、日次バッチが `baibai-web` 向けの serving view（`/api/macro/reading`・`views/macro-reading.json`）として export し、L3 レポートは引用した snapshot を `inputs.reading_snapshots` に記録する。

Baibai Loop の Macro タブは、この読み値と指標チャートを **1 つの一覧**として表示する。読み値と `web/config/macro-panel.yaml` の panel は同じ登録系列を 2 通りに射影したものなので、行は panel の 7 group の順に並べ、`series_id` で読み値を join して 1 行に sparkline・最新値・観測日・短期/長期トレンド・`statistic`・percentile・`z_score`・実効窓・注記を並べる。行を開くと拡大チャートと全項目が出る。**sparkline の期間は画面の期間・粒度で、percentile の実効窓は系列ごと**という別物なので、列見出しの ⓘ でその不一致を明示する。

行に出る状態は 4 つで、**取得失敗・`stale`・`insufficient_history` の 3 つは取得側の問題**（percentile の解釈可能性を壊す）、**`|z_score|` ≥ 3 の分布の端は読み値そのもの**である。極端な z を health に混ぜない：端にいることは reading が測った位置そのもので、panel の結論に最も近い情報である（誤値でないことの確認は L3 が一次情報と突き合わせて行う）。ページ上部の要約カードは 4 分類の件数だけを持ち、同じ分類が一覧の絞り込みでもあるため、件数から該当行へ 1 クリックで辿れる。view が未生成のときは読み値の列だけが空欄になり、チャートとレポート index は通常表示する。

## ③ 環境認識：macro context report を publish する

市場局面についての、日付と出所の明確な環境認識は application DB の immutable revision として残す。機械契約は `baibai_engine.macro.context.models.MacroContextDocument`、唯一の書き込み経路は `baibai-engine macro context publish` である。既存 head を読んで draft を作り、2件目以降は `--expected-head` にその ID を渡す。head が変わっていれば publish 全体が無変更で失敗する。

draft の反復中は `publish --check` で store に触れずに文書契約と publish gate を検証する（compare-and-swap は store が要るため実 publish のみ）。`inputs.indicator_series` は手書きせず、セクション → series の対応を書いた spec から `baibai_engine.macro.context.scaffold_inputs` で生成する — provider・最新観測日・vintage・実効窓を L1 store と reading 計算から導出するので、引用の provenance が常に store と一致する。

```bash
uv run baibai-engine macro context head
uv run python -m baibai_engine.macro.context.scaffold_inputs /tmp/spec.yaml --output /tmp/inputs.yaml
uv run baibai-engine macro context publish /tmp/macro-context-draft.yaml --check
uv run baibai-engine macro context publish /tmp/macro-context-draft.yaml \
  --expected-head macro-context-2026-07-01-example
uv run baibai-engine macro context show --latest --asof 2026-07-19
```

レポートは **1 種類だけ**で、常に下記の深度契約を満たす full 深度で書く。軽い事実確認のための軽量版は持たない（その用途は §② が毎営業日 機械で果たす）。レポートの中心的な価値は **統合**にある: チャネル別の評価を並べるだけでは投資戦略の土台にならないため、複数チャネルを横断する支配的な力（synthesis）・確率付きシナリオ・機械見積りの歪み補正（estimate caveats）・バーゲン地形を、後述の機械契約と publish gate で必須にしている。

**作成のきっかけは人間の判断だけ**である。定例義務・monitoring 発火時の更新義務・賞味期限の宣言は持たない。推奨リズムは (a) 米雇用統計の翌週、(b) スポットの資産運用判断の前、(c) Research Triage前にheadが古いとき、の3つで、書かない月があっても壊れるものは無い。鮮度の判断は読む側が持つ（後述の consumer 側鮮度規則）。

### 3 層構成：core（環境評価）・synthesis（統合評価）・connection（積立ループ接続）

レポートは **core 10 セクション + synthesis + connection 1 セクション** で構成する。core は use-case agnostic な環境評価（チャネル別の evidence 層）であり、日本株積立ループ固有の語彙（sector tilt・research 優先度・sizing caution）を持たない。synthesis は core の上に載る統合層で、やはり use-case agnostic である。connection はループ固有の語彙を 1 か所へ隔離する。読み手の順は summary → synthesis → core → connection であり、executive な統合が evidence より先に来る。

この分離は書き手の注意ではなく **参照方向の機械契約** で守る：connection が引用できる series は core が引用済みのものだけで、connection は依拠する core セクション（その series を実際に引用しているセクション）を `core_section_ids` で明示する。core 側へ sector tilt / research 優先度ヒント / sizing caution を書いた draft は schema が拒否する。series 以外の input（`screening market-snapshot` の市場内部やループ固有の記事）は connection が自分の入力として持ってよい——バーゲン地形は connection の担当であり、core を日本株ループの語彙で汚さないためである。ただし **prose は機械では縛れない**（core の judgment に行動指示を書き込むことは schema では止まらない）ので、そこは skill の敵対的 self-check と、publish 前に author と別 role が縦読みする独立レビューが受け持つ。core が単体で完結していることの構造的な証明になり、リポジトリ外のスポット資産運用判断の材料としてもそのまま読める。

### synthesis：支配的な力と相互作用

synthesis は「今の市場を動かしているのは何か」を **dominant force** として名指しし、力ごとに機序（`summary`）・伝達経路（`transmission`）・**反証（`counter_evidence`）**・方向・確度を書く。複数チャネルへ波及する力を選び、相互作用が判断を変える場合は `interactions` に書く。機械契約は文章品質を代理判定せず、次の参照方向だけを守る：

- 各 force は伝達チャネル 7 セクション（`rates_policy` / `growth_demand` / `inflation_costs` / `liquidity_credit` / `fx` / `japan` / `valuation`）から `core_section_ids` を名指しする（regime_summary・risk_environment・monitoring は名指せない）
- force が引用できる series は、名指ししたセクションが引用済みのものだけで、series ごとに正常取得した input の引用も要る
- `interactions` を書く場合は、宣言済みの force を 2 件以上 `force_ids` で名指しする

force の候補は §② reading の flags・|z| 極値・percentile 端・トレンド反転を束ね、§④ の 8 分析レンズと突き合わせて立てる。1 つの力を支持する事実と反証する事実の両方を一次情報で集めてから書く。

### セクション表と共通 field

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
| core | 8 | バリュエーション（`valuation`） | 米 ERP / CAPE、日本 ERP（市場全体PERまたは益回り − JGB 10y）、金、BTC | 各資産の相対的な位置を示す |
| core | 9 | リスク選好環境の評価とシナリオ（`risk_environment`） | セクション2〜8を支持・反証する系列 | 攻め／守りどちらの環境かを `stance`・確度・**反証条件**付きで評価し、base / bear / bull を **確率**と scorecard 条件付きで置く |
| core | 10 | 監視ポイント（`monitoring`） | 次の公表・会合と観測条件 | 何が出たらどの見方を変えるかを prose で明記する |
| connection | 11 | 日本株積立ループ接続（`japan_equity_loop`） | core が引用済みの series のみ | research 優先度ヒント（効く候補タイプを `applies_to` で判別可能に）、sector tilt、sizing caution、**バーゲン地形（`bargain_topography`）**、**機械見積りの歪み補正（`estimate_caveats`）** |

共通 field：

- `context_id` / `as_of` / `published_at`。`as_of`は**市場データの最終完全営業日**にする（著述日ではない）。screening review-set publishはpoint-in-time整合のため`as_of ≤ Review Set ASOF`のcontextだけをbindするので、週末・祝日に書くcontextの`as_of`を著述日にすると直近Review Setへ恒常的にbindされない
- `inputs.articles`：外部記事の一意な`input_id`、source / title / url / published_at / accessed_at / status / used_for（記事本文や監査ログは保存しない）。statement の `source_ids` は既知の正常取得 input へ解決する。引用が主張を十分に裏づけるかは独立 review で確認する
- `inputs.indicator_series`：一意な`input_id`、`baibai-engine macro`で確認したprovider / series / window / observation_as_of / status / used_for
- `inputs.machine_snapshots`：引用した自前コマンドの決定論出力（`screening market-snapshot` 等）。一意な `input_id`、`command` / `snapshot_asof` / `observation_as_of` / `accessed_at` / `status` / `used_for`。**自前出力は記事ではない**ので `inputs.articles` へ入れない：発行者も URL も無く、コマンドと訊ねた日付が identity である（記事枠へ入れると定義 doc の URL が数値の出所として読まれる）。`snapshot_asof` は as_of より未来にできず、`observation_as_of`（実際に使った最終市場日）はその as_of を超えられない。scorecard は専用 snapshot 契約で context / rules revision / 両 store / result digest も固定し、観測を 1 件も使わない pending-only 結果だけ `observation_as_of: null` を許す
- `inputs.reading_snapshots`：引用した macro reading の `rules_revision` と `reading_asof`。**reading input を持たない draft は publish されない**。レジーム要約は reading input を引用する必要があり、共通座標を機械読み値から始めることを強制する。`reading_asof` は as_of より未来でも 7 日より古くてもならず（reading は任意の as_of で再計算できるので、レポートは自分の as_of の reading を引く）、`rules_revision` は `method/macro/reading/` に実在する revision でなければ publish されない
- core の各セクションは`series_ids`、source付き`fact_summary`、方向・確度・source付き`judgment`、source付き`economic_connection`を持つ。connection セクションは`economic_connection`を持たず、代わりに`core_section_ids`とループ固有の項目を持つ
- `material_deltas`：core セクション2〜8の判断として置く。channel / direction / materiality / used_forを持ち、レポート全体で最低1つ必要
- `synthesis`：dominant force を 1 件以上置き、§synthesis の参照方向契約に従う。`interactions` は必要な場合に置く。**publish の要件**
- `probability`：セクション9の base / bear / bull に 1 つずつ置く主観ウェイト。0.05 刻み・各 [0.05, 0.90]・3 件合計 1.00（検証は整数化算術で決定論）。**publish の要件**。確率は「見立ての強さの明示」であり、優位性の数値・統計的有意性・sizing 入力のいずれでもない（§誠実性）。次回以降のレポートが settled scorecard と突き合わせることで、読みの較正データが蓄積される
- `sizing_cautions` / `sector_tilts` / `research_priority_hints`：connection セクションだけに置く。research 優先度ヒントは 1 件以上必須（着手順位を渡すことがこのセクションの存在理由）、sector tilt と sizing caution は該当が無ければ空でよい（core が支持しない tilt を埋めるために書かせない）
- `bargain_topography`：connection で「この局面でミスプライスがどこに・なぜ出やすいか」を書く。market snapshot への接地は独立 review で確認する
- `estimate_caveats`：今の環境が機械見積りをどの向きに歪めるかを、`affected_component`（`fv_anchor` / `reversion` / `carry` / `resilience`）と `applies_to` 付きで書く。research はこの caveat を機械値の消化時に参照する

各`series_id`はaliasではなくseries定義のcanonical IDを使って`inputs.indicator_series`にも置き、各要約・判断・接続の`source_ids`をinputへ結ぶ。series定義にないID、inputにないseries参照、正常取得した同系列inputを引用しないセクション、failed inputを引用する判断はpublishされない。変化がmaterialでないセクションも省略せず、確認したfactと「見方を維持する条件」を記す。

publish 済み revision は immutable なので、検証は**参照先が動くかどうか**で 2 層に分かれる。文書が自分自身について述べること（セクション構成・引用の連結・failed input・期限窓・context_id と as_of の一致）は読むたびに検証する。**registry membership と系列の公表頻度は publish 時だけ検証する**：系列の退役・改名・再分類は正常な運用であり、読み取りでも照合すると過去のレポートを遡って invalid にする。退役系列を引用するレポートは読み続けられるが、その系列を条件に持つ scorecard は採点できず、validator が warning を出す。

<a id="japanese-writing"></a>

### 日本語表現

共通規則は[`judgment-writing.md`](./judgment-writing.md)に従う。Macro Contextでは、さらに次を守る。

- `summary`は現況評価、支配的な力、前回からの重要な変化、最大の不確実性、リスク選好評価、scenario確率を、本文を読まなくても追える順で要約する。新しい根拠や判断を初出させない。
- 現況、中心見通し、条件付きの将来、判断更新の観測を混ぜない。現況は観測時点を基準にし、中心見通しを置く場合は対象期間、scenarioは成立条件、monitoringは将来観測と更新方向を明示する。
- `transmission`と`economic_connection`は、確認した起点、経路、伝達先、方向、限定の順に書く。同時発生だけから因果を確定せず、`counter_evidence`は反対仮説ではなく、名指した力または経路をどこまで弱めるかを示す反証材料として書く。
- title、summary、synthesis、connectionのclaim scopeは、引用sourceが支持する対象、母集団、観測量、期間から広げない。titleを本文より強くしない。
- `monitoring`は観測eventと条件を、その条件で変更する判断および変更方向へ一対一で対応させる。複数方向の条件を一つに置く場合も、条件ごとの更新方向を明記する。
- 同じ文章をsummary、synthesis、economic connection、core sectionへ反復しない。summaryは全体の結論、synthesisは経路横断の力、economic connectionは各sectionの伝達、coreは根拠とsection判断という役割を保つ。

<a id="depth-contract"></a>

### 深度契約（全レポート共通）

レポートは銘柄選定とスポット判断のリスクリワード判断の土台になるため、次の深度契約を常に満たす。

- **統合が最上位の契約**: 支配的な力（synthesis）は、reading の極値・flags・トレンド反転を束ねて名指しし、伝達チャネルへの波及を説明する。力ごとに支持する一次情報と**反証する一次情報の両方**を読んでから書く。相互作用が判断を変える場合は joint risk を書く。この文章品質は独立 review が担い、件数で代理しない。
- **焦点 fact 規律**: 各セクションの fact_summary は「判断を駆動する焦点 fact」（1 fact = 1 つの経済的観察）を先頭に置き、セクション全 series の座標（値・percentile・Δ）の網羅転記は**末尾の座標 fact 1 件に隔離**する。焦点が数値の壁に埋もれたレポートは統合の失敗であり、網羅性は reading snapshot の引用と UI の一覧が担う。
- **テーマ被覆**: 金利・政策 / インフレ・コスト / 需要・雇用 / 為替・流動性・credit / 日本の政策・金利 / 日本の需要 / energy・地政学・通商 / 市場内部・バリュエーション の8象限すべてにfactを置く。`inputs.articles`はTier-1中心に15本以上で、**数えるのは外部記事だけ**（`inputs.machine_snapshots` の自前出力と `inputs.reading_snapshots` は本数に数えない。自前出力を数えると外部の一次情報を集めた量を自分の計算で嵩上げできてしまう）。
- **日本の需要fact最低ライン**: セクション3または7に、実質賃金（毎月勤労統計）または実質消費、鉱工業生産を必ず含める。取得可能ならインバウンド（訪日外客数）・機械受注も置く。米国factだけで需要判断を組み立てない。
- **円水準の両側リスク**: セクション6に、円安継続と円反転（介入・利上げ）の両経路が輸出企業（為替換算益の剥落）と輸入コスト企業（margin回復）へ与える非対称を1つのjudgmentとして書く。片側の監視条件だけで済ませない。
- **バーゲン地形**: connection に`screening market-snapshot`のbenchmark 20d/60d・breadth・regimeをfact引用し、「この局面でミスプライスがどこに出やすいか（全面安で広く出る / 回転相場で取り残しに出る / 全面高でプールが縮む）」を`bargain_topography`として書く。機械 gate ではなく独立 review が接地を確認する。オプション IV は「市場が何をどれだけ恐れているか」の観測として併記できるが、買い時や投入判断には使わない。
- **日本株バリュエーションアンカー**: セクション8に市場全体のPERまたは益回り（日経・JPX公表の一次値、または全universeのin-house中央値）とJGB 10yの対比を置き、個別FVアンカーの妥当性を外側から検算できるようにする。
- **hintの識別力**: 全候補に等しく当てはまる助言（「net cash重視」等）はhintではない。各 research 優先度ヒントと sector tilt は、どの候補タイプ・sectorに効くかを`applies_to`で判別できる形で書く。
- **energy・通商・地政学**: セクション4または5に、原油と通商政策（関税）・地政学tailのfactを最低1つずつ置く。
- **reading 先読**: core を書く前に §② の reading を全系列読み、`stale` / `insufficient_history` / `flags` / 極端な `z_score` を確認する。機械読み値と自分の結論が矛盾する場合、どちらも盲信せず矛盾自体をjudgmentとして書く。

### scenario scorecard：見立てを後から採点できる形で書く

セクション9の base / bear / bull は、主観確率（`probability`）と、自由文の成立条件とは別の **機械照合可能な観測条件（scorecard）** を各シナリオ 2 つ以上持つ。確率と scorecard は組で意味を持つ：確率は見立ての強さを反証可能な数値にし、scorecard はその見立てが当たったかを後から機械で確認する。次のレポートは `previous_scorecard_review` で「どの条件が成立し、置いた確率とどう噛み合ったか」を書き、当たり外れの**度合い**を記録する。条件は `series_id` + 比較演算（`below` / `at_or_below` / `above` / `at_or_above`）+ 閾値 + 期限日で書き、series はそのセクションが引用済みのものに限る。期限日は as_of より後かつ 18 か月以内で、系列の公表頻度に対して次の観測を待てる幅を持たせる。同じ条件の重複は拒否する。

狙いは予測精度の測定ではなく、**機械照合できる条件でしか書けなくすることでシナリオの記述品質を事前に縛る**ことである。「金融環境が引き締まれば」のような採点不能な条件は書けなくなる。定例が無くても、次のレポートがいつになっても L1 履歴から遡って採点できる。

採点は次のレポート作成時に `baibai-engine macro context scorecard --context-id <前回id> --asof <今回asof> --format json` で L1 履歴と機械照合し、結果を `previous_scorecard_review` として接続できる。ただし **前回の採点は今回の解釈の前提にしない**：今回の評価をゼロベースで確定したあとに fact として接続する。出力を machine snapshot として引用する場合は context / rules revision / store path / result digest を含む identity をそのまま使う。

scorecard はレポート `as_of` の翌日から各条件の期限日までを評価する。期限内の最初の成立を `met`、期限日まで不成立なら `not_met`、期限前なら `pending` とする。読み取りは通常の L1 reader と同じ latest eligible vintage を使い、観測日の上限は条件期限、vintage の上限は採点 `asof` として分離する。provider run の再取得証明や settlement watermark は持たない。JSON は実際に読んだ store path、rules revision、採用観測の unit / vintage / source、結果 digest を含む。

### 監視ポイント

セクション10の各監視ポイントは、観測する event、見方を変える condition、変わる judgment を prose で明記する。単一閾値の trigger subsystem は持たない。レポートの鮮度は consumer が `as_of` と内容を読んで判断し、機械的には 45 日の staleness warning だけを出す。

### 鮮度は読む側が判断する

レポートは自分の賞味期限を宣言しない。鮮度の扱いは consumer が自分の規則として持つ。

- **screening review-set publish**: head レポートの `as_of` が判断 asof から 45 日より古ければ `macro_context_stale` warning を出す。warning は context-level summary の材料であり、E[r]順位・candidateの事実層・候補抽出のいずれも変えない。`as_of` が判断 asof より未来のときだけ hard error にする
- **Research Triage / スポット判断**: headが古い、または深度契約を満たさないと判断したら、research_triage作成の前に書き直す。判断の前提が古いままかは判断する人が決める
- **Baibai Loop**: Macro タブが head の `as_of` を表示し、読む人が古さを目で確認できる

### 分析の独立性

環境認識の前提にしてよいのは過去の客観的事実（価格・指標・イベント）だけで、過去の macro context revision にある分析・結論・tilt は前提にしない。保有中の建玉も分析に持ち込まない。一次情報と指標から、解釈を毎回ゼロベースで組み立てる。比較可能な時点からの変化と前回 scorecard の採点は、結論を確定させた後にレジーム要約のfactとして接続する。

**revision は分析レイヤーであり、手順（作業の指示）を書かない**。「次回からこう調べる」といった手順の話は skill（`macro-context`）に置く。revision には、screening / research / スポット判断の前提として使う環境認識と出所のメタデータだけを残す。

### 入門者向けの指標の読み方

指標は単独で結論にせず、方向・水準・市場予想との差・改定を分け、同じ経路の反証指標と組にして読む。系列の一次sourceと取得上の制約は[`./data-sources.md`](./data-sources.md)を参照する。

| 指標群 | 基本の読み方 | 必ず組み合わせる確認 |
| --- | --- | --- |
| 政策金利・国債金利 | 政策の現在地と市場が織り込む将来経路を分ける。長期金利上昇は割引率の上昇要因になりやすい | 実質金利、期待インフレ、イールドカーブ |
| PMI・生産・雇用・消費 | 50などの基準、水準の方向、雇用の遅行性を区別する。PMI の percentile は 3 年窓（post-COVID 局面のみ）なので 10 年窓の同じ数字より含意が弱い | 新規受注、失業保険申請、生産、実質消費 |
| CPI・賃金・輸入物価 | 総合と基調、前年比と前月比を分ける。賃金上昇は需要とcostの両経路を持つ | service CPI、実質賃金、為替、原油・銅 |
| 為替・金利差 | 為替だけで因果を確定せず、金融政策差とrisk-offを分ける | 日米金利、VIX、trade-weighted dollar |
| 流動性・credit・volatility | net liquidityは構成系列を同じ単位にそろえる。OASやVIX/MOVEの上昇は資金調達・risk appetiteの悪化を示し得る | NFCI、HY/CCC OAS、株式breadth |
| 日本固有系列 | BOJ、賃金物価、海外需要、投資家フローを順に接続する | USD/JPY、実質実効為替、鉱工業生産 |

## ④ ナレッジ：8 分析レンズ

個別の指標は単体で読まず、以下のレンズに束ねて環境認識に使う（1枚のパネルとして横断的に読む）。操作routingはskill[`macro-context`](../../.agents/skills/macro-context/SKILL.md)、分析詳細とsource規律は本docを正本とする。

1. **グローバル流動性**：net liquidity ≈ `us.fed_assets` − `us.reverse_repo` − `us.tga`（単位換算注意）。`us.m2` 前年比はリスク資産に約 10 週先行。
2. **実質金利・store-of-value**：`us.real_10y` + `us.breakeven_10y` + `usd_index.broad` + `gold`。名目 = 実質 + 期待インフレに分解。日本側は `jp.real_10y_proxy`（月末10Y JGB − コアCPI前年比）で、名目金利の上昇が実質でも締まっているのか、インフレに食われて実質マイナスのままかを読む。
3. **金融環境の合成**：`us.nfci` を `vix`・`us.move`・クレジット OAS と突き合わせ、slow-burn（広範化前の局所ストレス）を読む。
4. **リスク選好の温度計**：`btc_usd` + `vix` + `credit.us_hy_oas`/`credit.us_ccc_oas` + `us.nfci`。BTC は先行温度計になりやすい（単独 driver にはしない）。日本株の判断には `jp.n225_iv_30d` を併読する——`vix` は米国市場の恐怖で、判断対象が日本株なら代理変数になる。`jp.n225_iv_skew`（0.95 put − ATM put を 30 日満期へ補間したもの、大きいほど下落を恐れている）と `jp.n225_iv_term`（第 2 限月 − 手前限月、僅かな逆転は平常で、大きな負が目先のパニックが先の見通しより強い状態）を合わせて読む。水準の高低は他の系列と同じく **reading の `percentile` を正とする**——[option IV quantiles study](../../reports/studies/2026-07-31-option-iv-quantiles/report.md) は分位を凍結した第二の物差しではなく、この 3 系列が「いつ出ないか」「何に依存するか」を測った記録であり、store が 10 年窓を満たすまでの間だけ水準の当たりを付けるために読む。`iv_30d` と `iv_skew` は手前 2 限月が 30 日を挟む日にしか出ない（実測 83%）ので、SQ 直後に数日まとめて欠けるのは異常でなく、チェーンが 30 日を値付けしていないという事実である。同一行使価格のプットとコールの IV が大きく食い違う日は差を取る 2 つ（skew / term）を出さず `iv_30d` だけが残る——これも欠測でなく、その日は差を取れないという事実である。
5. **景気サイクル・breadth**：`us.initial_claims` + `us.industrial_production` + `copper` + `us.russell2000` + `us.10y_3m_spread`。`us.sox` は AI/半導体サイクルと日本半導体株の先行ゲージ。
6. **バリュエーション・ERP**：`us.sp500_earnings_yield` − `us.10y` ＝ 米ERP。益回り < 名目金利（ERP≤0）は警戒域。`us.sp500_cape` で長期割高度。**日本側は市場全体PER/益回り（日経・JPX公表値またはin-house universe中央値）− JGB 10y** を同じ構図で読み、個別FVアンカーの外側検算に使う。
7. **グローバル中銀の同期**：`us.fed_funds.upper` + `jp.policy_rate` + `ecb.policy_rate`。1 国でなく同期を読む。
8. **エネルギー・地政学**：`wti`/`brent` + `gold`。日本はエネルギー輸入依存が高く（中東 ~95%・ホルムズ ~74%）原油 spike が通貨・スタグフレーションに直結するため `usd_jpy` と併読。

## 意図的な境界（不足ではなく設計）

次の 3 点は「機能が足りない」ように読めるが、意図して引いた境界である。

**中国は proxy basket で読む。** 中国の直接系列は `usd_cny` の 1 本だけである。NBS 等の公式配信に機械可読で安定した無認証経路が無く、脆い scrape provider を足すと「取り込みの停止」と「系列自体の停止」を store の上で区別できない無音の失敗を増やす（§① の relay に関する注意と同じ理由）。代わりに **`copper`（中国の実需）・`aud_jpy`（資源国通貨として中国感応度が高い）・`em.equity`（EEM）・`usd_cny`** を横に読み、中国の需要と資金の向きを推す。安定した機械可読 source が現れたらこの方針を再評価する。境界は **L1 の系列取り込み**の側にあり、L3 のレポートでは NBS / 海関総署の公表値を `inputs.articles` の一次情報として引いてよい（[`./data-sources.md`](./data-sources.md)）。

**`next_print_estimate` は上端であり、entry timing には使えない。** これは「これ以降なら公表済みのはず」の線で、平常の公表待ちで負値や `stale` を出さないよう遅い側へ寄せてある。一方で「保有ウィンドウ内に CPI が落ちるか」のような事前確認には、**早い側に外れるイベントを見逃す**ので使えない。その用途には下端推定が要り、現状は L3 の monitoring と人手の暦確認が担う。

**3 年窓の percentile は 10 年窓と同じ意味を持たない。** PMI 4 系列（manifest 起点 2022-12）・credit OAS 4 系列（ICE BofA の配信範囲）・`jp.foreign_flows` は 3 年窓で読む。とくに PMI の 3 年は post-COVID の引き締め〜緩和局面しか含まないので、「PMI 48 = 25th percentile」は 10 年窓の同じ数字より弱い含意しか持たない。reading は `window_years` と `window_observations` を出しているので機械側は誠実であり、L3 執筆時は窓の長さを見てから percentile を読む。

## Material deltaとAIの境界

macro contextはdiscount rate、需要、資金調達、共通tail risk、sizing cautionだけを表す。AIの役割と株主価値の獲得可能性はmacro contextに置かず、企業別thesisで評価する。

## 接続：判断層にだけ効かせる（screen は macro-blind）

マクロの読みは機械スクリーニングの `run` には接続しない（`run` は財務事実だけを扱う決定論的なエンジンのまま）。効かせるのは判断層だけ：

- **select**（[`./screening-runtime.md`](./screening-runtime.md)）：material deltaと`as_of`鮮度warningをcontext-level summaryとして出す。E[r]順位とcandidateの事実層は変えない。
- **research**：material deltaが個別5年期待値へ影響する場合だけ、thesisのjudgmentへその因果と根拠を残す。マクロを数値ドライバー、採用gate、投入額ルールにはしない。
- **connection セクション**：Research Triageがresearch優先度ヒントとsizing cautionを消化する入口になる（skill `research-triage`）。

行動指示（売買タイミング・現金比率・配分指示）はcore にもconnection にも書かない。sector tiltとresearch優先度ヒントは着手順位を判断するjudgment入力であり、機械ranking・hard gate・自動sizingへは接続しない。

## 誠実性（honesty firewall）

マクロは標本数がほぼ 1 であり、screening のように多数の銘柄を横断する統計検証ができない。この工程は優位性の数値・統計的有意性・自動売買スコアを出さない。ここで得られるのは再現性と、判断を事実に根付かせる基盤であって、統計的な厳密さではない。§② の reading も記述統計であり、regime の機械分類・合成 score・統計的 signal は作らない。シナリオ確率もこの枠内にある：0.05 刻みの主観ウェイトは「見立ての強さの明示と後からの採点可能性」のためであり、edge の主張・有意性・自動 sizing の入力にはしない。

## 参考

- [`../doctrine.md`](../doctrine.md)：思想・柱 2（macroとAIの責務境界）
- [`./screening-runtime.md`](./screening-runtime.md)：material deltaとas_of鮮度warningを出すselect
- [`./data-sources.md`](./data-sources.md)：データソース Tier
