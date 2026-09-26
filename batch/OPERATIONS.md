# batch — production orchestration と cloud store 運用

本書はmachine処理、store転送、公開と復旧の手順を所有する。domain処理はengine、read modelはwebが担う。以下のcommandはrepository rootのBashから実行し、終了状態と次の操作は各節に従う。CLIの全引数は該当`--help`を参照する。

## 長時間処理の監視

対象run IDを固定して監視する。hard timeoutはworkflow定義を正本とする。

```bash
gh run watch <RUN_ID> --exit-status --compact --interval 60
```

外部監視がtimeoutでも処理が終了したとは限らないため、対象の状態と現在stepを確認し、activeな処理を重複起動しない。

## Cloudflare / GitHub Actions 構成

`baibai-stores`はL1・machine store・application DB replica、`baibai-serving`は表示用read modelを保持する。両bucketのPublic Development URLとcustom domainを無効にする。

| bucket | object | owner |
| --- | --- | --- |
| `baibai-stores` | `market.sqlite`（store-local data + `lake_store_origin` metadata） | `cloud-daily-batch` + ローカル`push-market`（cloud copyのmerge後だけupload） |
| `baibai-stores` | `lake/`（lake所有datasetのcanonical L1） | `cloud-daily-batch`と`cloud-tradingview-snapshot`の`publish-lake` + ローカル`r2_transfer.sh publish-lake` |
| `baibai-stores` | `runs.sqlite` | `cloud-daily-batch` + 明示的なローカルdailyの`push-machine` |
| `baibai-stores` | `macro.sqlite` | `cloud-daily-batch`（rolling窓）+ ローカル`push-macro`（全履歴。cloud copyのmerge後だけupload） |
| `baibai-stores` | `baibai.sqlite` | ローカル`publish.sh`（replica） |
| `baibai-serving` | `views/*.json` | GitHub Actions materialize |
| `baibai-serving` | `history/candidate-views/<asof>.json` | 日次batch、R2 lifecycleで31日後に削除 |

R2 lifecycle ruleは`history/candidate-views/`だけに設定する。bucket全体へ設定すると`views/meta.json`まで期限で消え、欠落を検知できない。Review Set membershipの長期履歴は保持しない。

`cloud-materialize`・`cloud-daily-batch`・`cloud-tradingview-snapshot`はGitHubの`cloud-publish`を共有する。GitHubとローカルのpublication競合は[ローカル発行手順](#ローカルからクラウドを更新する)のleaseで止める。servingへのwriteはGitHub Actionsだけが行う。application DBはlocal canonicalで、cloud copyはreplicaである。

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

servingはstore push成功後だけ発行する。失敗した日の表示更新は停止し、`views/meta.json`を最後に発行する。

provider secretは`JQUANTS_API_KEY` / `ESTAT_APP_ID` / `EDINET_API_KEY`。`bootstrap-cache`はさらに
`universe.required_jpx_flags`の4 source（特別注意銘柄 / 整理銘柄 / 取引停止 / 上場廃止警告）の公開URLを要求する。
非secretの`JPX_SPECIAL_CAUTION_INDEX_URL` / `JPX_REORGANIZATION_URL` / `JPX_TRADING_HALT_URL` /
`JPX_DELISTING_WARNING_URL`を`cloud-daily-batch.yml`の`Run daily batch` step envへliteralで置く（雛形は
`.env.sample`）。未配線ならJPX stepがfail-fastし、machine stores / serving uploadはskippedになる。


## 日常運用

### クラウド正本をローカルへ取得する

**前提**: research-triage / research / macro-contextの分析前に、`cloud-daily-batch`の実run状態を確認する。queued / in_progressなら通常の分析用pullを開始せず、完了後に実行する。固定時刻で安全性を判断しない。

```bash
gh run list --workflow cloud-daily-batch.yml --limit 5
```

TradingViewはL1だけを独立更新するため、完了済みreleaseをcurrent machine bundleへhydrateしてよい。`cloud-materialize`はmachine store / L1を書き換えないので待機条件にしない。ローカルの`publish-lake` / `push-machine` / `push-market` / `push-macro`とも意図的に重ねず、発行終了後にfreshにpullする。

**実行**:

```bash
batch/scripts/pull.sh
```

**成功確認**: `pull-machine`はreceipt / ETagでmarket / runs / macroを同じ確認済みbundleとして取得し、`hydrate-market`は開始時のcurrent L1 releaseを固定する。途中でpointerが動けばvisible storeを置換せず停止する。ただしmachine bundleとL1 pointerを同一transactionでは固定しない。receipt / generation / pointerの競合や検査失敗後のlocal stateは分析に使わず、原因を確認してfreshにpullし直す。`baibai.sqlite`はbulk pullしない。

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

変更codeをmainへ反映し、対象storeと変更内容を確認する。marketはlake所有tableを編集する前に現行releaseへhydrateする。変更後の再hydrateはlocal変更を上書きするため行わない。

GitHubの`cloud-publish` queueはGitHub内の順序だけを守る。GitHubとローカルの共通発行にはR2の`coordination/publication-lease.json`を使う。TTLは固定120分。`publish-lake` / `push-machine` / `push-market` / `push-macro`をローカルから行う際は、長い取得・準備を済ませてから、production read / merge / publishを次の同一lease内で実行する。`r2_transfer.sh`の低レベルcommandはleaseを暗黙取得しない。`push-app`、`publish.sh`、初回`seed-all`は対象外。

```bash
with_publication_lease() (
  handle="$(mktemp "${TMPDIR:-/tmp}/baibai-publication-lease.XXXXXX.json")"
  rm -f "$handle"
  uv run python -m baibai_batch.storage.publication_lease acquire --purpose operator --handle "$handle" || return $?
  if "$@"; then result=0; else result=$?; fi
  uv run python -m baibai_batch.storage.publication_lease release --handle "$handle" || return $?
  return "$result"
)
```

失敗時もreleaseを試みるため、上の関数内では`set -e`を使わない。release失敗時はhandleを残して原因を確認する。busyならowner・purpose・expiryと該当run/processの生存を確認し、blind retryしない。expired leaseのみ次のacquireが条件付きで引き継ぐ。L1とmachine storeのCAS / receiptは最終整合性境界として残り、Phase 1のservingはGitHub-only writerと`cloud-publish` queue、applicationはlocal正本のownerを維持する。ローカルrunnerも同じCLIを使う。

変更した対象に合う経路だけを実行する。lake所有tableの変更:

```bash
with_publication_lease bash -c 'batch/scripts/r2_transfer.sh publish-lake && batch/scripts/r2_transfer.sh push-market'
```

marketのstore-local tableだけの変更:

```bash
with_publication_lease batch/scripts/r2_transfer.sh push-market
```

macroだけの変更:

```bash
with_publication_lease batch/scripts/r2_transfer.sh push-macro
```

直前に`pull.sh`した3 storeでlocal daily全体を実行した場合は、上の単独pushではなく同じbundleを反映する。

```bash
with_publication_lease bash -c 'batch/scripts/r2_transfer.sh publish-lake && batch/scripts/r2_transfer.sh push-machine'
```

単独pushはcloudの全行を包含したmerge、bundle pushはpull時点の全ETag一致とCAS uploadの成功を確認する。選んだpushが成功し、表示更新も必要な場合だけ実行する。

```bash
gh workflow run cloud-materialize.yml --ref main
```

対象runの完了を[監視手順](#長時間処理の監視)で確認する。merge・origin・schema・CASの失敗では公開へ進まず、[R2安全境界](#r2-transferの安全境界)に従って原因とcloud copyを確認する。local dailyの3 storeを個別pullや無条件uploadで組み替えない。

leaseがmalformedなら自動修復しない。GitHubの`cloud-publish` writerにrunning/pendingがなく、ローカル発行processも動いていないことを確認し、固定key`coordination/publication-lease.json`だけをローカルへ取得して原因を調べる。人間がactive writerなしと確認した場合に限り、そのexact key 1個だけをR2から削除する。次のacquireが`If-None-Match: *`で再作成する。通常運用でprefix削除や手書きJSON上書きはしない。

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

application DBは判断とledgerの正本であり再生成しない。全writerを停止し、以下を同じshell sessionで実行する。まずローカルcheckpoint、失われた場合だけR2世代を選ぶ。R2はlocalより古い可能性がある。

**1. 復元前backup。** 既存canonicalがある場合だけ、WAL込みのconsistent snapshotを作る。backup失敗ならcanonicalへ触れない。

```bash
restore_backup=
if test -f stores/application/baibai.sqlite; then
  restore_backup="$(
    set -o pipefail
    uv run baibai-engine db backup | \
      uv run python -c 'import sys, yaml; print(yaml.safe_load(sys.stdin)["path"])'
  )" || exit 1
  test "$restore_backup" != None && test -f "$restore_backup" || exit 1
fi
```

**2. 候補選択。** ローカルcheckpointはbackup commandが検証済みsnapshotを直近10世代保持する。

```bash
ls -t stores/application/backups/ || exit 1
read -r -p '復元するcheckpoint path: ' target || exit 1
test -f "$target" || exit 1
```

ローカル候補が失われた場合は上の選択を行わず、R2の一覧を確認する。

```bash
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  aws s3api list-objects-v2 --bucket baibai-stores --prefix baibai.sqlite.bak- \
    --query 'Contents[].[Key,LastModified]' --output text \
    --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
)
```

一覧取得成功後、選んだexact keyを一意なstaging fileへ取得する。

```bash
read -r -p '復元するexact R2 key: ' restore_key || exit 1
target="$(mktemp /tmp/baibai-restore.XXXXXX.sqlite)" || exit 1
(
  set -a
  source .env || exit 1
  set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  aws s3api get-object --bucket baibai-stores --key "$restore_key" \
    --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" "$target"
) || exit 1
```

**3. 共通検査。** 出力を読み、integrityがexact `ok`、foreign key違反が0行、user_versionがcurrent codeのschemaと一致する場合だけ進む。sqlite3のexit 0だけでは合格にしない。runtime自動移行はない。

```bash
(
  set -e
  test -f "$target"
  sqlite3 "$target" 'PRAGMA integrity_check;'
  sqlite3 "$target" 'PRAGMA foreign_key_check;'
  sqlite3 "$target" 'PRAGMA user_version;'
)
```

**4. 配置と照合。** 検査合格とwriter停止を確認後、copy成功前にcanonicalを動かさない。

```bash
(
  set -e
  restore_stage="$(mktemp stores/application/baibai-restore.XXXXXX.sqlite)"
  cp "$target" "$restore_stage"
  rm -f stores/application/baibai.sqlite-wal stores/application/baibai.sqlite-shm
  mv -f "$restore_stage" stores/application/baibai.sqlite
  uv run baibai-engine position ledger
)
```

ledgerを復元対象の時点と照合し、不一致なら手順5へ進む。照合成功後は[application DB反映](#application-db-を反映する)へ進み、反映確認後に判断業務を再開する。公開途中の失敗では検証済みlocalを戻さず、upload・dispatch・materializeの到達点を確認して反映側を復旧する。ledgerを`head`へpipeしない。

**5. ローカル復元の検証失敗時。** 復元前backupがある場合だけ、次で戻す。backupがない場合は判断を再開せず、別候補の取得・検査へ戻る。

```bash
(
  set -e
  test -n "$restore_backup" && test -f "$restore_backup"
  restore_stage="$(mktemp stores/application/baibai-rollback.XXXXXX.sqlite)"
  cp "$restore_backup" "$restore_stage"
  failed_restore="stores/application/backups/baibai-failed-restore-$(date +%Y%m%dT%H%M%S).sqlite"
  test ! -e "$failed_restore"
  mv stores/application/baibai.sqlite "$failed_restore"
  rm -f stores/application/baibai.sqlite-wal stores/application/baibai.sqlite-shm
  mv -f "$restore_stage" stores/application/baibai.sqlite
  uv run baibai-engine position ledger
)
```

途中停止では残存pathと失敗commandを確認し、blockを無条件反復しない。

### 履歴を深くする

#### Macro履歴

系列追加後やrolling窓を超える取得断を補う場合に使う。対象期間の全履歴を取得し、Readingを確認する。

```bash
(
  set -e
  read -r -p '対象日 YYYY-MM-DD: ' asof
  read -r -a series_ids -p '対象series IDを空白区切りで入力: '
  test "${#series_ids[@]}" -gt 0
  uv run baibai-engine macro refresh "${series_ids[@]}" --all-history --end "$asof"
  uv run baibai-engine macro reading --asof "$asof"
)
```

数値・履歴不足を確認し、系列追加を含むcodeはmainへ反映してから、[macroのcloud更新](#ローカルからクラウドを更新する)へ進む。失敗時は保存済み範囲と未完範囲を確認し、日次batchに全履歴取得を代行させない。

#### Macro観測とregistryの修正

vintage・retraction・generation・no-lossの意味は[Macro reference](../docs/reference/macro.md#①-データindicator-series-を引く)を参照する。

**観測の撤回**: `macro retract <series_id> --observed-at <date> --expected-vintage <ts>`で確認した最新vintageを名指す。derived入力ではcommandが示す依存系列の同日を先に撤回する。CAS拒否なら現在vintageを読み直す。成功後は正しい下位観測、または当日の非表示を確認する。localの直接DELETEや同じCASの反復で復旧しない。

**系列の追加・退役・改名**:

1. writerが競合しない時間に、現行mainで`uv run baibai-batch validate-macro-stores`を実行する。
2. `engine/src/baibai_engine/macro/indicators/registry/`の定義を変更し、`definitions.py`の`_REGISTRY_MEMBERSHIP_GENERATIONS`へ変更後の集合に対応する、現在より大きいgenerationを設定する。過去と同じ集合へ戻る場合はdigestも同じなので、その既存keyの値を更新する。重複keyの追記や旧generation再利用はしない。
3. 変更codeをmainへ反映し、同じcodeのlocalで現行系列を1件以上指定して明示的な`macro refresh`を実行する。退役IDだけの指定ではpruneへ進めない。全履歴再計算が必要ならbase→derivedの順に実行する。
4. validator・Reading・意図した退役結果を確認後、`push-macro`を実行する。件数表示はcommit後の補助出力であり、ログの組合せをcommit条件にしない。発行済みContextの退役系列warningは確認するが本文を書き換えない。

**公開済みoptionsの集計式を変える場合**: `jp.n225_iv_30d`・`jp.n225_iv_skew`・`jp.n225_iv_term`は2段に分ける。まず3系列を退役し、generationを進めて上の手順でcloudへ反映する。そのpush成功後に新式と3系列を戻し、元digestの既存generation値を退役段階よりさらに大きい値へ更新する。再びcodeをmainへ反映してから必要範囲の全履歴を取得し、新式で値が出ない日に旧式の観測が残らないことを確認してpushする。長い取得窓はproviderの契約に従って分割する。日次batch待ち・localだけの削除・2段の一括化はしない。

**停止と復旧**: DB拒否・意図しない退役・provider取得失敗・merge/CAS失敗では後続公開へ進まない。provider失敗がcommit済みpruneを巻き戻すとは扱わない。誤った内容をcloudへ反映した場合は次のpushを止め、[R2 transferの安全境界](#r2-transferの安全境界)の前世代復元へ進む。部分pushによるreceipt不一致だけは[部分pushからの復旧](#部分-push-からの復旧)で扱う。

#### Market履歴

取得成功後、[ローカルからクラウドを更新する](#ローカルからクラウドを更新する)のlake変更またはstore-local変更の片方を実行する。

**成功確認**: `publish-lake` / `push-market`のno-loss検査を確認し、cloud copyをpullした後、`source_coverage`の`ok`窓が指定範囲を連続して覆うことを照合する。

```bash
(
  set -e
  read -r -p '確認する開始日 YYYY-MM-DD: ' history_start
  read -r -p '確認する終了日 YYYY-MM-DD: ' history_end
  batch/scripts/pull.sh
  sqlite3 -header stores/market/market.sqlite \
    "SELECT source, coverage_start, coverage_end, record_count, status
     FROM source_coverage
     WHERE NOT (coverage_end < '${history_start}' OR coverage_start > '${history_end}')
     ORDER BY source, coverage_start, coverage_end;"
)
```

### merge が検査するもの

`push-market` / `push-macro`はcloud-only dataや新しいgenerationを失う可能性があれば停止する。cloudとlocalのどちらかを推測で正本にして上書きしない。CAS / generation conflictでは同じ世代をblind retryせず、最新cloud stateからやり直す。local変更後にhydrateして成果を上書きしない。machine bundle途中pushは[部分 push からの復旧](#部分-push-からの復旧)へ進む。内部契約は`baibai_batch.storage.merge_market_store` / `baibai_batch.storage.merge_indicator_store`と対応testsを正本とする。

### 手動で lake を publish する

**前提**: schema cutoverなど、定時batchを待たずにローカルstoreからpublishする必要があること、worktreeが
cleanであること、storeの`lake_store_origin`が開始時current pointerと一致することを確認する。

**実行**:

経路は日次と同じ1つで、毎回全partitionを導出し、serving releaseからorigin束縛と履歴の床だけを読む。
[ローカル発行手順](#ローカルからクラウドを更新する)の`with_publication_lease`を同じshellで定義してから実行する。

```bash
(
  set -e
  with_publication_lease batch/scripts/r2_transfer.sh publish-lake
  uv run baibai-engine lake resolve --mirror stores --bucket baibai-stores --format json
)
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
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  aws s3api delete-object --bucket baibai-stores --key machine-manifest.json \
    --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
)
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

当日実行または過去日実行の片方だけを選ぶ。

```bash
gh workflow run cloud-daily-batch.yml --ref main
```

```bash
read -r -p '対象日 YYYY-MM-DD: ' asof &&
  gh workflow run cloud-daily-batch.yml --ref main -f asof="$asof"
```

dispatch成功後に対象runを確認し、[監視手順](#長時間処理の監視)へ進む。

```bash
gh run list --workflow cloud-daily-batch.yml --limit 10
```

**成功確認**: 対象runの終了通知を受けてconclusionを確認し、Discord通知、store push、serving freshnessを照合する。
対象run IDを固定して[監視手順](#長時間処理の監視)に従う。

**停止と復旧**: exit 1やcoverage不足ではuploadせず原因を直す。exit 3は後続の公開結果を確認し、公開成功なら繰延べた対象だけを復旧する。upload失敗・部分pushは対応する復旧節に従い、全工程をblindに再dispatchしない。

scheduled runはcron日を対象に営業日gateを適用する。queue遅延でJST日付が変わっても対象を翌日へ進めない。coverage不足はpublish前に停止する。欠測はwatchdogで確認し、修正後の手動dispatchは現行mainから行う。

非営業日は既存servingを変更せずskipする。exit 3では後続公開の成否を別に確認する。

## Password rotation

**前提**: 新しい32文字CSPRNG値をpassword manager等で生成する。実値をGit、issue、log、引数、shell historyへ
書かない。

**実行**:

```bash
(cd web/edge && npx wrangler secret put VIEW_PASSWORD)
```

**成功確認**: promptへ新しい値を入力し、旧値が401、新値が認証成功になることを確認する。次の401でbrowserの
旧値はlocalStorageから削除され、password入力画面へ戻る。

**停止と復旧**: 値を表示・引数化しない。失敗時はR2やapplication dataを変更せず、secret設定をやり直す。
Workerの再deployは不要である。

## R2 transferの安全境界

- upload前にPython `sqlite3.backup`でsnapshotを作り、WAL未checkpoint行を含めて`quick_check`する。
- 複数storeのpushは全snapshotの作成・検査を終えてからuploadを始める。3 store一括の`push-machine`はGitHub Actionsと明示的なlocal dailyで使い、pull時の全ETag一致と各PUTの`If-Match`を必須にする。`macro.sqlite` / `market.sqlite`を単独でローカルから進める場合は`push-macro` / `push-market`でcloud copyをmergeし、cloud側の行の取り残しを検出したら停止する。
- machine storeのpushは対象の前世代を`<key>.bak`に1世代保存する。復元時はwriterを停止し、対象bucketとexact `<key>.bak`・`<key>`を人間が確認したうえで、`aws s3api copy-object --cli-read-timeout 300`を使う。`aws s3 cp`へ置換しない。
- machine store の`.bak`は1世代のみで、次のpushで置き換わる。前世代は次のpushで置き換わるため、24時間保持される保証ではない。`baibai.sqlite`だけは`baibai.sqlite.bak-YYYYMMDD`（JST）で日ごとに1世代を残し、直近14世代を超えた分をpush成功後に削除する。machine storeにも再取得できない観測があるため前世代を残し、判断と確認済み事実を持つapplication storeは日別世代を残す。prune は`baibai.sqlite.bak-`配下をlistし、`baibai.sqlite.bak-YYYYMMDD`に一致するkeyだけを完全一致で削除する（prefix削除はしない）。
- 初回seedは既存のstore keyを1件でも検出したら停止し、再seedによるクラウド正本の上書きを許可しない。
- pullは固定4 key以外を受け付けず、全downloadと`quick_check`完了後に置換する。
- application store (`baibai.sqlite`) のpullは`pull-app`だけが行い、bulk pullは触らない。この storeの正本はローカルで、判断はローカルでpublishしてから`push-app`でcloudへ出すため、publish済みで未pushの窓ではローカルがcloudより進んでいる。cloud copyでの置換は再生成できないjudgmentを消すので、`pull-app`はローカルにfileがあれば止める。CIはcheckout直後で`stores/application/`が空なので素通りする。ローカルで意図して置き換えるときは、既存fileを自分で退避してから実行する。
- servingの`views/`は`aws s3 sync --delete`で完全像に合わせる。historyは追記だけで削除しない。
- `views/meta.json`は他のviewとhistoryが全て成功した後に最後にuploadする。
- bucket名は`R2_STORES_BUCKET` / `R2_SERVING_BUCKET`で明示的にoverrideできるが、通常は固定defaultを使う。

machine `.bak`を手動復元する場合は、対応する発行を停止して対象keyを確認してから実行する。

```bash
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  read -r -p '復元するexact machine key: ' store_key
  case "$store_key" in market.sqlite|runs.sqlite|macro.sqlite) ;; *) exit 1 ;; esac
  aws s3api copy-object --bucket baibai-stores --key "$store_key" \
    --copy-source "baibai-stores/${store_key}.bak" --cli-read-timeout 300 \
    --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
)
```

復元後はfreshにpullし、receipt / generationの整合と`quick_check`を確認する。不一致なら分析と公開を止め、[部分 push からの復旧](#部分-push-からの復旧)へ進む。

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

materializeはapplication / machine storeをread-onlyで読み、servingの完全像を作る。schema / storeがcodeと整合しなければpublishせず停止する。`views/`はcurrent image、`history/candidate-views/`は保持期限付き履歴、`views/meta.json`はfreshnessで最後に発行する。routeとJSON名の対応は[Web](../web/README.md)と`web/contracts/routes.json`、Web testsを正本とする。

手動生成:

```bash
uv run python -m baibai_web.materialize --output-dir <dir> [--batch daily|manual] [--repo-root <path>]
```

失敗時はstore schemaとhydrate状態を確認し、生成物を公開しない。

## 日次機械工程 — baibai-batch daily

calendar / coverage → ingest / bootstrap → screening / Review Set → deferred machine work → local export → pruneの順に進む。coverage / screening失敗はfatalで公開しない。一部後段処理はdeferredとなる。

以下は用途別の選択肢であり、続けて全件を実行しない。`.cache/daily-serving`はlocalの生成先である。

通常のlocal実行:

```bash
uv run python -m baibai_batch.jobs.daily --output-dir .cache/daily-serving
```

明示した対象日の再実行:

```bash
read -r -p '対象日 YYYY-MM-DD: ' asof &&
  uv run python -m baibai_batch.jobs.daily --asof "$asof" --output-dir .cache/daily-serving
```

同じ工程のmachine resultとmanifestを取得する場合:

```bash
read -r -p '対象日 YYYY-MM-DD: ' asof &&
  uv run baibai-batch daily --asof "$asof" --output-dir .cache/daily-serving \
    --format json --quiet --manifest-out .cache/daily-manifest.json
```

終了コード:

| exit | ローカル処理の結果 |
| --- | --- |
| 0 | 正常完了、または非営業日skip。skipは新しいexportを作らない |
| 1 | 致命的失敗。今回の結果を正常な公開へ進めない |
| 3 | fresh screeningとlocal exportは作成済みだが、macro refreshやprune等の繰延べ工程が失敗 |

クラウド公開はこの後にL1 release、machine store、serving views、history/freshnessの順で進む。local exportの有無を示す既存output名`published`を、R2公開成功と読み替えない。workflowが失敗しても一部反映が済んでいる場合がある。

coverage / screening / Review Setの失敗はfatal、macro refreshなど一部後段の失敗はdeferred。対象日が非営業日ならskipする。詳細なstepとoptionはpublic `--help`と実run logを確認する。

## local daily analysis — canonical Review SetのResearch Triage

local analysisの操作は[Research Triage skill](../.agents/skills/research-triage/SKILL.md)、statusの意味と入力境界は[runner reference](../docs/reference/analysis-operations.md)が所有する。ここでは同じcommand列と再実行手順を再掲しない。

## 日次結果のDiscord通知

`cloud-daily-batch` は run ごとに終端結果を Discord チャンネル `#batch-runs` へ1件通知する。run単位の通知は、watchdog、workflow履歴、serving freshnessと併せて読む。チャンネルは
code が選ばず repository secret `DISCORD_WEBHOOK_URL` が指す webhook で固定する。workflow 末尾の
単一 step（`if: always()`）が、cancel を含むあらゆる終端状態で1回だけ行う。

message は 4 要素からなる。

1. 見出し行 — label・as-of・失敗した step 名（あれば）。`[FAILED] as-of 2026-08-26 — failed step: hydrate`
2. `executor: GitHub Actions|Local` — 実行環境から判定した固定2値。見出しの直後に必ず1行表示する
3. `🆕 新規 Review Set 入り:` / `👋 Review Set 退出:` の 2 行 — それぞれ銘柄コード順・最大5件（超過時は全件数・表示件数・他の件数を明示）・`<ticker> <社名> E[r]±X.X%`。急落当日の候補と、Review Set から落ちた銘柄を通知だけで拾えるようにするための行である。**export に到達した run では常に出す** — 0 件の日は `なし`、delta view が読めない日は `計測なし（<理由>）` と書く。行が無いことは「0 件」「計測不能」「通知経路の異常」の3つを同時に意味してしまい、読み手が区別できない。非営業日の skip には Review Set が無いので出ない
4. `run:` — GitHub Actions の run URL。所要時間・step ごとの結果・lake release・error の本文はこの run log にある

label は5種。

| label | 意味 |
| --- | --- |
| `[OK]` | batch exit 0、upload および lease release まで成功 |
| `[SKIPPED]` | 非営業日 gate で skip（export なし） |
| `[DEGRADED]` | batch exit 3。screening は publish 済みで、見出しに最初の繰延べ失敗 step（macro / prune / task-reconcile）を表示 |
| `[FAILED]` | batch の致命的失敗（見出しに batch 内の stage 名）、または lease 解放を含む batch 以外の step の失敗（見出しに step 名） |
| `[CANCELLED]` | job が中断された（`timeout-minutes` 超過・手動 cancel） |

**通知の配送失敗は job を赤にしない**（`continue-on-error: true`）。workflowの失敗は公開の完全完了を示さず、途中までの反映がある場合は各stepの結果を確認する。配送失敗は
notify step の stderr に sanitized な理由（未設定・HTTPS 以外・Discord 以外の host・timeout・HTTP
status）で残り、webhook URL・response body は log に出ない。`#batch-runs` が静かな日は
`gh run list --workflow cloud-daily-batch.yml` で run 自体の有無と結論を見る。

### 欠測の検知（`cloud-batch-watchdog`）

daily scheduled runが欠測したときだけ`[MISSING]`を通知する。正常または対象runがin-flightなら通知しない。watchdog自身の失敗をdaily欠測と混同せず、両workflowの実run状態を確認する。過去日を確認する場合:

```bash
gh workflow run cloud-batch-watchdog.yml -f check_date=YYYY-MM-DD
```

### webhook rotation

`DISCORD_WEBHOOK_URL` は GitHub Actions の repository secret で、通知 step だけが読む（job env ・
CLI 引数・log には出ない）。secret の実値を Git・issue・log へ書かない。

1. Discord で `#batch-runs` の webhook を作り直す（または既存 webhook の token を再生成する）。
2. `gh secret set DISCORD_WEBHOOK_URL --repo <owner>/<repo>` で新しい URL を登録する。
3. 次の通常runで通知を確認する。即時確認が明示的に必要な場合だけ通知経路の受入を行い、rotationだけで過去日のscreeningを再生成しない。

旧 webhook は Discord 側で削除するまで有効。

<a id="edinet-research-facts"></a>

## EDINET Research factの初期化・日次差分

**前提**: current codeと整合するmarket store、既存EDINET document inventory、`EDINET_API_KEY`を使う。原典ZIP cacheは`.cache/screening/edinet/xbrl_zips/`である。

未初期化storeだけ、ローカルstaging storeを明示して初期抽出する。初期化済みproductionへ毎回実行しない。

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

`cloud-tradingview-snapshot.yml`が平日15:57 / 18:07 / 20:17 JSTに独立して起動する。GitHub queueによって実開始は遅れ、slot間隔が縮む場合もある。targetは予定時刻ではなくrun開始時のJST当日であり、exact-date masterとTradingViewの両方に同じtarget_dateを渡す。15:30より前の実行はR2やproviderへ接続せず終了する。当日のmasterだけはcalendarを確認して早期取得できる。取得中に日付を跨いだ場合は既存の`skipped_historical_asof`でno-opにする。

最初にfull-universe取得とL1 publishを完了したsnapshotが同日の正本となる。後続slotはL1からhydrateした行数をUniverseと照合し、保存済みならMCP接続前に終了する。失敗時は次slotが最初から取得し直す。TV workflowはremote `market.sqlite`や`machine-manifest.json`を更新せず、L1 releaseだけを永続化する。dailyは独立して実行する。

### 初期設定と認証の復旧

公式MCP SDKで対話認証する。秘密値をGit、L1、workflow artifact、チャットへ出さず、state fileはリポジトリ外に置く。認証URLを開く間はcommandを動かしたままにする。戻り先は`127.0.0.1:8765`であり、接続拒否の場合は待受終了・Windows/WSL間の接続を確認して再実行する。

```bash
(
  set -e
  uv run baibai-engine tradingview authorize \
    --state-file "$HOME/.cache/baibai-loop/tradingview/oauth.json"
  gh secret set TRADINGVIEW_OAUTH_STATE \
    --repo koumatsumoto/baibai-loop \
    --env tradingview-runtime \
    < "$HOME/.cache/baibai-loop/tradingview/oauth.json"
)
```

`TRADINGVIEW_OAUTH_STATE`の正本は`tradingview-runtime` environment secretとする。`TRADINGVIEW_SECRET_WRITER_TOKEN`には、このrepositoryの`Environments: Read and write`権限を持つfine-grained PATをrepository secretとして設定する。通常の`GITHUB_TOKEN`ではSecret更新を代行しない。更新されたOAuth stateは、データ取得より先にenvironment secretへ保存する。書戻し失敗時は取得を止め、対話認証から復旧する。PATの期限切れもこの失敗として扱う。`tradingview-runtime`にはrequired reviewers、wait timer、deployment approvalを設定しない。このenvironmentはrotating stateをjob開始時に読み込むために使う。

runnerでのOAuth smokeはmain refだけに限定する。確認するときは手動CIの`tradingview_oauth_smoke`を指定する。認証更新・Secret保存・2銘柄取得を行い、snapshotは書かない。前run終了後に新しいrunを開始して、更新stateの再利用を確認する。

```bash
gh workflow run ci.yml --ref main -f tradingview_oauth_smoke=true
```

ローカルの認証fileは初期投入用で、runnerが更新した後の最新版ではない。同じstateを複数processで更新したり、古いfileを再投入しない。対話認証による置換も、`cloud-publish`の実行がない時間に行う。

### 保存と再試行

当日の最初のfull-market取得とL1 publish成功がsnapshotとなる。保存済みなら後続slotはprovider接続前にno-op、失敗時は次slotが最初から取得する。過去日の穴を現在値で埋めない。

### TradingView Expectationsの障害診断

TradingView stepのsafe failure line、`TradingView progress:` summaryの順に読む。`chunks_completed / chunks_total`と`chunk_index`で失敗位置を、`provider_elapsed_seconds / elapsed_seconds`と`max_chunk_elapsed_seconds`でprovider待ちとintervalの影響を確認し、`oauth_rotations`で認証更新回数を見る。payload検証失敗では固定の`validation_reason`とcanonicalな`validation_field`を確認する。成功時は最終JSONの`rows / unresolved / normal_nulls`とfirst / last fetched_atも確認する。

- `provider_rate_limit`（429）: 同runを即時rerunせず、manual scanner callも重ねない。当日分のpartial snapshotが保存されていないことを確認し、次のscheduled runまたはprovider quota確認へ進む。
- `auth`: [対話認証](#初期設定と認証の復旧)をやり直す。
- `secret_persistence`: `tradingview-runtime`のEnvironment secretとwriter tokenの権限・期限を確認する。
- `provider_all_missing`: coverage不存在と断定せず、provider全体のdegradationの可能性を確認する。
- `provider_response`: 固定validation reasonを起点に応答契約を調べる。Secret・raw body・symbol一覧をIssueやlogへ転載しない。
- `timeout`: 最後のphase / chunkとprovider時間を確認する。初回実測なしに30分上限を延長しない。
- `storage`: calendar / master / schemaとlocal storeの整合を確認し、修復までpublishしない。


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
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
  aws s3api delete-object --bucket baibai-stores --key market.sqlite --endpoint-url "$endpoint"
  aws s3api delete-object --bucket baibai-stores --key runs.sqlite --endpoint-url "$endpoint"
  aws s3api delete-object --bucket baibai-stores --key macro.sqlite --endpoint-url "$endpoint"
  aws s3api delete-object --bucket baibai-stores --key baibai.sqlite --endpoint-url "$endpoint"
  batch/scripts/seed.sh
)
```

### Workerをdeployする

Actions variable `R2_ACCOUNT_ID`と、対象accountの`Workers Scripts Write`に絞ったsecret `CLOUDFLARE_API_TOKEN`を設定する。production deployは`web.yml`が所有する。

```bash
(
  set -e
  gh workflow run web.yml --ref main
  gh run list --workflow web.yml --limit 3
)
```

対象runを特定し、[監視手順](#長時間処理の監視)で成功を確認する。初回はsecret未設定のAPIが401となる。password managerで生成した32文字のCSPRNG値を、次のpromptへ入力する。

```bash
(cd web/edge && npx wrangler secret put VIEW_PASSWORD)
```

secret設定の成功後、表示を初回生成する。

```bash
(
  set -e
  gh workflow run cloud-materialize.yml --ref main
  gh run list --workflow cloud-materialize.yml --limit 3
)
```

対象materializeの成功後だけ実行する。

```bash
web/edge/scripts/verify-deployment.sh
```

未認証・誤認証が401、正認証で既存keyが成功し不存在keyが404となることを確認する。passwordはpromptだけで扱い、引数・Git・logへ出さない。TTYなしのexit 2は未検証であり、迂回しない。失敗時はworkflow/検査結果の原因を直し、deployやR2操作を重ねない。

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
Wrangler 4.136.1では`redact_query_string`が正式に扱えるため、deployment設定へ
`redact_query_string=true`を固定します。observabilityとLogpushは無効のまま維持します。
logs/tracesを将来有効化する変更は別の作業とし、その際は非秘密のqueryでredactionを受入確認します。
実際の共有tokenで試験しません。real-time logの`wrangler tail`、dashboard Live Logs、Tail Worker等は、
共有tokenを使うrequest中に起動・接続しません。

**実行**: 以下はrepository rootから実行し、promptへ値を入力します。`L1_R2_BASE_URL`は実bucketのjurisdictionに合う
`https://<account-endpoint>/<bucket>`を指定し、末尾にobject key、query、credentialを入れません。
endpointの実値もGitへ残さないためWorker secretとして設定します。

```bash
(
  set -e
  cd web/edge
  npx wrangler secret put READ_ACCESS_TOKEN
  npx wrangler secret put L1_R2_BASE_URL
  npx wrangler secret put L1_R2_ACCESS_KEY_ID
  npx wrangler secret put L1_R2_SECRET_ACCESS_KEY
)
```

**成功確認**: productionのScript Settingsでobservabilityが未設定またはlogs/tracesとも無効、Logpush無効、
tail consumerなしを確認します。認証値・bindingsは出力せず、設定項目だけを確認します。無効状態が異なる場合は
共有URLの利用を始めず、deployment設定と実設定を一致させます。
owner Bearerと共有Bearer/queryで既存JSONの具体値・更新時点を読み、未認証と旧/誤tokenが
401、重複shareが400になることを確認します。`/?share=...`は通常UIを開き、最初のAPI request前にURLから
shareが消え、owner保存値が維持されることを確認します。共有URLを第三者へ一般公開しません。

L1は[固定release手順](../docs/reference/market-lake.md#shared-raw-read)でcurrent → release manifest → dataset manifest → 実Parquetへ進む。対象の分析workspaceで取得・decodeし、同じreleaseの対象値をlocal baselineと照合する。HTTP 200や手動uploadだけでは成功としない。取得・decode・照合が失敗した場合は原因を確認して利用を止める。

**停止と復旧**: shared secret未設定・空は共有accessだけを無効化し、L1接続値の不足はlakeだけ503にします。
上流403/5xx等は安全な502となるので、read-only scopeとendpoint設定を確認します。秘密値や上流error bodyを
logへ出しません。設定失敗時はowner credential、store、daily batchを変更せず設定をやり直します。

**rotationと撤回**: 共有tokenは同じ`(cd web/edge && npx wrangler secret put READ_ACCESS_TOKEN)`で置換し、旧値401・新値成功を確認します。
次の401でfrontendは共有sessionだけを消します。共有撤回は`(cd web/edge && npx wrangler secret delete READ_ACCESS_TOKEN)`です。
L1 credentialは新しいbucket-scoped Object Read only tokenを作り、2つのS3 secretを更新してGETを確認した後に
旧R2 tokenを失効させます。更新途中のlake 502は全設定が揃ってから再確認します。L1公開自体の撤回は
`(cd web/edge && npx wrangler secret delete L1_R2_ACCESS_KEY_ID)`でgatewayを503にし、Cloudflare側でもその専用tokenを失効させます。
既存owner viewとcanonical dataは維持され、secret変更で再deployは不要です。
