# batch — production orchestration と cloud store 運用

本書はmachine処理、store転送、servingへの反映と復旧の手順を定める。domain処理はengine、read modelはweb、順序と転送はbatchが所有する。CLIの引数は該当`--help`、operator shellの呼出しと結果別の操作は本書を参照する。

<a id="calibration-全期間-rebuild-の所要時間"></a>
<a id="cloud-materialize-の所要時間"></a>
<a id="cloud-daily-batch-の所要時間"></a>

## 長時間処理の監視

| 処理 | 既存規模での所要目安 | 外部監視の時間予算 |
| --- | ---: | ---: |
| calibration全期間rebuild | 45分 | 60分 |
| cloud-materialize | 12分 | 20分 |
| cloud-daily-batch | 35分 | 60分 |

これは計画用の目安で、完了保証ではない。workflowのhard timeoutは実行定義を参照する。

```bash
gh run watch <RUN_ID> --exit-status --compact --interval 60
```

監視するrunを固定する。外部監視がtimeoutでも処理が終了したとは限らないため、対象の状態と現在stepを確認し、activeな処理を重複起動しない。calibrationの再構築範囲は[見積り較正](../docs/reference/estimate-calibration.md#store-の再構築)に従う。

## Cloudflare / GitHub Actions 構成

R2 bucketとobject keyは次の固定契約を使う。どちらのbucketもPublic Development URLとcustom domainを無効にする。

| bucket | object | owner |
| --- | --- | --- |
| `baibai-stores` | `market.sqlite`（store-local data + `lake_store_origin` metadata） | `cloud-daily-batch` + ローカル`push-market`（cloud copyのmerge後だけupload） |
| `baibai-stores` | `lake/`（lake所有datasetのcanonical L1） | `cloud-daily-batch`の`publish-lake` + ローカル`r2_transfer.sh publish-lake` |
| `baibai-stores` | `runs.sqlite` | `cloud-daily-batch` + 明示的なローカルdailyの`push-machine` |
| `baibai-stores` | `macro.sqlite` | `cloud-daily-batch`（rolling窓）+ ローカル`push-macro`（全履歴。cloud copyのmerge後だけupload） |
| `baibai-stores` | `baibai.sqlite` | ローカル`publish.sh`（replica） |
| `baibai-serving` | `views/*.json` | GitHub Actions materialize |
| `baibai-serving` | `history/candidate-views/<asof>.json` | 日次batch、R2 lifecycleで31日後に削除 |

R2 lifecycle ruleは`history/candidate-views/`だけに設定する。bucket全体へ設定すると`views/meta.json`まで期限で消え、欠落を検知できない。Review Set membershipの長期履歴は保持しない。

serving と Worker の境界:

- 両bucketはpublic accessを持たない。WorkerのR2 bindingは`baibai-serving`だけに限定する。
- 既存view APIは[共有read認証](../web/README.md#共有read)をSHA-256後に定数時間比較し、有限のrouteから`views/`または日付形式を検証した
  `history/candidate-views/`へ写像する。L1だけは[raw gateway](../docs/reference/market-lake.md#shared-raw-read)が
  bucket-scoped Object Read only S3 credentialで取得し、store snapshotや任意のhistory keyには到達しない。
  応答は`Cache-Control: no-store`で、CORSを有効化しない。
- Workers Assetsは`web/frontend/dist`を無認証で配信する。bundleは業務データを含まず、実データは認証済みAPIだけから取得する。HTTP navigationはWorkerが認証処理前にHTTPSへredirectし、HTTPS応答はHSTSを持つ。
- `cloud-materialize`はapplication data、`cloud-daily-batch`は平日夕方の機械工程をpublishする。2 workflowは`cloud-publish`の`queue: max`を共有し、pending writerをFIFOで保持して
  running/uploadを1件に限定する。
- ローカル`pull`はmachine storeだけを置換し、canonical application DBを上書きしない。ローカル`publish`はSQLite snapshotをstoresへ置き、materializeをdispatchする。

資格情報はprincipalごとに分ける。

| principal | 設定 | scope |
| --- | --- | --- |
| GitHub Actions | variable `R2_ACCOUNT_ID`、secrets `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / provider 3本、公開 JPX 規制 URL 4本 | 必要なtransfer/provider stepだけ、stores + serving read-write |
| GitHub Actions（通知） | secret `DISCORD_WEBHOOK_URL` | `cloud-daily-batch` の通知 step と `cloud-batch-watchdog` の警報 step のみ（job env に出さない） |
| GitHub Actions（Worker deploy） | variable `R2_ACCOUNT_ID`、secret `CLOUDFLARE_API_TOKEN` | 対象accountの`Workers Scripts Write`、`web`のdeploy stepのみ |
| ローカル`.env` | `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | stores read-write |
| Wrangler OAuth | `wrangler login` | bucket初期設定、Worker secretの手動設定 |
| Worker secret（閲覧認証） | `VIEW_PASSWORD` / `READ_ACCESS_TOKEN` | Worker runtimeだけ、ownerと共有を分離 |
| Worker secret（L1接続） | `L1_R2_BASE_URL` / `L1_R2_ACCESS_KEY_ID` / `L1_R2_SECRET_ACCESS_KEY` | `baibai-stores`だけのObject Read only、publisher credentialと分離 |

R2 S3 endpointは`https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`からscriptが組み立てる。credential、password、endpointの実値をGit、issue、logへ書かない。

R2 API tokenはbucketごとにread/writeを分けられない。このため`r2_transfer.sh`は`upload-serving-views`と`publish-serving-tail`を
`GITHUB_ACTIONS=true`以外で拒否する。前者は`views/`を`--delete`付きで同期するため、部分的なexportで実行すると
本番viewを削除する。

serving publishは`upload-serving-views`による`views/`差し替えと、`publish-serving-tail`による`history/`・
`views/meta.json`発行の2段である。`views/`の大半は数千件規模の`security--<ticker>.json`で、毎営業日書き換わる。
両段はstore push成功後だけ実行する。先に実行すると、remote storeにないrunを次の成功まで表示するためである。
machine側が失敗した日のviews uploadは`skipped`と報告する。`history/`は追記のみ、`meta.json`はfreshnessの表明なので、
いずれもstore永続化の成功後だけ発行する。

provider secretは`JQUANTS_API_KEY` / `ESTAT_APP_ID` / `EDINET_API_KEY`。`bootstrap-cache`はさらに
`universe.required_jpx_flags`の4 source（特別注意銘柄 / 整理銘柄 / 取引停止 / 上場廃止警告）の公開URLを要求する。
非secretの`JPX_SPECIAL_CAUTION_INDEX_URL` / `JPX_REORGANIZATION_URL` / `JPX_TRADING_HALT_URL` /
`JPX_DELISTING_WARNING_URL`を`cloud-daily-batch.yml`の`Run daily batch` step envへliteralで置く（雛形は
`.env.sample`）。未配線ならJPX stepがfail-fastし、machine stores / serving uploadはskippedになる。

workflow dispatchの日付はfull SHA checkout後、credentialを持たないstepでexact `YYYY-MM-DD`と順序を検証する。
`run:`へ`inputs.*`を展開せず、step envからshell変数として渡す。credentialは使うcommandのstep envだけへ渡し、
checkout・setup・dependency install・validationへ渡さない。外部Actionのfull SHA pinを含む境界は
`tools/quality/drift/check_workflow_trust.py`が検査する。

## 初回seedとWorker deploy

### Storeをseedする

**前提**: 初回だけ、ローカル4 storeのconsistent SQLite snapshotをstores bucketへ送る。4 keyの
いずれかが既に存在する場合は、古いローカルcopyによる正本の巻き戻しを防ぐため何も
uploadせず停止する。

**実行**:

```bash
batch/scripts/seed.sh
```

**成功確認**: commandが4 keyをuploadし、0で終了したことを確認する。4 keyすべてが存在するならseed済みなので、
再seedせず通常運用へ移る。

**停止と復旧**: 1〜3 keyだけが存在する場合は部分seedであり、通常のpull / merge / pushでは復旧できない。
まだ供用前で、他のwriterがなく、対象bucketと次のexact keyだけを人間が確認できた場合に限り、部分seedのkeyを
削除して`seed.sh`をやり直す。供用開始後または状態を確認できない場合は削除せず停止する。prefix削除は禁止する。

```bash
(set -a; source .env; set +a; \
 export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" \
        AWS_DEFAULT_REGION=auto
 endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
 aws s3api delete-object --bucket baibai-stores --key market.sqlite --endpoint-url "$endpoint"
 aws s3api delete-object --bucket baibai-stores --key runs.sqlite --endpoint-url "$endpoint"
 aws s3api delete-object --bucket baibai-stores --key macro.sqlite --endpoint-url "$endpoint"
 aws s3api delete-object --bucket baibai-stores --key baibai.sqlite --endpoint-url "$endpoint")
batch/scripts/seed.sh
```

### Workerをdeployする

**前提**: repository Actionsにvariable `R2_ACCOUNT_ID`と、対象accountだけに絞った
`Workers Scripts Write` secret `CLOUDFLARE_API_TOKEN`を設定する。production deployは
`.github/workflows/web.yml`が所有する。`VIEW_PASSWORD`にはpassword manager等で生成した32文字の
CSPRNG英数値を使う。

**実行**:

```bash
gh workflow run web.yml --ref main
gh run list --workflow web.yml --limit 3
cd web/edge
# 初回 deploy は secret 未設定時に全 API を 401 にする。
npx wrangler secret put VIEW_PASSWORD
```

workflowは`web/frontend/`・`web/edge/`の変更でだけ起き、PRではUI lint/build/testとWorker
types/typecheck/test/dry-runまで実行する。mainの同変更またはmainを明示したmanual dispatchだけが同じgateの後に
deployする。deploy対象jobはproduction concurrency groupで直列化し、deploy直前のremote `main`と対象treeが
一致するrunだけを反映する。後続docs-only commitはdeployを失わせず、後続web変更があるrunはstaleとしてskipする。
npm auditは、外部advisoryで無関係な緊急修正を止めないようdeploy jobに置かず、`node-audit.yml`のlockfile変更時と
`security.yml`の週次実行で検査する。Cloudflare tokenはdeploy stepだけへ渡す。

workflowがdefault branchに存在する状態で初回materializeを実行する。

```bash
gh workflow run cloud-materialize.yml --ref main
gh run list --workflow cloud-materialize.yml --limit 3
```

materialize完了後、passwordを画面表示・shell引数化せず、既存view API routeを未認証・誤認証・正認証で検査する。`VERIFY_TICKER`はservingに存在する4文字tickerへ必要に応じて変更する。keyを取るroute（screening history / macro context / ticker）はservingに無いkeyでも検査し、正認証が200ではなく404へ解決することを確かめる。どのkeyがservingに存在するかへ依存せず、既存view routeのauth境界を検査するためである。passwordはprompt入力のみを受けるので、TTYの無い経路（agent やpipe経由の実行）ではrequestを1本も送らずexit 2で止まる。

```bash
web/edge/scripts/verify-deployment.sh
```

**成功確認**: web workflowとmaterializeが成功した後、`verify-deployment.sh`で既存view API routeの未認証・誤認証・
正認証を確認する。正認証の不存在keyは404になることを確認する。

**停止と復旧**: owner Bearerは`VIEW_PASSWORD`未設定で401になり、共有認証も未設定なら全APIが401になる。passwordは引数やshell historyへ書かずpromptへ
入力する。TTYのない経路では検査scriptがrequestを送らずexit 2で止まる。失敗時はdeployやR2操作を重ねず、
workflow logまたは検査結果の原因を直す。

`*.workers.dev`のHTTP requestはWorkerが認証判定より前に308でHTTPSへredirectし、HTTPS responseはHSTSを返す。UI navigationは必ずこの経路を通り、hashed static assetだけをWorker invocationなしで配信する。

<a id="shared-read-setup"></a>

### 共有readとL1接続の設定

**前提**: mainのweb deployが共有read実装を含むことを確認します。CloudflareのR2 API token管理で、
production `baibai-stores`だけにscopeした **Object Read only** credentialを新規作成します。
publisherのread-write credentialは流用せず、canonical bucketのWorker bindingは追加しません。
Object Read onlyはbucket全体を読めます。L1以外を外部へ返さない境界はWorkerのnamespace allowlistであり、
prefix単位IAMではありません（[Cloudflare公式](https://developers.cloudflare.com/r2/api/tokens/)）。

`READ_ACCESS_TOKEN`はowner passwordとは別に、32 random bytes以上をCSPRNGで生成しbase64url等にします。
実値と共有URLはpassword manager等の非公開経路だけで扱い、Git、Issue、PR、CI log、artifact、shell引数へ
残しません。`wrangler.jsonc`はobservabilityとLogpushを明示的に無効化し、既存productionの無効状態を維持します。
固定版Wrangler 4.114.0のschemaは`redact_query_string`を受け付けません。Cloudflareの
[Script Settings API](https://developers.cloudflare.com/api/resources/workers/subresources/scripts/subresources/settings/methods/get/)
にはlogs/tracesのURL queryを除く同名設定がありますが、未対応fieldをWranglerへ追加して有効と見なしません。
logs/tracesを有効化する変更では、採用Wranglerのschemaとdeploy metadataがこの設定を正式に扱うことを確認し、
`redact_query_string=true`をdeployment設定へ固定して、非秘密のqueryでredactionを受入確認します。
実際の共有tokenで試験しません。real-time logの`wrangler tail`、dashboard Live Logs、Tail Worker等は、
共有tokenを使うrequest中に起動・接続しません。

**実行**: 以下は`web/edge`でpromptへ値を入力します。`L1_R2_BASE_URL`は実bucketのjurisdictionに合う
`https://<account-endpoint>/<bucket>`を指定し、末尾にobject key、query、credentialを入れません。
endpointの実値もGitへ残さないためWorker secretとして設定します。

```bash
cd web/edge
npx wrangler secret put READ_ACCESS_TOKEN
npx wrangler secret put L1_R2_BASE_URL
npx wrangler secret put L1_R2_ACCESS_KEY_ID
npx wrangler secret put L1_R2_SECRET_ACCESS_KEY
```

**成功確認**: productionのScript Settingsでobservabilityが未設定またはlogs/tracesとも無効、Logpush無効、
tail consumerなしを確認します。認証値・bindingsは出力せず、設定項目だけを確認します。無効状態が異なる場合は
共有URLの利用を始めず、deployment設定と実設定を一致させます。
owner Bearerと共有Bearer/queryで既存JSONの具体値・更新時点を読み、未認証と旧/誤tokenが
401、重複shareが400になることを確認します。`/?share=...`は通常UIを開き、最初のAPI request前にURLから
shareが消え、owner保存値が維持されることを確認します。共有URLを第三者へ一般公開しません。

L1は[固定release手順](../docs/reference/market-lake.md#shared-raw-read)でcurrent → release manifest →
dataset manifest → 小さい実Parquetへ進みます。対象ChatGPTの分析workspaceへ自動共有readで実ファイルが入り、
decodeして件数・null・具体値を計算できたことを確認します。その同じreleaseから次をlocal baselineと照合します。

- 3539または6675等の個別銘柄の日足・財務の期間、row、null、保存値
- 1営業日分の全銘柄daily barsのrow数と簡単な集計
- 2銘柄×約3年等の複数partition履歴の欠落・重複・release混在の有無

実bucketはこの利用側受入だけに使い、unit test、data rebuild、daily batch dispatchには使いません。
HTTP 200や手動uploadだけでは分析成功にしません。download不可、fileは入るがdecode不可、size制限、
parser不足等を実測して記録し、未達ならIssueを閉じません。変換機構をその場で追加せず、観測結果から次案を決めます。

**停止と復旧**: shared secret未設定・空は共有accessだけを無効化し、L1接続値の不足はlakeだけ503にします。
上流403/5xx等は安全な502となるので、read-only scopeとendpoint設定を確認します。秘密値や上流error bodyを
logへ出しません。設定失敗時はowner credential、store、daily batchを変更せず設定をやり直します。

**rotationと撤回**: 共有tokenは同じ`secret put READ_ACCESS_TOKEN`で置換し、旧値401・新値成功を確認します。
次の401でfrontendは共有sessionだけを消します。共有撤回は`npx wrangler secret delete READ_ACCESS_TOKEN`です。
L1 credentialは新しいbucket-scoped Object Read only tokenを作り、2つのS3 secretを更新してGETを確認した後に
旧R2 tokenを失効させます。更新途中のlake 502は全設定が揃ってから再確認します。L1公開自体の撤回は
`npx wrangler secret delete L1_R2_ACCESS_KEY_ID`でgatewayを503にし、Cloudflare側でもその専用tokenを失効させます。
既存owner viewとcanonical dataは維持され、secret変更で再deployは不要です。

## 日常運用

### クラウド正本をローカルへ取得する

**前提**: research-triage / research / macro-context の運用を始める前に実行する。平日16:43〜22:00 JSTは避ける。
この窓ではbatch前後のstoreが混ざり、実在しない断面を作り得る。窓内なら、次で当日のrunが`completed`か
確認してから進む。

```bash
gh run list --workflow cloud-daily-batch.yml --limit 1
```

**実行**:

```bash
batch/scripts/pull.sh
```

**成功確認**: `pull.sh`がmarket / runs / macroの全downloadとSQLite `quick_check`、
marketのlake復元を終えたことを確認する。

`pull.sh`は転送の全検査が成功してから3 storeを置換し、続いて`hydrate-market`でlake所有tableを復元する。
`baibai.sqlite`には触れない。転送中の失敗では既存storeを保持する。復元の失敗では取得済みstoreが残るが、
marketは分析可能とは限らないため、原因を解消して`hydrate-market`を完了してから分析へ進む。

### Screening入力を検証・補修する

**前提**: screening入力を実際に検証・補修する場合だけ実行する。Macro Contextの作成や、
既存Review Setを読むResearch Triageの前提にはしない。対象日を指定し、個別銘柄の確認が必要なら対象tickerを指定する。

**実行**:

```bash
uv run baibai-engine screening verify-cache-coverage --asof YYYY-MM-DD
# 個別銘柄の入力を確認する場合
uv run baibai-engine screening ticker-profile --ticker TICKER
```

**成功確認**: `verify-cache-coverage`が対象日の必要入力を満たし、個別確認では
`ticker-profile`が対象tickerを返すことを確認する。

**停止と復旧**: `verify-cache-coverage` が `required-field:<name>@<asof>` を返した場合は、同じas-ofで
`screening bootstrap-cache` を再実行する。bootstrapは表示された`resume_from`と
`remaining_ranges`に従い、欠損tickerの既知開示日だけをchunk補修する。既存の広い
`jquants_fin_summaries` coverageを`invalidate-coverage`で外すと正常な履歴まで再取得対象に
なるため、required-field補修には使わない。補修後は同じverify commandで
`market_cap_required_fields`と`valuation_required_fields`がminimum以上であることを確認する。

### ローカルからクラウドを更新する

**前提**: schemaを上げるcodeをmainへ入れ、ローカルの変更と対象storeを確認する。cloud copyを取り込んで
包含したものだけをcloudへ更新する。deep historyを単独反映する`market.sqlite` / `macro.sqlite`では`push-market` / `push-macro`が
次の3段を1コマンドで行う。

1. **download** — cloud copyをstagingへ取る
2. **schema確認** — market は現行schemaだけを受理し、macroは現行migrationでstaging copyを進める。R2 objectはmerge後のuploadまで変わらない
3. **merge → upload** — cloud copyをローカルstoreへmergeし、cloud側の行が1行でも取り残されるなら停止する。全て取り込めた場合だけuploadする

```bash
batch/scripts/r2_transfer.sh publish-lake          # market storeを進めた場合は先にこれ
batch/scripts/r2_transfer.sh push-market
batch/scripts/r2_transfer.sh push-macro
gh workflow run cloud-materialize.yml --ref main   # 表示へ反映する場合
```

ローカルでdaily全体を実行した場合は、実行直前の`pull.sh`が記録した3 storeのETagを使い、
同じbundleを1 commandで反映する。

```bash
batch/scripts/r2_transfer.sh publish-lake
batch/scripts/r2_transfer.sh push-machine
```

`push-machine`はupload前にmarket / runs / macroのremote ETagをすべてpull時点と照合し、1本でも
変わっていれば3本とも書かずに停止する。各PUTも同じETagへ`If-Match`を付け、3本成功後だけ
`machine-manifest.json`を更新する。

**成功確認**: 単独pushはmergeがcloud側の全行を包含したこと、bundle pushはpull時の全generationと
remoteが一致したことを確認し、いずれもCAS upload成功をcommand outputで確認する。
表示へ反映する場合は`cloud-materialize`の完了も確認する。

**market storeはlakeへpublishしてからpushする。** lake所有tableのcanonicalはR2のL1 releaseに
あり、`push-market`が送るのはstore-local data tableとstore自身のrelease origin metadataで
ある。`publish-lake`を飛ばすと、
dehydrateが「releaseが持つ行数と合わない」で停止する。ローカルが cloud より遅れている場合は先に
`hydrate-market`で現行releaseへ揃える — publishはstoreをhydrateしたreleaseの上にだけ積めるので、
別のwriterが進めたlakeの上へ古いstoreをpublishすることはできない。

`hydrate-market`はlake所有tableへ変更を加える前にだけ実行する。変更後に再度hydrateすると、行数が
同じUPDATEやDELETE+INSERTをrow-count guardでは識別できず、current releaseの内容で上書きする。
変更を残すなら先に`publish-lake`し、捨てる場合だけ明示的な復元操作としてhydrateする。daily workflowは
hydrate後にfetch/build/publishへ直列に進み、変更後の再hydrateを行わないため、hot pathへ全partition
比較や追加lockは置かない。

**publishするcodeは、cloudが動かすcodeでなければならない。** ローカルのschema versionがmainより先にあると、cloudが知らないversionのstoreを置くことになり、次の日次batchが`open_connection`のbaseline検査で停止する（`supported range`を挙げてfail-fastし、Discordに`[FAILED]`が出る。1世代の`.bak`も残る）。schemaを上げるcodeは**mainへ入れてからpushする**。

**pull側にschema検査を置いてはならない。** pullは転送とSQLite整合性確認に限定し、writerとreaderがcurrent schemaだけを受理する。これにより、storeを1行も書かない転送までschema不一致へ過剰に結合しない。

**停止と復旧**: mergeの取り残し、origin不一致、schema不一致、CAS failureではuploadしない。
最新cloud copyからやり直す。local dailyの`push-machine`は、そのdaily直前に`pull.sh`で取得した
3 storeを一組として使い、個別の`pull-runs`や無条件uploadでruns storeだけを差し替えない。

### application DB を反映する

**前提**: application DBの判断・確認済み事実・運用状態の更新を完了し、schema cutoverを含むcodeはmainへ入れる。

**実行**:

```bash
batch/scripts/publish.sh
```

引数なしでだけ実行する。`-h` / `--help`は外部書き込みを行わずusageを表示し、その他の引数はupload前に拒否する。
引数なしでは最初に`position ledger`を実行し、ledgerの整合性を確認する。保有価格の欠損・staleや権利単位の未確認は時価未評価として表示し、uploadを止めない。価格の読み取りは[`portfolio-ledger.md`](../docs/reference/portfolio-ledger.md#market-price-and-tax)を正本とし、配信のための台帳への価格転記は要求しない。

このscriptは`baibai.sqlite`のconsistent snapshotだけをstoresへ送り、`cloud-materialize`をdispatchする。servingへの直接writeは行わない。

**成功確認**: `publish.sh`とdispatchされた`cloud-materialize`が成功し、表示のas-ofと対象judgmentのIDを確認する。
Screeningの最新表示は、表示中の`review_set_id / run_revision_id`に束縛されたResearch TriageとCAAだけを載せる。
同日でも後発のScreening Runが公開されると、先行runの判断は最新一覧の対象から外れる。
公開済みCAAの反映は、そのimmutable IDのAssessment詳細と参照先Thesisで確認する。
最新一覧への非掲載だけを理由に、既存ResearchのTriage bindingを付け替えない。
後発Review SetのTriageを進める場合は、完了済みmachine bundleを取得して`research-triage`の通常手順で扱う。

**停止と復旧**: ledger preflightが失敗した場合はstoreをuploadせず停止する。exportはstoreのschemaがcodeと一致しない間、viewを1件も書かずexit 1で停止する。
schemaを一致させてから再実行する。application更新の表示には、そのsnapshot uploadとmaterializeの成功が必要である。cloud dailyによる独立した機械view更新とは区別する。

application DBはcurrent schemaだけを開き、クラウドはこのstoreをread-onlyで読む。schemaを上げるPRは、
source versionを限定したtemporary one-shot toolとexact commandを用意し、main merge後のattended cutover・検証・
cloud反映が終わるまで保持する。完了証拠を残してから同じdeliveryのcleanupで削除する。現在のtreeに汎用cutover
commandはないため、version不一致を見つけたoperatorは手書きSQLで進めず、schema変更issueへtoolを再構築する。

### application DB を復元する

application DBは判断とledgerの正本で、何も再生成しない。復元点は2系統ある。まずローカルcheckpointを使い、
それが失われた場合だけR2の世代を使う。

**共通前提**: engine CLI、Web backend、batchなどapplication DBを開く全writerを停止する。既存storeがある場合は、
置換前に次を実行し、出力されたpathを`restore_backup`へ設定する。このbackupはSQLite backup APIで
未checkpoint WALを含むconsistent snapshotを作り、`integrity_check`と`foreign_key_check`を通った場合だけ成功する。

```bash
restore_backup="$(uv run baibai-engine db backup | \
  uv run python -c 'import sys, yaml; print(yaml.safe_load(sys.stdin)["path"])')"
test "$restore_backup" != None && test -f "$restore_backup" || exit 1
```

backupに失敗した場合、またはwriter停止を確認できない場合はcanonical fileへ触れない。

#### ローカルcheckpointから復元する

**前提**: 復元対象の時点を確認する。checkpointは`uv run baibai-engine db backup`で作り、
WAL込みのsnapshotとして`integrity_check`と`foreign_key_check`を通したものだけを直近10世代残す。
runtime migrationはないため、復元候補の`user_version`がcurrent schemaと違う場合は直接配置しない。

**検査**: 候補を配置する前に、次の出力を確認する。`integrity_check`がexact `ok`、
`foreign_key_check`が0行でなければ停止する。

```bash
ls -t stores/application/backups/                      # 世代を新しい順に見る
read -r -p '復元するcheckpoint path: ' target
test -f "$target" || exit 1
sqlite3 "$target" 'PRAGMA integrity_check;'
sqlite3 "$target" 'PRAGMA foreign_key_check;'
sqlite3 "$target" 'PRAGMA user_version;'               # 戻す先のschema版
```

**配置**: 検査合格後、同じshell sessionでだけ実行する。writer停止を再確認し、旧DBのsidecarを除いてから配置する。

```bash
restore_stage="$(mktemp stores/application/baibai-restore.XXXXXX.sqlite)"
cp "$target" "$restore_stage" && \
  rm -f stores/application/baibai.sqlite-wal stores/application/baibai.sqlite-shm && \
  mv -f "$restore_stage" stores/application/baibai.sqlite
uv run baibai-engine position ledger | head -20        # ledger headを確認
```

**成功確認**: `integrity_check` / `foreign_key_check`、`user_version`、復元後のledger headを照合する。
戻したstoreの`user_version`はcurrent schemaと一致しなければならない。異なる版をCLIに開かせて自動変換しない。

**復旧**: 照合に失敗したら判断を再開しない。同じshell sessionで、失敗copyを一意な名前へ退避し、
共通前提で作ったconsistent snapshotをcanonical pathへ戻す。

```bash
restore_stamp="$(date +%Y%m%dT%H%M%S)"
failed_restore="stores/application/backups/baibai-failed-restore-${restore_stamp}.sqlite"
test ! -e "$failed_restore" || exit 1
mv stores/application/baibai.sqlite "$failed_restore"
rm -f stores/application/baibai.sqlite-wal stores/application/baibai.sqlite-shm
restore_stage="$(mktemp stores/application/baibai-rollback.XXXXXX.sqlite)"
cp "$restore_backup" "$restore_stage" && \
  mv -f "$restore_stage" stores/application/baibai.sqlite
uv run baibai-engine position ledger | head -20
```

#### R2の世代から復元する

**前提**: ローカルcheckpointが失われた場合だけ使う。cloud copyはローカルより古い可能性がある。
`pull-app`はローカルにfileがあれば止まるため、stagingへ直接取得する。

**取得**:

```bash
(set -a; source .env; set +a; \
 export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" \
        AWS_DEFAULT_REGION=auto
 endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
 aws s3api list-objects-v2 --bucket baibai-stores --prefix baibai.sqlite.bak- \
   --query 'Contents[].[Key,LastModified]' --output text --endpoint-url "$endpoint"
 aws s3api get-object --bucket baibai-stores --key baibai.sqlite.bak-YYYYMMDD \
   --endpoint-url "$endpoint" /tmp/baibai-restore.sqlite)
```

**検査**: 取得後、次の出力を確認する。`integrity_check`がexact `ok`、`foreign_key_check`が0行でなければ停止する。

```bash
sqlite3 /tmp/baibai-restore.sqlite 'PRAGMA integrity_check;'
sqlite3 /tmp/baibai-restore.sqlite 'PRAGMA foreign_key_check;'
sqlite3 /tmp/baibai-restore.sqlite 'PRAGMA user_version;'
```

**配置**: 検査合格後、writer停止を再確認し、同じshell sessionでだけ実行する。

```bash
restore_stage="$(mktemp stores/application/baibai-restore.XXXXXX.sqlite)"
cp /tmp/baibai-restore.sqlite "$restore_stage" && \
  rm -f stores/application/baibai.sqlite-wal stores/application/baibai.sqlite-shm && \
  mv -f "$restore_stage" stores/application/baibai.sqlite
uv run baibai-engine position ledger | head -20
```

**成功確認と停止**: 復元後にledger headを照合し、
次の`publish.sh`まで判断を再開しない。publish済みで未pushの窓ではローカルが進んでいるため、ローカル側が
残っているならR2世代へ戻さない。照合に失敗した場合は、ローカルcheckpoint手順の復旧blockで失敗copyを退避し、
`$restore_backup`が存在する場合はcanonical pathへ戻す。元のstoreが無かった場合は判断を再開せず、
別のR2世代をstagingで検証する。

### 履歴を深くする

#### Macro履歴

**前提**: 系列追加後またはrolling窓を超える取得断で、indicator storeの過去を補う場合に使う。
日次batchはfrequency別のrolling窓しか引き直さない。

**実行と成功確認**: ローカルで全履歴を取得し、`reading`で履歴不足・異常値がないことを確認してからpushする。

```bash
uv run baibai-engine macro refresh <series_id> ... --all-history --end YYYY-MM-DD
uv run baibai-engine macro reading --asof YYYY-MM-DD   # 履歴不足・異常値を確認
# series を追加した場合は、ここで registry を main へ入れてから push する
batch/scripts/r2_transfer.sh push-macro
```

**停止と復旧**: registryを追加した場合はcodeをmainへ入れるまでpushしない。`reading`またはmergeが停止したら
uploadせず、ローカルで原因を解消する。

#### Macro観測とregistryの修正

vintage、retraction、generation、no-lossの意味は[Macro reference](../docs/reference/macro.md#①-データindicator-series-を引く)を正本とする。

**観測の撤回**: 誤値をlocalでDELETEしてもcloud mergeが戻すため、`macro retract <series_id> --observed-at <date> --expected-vintage <ts>`で最新vintageを名指して撤回する。実行前に対象日とvintageを確認し、derived入力の場合はコマンドが示す依存系列の同日を先に撤回する。CAS拒否時は再実行せず現在vintageを読み直す。成功後は正しい下位vintageが読まれるか、下位が無ければ当日がreading / chartから外れることを確認する。誤った撤回は対象sourceとvintageを確認して修正し、blind DELETEや同じCASの反復をしない。

**系列の追加・退役・改名**:

1. mainを取り込んだcheckoutで`uv run baibai-batch validate-macro-stores`を実行し、変更前の観測と発行済みcontextを確認する。
2. series ID集合の変更では`definitions.py`のmembership digestを次のgenerationとして追記し、変更後もvalidatorを通す。退役系列の引用warningは確認し、発行済みcontextを書き換えない。load不能なら停止してcodeの原因を直す。
3. localで現行系列を指定した`macro refresh`を実行する。登録外系列のpruneはこの明示refreshの開始時だけ行う。全期間更新はbaseを先に、derivedを後に実行する。
4. cloudへ渡す前に変更codeをmainへ入れる。`registry-prune-pending`と`registry-prune`のtransaction ID・系列・削除件数を照合する。pendingだけではcommit成功と扱わない。validatorとreadingで成功を確認してから`push-macro`でcloud copyをmergeする。codeがmainへ入る前のpush、merge失敗後のblind overwriteは禁止する。

**停止と復旧**: 意図しないpruneや片方だけのlogを見つけたらpushを止める。cloudへ反映済みなら次のpushが1世代の`.bak`を置換する前に[部分pushからの復旧](#部分-push-からの復旧)で状態を確認し、検証したcopyから復元する。再取得できない履歴があるため、cloudの行を失わないmergeとbackupを省略しない。

#### Market履歴

**前提**: 日次batchが遡らない過去を補う場合に使う。local providerで必要範囲を取得し、完全なstoreを作ってからcloudへ反映する。

ローカルから載せる手順は、触ったtableがlake所有かどうかで分かれる。`_push_keys`はuploadする複製を`dehydrate_market_snapshot`に通し、**releaseが持たない行をlake所有tableに持つstoreのuploadを拒否する**ので、lake所有tableを増やした場合は`push-market`だけでは止まる。

```bash
# (a) lake所有tableを増やした場合: hydrate済みstoreで変更し、先にreleaseへ載せる
batch/scripts/r2_transfer.sh publish-lake
batch/scripts/r2_transfer.sh push-market

# (b) store-local table（source_coverageとtse_capital_policy_snapshots）だけを変えた場合
batch/scripts/r2_transfer.sh push-market

```

**成功確認**: `publish-lake` / `push-market`のno-loss検査を確認し、cloud copyをpullした後、`source_coverage`の`ok`窓が指定範囲を連続して覆うことを照合する。

```bash
batch/scripts/pull.sh
history_start=YYYY-MM-DD
history_end=YYYY-MM-DD
sqlite3 -header stores/market/market.sqlite \
  "SELECT source, coverage_start, coverage_end, record_count, status
   FROM source_coverage
   WHERE NOT (coverage_end < '${history_start}' OR coverage_start > '${history_end}')
   ORDER BY source, coverage_start, coverage_end;"
```

**停止と復旧**: lake所有tableを増やしたのに`publish-lake`していない場合、dehydrateが停止する。local取得がnonzeroならuploadせず、保存済み範囲と未完範囲を`source_coverage`で分け、未完範囲だけを再実行する。

### merge が検査するもの

どちらのmergeも、終わった時点でsource側だけに残る行が1行でもあれば停止する。日次batchが取得済みでローカルに無い行を、uploadで失わないための不変条件である。以下はstoreごとに違う部分。

`push-market`のmergeは`merge_market_store.py`である。対象はstore-local data tableと
`lake_store_origin`で、lake所有tableのcloud/local突き合わせはreleaseが引き取っている。
`source_coverage`はunionし、operator導出の`tse_capital_policy_snapshots`とstore自身の
`lake_store_origin`はtargetを保持する。cloud copyのoriginを取り込むと、local rowsを別release由来と
偽ってしまうためである。`publish-lake`はstoreをhydrateしたreleaseをlakeが既に離れていれば拒否し、
dehydrateはreleaseが持たない行を持つstoreのuploadを拒否する。

`source_coverage`は取得範囲の帳簿で、両側が書くので主キー`(source, coverage_key)`で`INSERT OR IGNORE`し、同じ主キーを両側が持つ場合はpayloadの一致を検証する。**比較しないのは、出所が何を言ったかではなくstoreがいつ読んだかを記録する`fetched_at_utc`だけ**——2つのstoreが同じ範囲を別の時刻に読めばそこは必ず食い違うので、比較すれば全てのmergeを拒否する。

`record_count`は行が在る場所でしか証明できない。R2が運ぶdehydrate済みのsourceでは証明せずclaimとして受け取り、targetのclaimは**実rowへ引き上げるだけで、決して引き下げない**。引き下げは、このstoreが満たされていないreleaseを記述しているclaimを、より小さい数値で置き換える操作である。次のhydrateが行を戻してもledgerは小さいままで、`verify-cache-coverage`が以後の全screening runを止める一方、再取得は永久に計画されない——`covered_intervals`が窓を落とすのはcountが0のときだけだからである。引き上げられないclaimはmerge後の検査で停止し、「lakeがserveしているreleaseからhydrateし直せ」と出る。**空のtargetもここで止まる**——「取得済み」と言うclaimを黙って0へ書き換える代わりに拒否する。

**ledgerは追記専用ではない。** 取得に失敗すると、その範囲は重なる`ok`窓から切り出され、残余が新しいkeyで書き直される（穴が失敗した場所に見えるようにするため）。keyによるunionは、後の取得が撤回した広い窓を古いcopyから復活させ得るので、mergeはそれを修復せず拒否する——同じsourceで`ok`窓が`failed` / `partial`窓に重なるledgerは、どのfetcherも書かない形である。

訂正可能な`jquants_short_sale_reports`のcoverageはdisclosure dateごとに1つのclaimを選ぶ。`ok`が`partial`/`failed`に勝ち、同種なら`fetched_at_utc`が新しい方が勝つ。`record_count`はreleaseが満たしたtargetの実rowから読み直す。行が1つも claimされないdateがあれば停止するが、**その検査はclaim選択の後**に置く——行はhydrateで、claimはmergeで届くので、cloudが取得して publishした日はtargetのtableに1段先に現れる。

`tse_capital_policy_snapshots`はoperatorが導出したもので、targetを丸ごと残しsourceから1行も
取り込まない。key mergeすると、後の導出が撤回した行が古いcopyから復活し、訂正した値は
「2つのstoreが食い違う」と読まれてpublish全体を止める。`jpx_delistings`と
`tender_offer_exit_values`はlake datasetであり、merge対象ではない。

mergeの対象tableは`merge_market_store.py`の`FACT_KEYS` / `DERIVED_KEYS`に列挙し、**それとlake datasetの合併がstoreのtable一覧と一致すること**をtestが確かめる。新しいtableはlakeかmergeのどちらかに分類しないと落ちる。

machine storeの全writerはdownload時のR2 ETagを保持し、backupは同じsource ETag、最終`PutObject`は同じdestination ETagを条件にする。日次batchは`pull-machine`が3 storeのgenerationを記録し、`push-machine`が全keyを事前照合してから各keyを条件付きで発行する。merge中またはupload直前に別writerがobjectを更新した場合はprecondition failureで停止し、最新cloud copyからやり直す。これにより、GitHub Actions外の手動pushと日次batchのどちらが後着しても、先に発行された更新を巻き戻さない。途中のkeyでnetwork / precondition failureになった場合はserving tailを発行しない。3 keyのPUTが全て終わってから書かれる`machine-manifest.json`（bundle receipt）も書かれないので、次回の`pull-machine`は旧receiptとの突合で停止する——次回runが自力で再構成することはない。復旧は下の「部分 push からの復旧」に従う。

`push-macro`のmergeは`merge_indicator_store.py`である。対象は事実を積み上げるtable（`observations` / `provider_runs`）だけで、主キーで`INSERT OR IGNORE`する。同じ主キーを両側が持つ場合は全payloadの一致をmerge前後に検証し、値・単位・source等が異なれば片方を正本と推測せずtransaction全体を停止する。source / target はschema version・列構成に加えて`schema.sql`由来の全persistent triggerとregistry state contractをcanonical定義へ完全一致させる。targetが保持する全series metadataは両端が有限なplausible rangeを持つことを前提とし、source / target observationをtransaction先頭でtargetのunitとrangeに照合する。いずれかの契約違反があればtargetを変更せず停止する。`series` / `aliases`はsourceから取り込まない。通常のopenは登録外seriesのfacts・metadata・aliasesを保持し、明示的な`macro refresh`だけが現行registryに無いseriesをpruneするため、古いbranchのread後もtargetに残る新系列へcloud factsをmergeできる。source の registry generation が target より新しい場合と、同世代なのに `source.series` membership がtargetから欠ける場合は、facts未取得のseriesでもmergeを拒否する。target が source より新しい世代でmetadataが無いseriesのrowだけを意図した退役としてskip件数に含める。`market.sqlite` / `runs.sqlite`は`push-macro`が触らない。

### 手動で lake を publish する

**前提**: schema cutoverなど、定時batchを待たずにローカルstoreからpublishする必要があること、worktreeが
cleanであること、storeの`lake_store_origin`が開始時current pointerと一致することを確認する。

**実行**:

経路は日次と同じ1つで、毎回全partitionを導出し、serving releaseからorigin束縛と履歴の床だけを読む。

```bash
batch/scripts/r2_transfer.sh publish-lake
uv run baibai-engine lake resolve --mirror stores --bucket baibai-stores --format json
```

**成功確認**: `resolve`のrelease identityがpublish結果と一致することを確認する。

**停止と復旧**: originが違えばdataset export前に停止する。SQLiteだけを古いbackupへ戻したstoreでは
実行せず、現行releaseからhydrateし直す。

### 部分 push からの復旧

`push-machine`は3 keyを逐次に条件付きPUTし、全て終わってから`machine-manifest.json`（bundle receipt）を
書く。2本目以降が失敗すると、receiptは旧世代のまま残り、bucketは「先行keyだけ新世代」というどのbatchも
書いていない組合せを持つ。この状態は`pull-machine`のreceipt突合が拒否する（安全側——実在しない
cross-sectionをscreeningへ渡さない）。

**停止条件**: 同じコマンドや日次batchを再実行しない。条件付きPUTは失敗したrunがpullした世代に束ねられ、
成功済みkeyのETagは既に変わっている。再実行は`push_pulled_keys`の事前照合で
`changed on R2 after the pull`となり、日次batchもpull stepで止まる。

#### 手順 (i): receiptを再生成する（既定）

**前提**: 失敗runのlogで部分pushとreceipt不一致を確認し、削除対象がbucket直下のexact key
`machine-manifest.json` 1件であることを確認する。このkeyはmutableで、Bucket Lockの対象prefix
（`lake/l1/canonical/` / `lake/manifests/`）の外にある。

**実行**:

```bash
(set -a; source .env; set +a; \
 AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" \
 AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" \
 AWS_DEFAULT_REGION=auto \
 aws s3api delete-object \
   --bucket baibai-stores \
   --key machine-manifest.json \
   --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com")
```

**成功確認**: 次の`pull-machine`が
`no machine bundle receipt … taking the store set unverified`と警告しつつ現行世代を記録し、次の日次batchが
3 keyのpush後に新しいreceiptを書く。以後の`pull-machine`でreceipt一致を確認する。

**危険と復旧**: prefix削除はしない。receiptが無い1周だけcross-sectionの保証が落ち、削除直後に戻す
操作はない。新しい完全pushがreceiptを再生成するまでを一つの復旧作業とする。完全pushが失敗したら、
追加削除や再dispatchをせず、そのrunの失敗をローカルで再現する。

#### 手順 (ii): ローカル成果のpushで復旧する

**前提**: publishすべきローカル変更があり、[ローカルからクラウドを更新する](#ローカルからクラウドを更新する)
のno-loss条件を満たす。`push-market`には、現行releaseからhydrateした後に変更したstoreを使う。変更後に
hydrateして成果を上書きしない。

**実行と成功確認**: `push-market`または`push-macro`を実行する。mergeとCAS pushの成功後、3 keyのHEADから
receiptが現在世代へ書き直される。次の`pull-machine`でreceipt一致を確認する。

**停止条件**: `push-market` / `push-macro`はruns storeを変更しない。local dailyのruns成果を反映する場合だけ、
直前のbundle pullからremote generationが変わっていないことを`push-machine`に検査させ、個別uploadは行わない。

### 日次 workflow を手動実行する

**前提**: 原因調査には使わず、ローカルで修正とgateを完了した後の最終確認に限定する。`asof`省略時は当日JSTを
market calendarで判定し、非営業日は成功扱いでskipする。過去日を指定すると営業日gateをskipする。

**実行**:

```bash
gh workflow run cloud-daily-batch.yml --ref main
gh workflow run cloud-daily-batch.yml --ref main -f asof=YYYY-MM-DD
gh run list --workflow cloud-daily-batch.yml --limit 10
gh run watch RUN_ID --exit-status --compact --interval 60
```

**成功確認**: 対象runの終了通知を受けてconclusionを確認し、Discord通知、store push、serving freshnessを照合する。
所要時間の見積りと待機契約は[cloud-daily-batch の所要時間](#cloud-daily-batch-の所要時間)に従う。

**停止と復旧**: exit 1やcoverage不足ではuploadせず原因を直す。exit 3は後続の公開結果を確認し、公開成功なら繰延べた対象だけを復旧する。upload失敗・部分pushは対応する復旧節に従い、全工程をblindに再dispatchしない。

通常cronは平日07:43 UTC（16:43 JST）。scheduled workflowは実行開始時のJST日付ではなく、直近の07:43 UTC cron日を対象にして営業日gateを適用する。GitHubのqueue遅延がJST日付をまたいでも、未公表の翌日データへ対象を進めない。同日必須なのは対象日の株価日足だけで、[J-Quants APIの公式更新時刻](https://jpx-jquants.com/ja/spec/data-update)は16:30頃のため13分の余裕を置く。JPX規制ページはevent駆動のstatus pageでcoverage gateが7営業日まで許容し、信用残は週次なので、いずれも夕方の更新を待つ必要がない（この実行より後に出た指定は翌営業日の実行が拾う）。分を半端にしているのは意図的で、GitHubがscheduleを:00 / :15 / :30 / :45へ集中させるため、その境界に置くとqueue待ちの後ろに並ぶ。schedule遅延自体は許容する。遅延ではなく**欠測**は`cloud-batch-watchdog`がpushで検知し、UIのas-ofとworkflow履歴は裏取りのpull経路として残る。16:43時点で株価日足が未更新ならcoverage gateがpublish前に停止し、復旧は現行mainから手動dispatchする。

daily batchはcoverageが完全でも`bootstrap-cache`を実行する。財務サマリーの直近7日を再取得するため、同日の先行runより後にJ-Quantsへ反映された開示は後続runで取り込まれる。bootstrap後はcoverageを再検証してからscreeningへ進む。

日次commandのexit 3でも後続のstore/serving公開は進められる。公開成功後の通知を`[DEGRADED]`とし、local結果と公開結果を分ける。非営業日skipは既存servingを変更しない。

## Actions 使用量の月次確認

以下の集計は取得したrunのwall timeを調べる診断であり、billable minutesや請求額ではない。実際の契約・利用量・請求はBillingで確認する。

**実行**:

```bash
gh run list --created ">=$(date -d '14 days ago' +%F)" --limit 1000 \
  --json workflowName,startedAt,updatedAt \
  --jq 'map(select(.startedAt >= "2020" and .updatedAt >= "2020"))
        | group_by(.workflowName)
        | map({wf: .[0].workflowName, runs: length, min: (map(((.updatedAt|fromdate)-(.startedAt|fromdate))/60) | add | floor)})
        | sort_by(-.min)'
```

**成功確認と判断**: 取得範囲とrun数を確認し、定時実行、PR、障害復旧dispatchを分けて負荷を見る。runごとの固定加算で請求へ換算せず、無料枠や予算を文書の概算から決めない。spending limitの変更は所有者が判断する。

#1016の「月2,500分超が2か月続いたらself-hosted runnerを再評価する」は運用上の検討目安であり、契約上の無料枠ではない。

## Password rotation

**前提**: 新しい32文字CSPRNG値をpassword manager等で生成する。実値をGit、issue、log、引数、shell historyへ
書かない。

**実行**:

```bash
cd web/edge
npx wrangler secret put VIEW_PASSWORD
```

**成功確認**: promptへ新しい値を入力し、旧値が401、新値が認証成功になることを確認する。次の401でbrowserの
旧値はlocalStorageから削除され、password入力画面へ戻る。

**停止と復旧**: 値を表示・引数化しない。失敗時はR2やapplication dataを変更せず、secret設定をやり直す。
Workerの再deployは不要である。

## R2 transferの安全境界

- upload前にPython `sqlite3.backup`でsnapshotを作り、WAL未checkpoint行を含めて`quick_check`する。
- 複数storeのpushは全snapshotの作成・検査を終えてからuploadを始める。3 store一括の`push-machine`はGitHub Actionsと明示的なlocal dailyで使い、pull時の全ETag一致と各PUTの`If-Match`を必須にする。`macro.sqlite` / `market.sqlite`を単独でローカルから進める場合は`push-macro` / `push-market`でcloud copyをmergeし、cloud側の行の取り残しを検出したら停止する。
- pushは上書き対象のremote objectを`<key>.bak`へ1世代copyしてからuploadする（R2内のserver-side copy。存在判定は`s3api head-object`の完全一致で、`.bak`自身をkey本体と誤認しない）。storeは原則sourceから再構築できるが、PMI履歴のようにpublisherが古いURLを落とすと再取得できない部分があるため、破損・誤pruneしたsnapshotによる上書きから前回分へ戻せる状態を保つ。復元は`.bak`を本keyへcopyし直す（`aws s3api copy-object`を使う。`aws s3 cp`のS3→S3経路はobject sizeで実装が切り替わり、multipart copyはGetObjectTagging、single-part copyは`x-amz-tagging-directive`を要求してどちらもR2が実装しない。CopyObjectはdirectiveを送らず5GBまでのobjectで通る）。R2はcopyが終わるまで応答を返さず、その待ちはobject sizeに比例してGB級のstoreではaws CLI既定のread timeout 60秒に収まらないため、pushの世代保存も手動復元も`--cli-read-timeout`を既定より広げて呼ぶ。**`market.sqlite`が運ぶのはstore-local data tableと`lake_store_origin` metadataである。** `push-machine`はkeyごとの処理時間を出すので、storeが伸びたときの内訳はrunのlogで見る。
- machine store の`.bak`は1世代のみで、次のpushで置き換わる。前世代は次のpushで置き換わるため、24時間保持される保証ではない。`baibai.sqlite`だけは`baibai.sqlite.bak-YYYYMMDD`（JST）で日ごとに1世代を残し、直近14世代を超えた分をpush成功後に削除する。machine storeにも再取得できない観測があるため前世代を残し、判断と確認済み事実を持つapplication storeは日別世代を残す。prune は`baibai.sqlite.bak-`配下をlistし、`baibai.sqlite.bak-YYYYMMDD`に一致するkeyだけを完全一致で削除する（prefix削除はしない）。registry編集後は日次workflowの`registry-prune-pending` / `registry-prune`行（transaction ID・series ID・observation/provider-run削除件数）を当日中に確認する。pending に対応する committed 行が無い実行や意図しないpruneを検出したら、次のpushが`.bak`を置き換える前に状態を確認・復元する。
- 初回seedは既存のstore keyを1件でも検出したら停止し、再seedによるクラウド正本の上書きを許可しない。
- pullは固定4 key以外を受け付けず、全downloadと`quick_check`完了後に置換する。
- application store (`baibai.sqlite`) のpullは`pull-app`だけが行い、bulk pullは触らない。この storeの正本はローカルで、判断はローカルでpublishしてから`push-app`でcloudへ出すため、publish済みで未pushの窓ではローカルがcloudより進んでいる。cloud copyでの置換は再生成できないjudgmentを消すので、`pull-app`はローカルにfileがあれば止める。CIはcheckout直後で`stores/application/`が空なので素通りする。ローカルで意図して置き換えるときは、既存fileを自分で退避してから実行する。
- servingの`views/`は`aws s3 sync --delete`で完全像に合わせる。historyは追記だけで削除しない。
- `views/meta.json`は他のviewとhistoryが全て成功した後に最後にuploadする。
- bucket名は`R2_STORES_BUCKET` / `R2_SERVING_BUCKET`で明示的にoverrideできるが、通常は固定defaultを使う。

### schema cutoverを修正するとき

**停止条件**: store全体を古いsnapshotへ戻さない。storeは毎営業日伸びるため、過去schemaのcopyへの交換は
それ以降の事実を失う。

**実行**: application / market / run storeはruntime migrationを持たない。schema変更PRが保持しているtemporary
one-shot toolを修正し、cutover前のlocal copyへ再実行してcurrent schemaの別fileを作る。toolがcleanup済みなら、
過去commandを推測せず、修正issueでexact source version・変換・検証を固定したtemporary toolを再構築する。
macro storeだけは実在する直前schemaからの一段migrationをstaging copyへ適用する。

**成功確認と復旧**: sourceと出力のintegrity・foreign key・必須table・保持対象row/headを照合してからmainへ入れ、
同じ作業でstoreを反映する。直前のpush自体が壊れた場合だけ、上の「R2 transferの安全境界」に従って`<key>.bak`の1世代を使う。

## Read modelの生成 — baibai_web.materialize

`baibai_web.readmodel` builders を共用して、UI が読む全 view を serving 配置どおりの
JSON に書き出す。Worker には業務ロジックを置かない設計の実体。

```bash
uv run python -m baibai_web.materialize --output-dir <dir> [--batch daily|manual] [--repo-root <path>]
```

出力（`<dir>` 配下）:

- `views/dashboard.json` / `views/tasks.json` / `views/screening_latest.json` / `views/operations.json`
- `views/screening_latest.json` は、有効な `reports/published/er-level-calibration-latest.yaml` と表示対象 operative run の method identity が一致する場合だけ、E[r] historical quintile と独立した8.5%以上帯の実現分布文脈を含む。run identity 不明、欠損・不正・期限切れでは field を `null` にして既存 screening 表を維持する
- `views/daily-delta.json`（前営業日の機械実行との差分。Dashboard の差分区画が読む）
- `views/macro.json`（最新 L3 Context 抜粋、指定基準日の L2 reading、全登録系列の現在読み値と
  一覧用の約1年・月次・最大13 point。daily 全履歴は含めない）
- `views/macro-series--<series_id>.json`（Macro タブで系列 dialog を開いた時だけ読む、1 系列の
  daily 全履歴。期間・粒度は browser 内で絞る）
- `views/macro-context--<context_id>.json`（published macro context の本文。Macro report 画面が読む）
- `views/capital-allocation-assessment--<capital_allocation_assessment_id>.json`（published Capital Allocation Assessment の本文。Assessment 画面が読む）
- `views/security--<ticker>.json`（保有 + 最新 run 掲載 + Review Set and Research Triage の ticker）
- `views/meta.json`（生成時刻・実データ更新時刻・store 別 as-of・batch 種別。UI の鮮度表示と同じ契約）
- `history/candidate-views/<asof>.json`（Screening Run と Security Analysis 全件を型付きUI read modelへ変換した履歴。31 日で削除）

この一覧と Worker の route 表の対応は `tests/web/test_cloud_export.py` が守る。Worker が写像する view を exporter が書かないと、その route は本番で恒久的に 404 になる。

書き出しの前に application store の `user_version` とmarket storeの完全なschema shapeがcodeのcurrent schemaと一致することを確認し、不一致ならviewを1件も作らずexit 1で停止する。その後、market storeがhydrate済みかを判定する。読み取り経路はread-onlyで初期化もcutoverもしない。storeが無いrootまたはtableを一つも持たないunwritten storeは空の正常状態としてexportする。

`views/` は毎回 export の完全な像に置換される（実行のたびに一度削除して作り直すので、対象から外れた古い view は残らない）。`history/` は追記のみで、この script は削除を行わない。上記の31日削除は serving store（R2 lifecycle）側の保持契約であり、script の挙動ではない。

Workerは認証後の`/api/screening/history`でCandidates履歴の日付一覧を返し、`/api/screening/history/YYYY-MM-DD`だけを`history/candidate-views/`へ写像する。任意history key、旧形式の`history/candidates/`、store snapshotは公開しない。L1 raw readは[共有gateway](#shared-read-setup)の境界に従う。

views の JSON は `baibai-web` の対応 API response と同形（pydantic `model_dump_json`）。`meta.json` は全 view / history の書き込み成功後に最後に書くので、途中失敗した出力 dir が新鮮さを主張する事態を避ける。

## 日次機械工程 — baibai-batch daily

営業日判定 → screening cache coverageの事前検証 → bootstrap（財務サマリーの直近7日を再取得）→ EDINET incremental extraction →
coverage再検証 → run → review-set →
macro series refresh → export → run store prune を順に実行する。
全 step は public CLI の subprocess で、step ごとにコマンドライン・exit code・所要秒を
stdout へ出す（scheduled workflow のログをそのまま読む前提）。

```bash
# ローカル通常実行（当日 JST。market calendar で非営業日なら exit 0 で skip）
uv run python -m baibai_batch.jobs.daily --output-dir <dir>

# scheduled workflow（直近の07:43 UTC cron日。JST日付をまたぐ遅延でも発火日を維持）
uv run python -m baibai_batch.jobs.daily --scheduled --output-dir <dir>

# 手動再実行・過去日（営業日 gate を skip）
uv run python -m baibai_batch.jobs.daily --asof YYYY-MM-DD --output-dir <dir>

# Discord 通知が読む最小 notice を書き出す
uv run python -m baibai_batch.jobs.daily --output-dir <dir> --notice-output <notice.json>

# 同じ工程のmachine resultを1 JSON objectとatomic manifestで受け取る
uv run baibai-batch daily --asof YYYY-MM-DD --output-dir <dir> \
  --format json --quiet --manifest-out <daily-manifest.json>
```

`--notice-output` を指定すると、batch が到達した終端 path で、Discord 通知に必要な
`asof`・`skipped`・最初の fatal / deferred `failed_stage`・Review Setの出入りだけを持つ JSON を atomic write する。
schema version や validation round-trip は持たず、各 step の所要時間・metrics・error 本文は
workflow log を読む。不正な `--asof` など batch 開始前の失敗では notice は無く、workflow の
step outcome から notifier が `[FAILED]` を出す。

終了コード:

| exit | ローカル処理の結果 |
| --- | --- |
| 0 | 正常完了、または非営業日skip。skipは新しいexportを作らない |
| 1 | 致命的失敗。今回の結果を正常な公開へ進めない |
| 3 | fresh screeningとlocal exportは作成済みだが、macro refreshやprune等の繰延べ工程が失敗 |

クラウド公開はこの後にL1 release、machine store、serving views、history/freshnessの順で進む。local exportの有無を示す既存output名`published`を、R2公開成功と読み替えない。workflowが失敗しても一部反映が済んでいる場合がある。

失敗ポリシー:

- screening 系（coverage / run / review-set）の失敗は致命的で即停止する（publish できる新しい
  run が無いため exit 1）。ただし `screening run` の exit 2 は品質警告つきの published run で
  あり、警告理由を表示して続行する。`verify-cache-coverage` の exit 1 は cache 不足マーカーが
  ある場合だけ bootstrap へ進み、マーカー無しの exit 1（rules 破損等の crash）は即停止する
- EDINET document state は日中にも変わり得るため、初回 coverage が complete でも
  `extract-edinet-metrics` を毎回実行する。変更のない metric row は baseline から再利用し、
  extraction 後の coverage と quarantine counters を current state に揃える
- macro refreshの失敗は繰り延べ、screeningのlocal exportまで進めてexit 3を返す。後続の公開と通知は前掲の終了状態に従う。

- 営業日判定は market store の `jquants_market_calendar` が情報源。対象日をカバーして
  いない場合は黙って続行せず明示エラーで停止する

## local daily analysis — canonical Review SetのResearch Triage

local analysisの操作は[Research Triage skill](../.agents/skills/research-triage/SKILL.md)、statusの意味と入力境界は[runner reference](../docs/reference/analysis-operations.md)が所有する。ここでは同じcommand列と再実行手順を再掲しない。

## 日次結果のDiscord通知

`cloud-daily-batch` は run ごとに終端結果を Discord チャンネル `#batch-runs` へ1件通知する。run単位の通知は、watchdog、workflow履歴、serving freshnessと併せて読む。チャンネルは
code が選ばず repository secret `DISCORD_WEBHOOK_URL` が指す webhook で固定する。workflow 末尾の
単一 step（`if: always()`）が、cancel を含むあらゆる終端状態で1回だけ行う。

message は 3 部からなる。

1. 見出し行 — label・as-of・失敗した step 名（あれば）。`[FAILED] as-of 2026-08-26 — failed step: hydrate`
2. `🆕 新規 Review Set 入り:` / `👋 Review Set 退出:` の 2 行 — それぞれ銘柄コード順・最大5件（超過時は全件数・表示件数・他の件数を明示）・`<ticker> <社名> E[r]±X.X%`。急落当日の候補と、Review Set から落ちた銘柄を通知だけで拾えるようにするための行である。**export に到達した run では常に出す** — 0 件の日は `なし`、delta view が読めない日は `計測なし（<理由>）` と書く。行が無いことは「0 件」「計測不能」「通知経路の異常」の3つを同時に意味してしまい、読み手が区別できない。非営業日の skip には Review Set が無いので出ない
3. `run:` — GitHub Actions の run URL。所要時間・step ごとの結果・lake release・error の本文はこの run log にある

label は5種。

| label | 意味 |
| --- | --- |
| `[OK]` | batch exit 0、upload まで成功 |
| `[SKIPPED]` | 非営業日 gate で skip（export なし） |
| `[DEGRADED]` | batch exit 3。screening は publish 済みで、見出しに最初の繰延べ失敗 step（macro / prune / task-reconcile）を表示 |
| `[FAILED]` | batch の致命的失敗（見出しに batch 内の stage 名）、または batch 以外の step の失敗（見出しに step 名） |
| `[CANCELLED]` | job が中断された（`timeout-minutes` 超過・手動 cancel） |

GitHub は `timeout-minutes` 超過を **cancel として扱う**。hang は日次 batch が最も踏みやすい
静かな失敗なので、notify step は `!cancelled()` ではなく `always()` で走らせ、`cancelled()` の値を
`--cancelled` で受けて `[CANCELLED]` を出し分ける。手動 cancel で 1 件多く届く代わりに、timeout を
取りこぼさない。

失敗 step の名指しは「batch 以外の step で success / skipped 以外の outcome を最初に持つもの」。
notify が outcome を受け取らない step（checkout / setup-uv / Playwright）の失敗は `pre-batch` と
書く。batch 自身が fatal / deferred failure に至った run は、`baibai_batch.jobs.daily` が `--notice-output` に
書いた JSON の最初の `failed_stage` を名指す。その JSON（as-of・skip の有無・失敗 stage・Review Setの出入り）は batch が
終端 path ごとに 1 回書く素の dict で、schema・validation・語彙表を持たない。読めなければ見出し行と
run URL だけになる。

notifier は repository dependency と Python 3.14 固有構文を使わず、checkout 直後の system `python3`
で import / CLI 実行できる（setup-python 前の smoke step が実 import で検査する）。message は
stdout にも出るので、run log でそのまま読める。

**通知の配送失敗は job を赤にしない**（`continue-on-error: true`）。workflowの失敗は公開の完全完了を示さず、途中までの反映がある場合は各stepの結果を確認する。配送失敗は
notify step の stderr に sanitized な理由（未設定・HTTPS 以外・Discord 以外の host・timeout・HTTP
status）で残り、webhook URL・response body は log に出ない。`#batch-runs` が静かな日は
`gh run list --workflow cloud-daily-batch.yml` で run 自体の有無と結論を見る。

### 欠測の検知（`cloud-batch-watchdog`）

run自身の通知は「runが起動したこと」を前提にする。GitHubは高負荷時にscheduled runを黙って落とし、cron直前に着地したmergeはその日のscheduleを差し替える。どちらの場合も成功通知も失敗通知も出ず、**沈黙**になる。人間は届かないメッセージの検知が最も苦手なので、沈黙のままにしない。

`.github/workflows/cloud-batch-watchdog.yml`が平日12:00 UTC（21:00 JST）に発火し、`cloud-daily-batch`のrun一覧を`gh api`で読む。直近20時間のrunから、batch cronを基準に決めた確認対象日を答えるrunだけを選び、その中に`conclusion=success`のcompleted runがあれば正常、無ければ同日のin-flight runの有無を判定する。別の日を答えるrunは、同じ20時間窓にあってもsuccess / in-flightの根拠にしない。どちらも無ければ同じ`#batch-runs`へ`[MISSING]`を送る。正常な日とin-flight時は何も送らない（2通目の`[OK]`はchannelを読み飛ばす習慣を作る）。正常に判定できたwatchdogは成功またはin-flightで警報を出さないが、無通知だけではwatchdog未実行・API失敗・配送失敗と区別できない。

- **20時間窓**は判定候補を取得する範囲であり、窓内のrunを日付に関係なく数える条件ではない。前日の07:43 UTC runが窓に入らない程度に短く、watchdog自身が数時間遅れて発火しても確認対象日の07:43 UTC runを取りこぼさない程度に長い。
- **確認対象日のまだ実行中のrunは欠測として数えない**。schedule queueが07:43 UTCのbatchを watchdog の発火時刻より後ろへ押し出すことがあるが、そのrunは完走すれば自分で結果を通知する（job timeoutに当たっても`[CANCELLED]`が出る）ので、watchdogが足せるものは無い。窓の中に確認対象日を答える`completed`でないrunが1本でもあれば`in_flight`として無送信にする。別日を答える実行中runは数えない。
- **営業日カレンダーは持たない**。非営業日は`cloud-daily-batch`自身がgreenのskip runとして完了するので、successとして数えられる。
- 手動の復旧dispatchも、run名が確認対象日を答える場合だけsuccess / in-flightとして数える。別日の復旧runは確認対象日の欠測を隠さない。
- run一覧が期待した形でなければ**警報を出さずにexit 1**する。parseの劣化が「run 0本」に落ちると、APIの形が変わるたびに誤報になるため。
- 過去日の判定は`gh workflow run cloud-batch-watchdog.yml -f check_date=YYYY-MM-DD`で再現する（その日の21:00 JSTに発火したwatchdogと同じ窓を評価する）。dispatch入力はcredentialを持たないvalidation stepでexact `YYYY-MM-DD`を検査してからstep env経由で渡す。
- watchdog自身もscheduleなので同時にskipされ得る。両workflowは同じ基盤に依存する。別基盤の監視は、実際の未検知と運用価値から必要性を判断する。

watchdog jobは何もinstallしない（checkoutとsystem `python3`だけ）。警報が必要なまさにその瞬間にtoolchainの都合で止まらないようにするためで、`tests/batch/test_cloud_batch_watchdog.py`がstep一覧で固定する。

定時runの完走率が要るときは、同じrun一覧をschedule eventだけで数える:

```bash
gh run list --workflow cloud-daily-batch --created ">=YYYY-MM-DD" --limit 200 --json event,conclusion \
  --jq '[.[] | select(.event=="schedule")] | "\([.[] | select(.conclusion=="success")] | length)/\(length) scheduled runs succeeded"'
```

### webhook rotation

`DISCORD_WEBHOOK_URL` は GitHub Actions の repository secret で、通知 step だけが読む（job env ・
CLI 引数・log には出ない）。secret の実値を Git・issue・log へ書かない。

1. Discord で `#batch-runs` の webhook を作り直す（または既存 webhook の token を再生成する）。
2. `gh secret set DISCORD_WEBHOOK_URL --repo <owner>/<repo>` で新しい URL を登録する。
3. 次の通常runで通知を確認する。即時確認が明示的に必要な場合だけ通知経路の受入を行い、rotationだけで過去日のscreeningを再生成しない。

旧 webhook は Discord 側で削除するまで有効。

### 実配送の確認

以下は通知経路を変更した際の受入ケースであり、文書修正や毎回のrotationですべて実行する手順ではない。

- 不正な `asof`（例: `2026-13-99`）の手動 run → validation step で止まり `[FAILED] … failed step: pre-batch` が1件届く。
- 有効な `asof` または次の通常 run → `[OK]` と 🆕 / 👋 の 2 行が1件届く。
- 非営業日が先に来た場合 → `[SKIPPED]` が届く。

各 message の as-of / 失敗 step 名 / run URL が正しいことを照合する。

**無通知は「配送失敗」だけを意味しない。** notify step 自体が動かない障害（checkout 失敗、
runner 未割当、job の強制終了）は通知経路の外側にある。`#batch-runs` が静かなときは、まず
`gh run list --workflow cloud-daily-batch.yml` で run 自体の有無と結論を見る。

<a id="edinet-research-facts"></a>

## EDINET Research factの初期化・日次差分

**前提**: schema 26の検証済みmarket storeと既存EDINET document inventoryを使う。schema 25からのcutoverはschema変更PRのone-shot手順で別fileへ行い、runtimeや日次batchに移行させない。API認証は既存`EDINET_API_KEY`、原典ZIP cacheは`.cache/screening/edinet/xbrl_zips/`である。

**production切替条件**: market storeの全writerとreaderを停止して旧storeをbackupし、schema変更PRのtemporary one-shot手順で25→26の別fileを作る。旧tableのschema・行数が不変、新tableのschemaが定義どおり、user_versionが26、integrity_checkがok、foreign_key_checkが0行であることを検証する。整合するcodeとstoreを同じ停止期間内に反映し、下記の初期抽出、固定releaseの発行、L1 queryとstore readerのsmoke確認が成功してから通常運用を再開する。cloud反映完了前にIssueをcloseしない。再開前の失敗ではcode/storeを揃えて戻し、再開後に新しい書込みがある場合は古いstoreで上書きせず、既存の復旧手順で新しい行を保持する。

初回はローカルのstaging storeへ明示して実行する。

```bash
uv run baibai-engine screening extract-edinet-facts \
  --asof YYYY-MM-DD --initialize --sqlite-path <STAGED_MARKET_SQLITE>
```

成功時は`source_coverage`の`edinet_research_facts / initialized`を記録する。失敗時は同じcommandで再開し、okの書類は再取得・再抽出しない。`--ticker`を繰り返した限定評価は全体のinitialized markerを作らない。有限parserの未対応は理由付きok、取得・ファイル破損はfailedに分ける。未知の形式を成功率だけのために補完しない。

**成功確認**: summaryのselected/reused/extracted/failedとmissing reasons、代表書類の原典照合、固定releaseのexport/query/hydrateを確認する。現行codeのmain反映、整合するL1発行、store-local coverageのクラウド反映を一組で行う。codeだけを先に日次運用へ入れない。

日次batchはReview Set発行後に`extract-edinet-facts --asof`を実行する。初期化前は明示的skip、初期化後は収録済みfamilyと新規年次・訂正を処理する。取得失敗は既存screening発行を止めず、他の後段処理を終えた後のexit codeへ反映する。raw cacheを残してretryできる。抽出契約が変わった場合はResearch fact専用revisionを更新し、既存EDINET metricsのbaselineを無効化しない。

storeのmergeでは同日でも別docIDの成否を混同しない。共有docIDはtargetのfactに対応する抽出claimを保持する。source側にしかないdocIDはtargetの抽出revisionを証明できないため、未確認のretry対象として取り込む。initialized markerは引き継げる。

**失敗時**: failedのdocIDを原典と照合し、認証・rate limit・破損cacheを原因別に解消して再実行する。factが残っていても、最新訂正の失敗を旧版で埋めない。利用側は[書類選択と欠測時の手順](../docs/reference/market-lake.md#edinet-research-query)に従う。

<a id="tradingview-expectations"></a>

## TradingView Analyst Expectations

日次batchは当日J-Quants masterの取得後、Lake公開前に`baibai-engine tradingview refresh`を呼ぶ。既存の`cloud-publish`内で直列実行し、失敗は非必須stepとして通知に残す。取得コマンドには暫定30分の上限を設け、終了しなければ30秒後に強制終了する。失敗後も既存Lakeの公開を続ける。上限は全Universeのライブ受入で所要時間を確認して再評価する。過去日付の手動batchを実行しても、TradingViewの現在値をその日付へ保存しない。

### 初期設定と認証の復旧

公式MCP SDKで対話認証する。秘密値をGit、L1、workflow artifact、チャットへ出さず、state fileはリポジトリ外に置く。認証URLを開く間はcommandを動かしたままにする。戻り先は`127.0.0.1:8765`であり、接続拒否の場合は待受終了・Windows/WSL間の接続を確認して再実行する。

```bash
uv run baibai-engine tradingview authorize \
  --state-file "$HOME/.cache/baibai-loop/tradingview/oauth.json"
gh secret set TRADINGVIEW_OAUTH_STATE \
  --repo koumatsumoto/baibai-loop \
  < "$HOME/.cache/baibai-loop/tradingview/oauth.json"
```

`TRADINGVIEW_SECRET_WRITER_TOKEN`には、このrepositoryだけのSecrets write権限を持つfine-grained PATを設定する。通常の`GITHUB_TOKEN`ではSecret更新を代行しない。更新されたOAuth stateは、データ取得より先に`TRADINGVIEW_OAUTH_STATE`へ保存する。書戻し失敗時は取得を止め、対話認証から復旧する。PATの期限切れもこの失敗として扱う。

runnerで確認するときは手動CIの`tradingview_oauth_smoke`を指定する。認証更新・Secret保存・2銘柄取得を行い、snapshotは書かない。前run終了後に新しいrunを開始して、更新stateの再利用を確認する。

```bash
gh workflow run ci.yml --ref main -f tradingview_oauth_smoke=true
```

ローカルの認証fileは初期投入用で、runnerが更新した後の最新版ではない。同じstateを複数processで更新したり、古いfileを再投入しない。対話認証による置換も、`cloud-publish`の実行がない時間に行う。

### 保存開始と受入

market schemaは28。既存storeのruntime自動移行は行わない。codeと整合するstoreを既存の再構築・転送手順で切り替えてから日次運用を開始する。初回snapshotがない間も既存の必須datasetの公開条件を維持し、TradingViewは任意datasetとして扱う。

collectorは当日の取引日・15:30 JST以降・当日masterを要求する。50 symbolsずつ、concurrency 1、既定15秒間隔で取得する。同日保存済みなら上書きせず終了する。429や全件missingはretry loopに入らず失敗し、次回はrun全体を再取得する。成功した日の`unresolved`は、その取得時点の事実として残す。

`response_bytes`はbatch応答payloadをUTF-8 JSONで表したbyte数で、HTTP圧縮後の通信量ではない。

初回の全市場1巡ではCLIのexpected universe、rows、unresolved、normal null数、response bytes、所要時間と、SQLite増分、既存Lake publishのobject/upload bytes、GC前後を分けて計測する。SQLite/L1 row parity、fixed releaseの過去・当日照会、cloud readbackまで確認し、Issue #1317へ記録する。少数銘柄のsmokeを全市場受入や容量実測の代わりにしない。

TradingViewの失敗ログは認証情報や応答本文を出さず、固定の`category`で示す。`auth`は再認証、`secret_persistence`はSecret書込権限、`provider_all_missing`・`provider_response`は提供元応答、`provider_rate_limit`・`provider_transport`・`timeout`は取得制限や通信、`time_guard`は当日引け後の条件、`storage`はschema・master・calendar、`internal`は未分類の実装エラーを確認する。再取得は当日の条件を満たす間だけ行い、過去日の穴を現在値で埋めない。
