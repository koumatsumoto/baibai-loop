# batch — production orchestration と cloud store 運用

本書はmachine処理、store転送、公開と復旧の手順を所有する。domain処理はengine、read modelはwebが担う。以下のcommandはrepository rootのBashから実行し、終了状態と次の操作は各節に従う。CLIの全引数は該当`--help`を参照する。

## 目的から探す

| 作業 | 手順 |
| --- | --- |
| 分析前に保存情報を揃える | [クラウド正本の取得](#クラウド正本をローカルへ取得する) |
| localで更新したmachine情報を反映する | [クラウド更新](#ローカルからクラウドを更新する) |
| 判断・確認済み事実を表示へ反映する | [application反映](#application-db-を反映する) |
| Macro観測・registryを直す | [撤回と退役](#macro観測とregistryの修正) |
| EDINET metricsのsourceを移動・変更する | [revision更新](#edinet-metrics-revision) |
| EDINET原典のResearch factsを揃える | [EDINET Research facts](#edinet-research-facts) |
| 初回の構成を確認する | [Cloudflare / GitHub Actions構成](#cloudflare--github-actions-構成) |


<a id="actions-write-boundary"></a>

## 開発環境と本番書込の実行境界

3系統の分担と開発用接続は[Python foundation](../docs/reference/python-foundation.md#cloud-development-boundary)に従う。
以下のlocal commandはユーザーのローカルPCで使う通常の運用経路である。
データ状態の正本と作業コピーは[architecture](../docs/architecture.md#store-authority)に従う。
`publish.sh`はapplicationの作業コピーを条件付きでクラウドへ反映し、Actionsのmaterializeを起動する。
machine / lake発行、初回seed、独立TradingView取得・公開も既存のlocal経路を維持する。

ChatGPTクラウドから外部データを更新・公開するときは、原則として依頼範囲に含まれる既存Actionsへ委譲する。
各節の前提・main ref・入力・対象storeを確認し、run IDを固定して完了と公開物を検証する。
ユーザーが明示依頼したtask更新は例外として、既存engineのtask編集を取得済み作業コピーへ適用し、
取得ETagへの条件付きPUTでR2正本へ直接反映できる。複数taskは同じコピーで編集し、1回の公開にまとめる。
対象外の行・状態・期限・参照が変わらないこととDB整合性を確認し、公開後は正本を再取得して照合する。
取得後に正本が変わった場合は停止し、無条件再送や自動mergeはしない。
既存backupの上書き・削除はtask更新の依頼に含めない。通常の`push-app`は両方を伴うため、
未許可ならそのまま実行しない。変更前snapshotを保持し、backupを新規保存する場合も既存keyを条件付き作成で保護する。
PUT結果が不明な場合は再取得して適用の有無を調べ、本文を二重追記しない。正本の更新とservingのmaterializeは別に確認する。
それ以外で対応workflowがない操作は、ローカルPCの既存手順へ引き継ぐ。
失敗時は原因と対象runの状態を確認し、直接uploadや重複dispatchで回避しない。
文書変更・PR作成だけを理由に本番workflow、OAuth smoke、R2 acceptanceを起動しない。

writer用R2 credential、GitHub Secret writer token、deploy tokenは下表の実行主体に配置し、
通常のクラウド開発へ渡さない。providerキーによる認可済み読取はPython foundationの接続表に従う。
値をInstall script・Start skill・image snapshot、Git、PR、Issue、log、artifactへ埋め込まず、設定の有無だけを確認する。
文書の変更は既存credentialやruntime権限を変更しない。

## 長時間処理の監視

対象run IDを固定して監視する。hard timeoutはworkflow定義を正本とする。

```bash
gh run watch <RUN_ID> --exit-status --compact --interval 60
```

外部監視がtimeoutでも処理が終了したとは限らないため、対象の状態と現在stepを確認し、activeな処理を重複起動しない。

## Cloudflare / GitHub Actions 構成

`baibai-stores`はL1・machine store・application DBの正本、`baibai-serving`は表示用read modelを保持する。両bucketのPublic Development URLとcustom domainを無効にする。

| bucket | object | owner |
| --- | --- | --- |
| `baibai-stores` | `market.sqlite`（store-local data + `lake_store_origin` metadata） | `cloud-daily-batch` + ローカル`push-market`（cloud copyのmerge後だけupload） |
| `baibai-stores` | `lake/`（lake所有datasetのcanonical L1） | `cloud-daily-batch`と`cloud-tradingview-snapshot`の`publish-lake` + ローカル`r2_transfer.sh publish-lake` |
| `baibai-stores` | `runs.sqlite` | `cloud-daily-batch` + 明示的なローカルdailyの`push-machine` |
| `baibai-stores` | `macro.sqlite` | `cloud-daily-batch`（rolling窓）+ ローカル`push-macro`（全履歴。cloud copyのmerge後だけupload） |
| `baibai-stores` | `baibai.sqlite` | 取得世代への条件付き更新（実行環境の分担は前節） |
| `baibai-serving` | `views/*.json` | GitHub Actions materialize |
| `baibai-serving` | `history/candidate-views/<asof>.json` | 日次batch、R2 lifecycleで31日後に削除 |

R2 lifecycle ruleは`history/candidate-views/`だけに設定する。bucket全体へ設定すると`views/meta.json`まで期限で消え、欠落を検知できない。Review Set membershipの長期履歴は保持しない。

`cloud-materialize`・`cloud-daily-batch`・`cloud-tradingview-snapshot`はGitHubの`cloud-publish`を共有する。GitHubとローカルのpublication競合は[ローカル発行手順](#ローカルからクラウドを更新する)のleaseで止める。servingへのwriteはGitHub Actionsだけが行う。application更新の取得世代・競合処理は[application反映](#application-db-を反映する)に従う。

資格情報はprincipalごとに分ける。

| principal | 設定 | scope |
| --- | --- | --- |
| GitHub Actions | variable `R2_ACCOUNT_ID`、secrets `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / provider 3本 | 必要なtransfer/provider stepだけ、stores + serving read-write |
| GitHub Actions（通知） | secret `DISCORD_WEBHOOK_URL` | `cloud-daily-batch` の通知 step と `cloud-batch-watchdog` の警報 step のみ（job env に出さない） |
| GitHub Actions（Worker deploy） | variable `R2_ACCOUNT_ID`、secret `CLOUDFLARE_API_TOKEN` | 対象accountの`Workers Scripts Write`、`web`のdeploy stepのみ |
| ローカルPCの`.env` | `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | stores read-write。通常のクラウド開発へ持ち込まない |
| Wrangler OAuth | `wrangler login` | bucket初期設定、Worker secretの手動設定 |
| Worker secret（閲覧認証） | `VIEW_PASSWORD` / `READ_ACCESS_TOKEN` | Worker runtimeだけ、ownerと共有を分離 |
| Worker secret（L1接続） | `L1_R2_BASE_URL` / `L1_R2_ACCESS_KEY_ID` / `L1_R2_SECRET_ACCESS_KEY` | `baibai-stores`だけのObject Read only、publisher credentialと分離 |

R2 S3 endpointは`https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`からscriptが組み立てる。credential、password、endpointの実値をGit、issue、logへ書かない。

servingは各workflowのsource store取得・materializeが成功した後だけ発行する。dailyではstore pushの成功も必要である。前提工程が失敗した場合は表示更新を止め、`views/meta.json`を最後に発行する。

provider secretは`JQUANTS_API_KEY` / `ESTAT_APP_ID` / `EDINET_API_KEY`。JPX規制情報の公開URLは
[JPX source設定](../engine/src/baibai_engine/market/jpx_sources.py)の固定値を使い、環境変数では指定しない。
`bootstrap-cache`は`universe.required_jpx_flags`の必要sourceを取得し、取得・検証に失敗した場合は
machine stores / serving uploadを停止する。


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

**成功確認**: `pull-machine`はreceipt / ETagでmarket / runs / macroを同じ確認済みbundleとして取得し、`hydrate-market`は開始時のcurrent L1 releaseを固定する。途中でpointerが動けばvisible storeを置換せず停止する。ただしmachine bundleとL1 pointerを同一transactionでは固定しない。receipt / generation / pointerの競合や検査失敗後のlocal stateは分析に使わず、原因を確認してfreshにpullし直す。`baibai.sqlite`はbulk pullしない。applicationの取得は[application反映](#application-db-を反映する)の`pull-app`前提に従う。

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

EDINET extractor revisionの更新では、[専用cutover手順](#edinet-metrics-revision)に従ってlease取得後にmainへ切り替える。market SQLite 28→29では、[専用cutover手順](#market-28-to-29)に従いwriterを抑止し、旧codeでfresh pull / hydrate後にlease内でmainへ切り替える。それ以外は変更codeをmainへ反映し、対象storeと変更内容を確認する。marketはlake所有tableを編集する前に現行releaseへhydrateする。変更後の再hydrateはlocal変更を上書きするため行わない。

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

失敗時もreleaseを試みるため、上の関数内では`set -e`を使わない。release失敗時はhandleを残して原因を確認する。busyならowner・purpose・expiryと該当run/processの生存を確認し、blind retryしない。expired leaseのみ次のacquireが条件付きで引き継ぐ。L1とmachine storeのCAS / receiptも整合性を検査し、servingはGitHub Actionsだけが書く。applicationの公開は取得世代への条件付きPUTで競合を止める。

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

**前提**: 空の作業領域へ`batch/scripts/r2_transfer.sh pull-app`でクラウド正本を取得し、取得したDBと`stores/.r2-generations/baibai.sqlite.etag`を一組として保持する（世代directoryを変更した場合も同じ組を使う）。取得済み作業コピーで判断・確認済み事実・運用状態を更新し、schema cutoverを含むcodeはmainへ入れる。
既存fileへのpullは未公開変更を守るため停止する。コピー更新が必要ならwriterを止め、`uv run baibai-engine db backup`でWAL込みのsnapshotを検証して退避した後、空の作業領域へ取得し直す。DBだけを別世代へ差し替えてsidecarを流用しない。

**実行**:

```bash
batch/scripts/publish.sh
```

引数なしでだけ実行する。`-h` / `--help`は外部書き込みを行わずusageを表示し、その他の引数はupload前に拒否する。
引数なしでは最初に`position ledger`を実行し、ledgerの整合性を確認する。保有価格の欠損・staleや権利単位の未確認は時価未評価として表示し、uploadを止めない。価格の読み取りは[`portfolio-ledger.md`](../docs/reference/portfolio-ledger.md#market-price-and-tax)を正本とし、配信のための台帳への価格転記は要求しない。

このscriptは取得ETagの一致を確認し、backupのsource条件とPUTの`If-Match`で競合を止めて`baibai.sqlite`のconsistent snapshotだけをstoresへ送り、`cloud-materialize`をdispatchする。servingへの直接writeは行わない。PUT応答のETagを次の更新基準へ保存する。未知世代ではuploadせず停止し、現在のHEADだけで取得世代を補わない。

**成功確認**: `publish.sh`とdispatchされた`cloud-materialize`が成功し、表示のas-ofと対象judgmentのIDを確認する。
Screeningの最新表示は、表示中の`review_set_id / run_revision_id`に束縛されたResearch TriageとCAAだけを載せる。
同日でも後発のScreening Runが公開されると、先行runの判断は最新一覧の対象から外れる。
公開済みCAAの反映は、そのimmutable IDのAssessment詳細と参照先Thesisで確認する。
最新一覧への非掲載だけを理由に、既存ResearchのTriage bindingを付け替えない。
後発Review SetのTriageを進める場合は、完了済みmachine bundleを取得して`research-triage`の通常手順で扱う。

**停止と復旧**: 世代競合では未公開変更を退避し、最新正本を取得して必要な変更をapplication serviceで再適用する。自動mergeや無条件再uploadはしない。PUT成功後の世代記録・backup整理・dispatchの失敗は部分成功として確認し、未反映と決めつけて旧DBを再送しない。ledger preflightが失敗した場合はstoreをuploadせず停止する。exportはstoreのschemaがcodeと一致しない間、viewを1件も書かずexit 1で停止する。
schemaを一致させてから再実行する。application更新の表示には、そのsnapshot uploadとmaterializeの成功が必要である。cloud dailyによる独立した機械view更新とは区別する。

application DBはcurrent schemaだけを開き、materializeとOwner MCPは取得済みコピーをread-onlyで読む。schemaを上げるPRは、
source versionを限定したtemporary one-shot toolとexact commandを用意し、main merge後のattended cutover・検証・
cloud反映が終わるまで保持する。完了証拠を残してから同じdeliveryのcleanupで削除する。現在のtreeに汎用cutover
commandはないため、version不一致を見つけたoperatorは手書きSQLで進めず、schema変更issueへtoolを再構築する。

### application DB を復元する

application DBは判断とledgerを保持するため再生成しない。通常のコピー更新は前節の`pull-app`を使う。この節は、明示的に選んだ過去snapshotへクラウド正本を戻す復旧である。
全writerを停止し、未公開の作業コピーをconsistent snapshotで退避する。次に空の作業領域へ`pull-app`し、復旧で置き換える現在のクラウド世代とDBを確保してから以下を同じshell sessionで実行する。現在の正本が取得・検査できなければ通常更新経路での復旧を止める。key消失は[初回seed](#storeをseedする)の既存key確認を含む別の復旧判断に戻し、無条件PUTへ迂回しない。
復旧候補はR2の日別世代、または出所・時点を照合できるローカルcheckpointから選ぶ。ローカル候補を自動的に正本より新しいとみなさない。

**1. 復元前backup。** 取得した現在の正本のWAL込みconsistent snapshotを作る。backup失敗なら復旧へ進まない。

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

R2候補を選ぶ場合は上の選択を行わず、R2の一覧を確認する。

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

**4. 配置と照合。** 検査合格とwriter停止を確認後、copy成功前に現在の作業コピーを動かさない。ここだけは明示的な復旧として、取得した現在のクラウド世代を保持したまま検査済み候補へ置換する。

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
- application store (`baibai.sqlite`) のpullは`pull-app`だけが行い、bulk pullは触らない。未公開の変更を失わないよう、`pull-app`はローカルにfileがあれば止める。CIはcheckout直後で`stores/application/`が空なので素通りする。作業コピーの更新・競合・復旧は[application反映](#application-db-を反映する)に従う。
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

<a id="edinet-metrics-revision"></a>

## EDINET metricsのextractor revision更新

**対象**: `market/edinet_metrics`のmanifestに含まれるsourceのpathまたはbytesが変わり、次回writerが旧revisionのbaselineを再利用できない場合。Research factsの手動revisionとは別であり、過去snapshot全体を書き換えない。

1. **lease取得前**に、対象PRのレビュー・CI、SQLite backup上の再抽出と比較、不足ZIPの取得を終える。下のcopy検証例を使い、exit 0、新revision、行数・抽出値・隔離理由・integrityを確認する。この段階ではmainもproduction storeも切り替えない。
2. 下記3 workflowのqueued / in_progressと、他のlocal operatorによる発行がないことを確認する。daily・TradingViewの次回scheduled slotを跨がずreadbackまで完了できる時間帯を選び、manual materializeもcutover中は開始しない。予定時刻は[daily](../.github/workflows/cloud-daily-batch.yml)・[TradingView](../.github/workflows/cloud-tradingview-snapshot.yml)の現行scheduleを参照する。確認後にoperator leaseを取得し、busyや取得失敗ならmainへ進まない。同じleaseが競合書込を防いでも、後着のworkflowはLeaseBusyで失敗するため、slotを落とさない時間調整も必要である。
3. **lease取得成功後**、確認済みheadのPRをmainへmergeし、そのmainをlocalへ反映する。以降の取得・抽出・転送を同じcodeで行う。
4. **同じlease内**で最新cloud storeを改めて`pull-machine`し、`hydrate-market`で現行Lake releaseへ揃える。実行時点の最新`edinet_metrics.asof_date`とrevisionを再確認する。準備時のcopyをproductionへそのまま戻さず、freshなstoreのbackupを比較元として保存する。
5. その最新as-ofを明示して通常storeへ再抽出し、exit 0、新revision、行数、revision以外の全列、隔離・quality理由、integrityを比較元と照合する。再抽出後のhydrateで成果を上書きしない。
6. **leaseを保持したまま**`publish-lake`、`push-market`の順に実行し、下記の固定release・cloud copyのreadbackを完了する。最後のpublishだけを`with_publication_lease`で囲み直したり、途中でreleaseしたりしない。
7. code/store/readbackの整合を確認してから、取得時と同じhandleでleaseをreleaseする。

手順2の確認（いずれかにqueued / in_progressがあれば完了後にやり直す）:

```bash
gh run list --workflow cloud-daily-batch.yml --limit 10
gh run list --workflow cloud-tradingview-snapshot.yml --limit 10
gh run list --workflow cloud-materialize.yml --limit 10
```

cutoverの外枠は既存の明示的なacquire/releaseを使う。以下の取得commandが成功してから手順3へ進む。handleを別shellへ引き継ぐ場合はpathを保持する。

```bash
cutover_dir="$(mktemp -d "${TMPDIR:-/tmp}/edinet-cutover.XXXXXX")"
cutover_handle="$cutover_dir/lease.json"
uv run python -m baibai_batch.storage.publication_lease acquire --purpose operator --handle "$cutover_handle" || exit 1
```

copy検証の実行例（手順1、repository root、意味を変えない移動の場合）。一時directoryは結果確認用に残す。既定はcache-only。原典不足の場合だけ`EDINET_API_KEY`を用意して`cache_only=False`で再実行すると、既存cacheを再利用し不足分を取得する。抽出結果の成功は終了コードと新revisionの両方で判定する。

```bash
uv run python - <<'PY'
from contextlib import closing
from datetime import date
from pathlib import Path
import os
import sqlite3
import tempfile

from baibai_engine.foundation.env import load_project_env
from baibai_engine.market.edinet_metrics.revision import compute_extractor_revision
from baibai_engine.market.edinet_metrics.service import extract_edinet_metrics_command
from baibai_engine.market.providers.edinet import EDINETProvider

load_project_env()
source = Path('stores/market/market.sqlite').resolve()
staged = Path(tempfile.mkdtemp(prefix='edinet-revision-')) / 'market.sqlite'
with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
    with closing(sqlite3.connect(staged)) as dst:
        src.backup(dst)
        latest = dst.execute('SELECT max(asof_date) FROM edinet_metrics').fetchone()[0]
        if latest is None:
            raise SystemExit('最新baselineなし。初期抽出として別途対象を決める。')
        columns = [row[1] for row in dst.execute('PRAGMA table_info(edinet_metrics)')
                   if row[1] != 'extractor_revision']
        query = ('SELECT ' + ','.join(columns)
                 + ' FROM edinet_metrics WHERE asof_date=? ORDER BY ticker')
        before = dst.execute(query, (latest,)).fetchall()
print(f'asof={latest} rows={len(before)} staged={staged}', flush=True)
provider = EDINETProvider(
    os.environ.get('EDINET_API_KEY'), Path('.cache/screening'),
    sqlite_path=staged, cache_only=True,
)
result = extract_edinet_metrics_command(
    asof_date=date.fromisoformat(latest), lookback_days=540,
    provider=provider, sqlite_path=staged,
)
if result != 0:
    raise SystemExit(result)  # 旧行の保持を成功と誤認しない。
with closing(sqlite3.connect(staged.as_uri() + '?mode=ro', uri=True)) as checked:
    after = checked.execute(query, (latest,)).fetchall()
    revisions = checked.execute(
        'SELECT DISTINCT extractor_revision FROM edinet_metrics WHERE asof_date=?',
        (latest,),
    ).fetchall()
    integrity = checked.execute('PRAGMA integrity_check').fetchall()
assert revisions == [(compute_extractor_revision(),)], revisions
assert before == after, 'revision以外の値が変化。公開せず差分を調べる。'
assert integrity == [('ok',)], integrity
print(f'PASS rows={len(after)} revision={revisions[0][0]} integrity=ok')
PY
```

手順5で通常storeを更新するcommand（lease保持中）:

```bash
cutover_asof=YYYY-MM-DD  # 手順4で再確認したlatest baselineの日付
uv run baibai-engine screening extract-edinet-metrics --asof "$cutover_asof" || exit 1
```

手順6の発行とreadback（同じshell・leaseを保持）。publishのstdout JSONを保存し、そのrelease ID/hashを固定する。`push-market`はlocalのstore-local tableをcloudとmergeするため、比較元はpush成功後のlocal storeとする。`pull-market`は通常storeを置換するのでreadbackには使わない。以下は既存R2 adapterで一時storeへ取得し、既存CLIで固定releaseをhydrateする。

```bash
batch/scripts/r2_transfer.sh publish-lake > "$cutover_dir/publish.json" || exit 1
batch/scripts/r2_transfer.sh push-market || exit 1
uv run python - "$cutover_dir" "$cutover_asof" <<'PY' || exit 1
from contextlib import closing
from datetime import date
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys

from baibai_batch.storage.lake_publish import Boto3R2Store
from baibai_engine.foundation.env import load_project_env
from baibai_engine.market.edinet_metrics.revision import compute_extractor_revision

load_project_env()
work = Path(sys.argv[1]).resolve()
asof = date.fromisoformat(sys.argv[2]).isoformat()
report = json.loads((work / 'publish.json').read_text())
readback = work / 'readback.sqlite'
assert not readback.exists(), '既存readbackを上書きせず、新しい一時directoryで確認する。'
local = Path('stores/market/market.sqlite').resolve()
metrics_sql = 'SELECT * FROM edinet_metrics WHERE asof_date=? ORDER BY ticker'
coverage_sql = 'SELECT * FROM source_coverage ORDER BY source, coverage_key'
with closing(sqlite3.connect(local.as_uri() + '?mode=ro', uri=True)) as source:
    expected = source.execute(metrics_sql, (asof,)).fetchall()
    expected_coverage = source.execute(coverage_sql).fetchall()
assert expected, '対象as-ofのlocal baselineなし。'
bucket = os.environ.get('R2_STORES_BUCKET', 'baibai-stores')
remote = Boto3R2Store(bucket=bucket)
generation = remote.head('market.sqlite')
assert generation is not None, 'cloud market storeなし。'
remote.download_file('market.sqlite', readback, expect_bytes=generation.size)
assert remote.head('market.sqlite') == generation, '取得中にcloud generationが変化。'
with closing(sqlite3.connect(readback.as_uri() + '?mode=ro', uri=True)) as cloud:
    cloud_coverage = cloud.execute(coverage_sql).fetchall()
assert cloud_coverage == expected_coverage, 'push後のstore-local coverageが不一致。'
subprocess.run([
    'uv', 'run', 'baibai-engine', 'lake', 'hydrate',
    '--mirror', str(work / 'mirror'), '--store', str(readback), '--bucket', bucket,
    '--release', report['release_id'],
    '--manifest-sha256', report['release_manifest_sha256'],
], check=True)
with closing(sqlite3.connect(readback.as_uri() + '?mode=ro', uri=True)) as checked:
    actual = checked.execute(metrics_sql, (asof,)).fetchall()
    revisions = checked.execute(
        'SELECT DISTINCT extractor_revision FROM edinet_metrics WHERE asof_date=?',
        (asof,),
    ).fetchall()
    assert actual == expected, '固定releaseの行数・revision・抽出値が不一致。'
    assert revisions == [(compute_extractor_revision(),)], 'current codeとrevisionが不一致。'
    assert checked.execute(coverage_sql).fetchall() == cloud_coverage, 'hydrateでcoverageが変化。'
    coverage = checked.execute(
        "SELECT record_count, status, error FROM source_coverage WHERE source='edinet_metrics' "
        'AND coverage_start=? AND coverage_end=?', (asof, asof),
    ).fetchall()
    assert coverage == [(len(actual), 'ok', None)], '対象baselineのcoverageが正常でない。'
    assert checked.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
print(f'PASS asof={asof} rows={len(actual)} revision={revisions[0][0]} coverage=ok integrity=ok')
print(f"release={report['release_id']} manifest_sha256={report['release_manifest_sha256']}")
PY
```

**成功確認**: 上記がexit 0かつPASSとなり、固定releaseの対象as-of全行（revisionを含む全列）、cloudのstore-local coverage、integrityが一致してからreleaseする。途中失敗や不一致なら次へ進まず、下記の停止・復旧へ進む。code反映、L1発行、store反映、readbackが揃うまで完了としない。日次batchにrevision移行の再構築を代行させない。

手順7のrelease（readback成功後）:

```bash
uv run python -m baibai_batch.storage.publication_lease release --handle "$cutover_handle" || exit 1
```

**停止・復旧**: leaseのTTLは固定120分であり、無期限の保護ではない。main切替後に失敗した場合、新codeと旧revision storeのままwriterを再開しない。期限内に整合を回復できない場合はdaily等のwriter停止を確認し、未完了のcutoverとして引き継ぐ。lease失効後の処理続行やblind retryをせず、再開時は再取得したlease内でfreshなcloud stateから確認する。release失敗時はhandleを保持する。認証・rate limit・不足ZIP・破損は対象を限定して直し、同じas-ofで再実行する。値の予期しない変化、generation競合、旧snapshotの消失があれば公開を止める。revisionを旧hashへ固定して再利用可能に見せない。

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

<a id="retail-dynamics"></a>

## 公開broker観測の限定取得

手動の限定入口は`baibai-engine retail-dynamics`または`baibai-batch retail-dynamics`である。取得は公開ページの通常HTTP経路を使い、明示した少数銘柄を直列で読み、前HTTP完了から次開始まで最低2秒を空ける。待機もrequest数・wall-clockの上限に含める。403・429は当該sourceの残りを止め、別sourceの確定した成功は保持する。スケジュール、publication lease、R2更新、UI反映はこの入口に含まれない。

取得先はprivateなローカルstageとし、ログへ値やHTMLを出さない。既存の出力先は通信前に拒否し、保存時の競合も上書きしない。部分成功をimportする前に再取得する場合も必ず別名を選ぶ。例の保存先directoryと現行schemaの隔離market作業コピーをあらかじめ用意し、repositoryや公開Actions artifactへstageを置かない。

```bash
uv run baibai-engine retail-dynamics collect --output /tmp/private-retail/observations.json
uv run baibai-engine retail-dynamics import --input /tmp/private-retail/observations.json --db /tmp/private-retail/market.sqlite
```

import対象は明示した既存の隔離作業コピーであり、current market schemaに整合させる。CLIは新規DBの初期化も旧schemaの移行も行わない。成功時はstage件数・新規挿入件数を確認し、再投入では新規挿入0を確認する。partialまたは失敗時はsafe summaryで未試行・HTTP障害・parse障害を正常nullと区別し、成功stageを保持する。schema・破損・保存障害は取得欠測へ置き換えず、コピーの整合を直してから再投入する。

本番導入は下記のcode/store切替を一組にする。後続の独立workflowでは取得をlease外で終え、公開段階で最新クラウド世代をpull/hydrateしてから確定stageを適用する。公開権限、private handoff、料金・所要時間の実測は定期実行前に別途受入する。保持・照会は[market lake](../docs/reference/market-lake.md#retail-dynamics-query)を参照する。

### 固定コホートの手動受入と再開

`cloud-retail-dynamics.yml`は定期実行を持たない、main限定の手動受入入口である。上の限定collectorと分け、保存済み全普通株の取得をpublication lease外で行う。初回に最新L1のreleaseを固定し、指定したexact-dateの保存済みmasterに必要な年partitionだけを読み、固定universeを選ぶ。この計画段階でmarket.sqliteや他datasetを取得・hydrateしない。新しいmasterをproviderから取得せず、普通株のproduction floor、分類、master日付・release bindingが不足する場合は停止する。planはmaster日付・release・ticker集合・collector policyに固定され、同じbatch IDの再開では別universeへ置換しない。

[GitHub-hosted runnerの1ジョブ6時間上限](https://docs.github.com/en/actions/reference/limits)に収めるため、既定50銘柄のコホートを直列のmatrix jobへ分ける。workflow全体の取得専用concurrencyで別batch ID・再実行も直列化する。各pairは応答後の最低2秒間隔を維持し、通信前後のcheckpointを含む実wall-clock上限を持つ。取得jobは4時間のsoft budgetに余裕を残して次pairを開始せず、4時間30分のprocess timeout、5時間のjob上限で打ち切る。HTTP timeoutの積だけを総所要時間の保証にしない。

確定したtyped観測と安全な成否は、既存`baibai-stores`の`coordination/retail-dynamics/<batch_id>/`へ条件付き保存してjob間で引き継ぐ。raw HTML・認証情報は保存せず、値をlog・Git・Actions artifactへ出さない。各HTTPの前にinflightを永続化し、結果の確定時に同じ世代を条件付き更新する。成功・正常null・確定した失敗は再開時に取得しない。403・429、CAS不一致、保存結果が不明なPUT、inflightのままの中断は日次run全体を停止し、自動再試行や経路変更をしない。

**受入の前提**: PR1のcode/store切替とreadbackが完了し、PR2のレビュー・CI・private handoff・費用と所要時間の受入が成功していること。新credential・serverは不要だが、この文書や開発テストは実R2書込・workflow dispatch・定期有効化を実行した証拠ではない。operatorは保存済みmasterの日付と再開用batch IDを選び、次をmainへdispatchする。

```bash
gh workflow run cloud-retail-dynamics.yml --ref main \
  -f batch_id="$retail_batch_id" -f master_asof="$retail_master_asof"
```

**成功確認**: 全固定cohort・pairのaccounting、未試行・uncertainがないこと、typed観測のsuccess・normalnull、parse/HTTP失敗を別に確認する。全pairの試行完了は、全銘柄・全指標に値があることを意味しない。publisherだけ既存`cloud-publish` concurrencyとpublication leaseへ入り、最新cloudをfresh pull/hydrateしてから原受領時刻のstageを適用する。欠落cohort、期限切れ・置換lease、古いhydrate originは公開前に拒否し、長いexport・upload後のpointer切替直前にもleaseを確認する。既存L1のpointer CASは世代競合を拒否する。leaseとpointerは別keyであり、この直前確認は両者の原子的なfencingではない。通常workflowはjob全体110分を既存lease TTL120分より短くする。

公開後は空cacheから固定releaseのretail 2 datasetだけを読み、原観測の全列・両tableの全受領履歴digestを照合して`readback: verified`を記録する。無変更の年/月partitionはmanifest object identity一致で確認し、新規upload objectの全byte読戻しは既存publisherが担う。無関係な市場Parquetや別SQLiteをこの独立readback用に再構築しない。同じstageの再投入は追加row 0である。JST日付は実受領UTCから導出し、長いrunや再開で日を跨いでも元の日付へ付け替えない。転送reportではcurrent reachable容量、retail読戻し対象容量、publish層のTransferReportを分け、lease/checkpoint/pull/hydrate等を含むrun総計と混同しない。

終端の独立`notify` jobは既存repository secret `DISCORD_WEBHOOK_URL`が指す宛先へrun単位で1件通知する。成功・既知の取得/parse失敗を含む公開・明示的な部分公開・以前の完全/部分公開・取得停止/未完了・公開確認失敗・取消を区別し、確認済みの固定pair数・typed数・既知失敗数・全評価値null数・不確定数・未試行数・追加row数と実行linkだけを載せる。matrix失敗時や旧terminalの未知集計値は不明とし、通知のためにR2へ読み直さない。銘柄別の値・raw HTML・private batch ID・認証情報・error本文は通知しない。secretは通知stepのenvだけへ渡し、追加のsecret名やcloudの`.env`は使わない。宛先が未登録なら既存名をrepositoryのActions secretに設定する。配送失敗はsanitizedな理由をstderrへ残し、batch結果を変更しない。job取消やrunner停止で通知できない場合もあるため、workflow履歴も確認する。このコード追加は実配送の確認や本番dispatchを意味しない。

**停止・再開**: soft budgetによる未試行だけなら、同じbatch IDとmaster日付で再dispatchし、確定pairを除いた残りを取得する。欠落cohort・cancelされたmatrixも同じ固定planから再開する。inflight・明示拒否・保存競合がある場合はaudit summaryと原因を確認し、同batchの取得を継続しない。公開失敗後は次回も最新正本をhydrateし、古い作業DBを上書き公開しない。公開済みでもreadback失敗なら完了ではなく、confirmed stageから再確認する。

停止したbatchの確定済み観測を活用して終了するとoperatorが明示的に選ぶ場合だけ、同じ保存済みbatch ID/master日付に`operation=publish_confirmed_partial`を指定する。通常の`collect_and_publish`が要求する全cohort・pair完了条件は緩めない。この復旧入口は取得jobを実行せず、保存済みplanがなければ拒否する。最新正本のfresh hydrateと同じlease/readback契約の下で、確定済みtyped観測だけを公開する。typed観測が0なら拒否する。欠落cohortは集計上未試行であり、未試行・uncertain・haltをnormalnullや確定失敗へ書き換えず、元checkpointも保持する。

部分公開のterminalには固定plan、公開release、読戻し完了、5状態の件数、元のJST受信日/source別typed件数を記録する。部分公開は全件完了と区別して同batchを閉じる。完全/部分どちらの検証済みterminalも次のplanで認識し、取得・公開jobをskipする。直接CLIを再実行してもcheckpointや作業DB、lease、L1を読み直さず`previously_verified`と追加row 0を返す。これは過去の検証結果であり、今回の新規公開・現在世代の再検証を意味しない。旧terminalの未知件数を0で補わない。長いbatchで受信日が跨ぐ場合、日別coverageを分け、全batchの試行完了を単一日の全銘柄coverageとしない。

private handoffの操作数も費用確認に含める。各銘柄でMonex1HTTP/Rakuten2HTTPが正常に終わる前提なら、各HTTP前のmarker3回とpair確定2回でcheckpoint PUTは5回、3,692銘柄で18,460回となる（plan/terminal PUTは別）。各cohortの取得前とCLI終了判定時に全checkpointを再走査し、存在するprivate objectはHEAD→GET→HEAD、PUT応答の確認にもHEADを使う。これらは既存TransferReportの対象外であり、SDK内部retryやwire bytesも別である。実providerの分岐・失敗・途中停止・保存済みterminalによるskipで操作数は変わる。R2全体の容量・Class A/B操作・料金を追加rowやupload bytesだけから推定しない。

初回の74cohortを順番に正常完走する同じ前提では、acquire前後のcheckpoint既存読取5,476回、publish時74回、plan既存読取75回からGETは5,625回となる。plan/terminal PUTと応答確認、存在しないcheckpoint/terminalのHEADも含むprivate handoff小計はPUT18,462・GET5,625・HEAD35,268、合計59,355論理操作である。これはlease、L1、market.sqliteのpull/hydrate、SDK retry等を除いた理論小計で、run全体の実測や課金額ではない。部分復旧・保存済みplanの再開・既公開skipは別の操作数となる。

受領履歴はL1で保持する。取得checkpointは引継ぎ用であり、検証用のraw応答やSQLite複製と同じ保存目的ではない。公開・readback完了後の整理では、固定planと小さなpublished manifestを残し、同batch IDを再取得へ戻さない。raw HTMLや重複検証copyの無期限保持は採用しない。正規化stageの容量だけをR2全体の容量・料金とみなさず、checkpointのClass A/B、既存全datasetのL1再exportとobject再利用、保存履歴を別に実測する。

<a id="market-28-to-29"></a>

### market SQLite 28→29のattended cutover

これはschema変更PRと同じdeliveryで行う一度限りの操作である。temporary tool `tools.cutover.market_28_to_29`はsource version 28だけを受け付け、read-onlyのconsistent backupを私有一時directory内で29へ変換・検証し、閉じた単独DBを上書きなしで別fileへ配置する。sourceの全table・index・行、`source_coverage`、`tse_capital_policy_snapshots`、`lake_store_origin`を照合し、新2tableは空で追加する。既存出力とsidecar、別version、shape差、破損、保持不一致は拒否する。runtime・dailyからは呼ばない。クラウド反映とreadbackの証拠を残すまでtoolを保持し、その後同じdeliveryのcleanupで削除する。

**事前リハーサル**: 旧codeで取得・hydrateした28の隔離コピーに対し、レビュー済みPR headで次を実行する。元fileは残す。結果の旧table件数・digest・schema保持、integrity/FK、version29、新table0件を確認する。このコピーを後日の本番反映に流用しない。

```bash
uv run python -m tools.cutover.market_28_to_29 --source /tmp/private-retail/market28.sqlite --output /tmp/private-retail/market29.sqlite
```

**本番切替の前提**: PRレビュー・CIとリハーサルが成功していること。まだ28を読む専用operator checkoutを使う。daily・TradingView・materializeの元の有効状態を記録し、cutover中は次の無効化で新規起動を抑止する。他のlocal writerとmanual dispatchも止め、queued / in_progressが完了したことを確認する。watchdogはread-only通知でありwriterを起動しないが、切替中の通知を理由にmanual rerunしない。writerが残る、停止を確認できない、leaseがbusyならmainを切り替えない。この準備と本番書込はoperatorのattended作業であり、開発検証では実行しない。

```bash
gh workflow disable cloud-daily-batch.yml
gh workflow disable cloud-tradingview-snapshot.yml
gh workflow disable cloud-materialize.yml
gh run list --workflow cloud-daily-batch.yml --limit 10
gh run list --workflow cloud-tradingview-snapshot.yml --limit 10
gh run list --workflow cloud-materialize.yml --limit 10
```

1. **旧28 codeのまま**operator leaseを取得し、同じhandleをcutover終了まで保持する。最新cloudの3 storeを`pull-machine`し、`hydrate-market`でcurrent L1を固定する。receipt / ETagを保持し、marketが28であること、integrity、固定releaseを確認する。old marketのconsistent backupをprivateに残す。これが実際の変換元である。
2. **新規writer抑止・lease内**でレビュー済みheadをmainへmergeし、同じoperator checkoutをそのmainへ切り替える。下のone-shotで別fileを作り、成功summaryを確認してcanonical market copyを置換する。source/targetに接続する全processを閉じ、canonical pathにWAL/SHMが残るなら停止して整合snapshotからやり直す。古いsidecarを新fileへ付けない。fresh pull時の3 storeの世代記録、runs、macroは変えない。
3. 同じlease内で`push-machine`を使う。3 storeのpull世代一致と条件付きPUTを既存経路が検査し、marketのupload用snapshotだけを同じL1へdehydrateする。**このschema切替に`push-market`を使わない**。その入口はremote28とlocal29を同一schemaでmergeしようとして拒否する。broker stageは適用せず、`publish-lake`も行わないので、L1のcurrent pointerと全既存履歴は変えない。
4. 新codeでfresh `pull-machine` / `hydrate-market`し、receipt一致、market29、integrity、同じL1 release、store-local件数/digest保持、旧dataset全履歴、新table0件をreadback確認する。runtime reader・既存batch preflightがschema不一致を出さないことを確認する。
5. code/store/readbackがそろった後にleaseをreleaseし、元々有効だったworkflowとlocal writerだけを再開する。次の通常daily/TradingView/materializeの成功を確認してcutover完了を記録する。確認前にwriterを再開しない。

操作例は同じshellのoperator checkout rootから行う。`cutover_dir`とhandleは失敗時も保持する。lease取得は旧codeで実行し、one-shot以降はmainへ切替済みのcodeで実行する。

```bash
cutover_dir="$(mktemp -d "${TMPDIR:-/tmp}/market-28-29.XXXXXX")"
cutover_handle="$cutover_dir/lease.json"
uv run python -m baibai_batch.storage.publication_lease acquire --purpose operator --handle "$cutover_handle" || exit 1
batch/scripts/r2_transfer.sh pull-machine || exit 1
batch/scripts/r2_transfer.sh hydrate-market || exit 1
uv run python -m baibai_batch.storage.sqlite_snapshot check --path stores/market/market.sqlite --schema-version 28 || exit 1
uv run python -m baibai_batch.storage.sqlite_snapshot create --source stores/market/market.sqlite --output "$cutover_dir/market28.sqlite" || exit 1
# 上記の抑止・レビューを確認してmain merge / operator code切替を行う。
uv run python -m tools.cutover.market_28_to_29 --source "$cutover_dir/market28.sqlite" --output "$cutover_dir/market29.sqlite" || exit 1
# 成功summaryとcanonicalのsidecar不存在を確認してから置換する。
mv "$cutover_dir/market29.sqlite" stores/market/market.sqlite
batch/scripts/r2_transfer.sh push-machine || exit 1
batch/scripts/r2_transfer.sh pull-machine || exit 1
batch/scripts/r2_transfer.sh hydrate-market || exit 1
uv run python -m baibai_batch.storage.sqlite_snapshot check --path stores/market/market.sqlite --schema-version 29 || exit 1
# 手順4の保持readbackを確認してからrelease / 元のworkflow状態への復帰を行う。
uv run python -m baibai_batch.storage.publication_lease release --handle "$cutover_handle" || exit 1
```

**失敗時**: main切替前ならcloud/codeを変えず、leaseをreleaseして停止する。main切替後はwriterを抑止したまま、同じ最新28 backupからtoolを修正・再実行し整合する29を作る。クラウドに変更がない段階で中止するならcodeも28を読むrevisionへ戻してから再開する。部分PUT/receipt不一致は[部分push復旧](#部分-push-からの復旧)に従い、古いbundleを全量戻さない。世代・L1 pointerの競合やlease失効なら続行せず、新しいleaseとfreshなcloud stateで再計画する。自動migration、無条件PUT、定期batch待ちでの解消は行わない。

<a id="tradingview-expectations"></a>

## TradingView Analyst Expectations

`cloud-tradingview-snapshot.yml`は平日15:37 / 17:37 / 19:37 / 21:37 / 23:37 JST（UTC `37 6,8,10,12,14 * * 1-5`）、土日09:37 / 13:37 / 17:37 JST（UTC `37 0,4,8 * * 0,6`）に独立して起動する。平日の祝日も既存5 slotを休場日の観測・未取得補完に使う。GitHub queueで実開始が遅れるため、targetは予定時刻ではなくrun開始時のJST当日とする。起動回数やjob成功数をprovider取得成功数として数えない。

手動・定期ともhydrateしたJPX calendarで当日を判定し、営業日は15:30以降、休場日は09:00以降に取得する。09:00前はsetup前に終了する。09:00以降の営業日朝はlease・pull・hydrate・preflightまで進んでcalendarを確認するが、master取得・OAuth/provider接続・保存へ進まず`before_close`となる。朝の手動観測で同日PKを埋め、引け後の取得を妨げない。

Universeは営業日なら当日master、休場日ならcalendar上の直近営業日の保存済みexact-date masterから選ぶ。選んだ営業日から実観測日までcalendarの日付が連続していることを確認し、間の欠落があれば停止する。休場日の必要masterやcalendarがなければ停止し、より古い保存済みmasterへ暗黙に退避しない。`observation_master_asof`はこの必須基準日との一致確認であり、時間guardを解除しない。同日再開は同じ基準日を使う。`snapshot_date`は実観測日のJST当日、`fetched_at_utc`は実受領完了時刻で、masterの日付へ現在値を保存しない。master基準日を結果・progress・summaryの`master_asof`に記録する。日跨ぎ前の正常batchは部分結果として残し、跨いだresponseは前日へ保存しない。

同日のnullやunresolvedとして保存した銘柄もaccount済みであり、その日は上書き・再取得しない。既保存の完全snapshotを何度も観測する診断ではない。

取得は50銘柄ずつ、応答完了後15秒以上の間隔とする。正常batchは検証後に確定し、新規rowがあれば`partial`でもL1へ公開する。後続slotはL1からhydrateした同日rowのticker集合をexact-date masterのUniverseと照合し、未保存銘柄だけを取得する。全銘柄account済みならMCP接続前に終了する。hydrate後にmasterのraw rowsがあれば、`source_coverage`がなくても再取得せずに使う。TV workflowはremote `market.sqlite`や`machine-manifest.json`を更新せず、L1 releaseだけを永続化する。dailyは独立して実行する。

summaryは営業日判定・master基準日・保存済み/追加/残件・unresolved・指標ごとの正常null・first/last受領時刻・provider障害・公開結果を分けて示す。partialをcooldownで取得skipした場合も保存済みrowの品質を示す。全件account済みでも全指標が利用可能とは限らない。成功・no-opはDiscord通知せず、partial・障害は既存通知に残す。

休日の同値観測も履歴として保持するが、独立した情報sampleとして数えない。分析時は各行の`fetched_at_utc`が評価のas-of cutoff以下であることを確認し、未来値や無条件のlatest-nonnullで欠測を埋めない。土日3 slotは候補頻度であり、実網羅率・欠測・受領時刻で受け入れを判断する。provider通信が0のno-opでもlease・pull・hydrateのR2 I/Oは発生する。R2 Standardの無料枠は[公式料金](https://developers.cloudflare.com/r2/pricing/)に従い、全bucketの月間保管量・Class A/B消費を確認する。使用量が不明なら無料とは保証せず、追加課金を前提に頻度を増やさない。

### 保存済みmasterを使う当日の手動観測

mainへmanual dispatchし、`observation_master_asof`だけに保存済みmasterの日付を指定する。`observations_json` / `observations_sha256`は空にする。営業日は当日、休場日はcalendar上の直近営業日のmasterを指定し、上記の取得時間を守る。J-Quants masterの追加取得は行わず、指定日がcalendarの必須基準日と異なる場合やmasterがない場合はOAuth/provider接続前に停止する。50銘柄ずつ、応答完了後15秒以上の間隔を必須とし、通常collector・normalizer・publication lease・cooldown・25分soft budget・L1 publisherを使う。

```bash
gh workflow run cloud-tradingview-snapshot.yml --ref main \
  -f observation_master_asof=2026-10-09 -f batch_size=50 -f interval=15
```

同じ観測日の再開は同じmaster基準日を指定し、fresh pull/hydrate後に同日の保存済み集合を除外する。正常null / unresolvedも保存済みとして上書き・再取得しない。provider失敗・Retry-After・日跨ぎ・soft budgetで止まった場合、新規確定rowがあればpartialを公開し、残件だけを同日中に再開する。summaryの`target_date` / `master_asof` / first・last fetched_at、件数、stop_reason、公開release、lease解放を確認する。partial公開は全Universe取得成功とは数えない。runner強制停止・timeoutやstorage/internal障害で正常な結果を出せなかった場合は公開完了とみなさず、[障害診断](#tradingview-expectationsの障害診断)に従う。

### 保存済み実観測の明示的な受入

providerから受領済みの応答を履歴へ残す場合は、通常collectorとは別に`accept-observations`を使う。新しいprovider接続は行わず、休場日でも実受領日のJST日付を`snapshot_date`、UTC受領完了時刻を`fetched_at_utc`にする。quote/provider更新時刻が不明ならNULLのままとし、過去の営業日へ付け替えない。1〜2応答・各最大50銘柄を既存`tradingview.forecast_snapshots`へ追加し、coverageは`partial`のままにする。同一PK・全列一致はno-op、異なる値なら受入全体をrollbackし、既存行を上書きしない。

元応答はLibraryに保持し、versionとSHA256を確認してから対象応答だけを選ぶ。入力JSONは`schema_version: 1`、`source_ref`（`library_file_id`, `file_id`, `version`, 元ファイルの`sha256`）、`observation_date_jst`、取得時の順序の`columns`、`observations`を持つ。各observationは`trial`、UTCの`timing.start/end`、構造化された元の`result`を保持する。`json.dumps(..., ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)`で改行なしに保存し、選択入力そのもののSHA256も求める。元ファイルのhashはprovenanceであり、選択入力のhashとは区別する。

作業用SQLiteだけで受入を確認する場合:

```bash
uv run baibai-engine tradingview accept-observations \
  --sqlite /tmp/market-acceptance.sqlite \
  --input /tmp/selected-observations.json \
  --input-sha256 <選択入力のSHA256>
```

本番反映は既存`cloud-tradingview-snapshot.yml`のmainへのmanual dispatchを使う。`observations_json`と`observations_sha256`の両方を設定し、`ref: main`と`inputs`を含むdispatch JSONをprivate fileから`gh api --method POST .../actions/workflows/cloud-tradingview-snapshot.yml/dispatches --input <file>`へ渡す。Actions inputs合計は65,535文字以内とする。raw応答をGit、shell引数、環境変数、logへ出さない。jobは`GITHUB_EVENT_PATH`から入力を検証し、既存publication lease取得→fresh pull/hydrate→受入→新規行がある場合だけ既存L1 publishの順に実行する。両入力が空のscheduled/manual実行は通常collectorへ進む。

公開後は空のcacheからR2の固定releaseを全dataset読み直してhydrateし、受入行の全列、受領時刻、受入前のTV履歴の件数/hash、他datasetと他月のobject hashを照合する。成功summaryの`readback: verified`を確認する。`published_readback_pending`だけなら公開は済んでいても読戻し未確認であり、完了として扱わない。同じ入力の再実行は一致する行を検証し、L1を再公開しない。OAuth state・通常cron・通常collectorの営業日/引け後guardはこの操作で変更しない。

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

GitHub Secretの初期投入に使ったローカル認証fileは、GitHub runnerが更新した後の最新版ではない。同じstateを複数processで更新したり、古いfileを再投入しない。対話認証による置換も、`cloud-publish`の実行がない時間に行う。

### 独立したローカル認証で取得・L1公開する

GitHubとlocalはそれぞれ独立したOAuth stateを使い、共有publication leaseだけでL1 writerを直列化する。GitHub側のenvironment secretとworkflowは変更しない。local成功はprovider quotaの分離や全市場取得の安定を保証しないため、GitHub失敗直後の自動fallbackやblind retryには使わない。

**前提**: current mainのcleanな専用runtime checkoutを用意する。runnerはそのcheckoutの`stores/market/market.sqlite`をfresh pull・hydrateで置換するため、分析・編集中のcheckoutを使わず、同checkoutのstoreを他processで更新しない。`.env`には既存のstores用R2 credentialと`JQUANTS_API_KEY`を設定し、`uv sync --locked`と、workflowと同じ[DuckDB httpfsの事前配置](../tools/diagnostics/provision_duckdb_httpfs.py)を済ませる。既存transferに必要なAWS CLIも使える状態にする。

初回のみ、GitHubへ投入したfileとは別のpathへ新規に対話認証する。本番Secretや、その元になった古いfileをコピーしない。fileはrepo外に保存し、token rotationも同じfileへatomicに書き戻す。

```bash
uv run baibai-engine tradingview authorize \
  --state-file "$HOME/.cache/baibai-loop/tradingview-local/oauth.json"
```

同じfileを使う取得・smoke・再認証を同時実行しない。GitHubの手動OAuth smokeも、provider requestの重複を避けるためlocal取得中には実行しない。cloud smokeはL1を書かずlocal認証とも独立しているため、既存のGitHub queueを維持しpublication leaseには参加しない。

**実行**: 専用checkoutのrootから、[営業日・休場日の共通取得条件](#tradingview-expectations)に従って実行する。

```bash
uv run baibai-batch tradingview \
  --state-file "$HOME/.cache/baibai-loop/tradingview-local/oauth.json"
```

runnerは実観測日のJST当日を固定し、lease取得 → market pull → current L1 hydrate → calendarとexact-date masterのpreflight → 必要な営業日masterと未保存銘柄のTV取得 → 新規rowの`partial`または`saved`をL1公開 → releaseの順に進む。取得時間、休場日の保存済みmaster選択、calendar欠落時の停止、同日再開、batch size・intervalは[共通契約](#tradingview-expectations)を使う。全件保存済みならOAuth接続前に終了し、過去日のbackfillはしない。TV取得は25分のsoft budgetで通常終了を図り、30分のhard timeout、取得前のpullから公開まで全体60分で打ち切る。remote `market.sqlite`・machine bundle・servingは更新しない。

**成功確認**: exit 0、`saved`または`partial`の`rows_added`・`rows / expected_universe`・`remaining`・`stop_reason`、L1 publish reportのrelease ID/hash、lease解放を確認する。固定releaseの行数・Universe・null・unresolved・`fetched_at_utc`・前日snapshot保持を[L1参照手順](../docs/reference/market-lake.md#shared-raw-read)で照合する。`partial`は未取得が残るDEGRADEDであり、provider正常や全指標coverageを示さない。`already_saved` / `skipped_historical_asof` / `before_close` / `before_observation`は取得・公開成功と区別したno-opである。同日後続runがfresh hydrateから保存済みrowを再取得しないことも確認する。

**停止・復旧**: busyはexit 3、その他の失敗はexit 1。失敗・SIGINT/SIGTERM・timeoutでは子processを停止してからreleaseする。release失敗時はstderrに示されたhandleを保持し、[既存lease手順](#ローカルからクラウドを更新する)に従う。SIGKILL・host停止ではreleaseできない場合がある。公開失敗後のlocal snapshotだけを根拠に保存済みとせず、次回はfresh pull/hydrateから始める。

認証失敗はlocal fileだけを再認証し、GitHub Secretを変更しない。`secret_persistence`ならfileの書込権限・空き容量を確認し、保存できなかったrotationを古いfileから再試行せず再認証する。継続認証を明示的に確認するときだけ、取得中でないことを確認して次を使う。2銘柄取得とtoken更新を行うがsnapshotは保存しない。次processで同じfileを再利用できることを確認する。

```bash
uv run baibai-engine tradingview smoke \
  --state-file "$HOME/.cache/baibai-loop/tradingview-local/oauth.json"
```

engineの`refresh --state-file`は取得だけの低レベル入口であり、production取得・公開には上のbatch runnerを使う。local file指定時にGitHub認証へfallbackすることはない。

### 保存と再試行

当日の各銘柄で最初に保存した正常応答を維持する。正常batchは各transactionで保存し、取得不良batchは保存せず次の未保存batchを試す。応答不良が3 batch連続した場合と明示的429では新規requestを止める。同一batchの即時retryは行わず、skip後も通常intervalを置く。新規rowが0件なら失敗とし、過去に公開した部分snapshotだけで今回を成功にしない。次slotは公開済みの未保存銘柄から再開する。過去日の穴を現在値で埋めない。

### TradingView Expectationsの障害診断

run summaryの`requests_attempted` / `symbols_requested`は`fetch(chunk)`に渡した論理的なbatch試行数・銘柄数（実HTTP送信数とは限らない）、`rows_added`は検証後に保存した行数であり、HTTP成功数とは区別する。`preflight_saved_rows`と`rows_added` / `remaining`で再開を確認し、`normal_nulls`と`unresolved`を分ける。progressがない取得失敗は件数不明として扱う。MCP transportの`mcp_http_status`とtool応答に明示された`provider_http_status`を混同せず、未観測statusを200と推定しない。

`retry_not_before_utc`はMCP HTTPのRetry-Afterが読めた場合の待機期限だけを表し、元ヘッダーを保存しない。Actionsは既存publication lease内で`baibai-stores/coordination/tradingview-actions-cooldown.json`へUTC期限だけを条件付き保存する。次runは未保存分の取得前に読み、期限内ならprovider非接続、期限切れなら再開する。objectなしは未設定として通常処理、不正object・読取失敗は取得を止める。期限は短縮・削除せず、正常runやログ削除で消さない。新credential・権限・datasetは不要。

待機期限のR2保存失敗はjob失敗として通知する。未保存の期限を次runが復元する保証はないため、記録失敗時もworkflowを停止し、期限を確認してから再開する。強制終了やVM消失で期限記録前にrunnerが失われた場合の永続化保証はないため、この場合はworkflowを停止し、直前応答と待機期限を確認してから再開する。自動引継ぎはこのActions workflowの範囲であり、local・Chatの手動取得も同じ待機指示を守る。cronは最小実開始間隔を保証しない。

TradingView stepのsafe failure line、`TradingView progress:` summaryの順に読む。`chunks_completed / chunks_total`と`chunk_index`で失敗位置を、`provider_elapsed_seconds / elapsed_seconds`と`max_chunk_elapsed_seconds`でprovider待ちとintervalの影響を確認し、`oauth_rotations`で認証更新回数を見る。payload検証失敗では固定の`validation_reason`とcanonicalな`validation_field`を確認する。正常終了時は最終JSONの`rows_added / rows / expected_universe / remaining / stop_reason / unresolved / normal_nulls`とfirst / last fetched_atも確認する。

- `provider_rate_limit`（429）: `is_error=false`でも、`success=false`のerrorが明確なHTTP 429を示す場合はこの分類になる。同runで新規requestを止め、manual scanner callを重ねない。既に正常batchがあれば部分公開を確認し、明示待機期限があればそれを守ったうえで次のscheduled slotまたはprovider quota確認へ進む。
- `auth`: [対話認証](#初期設定と認証の復旧)をやり直す。
- `secret_persistence`: `tradingview-runtime`のEnvironment secretとwriter tokenの権限・期限を確認する。
- `provider_all_missing`: coverage不存在と断定せず、provider全体のdegradationの可能性を確認する。
- `provider_response`: 固定validation reasonを起点に応答契約を調べる。Secret・raw body・symbol一覧をIssueやlogへ転載しない。
- `provider_transport`: 接続・通信の失敗を確認し、provider側の状態と次slotを見て再取得を判断する。
- `time_guard`: target日がJST当日で15:30以降か確認する。条件を満たさなければ取得せず、過去日を現在値で補完しない。
- `internal`: safe failure lineと失敗stepを確認し、実装エラーとして原因を調べる。
- `timeout`: 最後のphase / chunkとprovider時間を確認し、原因確認なしにtimeoutを延長しない。
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
再seedせず通常運用へ移る。seedは取得世代を記録しないため、applicationの通常更新を始める前に
[application反映](#application-db-を反映する)の`pull-app`で正本と世代を取得する。

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
残しません。`wrangler.jsonc`ではobservabilityとLogpushを無効にし、`redact_query_string=true`を設定します。real-time logの`wrangler tail`、dashboard Live Logs、Tail Worker等は、
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
