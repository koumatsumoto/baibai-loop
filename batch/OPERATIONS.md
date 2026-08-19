# batch — production orchestration と cloud store 運用

Cloudflare 配信（issue #467 の設計）の compute と転送の入口。ここにある Python / shell
script は GitHub Actions とローカル運用から呼ぶ orchestration で、stable CLI ではない
（安定契約は `baibai-engine` / `baibai-web` 側にある）。read model の生成と日次 batch は
ローカル単体でも実行でき、転送 script だけが R2 を使う。

## Cloudflare / GitHub Actions 構成

R2 bucketとobject keyは次の固定契約を使う。どちらのbucketもPublic Development URLとcustom domainを無効にする。

| bucket | object | owner |
| --- | --- | --- |
| `baibai-stores` | `market.sqlite`（lakeが持たない4 tableだけ） | `cloud-daily-batch` + `cloud-history-backfill`（手動 dispatch。窓を名指しして履歴を遡る）+ ローカル`push-market`（cloud copyのmerge後だけupload） |
| `baibai-stores` | `lake/`（fetch由来15 datasetのcanonical L1） | `cloud-daily-batch`の`publish-lake` + ローカル`r2_transfer.sh publish-lake` |
| `baibai-stores` | `runs.sqlite` | `cloud-daily-batch` |
| `baibai-stores` | `macro.sqlite` | `cloud-daily-batch`（rolling窓）+ ローカル`push-macro`（全履歴。cloud copyのmerge後だけupload） |
| `baibai-stores` | `baibai.sqlite` | ローカル`publish.sh`（replica） |
| `baibai-serving` | `views/*.json` | GitHub Actions materialize |
| `baibai-serving` | `history/candidate-views/<asof>.json` | 日次batch、R2 lifecycleで31日後に削除 |
| `baibai-serving` | `history/longlists/<asof>.json` | 日次batch、R2 lifecycleで400日後に削除 |
| `baibai-serving` | `system/latest-run.json` | 日次batch、毎runで上書き（`views/`外なのでexportの再生成で消えない） |

R2 lifecycle rule は `history/candidate-views/` を31日、`history/longlists/` を400日で削除するよう**prefix指定で**追加する。後者は四半期の着手遅延計測へ1年以上の exact first-seen sourceを供給し、UI routeからは公開しない。**prefixを持たないbucket全体のruleを作らない** — `system/latest-run.json`が静かに失効し、`/system`のrunカードが恒久的に「記録なし」表示へ落ちる（消えたことに気づけない）。

資格情報はprincipalごとに分ける。

| principal | 設定 | scope |
| --- | --- | --- |
| GitHub Actions | variable `R2_ACCOUNT_ID`、secrets `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / provider 3本、公開 JPX 規制 URL 4本 | 必要なtransfer/provider stepだけ、stores + serving read-write |
| GitHub Actions（通知） | secret `DISCORD_WEBHOOK_URL` | `cloud-daily-batch` の通知 step と `cloud-batch-watchdog` の警報 step のみ（job env に出さない） |
| GitHub Actions（Worker deploy） | variable `R2_ACCOUNT_ID`、secret `CLOUDFLARE_API_TOKEN` | 対象accountの`Workers Scripts Write`、`web`のdeploy stepのみ |
| ローカル`.env` | `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | stores + serving read-write（bucket scopeにserving を含む。longlist history取得用） |
| Wrangler OAuth | `wrangler login` | bucket初期設定、Worker secretの手動設定 |
| Worker secret | `VIEW_PASSWORD` | Worker runtimeだけ |

R2 S3 endpointは`https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`からscriptが組み立てる。credential、password、endpointの実値をGit、issue、logへ書かない。

R2 API tokenのpermissionはtoken単位で、bucketごとにread/writeを分けられない。ローカルtokenはlonglist historyの取得のためservingをbucket scopeへ含むので、書き込み権も同時に持つ。servingの書き手を日次batch（`cloud-daily-batch`と`cloud-materialize`）だけに保つ境界は`r2_transfer.sh`側にあり、`upload-serving-views`・`publish-serving-tail`・`upload-run-summary`は`GITHUB_ACTIONS=true`以外では拒否する。`upload-serving-views`は`views/`を`--delete`付きで同期するため、部分的なローカルexportでの実行は本番viewの削除になる。

servingのpublishは2段である。`upload-serving-views`が`views/`を差し替え、`publish-serving-tail`が`history/`と`views/meta.json`を出す。`views/`はほぼ全部が`security--<ticker>.json`で、object数は掲載tickerの数だけ動く（数千件のorder）。`views/`は毎営業日書き換わる揮発物なので、日次batchではstore pushと同時に走らせる。`history/`は追記のみで消えず、`meta.json`はfreshnessの表明なので、両方がstore永続化の成功後にしか出ない。これによりstore pushが失敗したrunは、storeに存在しないrunの永続記録を残さない。

provider secretは`JQUANTS_API_KEY` / `ESTAT_APP_ID` / `EDINET_API_KEY`。加えて日次batchが毎回実行する`bootstrap-cache`では、JPX規制provider（`universe.required_jpx_flags`の4 source: 特別注意銘柄 / 整理銘柄 / 取引停止 / 上場廃止警告）が公開JPXページのURLを要求する。これらは非secretのため`cloud-daily-batch.yml`の`Run daily batch` step envにliteralで置く（`JPX_SPECIAL_CAUTION_INDEX_URL` / `JPX_REORGANIZATION_URL` / `JPX_TRADING_HALT_URL` / `JPX_DELISTING_WARNING_URL`。雛形は`.env.sample`）。未配線だとbootstrapのJPX stepがfail-fastし、machine stores / serving uploadはskippedになる。

workflow dispatchの日付はfull SHAへ固定したcheckoutの後、credentialを持たないvalidation stepでexact `YYYY-MM-DD`と順序を検証する。`run:`へ`inputs.*`を展開せず、step envからshell変数として渡す。R2・provider・Cloudflare・Discordのcredentialは、それぞれを使うcommandのstep envだけへ渡し、checkout・setup・dependency install・validationへは渡さない。全外部Actionのfull SHA pinとこれらの境界は`tools/quality/drift/check_workflow_trust.py`が検査する。

## 初回seedとWorker deploy

初回だけ、ローカル4 storeのconsistent SQLite snapshotをstores bucketへ送る。4 keyの
いずれかが既に存在する場合は、古いローカルcopyによる正本の巻き戻しを防ぐため何も
uploadせず停止する。

```bash
batch/scripts/seed.sh
```

production deployは`.github/workflows/web.yml`が所有する。workflowは`web/frontend/`・`web/edge/`の変更でだけ起き、PRはUI lint/build/testとWorker types/typecheck/test/dry-runまで、mainの`web/frontend/`または`web/edge/`変更とmainを明示したmanual dispatchは同じgateの後にdeployする。npm auditはこのjobに置かない。advisoryはrepositoryの外で公表されるので、publishの前に立てると無関係な緊急修正を止める（`node-audit.yml`がlockfile変更時に、`security.yml`が週次に問う）。deploy対象jobは共通のproduction concurrency groupで直列化し、deploy直前のremote `main`と`web/frontend/`・`web/edge/`のtreeが一致するrunだけを反映する。docs-only等の後続commitはdeployを失わせず、後続web変更があるrunだけをstaleとしてskipする。Cloudflare API tokenは対象accountだけに絞った`Workers Scripts Write`を使い、repository Actionsのvariable `R2_ACCOUNT_ID`とsecret `CLOUDFLARE_API_TOKEN`を設定する。tokenはdeploy stepだけへ渡す。初回deployはworkflowをmainから手動実行する。

```bash
gh workflow run web.yml --ref main
gh run list --workflow web.yml --limit 3
cd web/edge
# 初回 deploy は secret 未設定時に全 API を 401 にする。
npx wrangler secret put VIEW_PASSWORD
```

`VIEW_PASSWORD`はpassword manager等で生成した32文字のCSPRNG英数値を使い、値を引数やshell historyへ書かずpromptへ入力する。

workflowがdefault branchに存在する状態で初回materializeを実行する。

```bash
gh workflow run cloud-materialize.yml --ref main
gh run list --workflow cloud-materialize.yml --limit 3
```

materialize完了後、passwordを画面表示・shell引数化せず、全API routeを未認証・誤認証・正認証で検査する。`VERIFY_TICKER`はservingに存在する4文字tickerへ必要に応じて変更する。keyを取るroute（screening history / macro context / ticker）はservingに無いkeyでも検査し、正認証が200ではなく404へ解決することを確かめる。どのkeyがservingに存在するかへ依存せず、Workerが答える全routeのauth境界を検査するためである。passwordはprompt入力のみを受けるので、TTYの無い経路（agent やpipe経由の実行）ではrequestを1本も送らずexit 2で止まる。

```bash
web/edge/scripts/verify-deployment.sh
```

`*.workers.dev`のHTTP requestはWorkerが認証判定より前に308でHTTPSへredirectし、HTTPS responseはHSTSを返す。UI navigationは必ずこの経路を通り、hashed static assetだけをWorker invocationなしで配信する。

## 日常運用

### repository store layout の cutover と rollback

repository更新でstore layoutが変わるときは、codeだけを切り替えてruntimeを起動しない。
全local writer（engine CLI、Web backend、batch）を停止し、現在checkoutしている新codeから
次のone-time commandを実行する。既定はread-onlyのdry-runで、4 SQLiteの
`integrity_check` / `foreign_key_check` / schema / required tables、application DBの全table
row count / ledger append head / logical dump identity、calibration tree manifest、同一filesystem
を検査する。

```bash
uv run python -m baibai_batch.storage.store_layout_migration forward --dry-run
uv run python -m baibai_batch.storage.store_layout_migration forward --apply
```

`--apply`はapplication DBのSQLite snapshotを`stores/application/backups/`へ作り、その
logical identityを確認してから各resourceを同一filesystem内でLinuxのatomic no-replace rename
で移動する。rename後も
移動前と同じinode・schema・table・row/head/ledger identityであることを検査する。途中のmove
または検査が失敗した場合は、完了済みrenameを逆順に戻して非0で停止する。sourceとdestination
が両方存在する場合は片方を推測・merge・削除せず、書き込み前に停止する。

旧codeへ戻す必要があるときは、**旧codeをcheckoutまたは起動する前に**同じ新codeから逆方向を
適用する。

```bash
uv run python -m baibai_batch.storage.store_layout_migration rollback --dry-run
uv run python -m baibai_batch.storage.store_layout_migration rollback --apply
git checkout <verified-old-revision>
```

新codeへ復帰するときは新revisionをcheckoutした直後、どのruntimeも起動する前に`forward`を
再適用する。repository layout guardは旧layoutが残る間、通常runtimeを意図的に拒否する。
SQLite sidecarがある場合はwriter停止・checkpointが完了していないため移行しない。backupを
含む実行結果とdry-run結果を保持し、canonical application DBが片側に1つだけあることを確認する。
この逆方向操作はpathのrollbackであってschema downgradeではない。旧revisionが各storeの表示された
schema versionを読めることを、そのrevisionのschema定義とtestで確認してからcheckoutする。

### クラウド正本をローカルへ取得する

shortlist / research / macro-context の運用を始める前に、クラウド正本のmachine storeをローカルへ取得する。

```bash
batch/scripts/pull.sh
uv run baibai-engine screening verify-cache-coverage --asof YYYY-MM-DD
uv run baibai-engine screening ticker-profile --ticker TICKER
```

`verify-cache-coverage` が `required-field:<name>@<asof>` を返した場合は、同じas-ofで
`screening bootstrap-cache` を再実行する。bootstrapは表示された`resume_from`と
`remaining_ranges`に従い、欠損tickerの既知開示日だけをchunk補修する。既存の広い
`jquants_fin_summaries` coverageを`invalidate-coverage`で外すと正常な履歴まで再取得対象に
なるため、required-field補修には使わない。補修後は同じverify commandで
`market_cap_required_fields`と`valuation_required_fields`がminimum以上であることを確認する。

`pull.sh`はmarket/runs/macroの全downloadとSQLite `quick_check`が成功してから3 storeを置換し、`baibai.sqlite`には触れない。**batchが走っている間にpullすると、batch前のstoreとbatch後のstoreが混ざった断面がローカルへ載る**。`quick_check`は各storeを個別に見るのでこれを通し、screeningが読む価格・run・macro seriesの組み合わせが実在しない断面になる。避けるべき窓はcronの実値から導ける — 平日07:43 UTC（16:43 JST）に始まり、schedule遅延（実測median約2時間）とjob実行（`timeout-minutes: 60`）、既存の遅延余裕を含む**16:43〜21:30 JST**である。この窓を外すか、`gh run list --workflow cloud-daily-batch.yml --limit 1`で当日のrunが`completed`であることを確かめてからpullする。

### ローカルからクラウドを更新する

**ローカルで開発してschemaやデータを進めたら、cloud copyを取り込んで包含したものでcloudを更新する。** これが`market.sqlite` / `macro.sqlite`の標準手順で、`push-market` / `push-macro`が3段を1コマンドで行う。

1. **download** — cloud copyをstagingへ取る
2. **migrate** — `migrate_store.py`がそのcopyを現行schemaへ進める。storeを開くことがmigrationなので、走るのは日次batchが走らせるのと同じcodeである。進めるのはstagingのcopyだけで、R2のobjectはmerge後のuploadまで変わらない
3. **merge → upload** — cloud copyをローカルstoreへmergeし、cloud側の行が1行でも取り残されるなら停止する。全て取り込めた場合だけuploadする

```bash
batch/scripts/r2_transfer.sh publish-lake          # market storeを進めた場合は先にこれ
batch/scripts/r2_transfer.sh push-market
batch/scripts/r2_transfer.sh push-macro
gh workflow run cloud-materialize.yml --ref main   # 表示へ反映する場合
```

**market storeはlakeへpublishしてからpushする。** fetch由来15 tableのcanonicalはR2のL1 releaseに
あり、`push-market`が送るのはそれを空にした残り4 tableだけである。`publish-lake`を飛ばすと、
dehydrateが「releaseが持つ行数と合わない」で停止する。ローカルが cloud より遅れている場合は先に
`hydrate-market`で現行releaseへ揃える — publishはstoreをhydrateしたreleaseの上にだけ積めるので、
別のwriterが進めたlakeの上へ古いstoreをpublishすることはできない。

**cloud copyがcodeより古いのは正常な過渡状態である。** cloud copyのschemaは日次batchがstoreを開いたときに上がるので、schema bumpから次の実行までラグが残る。cronは平日だけなので、週末にschemaを上げると月曜まで続く。この間もmigrate段があるためpushは通り、日次batchを起こす必要はない。

**publishするcodeは、cloudが動かすcodeでなければならない。** ローカルのschema versionがmainより先にあると、cloudが知らないversionのstoreを置くことになり、次の日次batchが`open_connection`のbaseline検査で停止する（`supported range`を挙げてfail-fastし、Discordに`[FAILED]`が出る。1世代の`.bak`も残る）。schemaを上げるcodeは**mainへ入れてからpushする**。

**pull側にschema検査を置いてはならない。** 検査を置くと、ラグを解消する経路（pull → open → push）がstep 1で落ちて自己修復が止まり、storeを1行も書かない`cloud-materialize`まで道連れになる。schemaがずれている間に妥当域外の値が入る心配も要らない — 書き込み経路は全て`open_connection`を通り、そこで必ずmigrationが先に走る。

`runs.sqlite`はこの手順を持たない。cloudが唯一のwriterなので、ローカルからのuploadは巻き戻しにしかならない。

### application DB を反映する

application DBのjudgment更新をクラウド表示へ反映する。

```bash
batch/scripts/publish.sh
```

このscriptは`baibai.sqlite`のconsistent snapshotだけをstoresへ送り、`cloud-materialize`をdispatchする。servingへの直接writeは行わない。

application DBのschemaはローカルのCLI実行でmigrateされ、クラウドはこのstoreをread-onlyで読む。schema migrationを含むcodeがmainへ入ったら、次の`cloud-daily-batch`より前に`publish.sh`を実行する。exportはstoreのschemaがcodeと一致しない間viewを1件も書かずexit 1で停止するため、未publishのままではscreening結果も含めて何も更新されない。

### application DB を復元する

application DBは判断とledgerの正本で、何も再生成しない。復元点は2系統ある。

**ローカルのcheckpoint**: `initialize_database`は未適用のmigrationを見つけると、最初の文を実行する前に`stores/application/backups/baibai-<JST stamp>.sqlite`を書く（`sqlite3.Connection.backup`によるWAL込みのsnapshot、`integrity_check`と`foreign_key_check`つき）。直近10世代を残し、それを超えた分はcheckpoint作成に成功した後だけ削除する。手動で取るときは`uv run baibai-engine db backup`。

```bash
ls -t stores/application/backups/                      # 世代を新しい順に見る
target=stores/application/backups/baibai-<stamp>.sqlite
sqlite3 "$target" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
sqlite3 "$target" 'PRAGMA user_version;'               # 戻す先のschema版
mv stores/application/baibai.sqlite stores/application/baibai-before-restore.sqlite
cp "$target" stores/application/baibai.sqlite
uv run baibai-engine position ledger | head -20        # ledger headを確認
```

戻したstoreはcheckpoint時点のschema版なので、次のCLI実行が未適用のmigrationを（新しいcheckpointを取ってから）適用する。欠陥のあるmigrationがまだmainに居るなら、先に修正を入れてから実行する。

**R2の世代**: ローカルのcheckpointが失われた場合に使う。`pull-app`はローカルにfileがあれば止まるので、この経路はstagingへ直接取得する。

```bash
aws s3api list-objects-v2 --bucket baibai-stores --prefix baibai.sqlite.bak- \
  --query 'Contents[].[Key,LastModified]' --output text --endpoint-url "$endpoint"
aws s3api get-object --bucket baibai-stores --key baibai.sqlite.bak-YYYYMMDD \
  --endpoint-url "$endpoint" /tmp/baibai-restore.sqlite
sqlite3 /tmp/baibai-restore.sqlite 'PRAGMA integrity_check; PRAGMA user_version;'
```

**cloud copyはローカルより古い可能性がある。** publish済みで未pushの窓ではローカルが進んでいるので、R2の世代へ戻すのはローカル側が失われたときだけにする。戻した後は`user_version`とledger headを確認し、次の`publish.sh`まで判断を再開しない。

### 履歴を深くする

indicator storeの履歴を深くしてクラウドへ載せる。日次batchはfrequency別のrolling窓しか引き直さないため、cloud正本の履歴は前へ伸びるだけで過去へ伸びない。系列を追加した後や窓を超える取得断の後は、ローカルで全履歴を取得してから`push-macro`する。

```bash
uv run baibai-engine macro refresh <series_id> ... --all-history --end YYYY-MM-DD
uv run baibai-engine macro reading --asof YYYY-MM-DD   # 履歴不足・異常値を確認
# series を追加した場合は、ここで registry を main へ入れてから push する
batch/scripts/r2_transfer.sh push-macro
```

market storeの履歴を深くしてクラウドへ載せる。日次batchは前へしか伸ばさないので、過去へ伸ばす経路は2つある。**ローカルに既にその履歴があるなら取り直さない** — providerを一度も呼ばずに数分で載る。ローカルにも無い履歴だけ`cloud-history-backfill`をdispatchして取る。

ローカルから載せる手順は、触ったtableがlake所有かどうかで分かれる。`_push_keys`はuploadする複製を`dehydrate_market_snapshot`に通し、**releaseが持たない行をlake所有tableに持つstoreのuploadを拒否する**ので、fetch由来15 tableを増やした場合は`push-market`だけでは止まる。

```bash
# (a) lake所有の15 tableを増やした場合: hydrate済みstoreで変更し、先にreleaseへ載せる
batch/scripts/r2_transfer.sh publish-lake
batch/scripts/r2_transfer.sh push-market

# (b) lakeが持たない4 table（source_coverageとoperator-derived 3 table）だけを変えた場合
batch/scripts/r2_transfer.sh push-market

# (c) ローカルにも無い履歴を取る場合
gh workflow run cloud-history-backfill.yml --ref main \
  -f start=YYYY-MM-DD -f end=YYYY-MM-DD
```

`cloud-history-backfill`は財務サマリーが律速で、実測は3.4年で2時間32分（うち財務2時間05分）である。job上限は5時間なので、大量欠損は3〜4年ずつに分けてdispatchする。coverageのmergeが繋ぐので分割しても結果は同じになる。source failureまでにcommitされたchunkは、store SHA-256が変わった場合だけ`quick_check`と`push-market`を通してR2へ保存し、workflow自体は元の非0で失敗する。変更が無いfailureはuploadをskipする。3つのcloud writerは`cloud-publish`の`queue: max`を共有し、1件だけを実行しながらpending runをFIFOで保持する。

### merge が検査するもの

どちらのmergeも、終わった時点でsource側だけに残る行が1行でもあれば停止する。日次batchが取得済みでローカルに無い行を、uploadで失わないための不変条件である。以下はstoreごとに違う部分。

`push-market`のmergeは`merge_market_store.py`である。**対象はlakeが持たない4 tableだけ**で、fetch由来15 tableのcloud/local突き合わせはreleaseが引き取っている——`publish-lake`はstoreをhydrateしたreleaseをlakeが既に離れていれば拒否し、dehydrateはreleaseが持たない行を持つstoreのuploadを拒否する。

`source_coverage`は取得範囲の帳簿で、両側が書くので主キー`(source, coverage_key)`で`INSERT OR IGNORE`し、同じ主キーを両側が持つ場合はpayloadの一致を検証する。**比較しないのは、出所が何を言ったかではなくstoreがいつ読んだかを記録する`fetched_at_utc`だけ**——2つのstoreが同じ範囲を別の時刻に読めばそこは必ず食い違うので、比較すれば全てのmergeを拒否する。

`record_count`は行が在る場所でしか証明できない。R2が運ぶdehydrate済みのsourceでは証明せずclaimとして受け取り、targetのclaimは**実rowへ引き上げるだけで、決して引き下げない**。引き下げは、このstoreが満たされていないreleaseを記述しているclaimを、より小さい数値で置き換える操作である。次のhydrateが行を戻してもledgerは小さいままで、`verify-cache-coverage`が以後の全screening runを止める一方、再取得は永久に計画されない——`covered_intervals`が窓を落とすのはcountが0のときだけだからである。引き上げられないclaimはmerge後の検査で停止し、「lakeがserveしているreleaseからhydrateし直せ」と出る。**空のtargetもここで止まる**——「取得済み」と言うclaimを黙って0へ書き換える代わりに拒否する。

**ledgerは追記専用ではない。** 取得に失敗すると、その範囲は重なる`ok`窓から切り出され、残余が新しいkeyで書き直される（穴が失敗した場所に見えるようにするため）。keyによるunionは、後の取得が撤回した広い窓を古いcopyから復活させ得るので、mergeはそれを修復せず拒否する——同じsourceで`ok`窓が`failed` / `partial`窓に重なるledgerは、どのfetcherも書かない形である。

訂正可能な`jquants_short_sale_reports`のcoverageはdisclosure dateごとに1つのclaimを選ぶ。`ok`が`partial`/`failed`に勝ち、同種なら`fetched_at_utc`が新しい方が勝つ。`record_count`はreleaseが満たしたtargetの実rowから読み直す。行が1つも claimされないdateがあれば停止するが、**その検査はclaim選択の後**に置く——行はhydrateで、claimはmergeで届くので、cloudが取得して publishした日はtargetのtableに1段先に現れる。

`jpx_delistings` / `tender_offer_exit_values` / `tse_capital_policy_snapshots`はoperatorが導出したもので、targetを丸ごと残しsourceから1行も取り込まない。key mergeすると、後の導出が撤回した行が古いcopyから復活し、訂正した値は「2つのstoreが食い違う」と読まれてpublish全体を止める。

mergeの対象tableは`merge_market_store.py`の`FACT_KEYS` / `DERIVED_KEYS`に列挙し、**それとlake datasetの合併がstoreのtable一覧と一致すること**をtestが確かめる。新しいtableはlakeかmergeのどちらかに分類しないと落ちる。

machine storeの全writerはdownload時のR2 ETagを保持し、backupは同じsource ETag、最終`PutObject`は同じdestination ETagを条件にする。日次batchは`pull-machine`が3 storeのgenerationを記録し、`push-machine`が全keyを事前照合してから各keyを条件付きで発行する。merge中またはupload直前に別writerがobjectを更新した場合はprecondition failureで停止し、最新cloud copyからやり直す。これにより、GitHub Actions外の手動pushと日次batchのどちらが後着しても、先に発行された更新を巻き戻さない。途中のkeyでnetwork / precondition failureになった場合はserving tailを発行せず、次回runが各keyの現行generationをpullして再構成する。

`push-macro`のmergeは`merge_indicator_store.py`である。対象は事実を積み上げるtable（`observations` / `provider_runs`）だけで、主キーで`INSERT OR IGNORE`する。同じ主キーを両側が持つ場合は全payloadの一致をmerge前後に検証し、値・単位・source等が異なれば片方を正本と推測せずtransaction全体を停止する。source / target はschema version・列構成に加えて`schema.sql`由来の全persistent triggerとregistry state contractをcanonical定義へ完全一致させる。targetが保持する全series metadataは両端が有限なplausible rangeを持つことを前提とし、source / target observationをtransaction先頭でtargetのunitとrangeに照合する。いずれかの契約違反があればtargetを変更せず停止する。`series` / `aliases`はsourceから取り込まない。通常のopenは登録外seriesのfacts・metadata・aliasesを保持し、明示的な`macro refresh`だけが現行registryに無いseriesをpruneするため、古いbranchのread後もtargetに残る新系列へcloud factsをmergeできる。source の registry generation が target より新しい場合と、同世代なのに `source.series` membership がtargetから欠ける場合は、facts未取得のseriesでもmergeを拒否する。target が source より新しい世代でmetadataが無いseriesのrowだけを意図した退役としてskip件数に含める。`market.sqlite` / `runs.sqlite`は`push-macro`が触らない。

### 日次 workflow を手動実行する

`asof`省略時は当日JSTをmarket calendarで判定し、非営業日は成功扱いでskipする。過去日を指定すると営業日gateをskipする。

```bash
gh workflow run cloud-daily-batch.yml --ref main
gh workflow run cloud-daily-batch.yml --ref main -f asof=YYYY-MM-DD
gh run list --workflow cloud-daily-batch.yml --limit 10
```

通常cronは平日07:43 UTC（16:43 JST）。同日必須なのは`asof = today`が依存する株価日足だけで、[J-Quants APIの公式更新時刻](https://jpx-jquants.com/ja/spec/data-update)は16:30頃のため13分の余裕を置く。JPX規制ページはevent駆動のstatus pageでcoverage gateが7営業日まで許容し、信用残は週次なので、いずれも夕方の更新を待つ必要がない（この実行より後に出た指定は翌営業日の実行が拾う）。分を半端にしているのは意図的で、GitHubがscheduleを:00 / :15 / :30 / :45へ集中させるため、その境界に置くとqueue待ちの後ろに並ぶ。schedule遅延自体は許容する（実測でmedian約2時間）。遅延ではなく**欠測**は`cloud-batch-watchdog`がpushで検知し、UIのas-ofとworkflow履歴は裏取りのpull経路として残る。16:43時点で株価日足が未更新ならcoverage gateがpublish前に停止し、復旧は現行mainから手動dispatchする。

daily batchはcoverageが完全でも`bootstrap-cache`を実行する。財務サマリーの直近7日を再取得するため、同日の先行runより後にJ-Quantsへ反映された開示は後続runで取り込まれる。bootstrap後はcoverageを再検証してからscreeningへ進む。

`daily_batch.py`のexit 3はfresh screening exportを持つため、workflowはstores/serving uploadまで完了させてからjobを失敗にする。exit 1は新しいpublish可能runがないためuploadしない。非営業日skipはexportがないため既存servingを変更しない。

## Password rotation

```bash
cd web/edge
npx wrangler secret put VIEW_PASSWORD
```

新しい32文字CSPRNG値をpromptへ入力する。次のAPI 401でbrowserの旧値がlocalStorageから削除され、password入力画面へ戻る。Workerの再deploy、R2変更、application data更新は不要。

## R2 transferの安全境界

- upload前にPython `sqlite3.backup`でsnapshotを作り、WAL未checkpoint行を含めて`quick_check`する。
- 複数storeのpushは全snapshotの作成・検査を終えてからuploadを始める。3 store一括のmachine store pushはGitHub Actionsからだけ許可する（`runs.sqlite`はcloudが唯一のwriterで、無条件uploadが古いローカルcopyで巻き戻すため）。`macro.sqlite` / `market.sqlite`はローカルからも`push-macro` / `push-market`でuploadできるが、いずれもcloud copyのmergeを通した後だけで、mergeがcloud側の行の取り残しを検出したら停止する。
- pushは上書き対象のremote objectを`<key>.bak`へ1世代copyしてからuploadする（R2内のserver-side copy。存在判定は`s3api head-object`の完全一致で、`.bak`自身をkey本体と誤認しない）。storeは原則sourceから再構築できるが、PMI履歴のようにpublisherが古いURLを落とすと再取得できない部分があるため、破損・誤pruneしたsnapshotによる上書きから前回分へ戻せる状態を保つ。復元は`.bak`を本keyへcopyし直す（`aws s3api copy-object`を使う。`aws s3 cp`のS3→S3経路はobject sizeで実装が切り替わり、multipart copyはGetObjectTagging、single-part copyは`x-amz-tagging-directive`を要求してどちらもR2が実装しない。CopyObjectはdirectiveを送らず5GBまでのobjectで通る）。R2はcopyが終わるまで応答を返さず、その待ちはobject sizeに比例してGB級のstoreではaws CLI既定のread timeout 60秒に収まらないため、pushの世代保存も手動復元も`--cli-read-timeout`を既定より広げて呼ぶ。**`market.sqlite`が運ぶのはlakeが持たない4 tableだけである。** runner実測は`market.sqlite` 4,972,544 bytes（snapshot 46秒・backup 114秒・upload 2秒）、`runs.sqlite` 52,838,400 bytes（1秒・7秒・3秒）、`macro.sqlite` 256,184,320 bytes（2秒・17秒・14秒）である。market storeのsnapshotが46秒なのは、空にする前のfull storeを一度copyするためである。この`.bak` 114秒は置き換えられる側が1.88GBだった初回の値で、以降は5MBのcopyになる。`push-machine`はkeyごとに`store push: key=... bytes=... snapshot=...s backup=...s upload=...s`を出すので、storeが伸びたときの内訳はrunのlogで見る。
- machine store の`.bak`は1世代のみで、次のpushで置き換わる。日次batchが毎営業日pushするため、実質の巻き戻し猶予は約24時間である。`baibai.sqlite`だけは`baibai.sqlite.bak-YYYYMMDD`（JST）で日ごとに1世代を残し、直近14世代を超えた分をpush成功後に削除する。machine storeはsourceから作り直せて毎営業日書き換わるのに対し、application storeのjudgmentとledgerは何も再生成しないためである。prune は`baibai.sqlite.bak-`配下をlistし、`baibai.sqlite.bak-YYYYMMDD`に一致するkeyだけを完全一致で削除する（prefix削除はしない）。registry編集後は日次workflowの`registry-prune-pending` / `registry-prune`行（transaction ID・series ID・observation/provider-run削除件数）を当日中に確認する。pending に対応する committed 行が無い実行や意図しないpruneを検出したら、次のpushが`.bak`を置き換える前に状態を確認・復元する。
- 初回seedは既存のstore keyを1件でも検出したら停止し、再seedによるクラウド正本の上書きを許可しない。
- pullは固定4 key以外を受け付けず、全downloadと`quick_check`完了後に置換する。
- application store (`baibai.sqlite`) のpullは`pull-app`だけが行い、bulk pullは触らない。この storeの正本はローカルで、判断はローカルでpublishしてから`push-app`でcloudへ出すため、publish済みで未pushの窓ではローカルがcloudより進んでいる。cloud copyでの置換は再生成できないjudgmentを消すので、`pull-app`はローカルにfileがあれば止める。CIはcheckout直後で`stores/application/`が空なので素通りする。ローカルで意図して置き換えるときは、既存fileを自分で退避してから実行する。
- servingの`views/`は`aws s3 sync --delete`で完全像に合わせる。historyは追記だけで削除しない。
- `views/meta.json`は他のviewとhistoryが全て成功した後に最後にuploadする。
- bucket名は`R2_STORES_BUCKET` / `R2_SERVING_BUCKET`で明示的にoverrideできるが、通常は固定defaultを使う。

### migrationを戻すとき

**store全体を古いsnapshotへ戻す経路は持たない。** storeは毎営業日伸びるので、過去のschemaで凍結したcopyへ戻すと、そのcopy以降に積んだ事実を全て捨てることになる。migrationが壊したのはschemaであってその日以前の事実ではないので、交換の割に合わない。

migrationに欠陥が見つかったときは、**修正migrationを前へ足す**。`BASELINE_VERSION..SQLITE_SCHEMA_VERSION`が受理するのは前進だけで、`open_connection`はこの範囲を外れたstoreをfail-fastで拒否する。誤変換した列を作り直す場合は`rebuild_table`を使う新しいmigrationを足し、日次batchがstoreを開いた時点で適用される。

直前のpushが壊れた場合の巻き戻しは`<key>.bak`の1世代で、これは上の「R2 transferの安全境界」が扱う範囲である。

## export_read_models.py — read model の材料化

`baibai_web.readmodel` builders を共用して、UI が読む全 view を serving 配置どおりの
JSON に書き出す。Worker には業務ロジックを置かない設計の実体。

```bash
uv run python -m baibai_web.materialize --output-dir <dir> [--batch daily|manual] [--repo-root <path>]
```

出力（`<dir>` 配下）:

- `views/dashboard.json` / `views/screening_latest.json` / `views/operations.json`
- `views/screening_latest.json` は、有効な `reports/published/er-level-calibration-latest.yaml` と表示対象 operative run の method identity が一致する場合だけ、E[r] historical quintile と独立した8.5%以上帯の実現分布文脈を含む。run identity 不明、欠損・不正・期限切れでは field を `null` にして既存 screening 表を維持する
- `views/daily-delta.json`（前営業日の機械実行との差分。Dashboard の差分区画が読む）
- `views/system.json`（4 store の as-of / 行数 / サイズと、取得が失敗したままの系列。`/system` が読む。後述の[システム状態の配信](#システム状態の配信--viewssystemjson-と-systemlatest-runjson)）
- `views/macro--<period>-<granularity>.json`（1y|5y|10y|max × daily|weekly|monthly|yearly）
- `views/macro-reading.json`（全登録系列の機械読み値。indicator store か reading rules が
  無ければ警告のうえ書かず、Macro タブは該当パネルだけを非表示にする）
- `views/macro-context--<context_id>.json`（published macro context の本文。Macro report 画面が読む）
- `views/assessment--<assessment_id>.json`（published bargain assessment の本文。Assessment 画面が読む）
- `views/security--<ticker>.json`（保有 + 最新 run 掲載 + shortlist の ticker）
- `views/meta.json`（生成時刻・実データ更新時刻・store 別 as-of・batch 種別。UI の鮮度表示と同じ契約）
- `history/candidate-views/<asof>.json`（run とCandidates全件を型付きUI read modelへ変換した履歴。31 日で削除）
- `history/longlists/<asof>.json`（latest runに束縛されたmachine selectionの `ticker` / `rank` / `er_annual`。selection欠損日と空longlistも空recordとして発行し、400日で削除）

この一覧と Worker の route 表の対応は `tests/web/test_cloud_export.py` が守る。Worker が写像する view を exporter が書かないと、その route は本番で恒久的に 404 になる。

書き出しの前に application store の `user_version` が code の schema version と一致することを確認し、不一致なら view を 1 件も作らず exit 1 で停止する（読み取り経路は read-only で migrate しないため、不一致は build の途中で素の SQL error になる）。store が無い root は judgment 空の正常状態として export する。

`views/` は毎回 export の完全な像に置換される（実行のたびに一度削除して作り直すので、対象から外れた古い view は残らない）。`history/` は追記のみで、この script は削除を行わない。上記の31日 / 400日削除は serving store（R2 lifecycle）側の保持契約であり、script の挙動ではない。

Workerは認証後の`/api/screening/history`でCandidates履歴の日付一覧を返し、`/api/screening/history/YYYY-MM-DD`だけを`history/candidate-views/`へ写像する。任意key、旧形式の`history/candidates/`、store bucketは公開しない。

views の JSON は `baibai-web` の対応 API response と同形（pydantic `model_dump_json`）。`meta.json` は全 view / history の書き込み成功後に最後に書くので、途中失敗した出力 dir が新鮮さを主張する事態を避ける。

四半期の着手遅延計測では、既存targetを上書きしない download と専用 source を使う。

```bash
batch/scripts/r2_transfer.sh pull-longlist-history /tmp/baibai-longlist-history
.venv/bin/python -m tools.experiments.measure_daily_delta_effect \
  --longlist-history-dir /tmp/baibai-longlist-history \
  --as-of YYYY-MM-DD
```

同じ dir を `screening select --longlist-history-dir` へ渡すと、run store の retention で前 as-of が消えた日でも差分診断の前回側を復元できる。run store に前 as-of が残っていればそちらが優先され、母数は `selection.diagnostics.previous_overlap.previous_candidates_source` に出る。

## daily_batch.py — 日次機械工程の 1 コマンド実行

営業日判定 → screening cache coverageの事前検証 → bootstrap（財務サマリーの直近7日を再取得）→ EDINET incremental extraction →
coverage再検証 → run → select →
macro series refresh → export → run store prune を順に実行する。
全 step は public CLI の subprocess で、step ごとにコマンドライン・exit code・所要秒を
stdout へ出す（scheduled workflow のログをそのまま読む前提）。

```bash
# 通常（当日 JST。market calendar で非営業日なら exit 0 で skip）
uv run python -m baibai_batch.jobs.daily --output-dir <dir>

# 手動再実行・過去日（営業日 gate を skip）
uv run python -m baibai_batch.jobs.daily --asof YYYY-MM-DD --output-dir <dir>

# structured summary を書き出す（workflow の Discord 通知が読む）
uv run python -m baibai_batch.jobs.daily --output-dir <dir> --summary-output <summary.json>
```

`--summary-output` を指定すると、success / skip / deferred / fatal の全終端パスで
`BatchExecutionSummary` JSON を atomic write する。screening / macro / serving-export / prune の
論理結果ごとに datasets・所要時間・metrics・typed errors を確定し、不正な `--asof` も
`invalid_asof` error を持つ fatal summary になる。stdout/stderr の既存診断はそのまま維持し、
summary には redaction 済みの typed errors だけを渡す。

終了コード:

| exit | 意味 |
| --- | --- |
| 0 | 完走。または非営業日（当日 gate で `skip` を出して即終了） |
| 1 | 致命的失敗で停止（screening chain・営業日判定・calendar 不備。publish に至らない） |
| 3 | export まで publish 済みだが、繰延べステップ（macro refresh / prune）が失敗 |

失敗ポリシー:

- screening 系（coverage / run / select）の失敗は致命的で即停止する（publish できる新しい
  run が無いため exit 1）。ただし `screening run` の exit 2 は品質警告つきの published run で
  あり、警告理由を表示して続行する。`verify-cache-coverage` の exit 1 は cache 不足マーカーが
  ある場合だけ bootstrap へ進み、マーカー無しの exit 1（rules 破損等の crash）は即停止する
- EDINET document state は日中にも変わり得るため、初回 coverage が complete でも
  `extract-edinet-metrics` を毎回実行する。変更のない metric row は baseline から再利用し、
  extraction 後の coverage と quarantine counters を current state に揃える
- macro series refresh の失敗は繰延べる: export まで完走して screening 結果は publish し、
  最後に exit 3 で終了する（scheduled workflow の失敗通知は発火し、鮮度は meta の
  `macro_asof` に現れる）。繰延べた失敗の詳細は発生時点で stderr にも出す
- `select` の前回 run 比較は、runs store の「target より前の最大 as-of の最新 revision」を
  この script が決定論的に解決して `--previous-run-revision-id` で渡す（同一日の再実行が
  複数 revision を作っても停止しない）
- 営業日判定は market store の `jquants_market_calendar` が情報源。対象日をカバーして
  いない場合は黙って続行せず明示エラーで停止する

## notify_discord.py — 日次 batch 結果の Discord 通知

`cloud-daily-batch` は run ごとに終端結果を Discord チャンネル `#batch-runs` へ1件通知する。
共通 Logger ではなく workflow 単位の通知 adapter で、チャンネルは code が選ばず repository
secret `DISCORD_WEBHOOK_URL` が指す webhook で固定する。workflow 末尾の単一 step
（`if: always()`）が、cancel を含むあらゆる終端状態で1回だけ行う。

通知する結果は5種。

| label | overall outcome | 意味 | publish state |
| --- | --- | --- | --- |
| `[OK]` | succeeded | batch exit 0、local export あり、両 upload 成功 | published |
| `[SKIPPED]` | skipped_non_business_day | 非営業日 gate で skip（export なし） | not_generated |
| `[DEGRADED]` | published_with_deferred_failure | batch exit 3。screening は publish 済み、繰延べ step（macro / prune）が失敗 | published |
| `[FAILED]` | failed | 致命的失敗、batch 以外 step の失敗、summary 欠落・invalid・矛盾、upload 失敗 | upload step の status に従う |
| `[CANCELLED]` | cancelled | job が中断された（`timeout-minutes` 超過・手動 cancel） | 中断時点の観測値 |

GitHub は `timeout-minutes` 超過を **cancel として扱う**。hang は日次 batch が最も踏みやすい
静かな失敗なので、notify step は `!cancelled()` ではなく `always()` で走らせ、`cancelled()` の値を
`--cancelled` で受けて `[CANCELLED]` を出し分ける。手動 cancel で 1 件多く届く代わりに、timeout を
取りこぼさない。

判定の優先順は「batch 以外の step 失敗 → upload 失敗（`upload_failed`）→ batch summary の
outcome」。upload 失敗は batch が成功していても `[FAILED]` を優先する。setup/sync/pull/smoke の失敗は
batch 未到達（`not_started`）の `[FAILED]`、batch 実行後の summary 欠落・invalid は
`unavailable` の `[FAILED]`。checkout / setup-uv / Playwright のように notify が step outcome を
受け取らない step の失敗は、成功している `setup` を名指ししないよう stage `pre-batch` として報告する。summary が succeeded / degraded を主張しても observable な publish 状態
（local export / 両 upload 成功）が一致しない「矛盾」は、`summary_conflict` error を付けて
`[FAILED]` になる（静かな publish 劣化を `[OK]` として隠蔽しない）。exit 3 は publish 済みの
`[DEGRADED]`、upload 失敗は `[FAILED]`（`upload_failed`）という契約を README と test で固定する。

message には workflow 名・repository・trigger・run attempt・overall outcome・as-of・総所要時間・
batch ごとの status / datasets / metrics・publish state・GitHub Actions run URL を含む。
`serving-export` の `delta_entered_tickers` / `delta_exited_tickers`（それぞれ E[r] 降順・
最大5件・`<ticker> <社名> E[r]±X.X%`）は専用行 `🆕 新規 longlist 入り:` / `👋 longlist 退出:`
として出す。急落当日の候補と、pool から落ちた銘柄を通知だけで拾えるようにするための行である。
**この2行は batch summary が読めた run では常に出す** — 0 件の日は `なし`、`delta_measured`
が false の日は `計測なし（<理由>）` と書く。行が無いことは「0 件」「計測不能」「通知経路の
異常」の3つを同時に意味してしまい、読み手が区別できない。error は
failed を degraded より先に表示し、4件以上は上位3件 + 残件数へ折りたたむ。error message は固定
code / stage / impact と検証済み scalar だけから作り、subprocess の stderr・例外本文・provider
response body は載せない（1行400文字以内、全体2000文字以内）。

`daily_batch.py` が `--summary-output` に書いた `BatchExecutionSummary` を読み、GitHub metadata と
step outcome を合成して `WorkflowRunSummary` を確定し、配送結果（delivered / failed）も記録して
atomic write してから、同じ model を Discord へ render する。notifier は repository dependency と
Python 3.14 固有構文を使わず、checkout 直後の system `python3` で import / CLI 実行できる
（setup-python 前の smoke step が `py_compile` と実 import の両方で検査する）。

通知の配送に失敗した run は、data 処理が成功していても job を失敗にする。`#batch-runs` に届かない
正常 run は配送失敗を意味するので、GitHub Actions の run log（通知 step の stderr）で data 処理の
成功と配送の失敗を区別して確認する。webhook URL・response body は log に出ない。未設定・HTTPS 以外・
Discord 以外の host・timeout・HTTP error はすべて sanitized な理由で non-zero 終了する。

### 欠測の検知（`cloud-batch-watchdog`）

run自身の通知は「runが起動したこと」を前提にする。GitHubは高負荷時にscheduled runを黙って落とし、cron直前に着地したmergeはその日のscheduleを差し替える。どちらの場合も成功通知も失敗通知も出ず、**沈黙**になる。人間は届かないメッセージの検知が最も苦手なので、沈黙のままにしない。

`.github/workflows/cloud-batch-watchdog.yml`が平日12:00 UTC（21:00 JST）に発火し、`cloud-daily-batch`のrun一覧を`gh api`で読み、直近20時間に**conclusion=successのcompleted runが1本も無ければ**同じ`#batch-runs`へ`[MISSING]`を送る。正常な日は何も送らない（2通目の`[OK]`はchannelを読み飛ばす習慣を作る）。したがって`#batch-runs`の沈黙は「当日のbatchが正常だった」を意味する。

- **20時間窓**の両端はGitHubのschedule遅延（median約2時間）で決まる。前日の07:43 UTC runが窓に入らない程度に短く（前日の成功で当日の欠測を隠さない）、watchdog自身が数時間遅れて発火しても当日の07:43 UTC runを取りこぼさない程度に長い。
- **まだ実行中のrunは欠測として数えない**。schedule queueが07:43 UTCのbatchを watchdog の発火時刻より後ろへ押し出すことがあるが、そのrunは完走すれば自分で結果を通知する（job timeoutに当たっても`[CANCELLED]`が出る）ので、watchdogが足せるものは無い。窓の中に`completed`でないrunが1本でもあれば`in_flight`として無送信にする。
- **営業日カレンダーは持たない**。非営業日は`cloud-daily-batch`自身がgreenのskip runとして完了するので、successとして数えられる。
- 手動の復旧dispatchもsuccessとして数えるので、当日中に復旧すれば警報は出ない。
- run一覧が期待した形でなければ**警報を出さずにexit 1**する。parseの劣化が「run 0本」に落ちると、APIの形が変わるたびに誤報になるため。
- 過去日の判定は`gh workflow run cloud-batch-watchdog.yml -f check_date=YYYY-MM-DD`で再現する（その日の21:00 JSTに発火したwatchdogと同じ窓を評価する）。dispatch入力はcredentialを持たないvalidation stepでexact `YYYY-MM-DD`を検査してからstep env経由で渡す。
- watchdog自身もscheduleなので同時にskipされ得る。独立した2本が同日に両方skipされる確率は単発よりずっと低い。それでも不足が観測されたらCloudflare Worker cronへ格上げする。

watchdog jobは何もinstallしない（checkoutとsystem `python3`だけ）。警報が必要なまさにその瞬間にtoolchainの都合で止まらないようにするためで、`tests/batch/test_cloud_batch_watchdog.py`がstep一覧で固定する。

### webhook rotation

`DISCORD_WEBHOOK_URL` は GitHub Actions の repository secret で、通知 step だけが読む（job env ・
CLI 引数・log には出ない）。secret の実値を Git・issue・log へ書かない。

1. Discord で `#batch-runs` の webhook を作り直す（または既存 webhook の token を再生成する）。
2. `gh secret set DISCORD_WEBHOOK_URL --repo <owner>/<repo>` で新しい URL を登録する。
3. 過去営業日の `asof` で手動 run を発火し、`#batch-runs` に1件届くことを確認する。

旧 webhook は Discord 側で削除するまで有効。

### 実配送の確認

`#batch-runs` への実配送は次で確認する。

- 不正な `asof`（例: `2026-13-99`）の手動 run → `[FAILED]`（`invalid_asof`）が1件届く。
- 有効な `asof` または次の通常 run → `[OK]` が1件届く。
- 非営業日が先に来た場合 → reason 付き `[SKIPPED]`（no-publish）が届く。

各 message の batch 名 / datasets / 件数 / 所要時間 / publish 状態 / run URL が正しいことを照合する。

**無通知は「配送失敗」だけを意味しない。** notify step 自体が動かない障害（checkout 失敗、
runner 未割当、job の強制終了）は通知経路の外側にある。`#batch-runs` が静かなときは、まず
`gh run list --workflow cloud-daily-batch.yml` で run 自体の有無と結論を見る。

## システム状態の配信 — `views/system.json` と `system/latest-run.json`

Baibai Loop の `/system`（ヘッダ歯車メニュー → システム状態）は、判断用 3 タブから運用状態を
切り離して置く画面である。材料は 2 つで、更新される時点が違う。

| object | 書く側 | 内容 | 失敗 run での更新 |
| --- | --- | --- | --- |
| `views/system.json` | `export_read_models.py` | 4 store の as-of / 行数 / サイズ、直近取得が失敗したままの系列と連続失敗数・失敗開始時刻 | されない（exportに到達しないため、最後にpublishされた時点のまま） |
| `system/latest-run.json` | `cloud-daily-batch` の upload step | 通知と同じ `WorkflowRunSummary`（outcome / batch別結果 / error / run URL） | される |

失敗した run は export を出さないので、`views/` の中だけでは batch の失敗が UI に届かない。
`system/latest-run.json` は `views/` の外に置き、`upload-serving-views` の `--delete` 同期と
lifecycle の対象外にして、次の成功 publish でも消えないようにする。upload は best-effort で、
失敗しても run の outcome・通知の配送結果・publish 状態を変えない（GitHub Actions の log には残る）。

`views/system.json` に載せるのは「今の読みを変えない運用状態」だけで、判断に影響する staleness
（macro reading の stale、screening run の stale、store 読み取りエラー）は判断画面に残す。
provider の取得健全性は両方に出るが役割が違う: `/macro` は「この読みは信用できるか」、`/system` は
「どの provider をいつから直すべきか」を見る。

run 履歴は 1 件だけ持つ。時系列は Discord `#batch-runs` と GitHub Actions の run 履歴が保持し、
provider の「いつから失敗しているか」は indicator store の `provider_runs` から導出する。

`system/latest-run.json` は最後に summary を書けた run で止まる。upload は best-effort で、
notify が summary を書く前に落ちれば更新されない。run カードが `finished_at` と経過日数を出すのは
このためで、止まった object を最新の run と読み違えないようにしている。

shortlist preflight はこの object を read-only の一時ファイルへ取得してから run store と照合する。既存 path を上書きしないので、1 cycle ごとに新しい一時 path を使う。

```bash
batch/scripts/r2_transfer.sh pull-run-summary <new-temp-path>/latest-run.json
uv run baibai-engine screening shortlist preflight \
  --asof <ASOF> --cloud-summary <new-temp-path>/latest-run.json
```

`provider_runs` は cloud の日次 batch とローカル実行の両方が書く。ローカルで API key 未設定のまま
叩けばその失敗が最新行になり、cloud が健全でも `/system` に失敗として出る。逆に cloud で落ちた系列を
ローカルで手動 refresh すると streak が消える。実行環境を区別する列は持たないので、系列ごとの
判断は Discord の run 結果と併せて行う。
