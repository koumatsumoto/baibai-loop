# tools/cloud — クラウド配信向けのローカルバッチ script

Cloudflare 配信（issue #467 の設計）の compute と転送の入口。ここにある Python / shell
script は GitHub Actions とローカル運用から呼ぶ orchestration で、stable CLI ではない
（安定契約は `baibai-engine` / `baibai-app` 側にある）。read model の生成と日次 batch は
ローカル単体でも実行でき、転送 script だけが R2 を使う。

## Cloudflare / GitHub Actions 構成

R2 bucketとobject keyは次の固定契約を使う。どちらのbucketもPublic Development URLとcustom domainを無効にする。

| bucket | object | owner |
| --- | --- | --- |
| `baibai-stores` | `market.sqlite` / `runs.sqlite` | `cloud-daily-batch` |
| `baibai-stores` | `macro.sqlite` | `cloud-daily-batch`（rolling窓）+ ローカル`push-macro`（全履歴。cloud copyのmerge後だけupload） |
| `baibai-stores` | `baibai.sqlite` | ローカル`publish.sh`（replica） |
| `baibai-serving` | `views/*.json` | GitHub Actions materialize |
| `baibai-serving` | `history/select/<asof>.json` | 日次batch、削除しない |
| `baibai-serving` | `history/candidate-views/<asof>.json` | 日次batch、R2 lifecycleで31日後に削除 |

R2 lifecycle rule（31日削除）は`history/candidate-views/`へ追加する。旧形式の`history/candidates/` ruleは既存objectが31日で自然失効するまで残し、その後に削除する。Workerは旧prefixへ到達しない。

資格情報はprincipalごとに分ける。

| principal | 設定 | scope |
| --- | --- | --- |
| GitHub Actions | variable `R2_ACCOUNT_ID`、secrets `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / provider 3本、公開 JPX 規制 URL 4本 | stores + serving read-write |
| ローカル`.env` | `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | storesだけread-write |
| Wrangler OAuth | `wrangler login` | bucket初期設定、Worker deploy、Worker secret |
| Worker secret | `VIEW_PASSWORD` | Worker runtimeだけ |

R2 S3 endpointは`https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`からscriptが組み立てる。credential、password、endpointの実値をGit、issue、logへ書かない。

provider secretは`JQUANTS_API_KEY` / `ESTAT_APP_ID` / `EDINET_API_KEY`。加えて日次batchが当日cache不足で`bootstrap-cache`へ入ると、JPX規制provider（`universe.required_jpx_flags`の4 source: 特別注意銘柄 / 整理銘柄 / 取引停止 / 上場廃止警告）が公開JPXページのURLを要求する。これらは非secretのため`cloud-daily-batch.yml`のjob envにliteralで置く（`JPX_SPECIAL_CAUTION_INDEX_URL` / `JPX_REORGANIZATION_URL` / `JPX_TRADING_HALT_URL` / `JPX_DELISTING_WARNING_URL`。雛形は`.env.sample`）。未配線だとbootstrapのJPX stepがfail-fastし、machine stores / serving uploadはskippedになる。

## 初回seedとWorker deploy

初回だけ、ローカル4 storeのconsistent SQLite snapshotをstores bucketへ送る。4 keyの
いずれかが既に存在する場合は、古いローカルcopyによる正本の巻き戻しを防ぐため何も
uploadせず停止する。

```bash
tools/cloud/seed.sh
```

WorkerはUIをbuildしてからdeployする。`VIEW_PASSWORD`はpassword manager等で生成した32文字のCSPRNG英数値を使い、値を引数やshell historyへ書かずpromptへ入力する。

```bash
cd cloud/worker
npm ci
npm run types:check
npm run typecheck
npm test
# 初回 deploy は secret 未設定時に全 API を 401 にする。
npm run deploy
npx wrangler secret put VIEW_PASSWORD
```

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
tools/cloud/r2_transfer.sh push-macro
gh workflow run cloud-materialize.yml --ref main
```

`push-macro`はcloud copyをstagingへdownloadし、`merge_indicator_store.py`でローカルstoreへmergeしてからuploadする。mergeは全tableを主キーで`INSERT OR IGNORE`し、target側にある行はtargetの値を残す（series定義は現registry由来のものを保つ）。merge後にsource側だけに残る行が1行でもあれば停止するので、日次batchが取得済みでローカルに無い観測（rolling窓の最新日など）をuploadで失わない。`market.sqlite` / `runs.sqlite`はcloudが唯一のwriterなので`push-macro`は触らない。

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

- upload前にPython `sqlite3.backup`でsnapshotを作り、WAL未checkpoint行を含めて`quick_check`する。
- 複数storeのpushは全snapshotの作成・検査を終えてからuploadを始める。3 store一括のmachine store pushはGitHub Actionsからだけ許可する（cloudが唯一のwriterである`market.sqlite` / `runs.sqlite`を古いローカルcopyで巻き戻さないため）。`macro.sqlite`はローカルからも`push-macro`でuploadできるが、cloud copyのmergeを通した後だけで、mergeがcloud側の行の取り残しを検出したら停止する。
- pushは上書き対象のremote objectを`<key>.bak`へ1世代copyしてからuploadする（R2内のserver-side copy。存在判定は`s3api head-object`の完全一致で、`.bak`自身をkey本体と誤認しない）。storeは原則sourceから再構築できるが、PMI履歴のようにpublisherが古いURLを落とすと再取得できない部分があるため、破損・誤pruneしたsnapshotによる上書きから前回分へ戻せる状態を保つ。復元は`.bak`を本keyへcopyし直す（`aws s3api copy-object`を使う。`aws s3 cp`のS3→S3経路はobject sizeで実装が切り替わり、multipart copyはGetObjectTagging、single-part copyは`x-amz-tagging-directive`を要求してどちらもR2が実装しない。CopyObjectはdirectiveを送らず5GBまでのobjectで通る）。
- `.bak`は1世代のみで、次のpushで置き換わる。日次batchが毎営業日pushするため、実質の巻き戻し猶予は約24時間である。registry編集ミスによる観測行のpruneは無音で起きるので、series registryを変更した日は当日のうちにMacroタブのdata healthとseries件数を確認する。
- 初回seedは既存のstore keyを1件でも検出したら停止し、再seedによるクラウド正本の上書きを許可しない。
- pullは固定4 key以外を受け付けず、全downloadと`quick_check`完了後に置換する。
- servingの`views/`は`aws s3 sync --delete`で完全像に合わせる。historyは追記だけで削除しない。
- `views/meta.json`は他のviewとhistoryが全て成功した後に最後にuploadする。
- bucket名は`R2_STORES_BUCKET` / `R2_SERVING_BUCKET`で明示的にoverrideできるが、通常は固定defaultを使う。

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
- `history/select/<asof>.json`（machine selection のサマリ。無期限保持する軽量履歴）
- `history/candidate-views/<asof>.json`（run とCandidates全件を型付きUI read modelへ変換した履歴。31 日で削除）

書き出しの前に application store の `user_version` が code の schema version と一致することを確認し、不一致なら view を 1 件も作らず exit 1 で停止する（読み取り経路は read-only で migrate しないため、不一致は build の途中で素の SQL error になる）。store が無い root は judgment 空の正常状態として export する。

`views/` は毎回 export の完全な像に置換される（実行のたびに一度削除して作り直すので、対象から外れた古い view は残らない）。`history/` は追記のみで、この script は削除を行わない。上記の「無期限保持 / 31 日で削除」は serving store（R2 lifecycle）側の保持契約であり、script の挙動ではない。

Workerは認証後の`/api/screening/history`でCandidates履歴の日付一覧を返し、`/api/screening/history/YYYY-MM-DD`だけを`history/candidate-views/`へ写像する。任意key、旧形式の`history/candidates/`、`history/select/`、store bucketは公開しない。

views の JSON は `baibai-app` の対応 API response と同形（pydantic `model_dump_json`）。`meta.json` は全 view / history の書き込み成功後に最後に書くので、途中失敗した出力 dir が新鮮さを主張する事態を避ける。

## daily_batch.py — 日次機械工程の 1 コマンド実行

営業日判定 → screening cache coverage（不足時のみ bootstrap）→ run → select →
macro series refresh → export → run store prune を順に実行する。
全 step は public CLI の subprocess で、step ごとにコマンドライン・exit code・所要秒を
stdout へ出す（scheduled workflow のログをそのまま読む前提）。

```bash
# 通常（当日 JST。market calendar で非営業日なら exit 0 で skip）
uv run python tools/cloud/daily_batch.py --output-dir <dir>

# 手動再実行・過去日（営業日 gate を skip）
uv run python tools/cloud/daily_batch.py --asof YYYY-MM-DD --output-dir <dir>
```

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
- macro series refresh の失敗は繰延べる: export まで完走して screening 結果は publish し、
  最後に exit 3 で終了する（scheduled workflow の失敗通知は発火し、鮮度は meta の
  `macro_asof` に現れる）。繰延べた失敗の詳細は発生時点で stderr にも出す
- `select` の前回 run 比較は、runs store の「target より前の最大 as-of の最新 revision」を
  この script が決定論的に解決して `--previous-run-revision-id` で渡す（同一日の再実行が
  複数 revision を作っても停止しない）
- 営業日判定は market store の `jquants_market_calendar` が情報源。対象日をカバーして
  いない場合は黙って続行せず明示エラーで停止する
