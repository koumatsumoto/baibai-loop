# tools/cloud — クラウド配信向けのローカルバッチ script

Cloudflare 配信（issue #467 の設計）の compute と転送の入口。ここにある Python / shell
script は GitHub Actions とローカル運用から呼ぶ orchestration で、stable CLI ではない
（安定契約は `baibai-engine` / `baibai-app` 側にある）。read model の生成と日次 batch は
ローカル単体でも実行でき、転送 script だけが R2 を使う。

## Cloudflare / GitHub Actions 構成

R2 bucketとobject keyは次の固定契約を使う。どちらのbucketもPublic Development URLとcustom domainを無効にする。

| bucket | object | owner |
| --- | --- | --- |
| `baibai-stores` | `market.sqlite` | `cloud-daily-batch` + `cloud-history-backfill`（手動 dispatch。窓を名指しして履歴を遡る）+ ローカル`push-market`（cloud copyのmerge後だけupload） |
| `baibai-stores` | `runs.sqlite` | `cloud-daily-batch` |
| `baibai-stores` | `macro.sqlite` | `cloud-daily-batch`（rolling窓）+ ローカル`push-macro`（全履歴。cloud copyのmerge後だけupload） |
| `baibai-stores` | `baibai.sqlite` | ローカル`publish.sh`（replica） |
| `baibai-stores` | `schema-migrations/market-v13.sqlite` | `cloud-daily-batch`（write-once rollback artifact） |
| `baibai-serving` | `views/*.json` | GitHub Actions materialize |
| `baibai-serving` | `history/candidate-views/<asof>.json` | 日次batch、R2 lifecycleで31日後に削除 |
| `baibai-serving` | `system/latest-run.json` | 日次batch、毎runで上書き（`views/`外なのでexportの再生成で消えない） |

R2 lifecycle rule（31日削除）は`history/candidate-views/`へ**prefix指定で**追加する。旧形式の`history/candidates/` ruleは既存objectが31日で自然失効するまで残し、その後に削除する。Workerは旧prefixへ到達しない。**prefixを持たないbucket全体のruleを作らない** — `system/latest-run.json`が静かに失効し、`/system`のrunカードが恒久的に「記録なし」表示へ落ちる（消えたことに気づけない）。

資格情報はprincipalごとに分ける。

| principal | 設定 | scope |
| --- | --- | --- |
| GitHub Actions | variable `R2_ACCOUNT_ID`、secrets `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / provider 3本、公開 JPX 規制 URL 4本 | 必要なtransfer/provider stepだけ、stores + serving read-write |
| GitHub Actions（通知） | secret `DISCORD_WEBHOOK_URL` | `cloud-daily-batch` の通知 step のみ（job env に出さない） |
| GitHub Actions（Worker deploy） | variable `R2_ACCOUNT_ID`、secret `CLOUDFLARE_API_TOKEN` | 対象accountの`Workers Scripts Write`、`web`のdeploy stepのみ |
| ローカル`.env` | `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | storesだけread-write |
| Wrangler OAuth | `wrangler login` | bucket初期設定、Worker secretの手動設定 |
| Worker secret | `VIEW_PASSWORD` | Worker runtimeだけ |

R2 S3 endpointは`https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`からscriptが組み立てる。credential、password、endpointの実値をGit、issue、logへ書かない。

provider secretは`JQUANTS_API_KEY` / `ESTAT_APP_ID` / `EDINET_API_KEY`。加えて日次batchが当日cache不足で`bootstrap-cache`へ入ると、JPX規制provider（`universe.required_jpx_flags`の4 source: 特別注意銘柄 / 整理銘柄 / 取引停止 / 上場廃止警告）が公開JPXページのURLを要求する。これらは非secretのため`cloud-daily-batch.yml`の`Run daily batch` step envにliteralで置く（`JPX_SPECIAL_CAUTION_INDEX_URL` / `JPX_REORGANIZATION_URL` / `JPX_TRADING_HALT_URL` / `JPX_DELISTING_WARNING_URL`。雛形は`.env.sample`）。未配線だとbootstrapのJPX stepがfail-fastし、machine stores / serving uploadはskippedになる。

workflow dispatchの日付はfull SHAへ固定したcheckoutの後、credentialを持たないvalidation stepでexact `YYYY-MM-DD`と順序を検証する。`run:`へ`inputs.*`を展開せず、step envからshell変数として渡す。R2・provider・Cloudflare・Discordのcredentialは、それぞれを使うcommandのstep envだけへ渡し、checkout・setup・dependency install・validationへは渡さない。全外部Actionのfull SHA pinとこれらの境界は`tools/drift/check_workflow_trust.py`が検査する。

## 初回seedとWorker deploy

初回だけ、ローカル4 storeのconsistent SQLite snapshotをstores bucketへ送る。4 keyの
いずれかが既に存在する場合は、古いローカルcopyによる正本の巻き戻しを防ぐため何も
uploadせず停止する。

```bash
tools/cloud/seed.sh
```

production deployは`.github/workflows/web.yml`が所有する。PRはUI lint/build/testとWorker types/typecheck/test/dry-runまで、mainの`ui/`または`cloud/worker/`変更とmainを明示したmanual dispatchは同じgateの後にdeployする。deploy対象jobは共通のproduction concurrency groupで直列化し、deploy直前のremote `main`と`ui/`・`cloud/worker/`のtreeが一致するrunだけを反映する。docs-only等の後続commitはdeployを失わせず、後続web変更があるrunだけをstaleとしてskipする。Cloudflare API tokenは対象accountだけに絞った`Workers Scripts Write`を使い、repository Actionsのvariable `R2_ACCOUNT_ID`とsecret `CLOUDFLARE_API_TOKEN`を設定する。tokenはdeploy stepだけへ渡す。初回deployはworkflowをmainから手動実行する。

```bash
gh workflow run web.yml --ref main
gh run list --workflow web.yml --limit 3
cd cloud/worker
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
tools/cloud/verify_worker.sh
```

## 日常運用

application DBのjudgment更新をクラウド表示へ反映する。

```bash
tools/cloud/publish.sh
```

このscriptは`baibai.sqlite`のconsistent snapshotだけをstoresへ送り、`cloud-materialize`をdispatchする。servingへの直接writeは行わない。

application DBのschemaはローカルのCLI実行でmigrateされ、クラウドはこのstoreをread-onlyで読む。schema migrationを含むcodeがmainへ入ったら、次の`cloud-daily-batch`より前に`publish.sh`を実行する。exportはstoreのschemaがcodeと一致しない間viewを1件も書かずexit 1で停止するため、未publishのままではscreening結果も含めて何も更新されない。

indicator storeの履歴を深くしてクラウドへ載せる。日次batchはfrequency別のrolling窓しか引き直さないため、cloud正本の履歴は前へ伸びるだけで過去へ伸びない。系列を追加した後や窓を超える取得断の後は、ローカルで全履歴を取得してから`push-macro`する。

```bash
uv run baibai-engine macro refresh <series_id> ... --all-history --end YYYY-MM-DD
uv run baibai-engine macro reading --asof YYYY-MM-DD   # 履歴不足・異常値を確認
# series を追加した場合は、ここで registry を main へ入れてから push する
tools/cloud/r2_transfer.sh push-macro
gh workflow run cloud-materialize.yml --ref main
```

market storeの履歴を深くしてクラウドへ載せる。日次batchは前へしか伸ばさないので、過去へ伸ばす経路は2つある。**ローカルに既にその履歴があるなら取り直さない** — `push-market`がcloud copyをmergeしてからuploadするので、providerを一度も呼ばずに数分で載る。ローカルにも無い履歴だけ`cloud-history-backfill`をdispatchして取る。

```bash
tools/cloud/r2_transfer.sh push-market                # ローカルに履歴がある場合
gh workflow run cloud-history-backfill.yml --ref main \
  -f start=YYYY-MM-DD -f end=YYYY-MM-DD               # ローカルにも無い場合
```

`cloud-history-backfill`は財務サマリーが律速で、実測は3.4年で2時間32分（うち財務2時間05分）である。job上限は5時間なので、大量欠損は3〜4年ずつに分けてdispatchする。coverageのmergeが繋ぐので分割しても結果は同じになる。source failureまでにcommitされたchunkは、store SHA-256が変わった場合だけ`quick_check`と`push-market`を通してR2へ保存し、workflow自体は元の非0で失敗する。変更が無いfailureはuploadをskipする。3つのcloud writerは`cloud-publish`の`queue: max`を共有し、1件だけを実行しながらpending runをFIFOで保持する。

`push-market`はcloud copyをstagingへdownloadし、`merge_market_store.py`でローカルstoreへmergeしてからuploadする。storeの全12 tableが事実tableで、`source_coverage`も1日1行の粒度（`coverage_key`が日付）なので範囲のunion演算は要らない。主キーで`INSERT OR IGNORE`し、同じ主キーを両側が持つ場合はpayloadの一致をmerge前後に検証する。**比較しないのは、出所が何を言ったかではなくstoreがいつどう読んだかを記録する列だけ**（fetch時刻、およびEDINETが公開後に書き換える改訂marker）——2つのstoreが同じ記録を別の時刻に読めばそこは必ず食い違うので、比較すれば全てのmergeを拒否する。価格・財務・保有・被覆の範囲と件数は比較対象に残る。除外列は`merge_market_store.py`の`UNCOMPARED`に列挙してあり、事実列へ伸びていないことをtestが確かめる。merge後にsource側だけに残る行が1行でもあれば停止するので、日次batchが取得済みでローカルに無い行をuploadで失わない。source / targetとも現行schemaでなければ停止する——cloud copyが古いときの復旧はcloud側でstoreを開かせることであって、こちらでmigrateしてcloudが書いたことのない形を publish することではない。mergeの対象tableは`merge_market_store.py`の`FACT_KEYS`に列挙してあり、storeのtable一覧とずれたらtestが落ちる。

`push-macro`はcloud copyをstagingへdownloadし、`merge_indicator_store.py`でローカルstoreへmergeしてからuploadする。mergeの対象は事実を積み上げるtable（`observations` / `provider_runs`）だけで、主キーで`INSERT OR IGNORE`する。同じ主キーを両側が持つ場合は全payloadの一致をmerge前後に検証し、値・単位・source等が異なれば片方を正本と推測せずtransaction全体を停止する。source / target はschema version・列構成に加えて`schema.sql`由来の全persistent triggerとregistry state contractをcanonical定義へ完全一致させる。targetが保持する全series metadataは両端が有限なplausible rangeを持つことを前提とし、source / target observationをtransaction先頭でtargetのunitとrangeに照合する。いずれかの契約違反があればtargetを変更せず停止する。schema v5 rollout中はread-only source v4も同じ構造契約を検査して受理し、`jp.foreign_flows`のlegacy unit `jpy`を値非rescaleで`jpy-thousand`へ正規化して挿入する。targetは必ず現行schemaでなければならない。merge後にsource側だけに残る行が1行でもあれば停止するので、日次batchが取得済みでローカルに無い観測（rolling窓の最新日など）をuploadで失わない。`series` / `aliases`はsourceから取り込まない。通常のopenは登録外seriesのfacts・metadata・aliasesを保持し、明示的な`macro refresh`だけが現行registryに無いseriesをpruneするため、古いbranchのread後もtargetに残る新系列へcloud factsをmergeできる。source の registry generation が target より新しい場合と、同世代なのに `source.series` membership がtargetから欠ける場合は、facts未取得のseriesでもmergeを拒否する。target が source より新しい世代でmetadataが無いseriesのrowだけを意図した退役としてskip件数に含める。`market.sqlite` / `runs.sqlite`は`push-macro`が触らない。

### indicator storeのschemaがcloudとcodeでずれているとき

**cloud copyのschemaはcloud側でstoreを開くことによって上がる。** `macro refresh`が`open_connection`を通り、そこでmigrationが走ってから書き込み、`push-machine`が現行schemaのsnapshotをuploadする。したがって「cloud copyがcodeより1つ以上古い」のはschema bumpから次の日次batchまでの**正常な過渡状態**であって、不正なpushの痕跡ではない。cronは平日だけなので、週末にschemaを上げると月曜の実行までこのラグが残る。

この状態では`push-macro`が停止する。mergeはtargetに現行schemaを要求し、sourceは1 version前までしか受理しないため、2 version以上離れると`check_sqlite`を通ってもmergeで止まる。**復旧はcloud側でstoreを開かせることであって、cloud copyを手でmigrateすることではない。**

```bash
gh workflow run cloud-daily-batch.yml --ref main   # cloud copyがopenでmigrateされ現行schemaでpushされる
tools/cloud/r2_transfer.sh push-macro              # その後で通る
```

**pull側にschema検査を置いてはならない。** 検査を置くと、ラグを解消する唯一の経路（日次batchのpull → open → push）がstep 1で落ちて自己修復が止まり、storeを1行も書かない`cloud-materialize`まで道連れになる。schemaがずれている間に妥当域外の値が入る心配も要らない — 書き込み経路は全て`open_connection`を通り、そこで必ずmigrationが先に走る。

decision-cycleやmacro分析を始める前に、クラウド正本のmachine storeをローカルへ取得する。

```bash
tools/cloud/pull.sh
uv run baibai-engine screening verify-cache-coverage --asof YYYY-MM-DD
uv run baibai-engine screening ticker-profile --ticker TICKER
```

`pull.sh`はmarket/runs/macroの全downloadとSQLite `quick_check`が成功してから3 storeを置換し、`baibai.sqlite`には触れない。日次batchと同時に実行して世代を跨がないよう、通常は18:30 JST前後とGitHub Actions実行中を避ける。

日次workflowを手動実行する。`asof`省略時は当日JSTをmarket calendarで判定し、非営業日は成功扱いでskipする。過去日を指定すると営業日gateをskipする。

```bash
gh workflow run cloud-daily-batch.yml --ref main
gh workflow run cloud-daily-batch.yml --ref main -f asof=YYYY-MM-DD
gh run list --workflow cloud-daily-batch.yml --limit 10
```

通常cronは平日09:30 UTC（18:30 JST）。株価日足の16:30 JST更新と、18:00 JST更新のJPX系日次datasetの後に余裕を置く。GitHub Actionsのschedule遅延は許容し、UIのas-ofとworkflow履歴で検知する。

`daily_batch.py`のexit 3はfresh screening exportを持つため、workflowはstores/serving uploadまで完了させてからjobを失敗にする。exit 1は新しいpublish可能runがないためuploadしない。非営業日skipはexportがないため既存servingを変更しない。

`*.workers.dev`のHTTP requestはWorkerが認証判定より前に308でHTTPSへredirectし、HTTPS responseはHSTSを返す。UI navigationは必ずこの経路を通り、hashed static assetだけをWorker invocationなしで配信する。

## Password rotation

```bash
cd cloud/worker
npx wrangler secret put VIEW_PASSWORD
```

新しい32文字CSPRNG値をpromptへ入力する。次のAPI 401でbrowserの旧値がlocalStorageから削除され、password入力画面へ戻る。Workerの再deploy、R2変更、application data更新は不要。

## R2 transferの安全境界

- `market.sqlite`のv13からv14へのmigration前に、workflowは`schema-migrations/market-v13.sqlite`を固定keyへ一度だけ保存する。既存objectは上書きせず、毎回再downloadして非空・`quick_check`・`user_version = 13`を検証してからbatchを開始する。v14 storeに対してartifactが存在しなければ処理を停止する。
- upload前にPython `sqlite3.backup`でsnapshotを作り、WAL未checkpoint行を含めて`quick_check`する。
- 複数storeのpushは全snapshotの作成・検査を終えてからuploadを始める。3 store一括のmachine store pushはGitHub Actionsからだけ許可する（`runs.sqlite`はcloudが唯一のwriterで、無条件uploadが古いローカルcopyで巻き戻すため）。`macro.sqlite` / `market.sqlite`はローカルからも`push-macro` / `push-market`でuploadできるが、いずれもcloud copyのmergeを通した後だけで、mergeがcloud側の行の取り残しを検出したら停止する。
- pushは上書き対象のremote objectを`<key>.bak`へ1世代copyしてからuploadする（R2内のserver-side copy。存在判定は`s3api head-object`の完全一致で、`.bak`自身をkey本体と誤認しない）。storeは原則sourceから再構築できるが、PMI履歴のようにpublisherが古いURLを落とすと再取得できない部分があるため、破損・誤pruneしたsnapshotによる上書きから前回分へ戻せる状態を保つ。復元は`.bak`を本keyへcopyし直す（`aws s3api copy-object`を使う。`aws s3 cp`のS3→S3経路はobject sizeで実装が切り替わり、multipart copyはGetObjectTagging、single-part copyは`x-amz-tagging-directive`を要求してどちらもR2が実装しない。CopyObjectはdirectiveを送らず5GBまでのobjectで通る）。R2はcopyが終わるまで応答を返さず、その待ちはobject sizeに比例してGB級のstoreではaws CLI既定のread timeout 60秒に収まらないため、pushの世代保存も手動復元も`--cli-read-timeout`を既定より広げて呼ぶ。`market.sqlite`は10年履歴で約1.3GBあり、3 store合計のpush（snapshot作成・`.bak`のserver-side copy・upload）は実測で約2分である。
- `.bak`は1世代のみで、次のpushで置き換わる。日次batchが毎営業日pushするため、実質の巻き戻し猶予は約24時間である。registry編集後は日次workflowの`registry-prune-pending` / `registry-prune`行（transaction ID・series ID・observation/provider-run削除件数）を当日中に確認する。pending に対応する committed 行が無い実行や意図しないpruneを検出したら、次のpushが`.bak`を置き換える前に状態を確認・復元する。
- 初回seedは既存のstore keyを1件でも検出したら停止し、再seedによるクラウド正本の上書きを許可しない。
- pullは固定4 key以外を受け付けず、全downloadと`quick_check`完了後に置換する。
- servingの`views/`は`aws s3 sync --delete`で完全像に合わせる。historyは追記だけで削除しない。
- `views/meta.json`は他のviewとhistoryが全て成功した後に最後にuploadする。
- bucket名は`R2_STORES_BUCKET` / `R2_SERVING_BUCKET`で明示的にoverrideできるが、通常は固定defaultを使う。

### market schema v14のrollback

v14 migration後にEDINET document stateの欠損または誤変換が確認された場合は、日次workflowを停止する。最初にwrite-once artifactを別pathへdownloadし、実SQLiteとして非空・`quick_check`・schema v13を満たすことをdry-run確認する。その後artifactを正本keyへserver-side copyし、`pull-machine`で取得して再検査する。復旧中にv14 codeでstoreを開くと再migrationされるため、原因修正版またはv13 codeへ切り替えるまでworkflowを再開しない。

```bash
tools/cloud/r2_transfer.sh download-market-v13-rollback \
  /tmp/baibai-market-v13-rollback.sqlite
uv run python tools/cloud/sqlite_snapshot.py check \
  --path /tmp/baibai-market-v13-rollback.sqlite --schema-version 13
aws s3api copy-object \
  --bucket baibai-stores \
  --key market.sqlite \
  --copy-source baibai-stores/schema-migrations/market-v13.sqlite \
  --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" \
  --cli-read-timeout 300
tools/cloud/r2_transfer.sh pull-machine
uv run python tools/cloud/sqlite_snapshot.py check \
  --path data/screening/market.sqlite --schema-version 13
```

## export_read_models.py — read model の材料化

`baibai_app.readmodel` builders を共用して、UI が読む全 view を serving 配置どおりの
JSON に書き出す。Worker には業務ロジックを置かない設計の実体。

```bash
uv run python tools/cloud/export_read_models.py --output-dir <dir> [--batch daily|manual] [--repo-root <path>]
```

出力（`<dir>` 配下）:

- `views/dashboard.json` / `views/screening_latest.json` / `views/operations.json`
- `views/macro--<period>-<granularity>.json`（1y|5y|10y|max × daily|weekly|monthly|yearly）
- `views/macro-reading.json`（全登録系列の機械読み値。indicator store か reading rules が
  無ければ警告のうえ書かず、Macro タブは該当パネルだけを非表示にする）
- `views/security--<ticker>.json`（保有 + 最新 run 掲載 + shortlist の ticker）
- `views/meta.json`（生成時刻・実データ更新時刻・store 別 as-of・batch 種別。UI の鮮度表示と同じ契約）
- `history/candidate-views/<asof>.json`（run とCandidates全件を型付きUI read modelへ変換した履歴。31 日で削除）

書き出しの前に application store の `user_version` が code の schema version と一致することを確認し、不一致なら view を 1 件も作らず exit 1 で停止する（読み取り経路は read-only で migrate しないため、不一致は build の途中で素の SQL error になる）。store が無い root は judgment 空の正常状態として export する。

`views/` は毎回 export の完全な像に置換される（実行のたびに一度削除して作り直すので、対象から外れた古い view は残らない）。`history/` は追記のみで、この script は削除を行わない。上記の「31 日で削除」は serving store（R2 lifecycle）側の保持契約であり、script の挙動ではない。

Workerは認証後の`/api/screening/history`でCandidates履歴の日付一覧を返し、`/api/screening/history/YYYY-MM-DD`だけを`history/candidate-views/`へ写像する。任意key、旧形式の`history/candidates/`、store bucketは公開しない。

views の JSON は `baibai-app` の対応 API response と同形（pydantic `model_dump_json`）。`meta.json` は全 view / history の書き込み成功後に最後に書くので、途中失敗した出力 dir が新鮮さを主張する事態を避ける。

## daily_batch.py — 日次機械工程の 1 コマンド実行

営業日判定 → screening cache coverage（不足時のみ bootstrap）→ EDINET incremental extraction →
初回 coverage 不足時のみ再検証 → run → select →
macro series refresh → export → run store prune を順に実行する。
全 step は public CLI の subprocess で、step ごとにコマンドライン・exit code・所要秒を
stdout へ出す（scheduled workflow のログをそのまま読む前提）。

```bash
# 通常（当日 JST。market calendar で非営業日なら exit 0 で skip）
uv run python -m tools.cloud.daily_batch --output-dir <dir>

# 手動再実行・過去日（営業日 gate を skip）
uv run python -m tools.cloud.daily_batch --asof YYYY-MM-DD --output-dir <dir>

# structured summary を書き出す（workflow の Discord 通知が読む）
uv run python -m tools.cloud.daily_batch --output-dir <dir> --summary-output <summary.json>
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
batch ごとの status / datasets / metrics・publish state・GitHub Actions run URL を含む。error は
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

Baibai App の `/system`（ヘッダ歯車メニュー → システム状態）は、判断用 3 タブから運用状態を
切り離して置く画面である。材料は 2 つで、更新される時点が違う。

| object | 書く側 | 内容 | 失敗 run での更新 |
| --- | --- | --- | --- |
| `views/system.json` | `export_read_models.py` | 4 store の as-of / 行数 / サイズ、直近取得が失敗したままの系列と連続失敗数・失敗開始時刻 | されない（exportに到達しないため、最後にpublishされた時点のまま） |
| `system/latest-run.json` | `cloud-daily-batch` の upload step | 通知と同じ `WorkflowRunSummary`（outcome / batch別結果 / error / run URL） | される |

失敗した run は export を出さないので、`views/` の中だけでは batch の失敗が UI に届かない。
`system/latest-run.json` は `views/` の外に置き、`upload-serving` の `--delete` 同期と
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

`provider_runs` は cloud の日次 batch とローカル実行の両方が書く。ローカルで API key 未設定のまま
叩けばその失敗が最新行になり、cloud が健全でも `/system` に失敗として出る。逆に cloud で落ちた系列を
ローカルで手動 refresh すると streak が消える。実行環境を区別する列は持たないので、系列ごとの
判断は Discord の run 結果と併せて行う。
