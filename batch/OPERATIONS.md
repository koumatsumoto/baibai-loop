# 運用手順

machine処理、store反映、lake・表示の公開と復旧を扱う。repository rootのBashから、依頼された対象だけを操作する。全optionと内部判定は各CLIのhelp・実装が正本。責務とデータ正本は[architecture](../docs/architecture.md#store-authority)、開発準備は[development](../docs/development.md)にある。

<a id="actions-write-boundary"></a>
## 実行環境と接続

通常の取得・公開は既存Actionsを優先する。個別に依頼されたapplication更新は、認可された運用環境で取得済み作業コピーを正規CLIで更新し、条件付き反映する。物理的なPCだけに限定せず、対応OS・依存・接続を確認する。開発変更を本番操作・新課金・権限拡大の許可へ読み替えない。

baibai-storesは正本、baibai-servingは表示用。serving writeはActionsだけが行い、ローカルから直接uploadしない。両bucketのPublic Development URLとcustom domainは無効にする。serving lifecycleはhistory/candidate-views/だけに設定し、views/meta.jsonを含むbucket全体へ期限を掛けない。

| 実行主体 | 設定の所在と範囲 |
| --- | --- |
| 取得・公開Actions | R2_ACCOUNT_ID、R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY、JQUANTS_API_KEY/ESTAT_APP_ID/EDINET_API_KEY。必要stepだけへ渡す |
| 通知 | repository secret DISCORD_WEBHOOK_URL。通知stepだけが使う |
| Worker deploy | 対象accountのWorkers Scripts Writeに絞ったCLOUDFLARE_API_TOKEN |
| 認可されたlocal運用 | repo外で扱う秘密と.env。stores用writerを通常開発へ流用しない |
| Worker閲覧 | VIEW_PASSWORDとREAD_ACCESS_TOKENを分離 |
| Worker lake読取 | baibai-storesだけのObject Read only専用credential |

秘密値・endpoint・私的なDB/原稿をGit・Issue・log・公開artifactへ置かない。認証失敗を別経路や権限拡大で迂回せず、設定の有無と対象接続を確認する。運用内容の許可はbroker操作や未報告取引の入力を含まない。

## 日次実行と結果確認

code修正が必要ならmain反映を終えてから、必要な取得・再計算を実行する。対象run IDを固定し、監視timeoutだけで終了と決めて重複dispatchしない。

```bash
gh workflow run cloud-daily-batch.yml --ref main
gh run list --workflow cloud-daily-batch.yml --limit 5
gh run watch <RUN_ID> --exit-status --interval 60
```

過去日を明示する場合だけdispatchへasofを渡す。通常cronは元の対象日を保ち、queue遅延で翌日へ進めない。非営業日はScreeningをskipして旧runの値・ID・実計算時刻を保ち、経済原本と表示を更新する。

dailyは取得→Screening→独立指標refresh→全views exportを行う。Screening取得/計算失敗でも独立した後段は進める。exit 0はexport完了、exit 1は致命的失敗で公開停止、exit 3はexport完了だが一部工程が失敗した結果。R2公開は後段なので、export_readyやworkflow失敗だけで反映状態を断定しない。

取得期限はworkflowが生成・公開の余白を残して設定する。retry/deadline停止で完了済みL1を取り消さず、次runで成功chunkから再開する。Retry-Afterを短縮せず、認証・403・解析失敗へtransport retryを広げない。日次に移行・全期間backfill・較正再構築を代行させない。

通知とrun log、store push、release、views/meta.jsonを照合する。通知配送失敗はjobを赤にしないため、通知がない場合もrun自体を確認する。watchdogのMISSINGは対象日の成功run不足で、過去日確認は次を使う。

```bash
gh workflow run cloud-batch-watchdog.yml -f check_date=<YYYY-MM-DD>
```

## クラウド正本をローカルへ取得する

分析用pull前にdailyのqueued/in_progressを確認し、通常は完了後に取得する。ローカルpublisherとも重ねない。TradingViewの完了releaseは現在machine bundleへhydrateでき、materializeはmachine/L1を書かないので待機対象ではない。

```bash
batch/scripts/pull.sh
```

machine receipt/ETagとhydrate開始時のmarket releaseを照合する。両者を同一transactionでは固定しないため、競合・検査失敗後のlocal stateを分析へ使わずfreshに取得し直す。applicationはbulk pullせずpull-appを使う。

Screening入力の補修が必要な時だけverify-cache-coverageと対象ticker-profileを使い、取得欠落を同じas-ofのbootstrap-cacheで補う。提供元のnullやL2計算不能を取得失敗とせず、値が埋まるまで全期間を再取得しない。

## ローカルからクラウドを更新する

変更codeをmainへ反映し、lake対象tableは**変更前**に現行releaseからhydrateする。変更後のhydrateは成果を上書きする。長い取得・準備を先に済ませ、production read/merge/publishは同じpublication lease内で行う。

```bash
with_publication_lease() (
  handle="$(mktemp "${TMPDIR:-/tmp}/baibai-publication-lease.XXXXXX.json")"
  rm -f "$handle"
  uv run python -m baibai_batch.storage.publication_lease acquire --purpose operator --handle "$handle" || return $?
  if PUBLICATION_LEASE_HANDLE="$handle" "$@"; then result=0; else result=$?; fi
  uv run python -m baibai_batch.storage.publication_lease release --handle "$handle" || return $?
  return "$result"
)
```

失敗時もreleaseするため関数内でset -eを使わない。busyならowner・expiryとrun/processを確認し、blind retryしない。release失敗はhandleを保つ。GitHubのcloud-publish queueだけではlocal writerを排除しない。push-app/publish.shと初回seedはこのlease対象ではない。

対象に合う一経路だけを使う。lake変更はpublish-lake→push-market、store-localだけならpush-market、経済原本だけならpush-indicators。直前のbundleからlocal daily全体を実行した時はpublish-lake→push-machineとし、3 storeを個別pullで組み替えない。

```bash
with_publication_lease bash -c 'batch/scripts/r2_transfer.sh publish-lake && batch/scripts/r2_transfer.sh push-market'
with_publication_lease batch/scripts/r2_transfer.sh push-indicators
```

merge・origin・schema・取得ETag/CASの失敗では公開を止める。成功後、表示更新も必要ならcloud-materializeをmainへdispatchし、対象runと公開物を確認する。lake→machine store→serving→metaの順を保ち、coverageだけが先に進む状態を作らない。

malformed leaseは自動修復しない。GitHubとlocalにactive/pending writerがないと確認した場合だけexact coordination/publication-lease.jsonを調べ、対象1keyの削除後に通常acquireで再作成する。prefix削除や手書きJSON上書きを通常運用へ入れない。

## application DB を反映する

空の作業領域へpull-appし、DBとstores/.r2-generations/baibai.sqlite.etagを一組で保持する。既存コピーを更新する時はwriterを止め、db backupでWAL込みsnapshotを退避してから取得し直す。DBだけを別世代へ替えてsidecarを流用しない。

```bash
batch/scripts/r2_transfer.sh pull-app
batch/scripts/publish.sh
```

publish.shは引数なしで使う。台帳preflight、取得世代への条件付きbackup/PUT、次更新用ETag記録、materialize dispatchを行う。保有quote/basis不足による時価未評価は実取引保存の障害と区別する。未知世代を現在HEADで補わない。

application backupは実行時JST日付の日別keyで、同日keyを置換する。古い世代の整理を行わない依頼ではR2_APP_BACKUP_PRUNE=skipを指定できるが、同日置換は変わらない。元世代の保全や置換が依頼範囲に含まれるか確認する。

保存、R2反映、serving生成、表示を別々に確認する。対象Proposalのimmutable IDと参照評価を読み戻し、最新候補へ固定Selectionを付け替えない。競合なら未公開変更を退避し、最新正本へ必要差分を正規serviceで再適用する。PUT後の記録/dispatch失敗を未反映と決めて旧DBを再送しない。

current schemaだけを開く。schema変更はsource versionを限定した一回移行を別コピーで検証し、対応codeとstoreの反映を同じdeliveryで終えたらtoolを撤去する。旧DB/keyや完了済みcutoverを現行正本へ再適用しない。

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
    uv run baibai-engine db backup --db stores/application/baibai.sqlite | \
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
  uv run baibai-engine position ledger --db stores/application/baibai.sqlite
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
  failed_restore="stores/application/backups/failed-restore-$(date +%Y%m%dT%H%M%S).sqlite"
  test ! -e "$failed_restore"
  mv stores/application/baibai.sqlite "$failed_restore"
  rm -f stores/application/baibai.sqlite-wal stores/application/baibai.sqlite-shm
  mv -f "$restore_stage" stores/application/baibai.sqlite
  uv run baibai-engine position ledger --db stores/application/baibai.sqlite
)
```

途中停止では残存pathと失敗commandを確認し、blockを無条件反復しない。


<a id="fixed-release-read"></a>
## 固定releaseを読む

最初にcurrentを一度解決し、release IDとmanifest SHA-256を固定する。以後は同じmanifestが列挙したdataset/objectだけを読む。optional datasetなしは未収録で、他releaseのpartitionやprovider値で穴埋めしない。

```bash
uv run baibai-engine lake resolve --mirror stores --bucket baibai-stores --format json
uv run baibai-engine lake resolve --mirror stores --bucket baibai-stores \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

resolveはmanifestの解決で、data objectの検証や任意SQL実行ではない。

objectのdigest・bytes・row数・schemaを通常readerで照合する。共有gatewayは/api/lake/current→release manifest→dataset manifest→列挙Parquetの順にraw GETする。gatewayのHTTP 200はdecode・整合の証明ではなく、照合は利用側が行う。固定objectが404なら分析を止め、再開は新しい分析としてcurrentから始める。

<a id="local-release-hydrate"></a>
### remote拡張なしのhydrate

必要量とsnapshot用容量を確認し、manifest/objectの完全mirrorを認可済み読取で揃える。部分JSON exportで正規market readerを代用しない。

```bash
uv run baibai-engine lake hydrate \
  --mirror <COMPLETE_LOCAL_MIRROR> --store <LOCAL_MARKET_COPY> \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

ローカルmirrorではbucketを付けない。不足・破損・容量不足なら停止し、途中でreleaseを替えない。hydrateは対象tableを置換するが、store-local coverageの取得記録は生成しない。旧contractのreleaseは元commitのcodeで読み、runtime互換を増やさない。

完全mirrorからParquetを読む最小例:

```python
from pathlib import Path
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import (
    accepted_dataset, materialize_partitions, resolve_release, selected_partitions,
)

mirror = Path("<COMPLETE_LOCAL_MIRROR>")
source = LocalMirrorSource(mirror)
release = resolve_release(source, "<RELEASE_ID>", manifest_sha256="<MANIFEST_SHA256>")
name = "edinet.documents"
paths = materialize_partitions(
    LakeObjectCache(root=mirror, source=source),
    dataset=accepted_dataset(release, name), partitions=selected_partitions(release, name),
)
with lake_session() as session:
    rows = session.connection.read_parquet([str(path) for path in paths]).fetchall()
```

<a id="edinet-research-query"></a>
### EDINET factsの書類選択

同じ固定releaseを作業SQLiteへhydrateし、保存inventoryからlatest annualと訂正を選ぶ。

```python
from datetime import date
from pathlib import Path
from baibai_engine.market.edinet_facts.selection import latest_annual_filing, usable_document
from baibai_engine.market.edinet_facts.store import document_inventory

doc = latest_annual_filing(
    document_inventory(Path("/tmp/research-fixed.sqlite"), date.fromisoformat("<YYYY-MM-DD>")),
    "<TICKER>",
)
if doc is None or not usable_document(doc):
    raise ValueError("年次書類を確定できません。原典を確認してください。")
```

cutoffはJST日末でintraday PITではない。選択なしは書類不存在でなく、未抽出・公開状態未確認なら原典へ戻る。segment/debtをsource_doc_idで選び、metadata期間・連結・通貨を揃える。全債務や資金余命を抽出行だけで確定しない。

### 保持とGC

retention rootはcurrent releaseのみ。到達objectとmanifestを保持し、未到達/stagingはgrace後の候補になる。gcはdry-runを読み、apply時に再planしたhashが一致した場合だけ削除する。calibrationは対象外。

```bash
uv run baibai-engine lake inventory --root stores
uv run baibai-engine lake gc --mirror stores
uv run baibai-engine lake gc --mirror stores --apply --plan-hash <HASH>
```

immutable prefixのBucket Lockとmutable pointer/stagingを区別する。rollback pointerや全履歴監査を追加せず、問題releaseは正しいstoreから新releaseを前向きに発行する。

## R2 transferの安全境界

machine pushは前世代をkey.bakに1世代保存し、次pushで置換する。applicationだけ日別backupを保持する。3 storeは原子的反映ではなく、全PUT後にmachine-manifest.json receiptを書く。schema修正でstore全体を古いcopyへ戻すと後続事実を失うため、sourceを限定した移行を使う。

machine前世代を復元する場合はwriterを止め、対象bucketとexact key/key.bakを確認し、aws s3api copy-object --cli-read-timeout 300を使う。復元後はfresh pullし、receipt・generationの整合を確認する。盲目的なcopyやprefix削除は行わない。

## 部分 push からの復旧

先行keyだけPUT成功すると旧receiptと不整合になりpullが停止する。同じpush/dailyを再送せず、到達点をlogで確認する。local成果を残す必要があり現行origin/merge条件を満たすならpush-market/push-indicatorsで反映してreceiptを更新する。runs成果は元bundleの世代条件を満たすpush-machineを使う。

receipt再生成で復旧する場合だけ、対象がbucket直下のmutable machine-manifest.json一件であることを確認する。

```bash
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  aws s3api delete-object --bucket baibai-stores --key machine-manifest.json \
    --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
)
```

次pullは未検証setとして現世代を取得し、次の完全pushで新receiptを作る。その一周のcross-section保証が落ちるため、receipt一致を再確認するまでを復旧とする。失敗時に追加削除・再dispatchを繰り返さない。

## 指標観測とregistryの修正

原本が誤っていればsource訂正取得または明示再構築。local DELETEをcloud mergeによる訂正に使わない。系列追加/退役/改名や式変更はL2定義を直し、外部取得条件を変える場合だけCollectionも直す。原本から再計算できる変更へ再取得・scalar撤回・registry世代台帳を要求しない。

```bash
uv run baibai-engine collection economic list
uv run baibai-engine collection economic refresh <SOURCE_ID> --all-history --end <YYYY-MM-DD>
uv run baibai-engine market-context reading --asof <YYYY-MM-DD>
uv run baibai-batch validate-market-context-stores
```

最後のcommandは原本storeのpayload・record count等を確認する。L2数式の証明やL3全履歴検査ではない。原本更新だけpush-indicatorsへ進み、式変更だけなら不要。取得失敗でcommit済み原本を取り消さず、L2欠測を直すため全取得を繰り返さない。

optionsのall-historyは保存済み古い日を再利用して直近と不足日を取得する。古い訂正は日付範囲を明示する。購読rolling境界の提供不能を示し、空応答と途中失敗を区別する。式変更だけでrawを再取得しない。

PMI manifestは公表翌週にdry-run→更新→対象月refresh→公表値照合→commitする。

```bash
uv run python -m baibai_engine.market_context.indicators.pmi_manifest --dry-run
uv run python -m baibai_engine.market_context.indicators.pmi_manifest
```

PDF自身のPMI名・embargo月・対象headline月を確認し、月の穴を飛び越えない。indexの最新月以前はarchive snapshotからrelease IDを辿り、既存月と照合する。翌月PDFの明示restateを使う時も元の対象月を保つ。URL訂正と同URL本文差替えを区別し、抽出式だけなら保存PDFを再読する。

<a id="edinet-metrics-revision"></a>
## EDINET metricsの意味変更

[抽出意味revision](../docs/reference/data-sources.md#edinet-metric-revision)を変える時だけ実施する。Research facts revisionとは別。隔離コピーで保持CSV ZIPを再抽出し、期待差分・quality/failure・coverageを確認する。

main反映とwriterの非競合を確認し、同じlease内でfresh pull/hydrate→比較元backup→extract-edinet-metrics→publish-lake→push-market→固定release読戻しを行う。原本不足で取得が必要な時だけrefresh-edinet-metricsを使う。対象as-ofを指定し、保存値とrevision・store-local情報の整合を確認する。

期待外差分、取得失敗、世代競合、lease失効で後段を止める。準備コピーを本番へ戻さず、再抽出後にhydrateせず、旧値のrevisionだけを付け替えない。通常dailyへ意味移行を代行させない。

<a id="edinet-source-archives"></a>
## EDINET原本とResearch facts

原本再利用は明示したmarket SQLiteのedinet_source_archivesから行い、以前の端末ZIPを暗黙に取り込まない。stored_onは初回保存UTC日で開示・初回利用可能日ではない。受領ZIPとL2の選択文書・metricsを混同せず、取得エラーで既存ZIPを上書きしない。

<a id="edinet-research-facts"></a>
未初期化storeだけ、整合する隔離marketとdocument inventoryを指定して初期抽出する。

```bash
uv run baibai-engine screening refresh-edinet-facts \
  --asof <YYYY-MM-DD> --initialize --sqlite-path <STAGED_MARKET_SQLITE>
```

失敗後は同commandで再開し、ok書類を再取得しない。保存原本だけの再計算はextract-edinet-facts。ticker限定は全体initializedを作らない。未対応形式の理由付きokと取得/破損のfailedを区別し、代表原典、missing reasons、export/hydrateを確認してcode・L1・coverageを一組で反映する。

dailyは未初期化をskipし、初期化後の新規年次/訂正を独立処理する。最新訂正失敗を旧版で埋めず、failed docIDの原因を直して再開する。利用側の選択は[固定release](#edinet-research-query)に従う。


<a id="external-assessment-acquisition"></a>
## 外部参考評価の取得・再開

限定collectは明示した少数銘柄の公開HTTPを直列で読み、private captureへ保存する。通常TLS/proxyを使い、403/429でそのsourceを止める。取得先を別名にし、部分captureを再取得で上書きしない。既存のcurrent-schema隔離marketへimportする。

```bash
uv run baibai-engine external-assessment collect --source minkabu --tickers 7203 4436 --output /tmp/private-external-assessment/responses.json
uv run baibai-engine external-assessment import --input /tmp/private-external-assessment/responses.json --db /tmp/private-external-assessment/market.sqlite
```

importは原本を保持して選択attemptを再解析する。parse失敗時にL1は残り得るがL2を部分追記しない。原本/観測の挿入件数と再投入0を確認し、HTTP・parse・未試行・正常nullを分ける。新規DB初期化や旧schema移行は行わない。

### 日次取得と固定コホートの再開

source別planは固定release/calendar/masterとparser/request条件へ束縛する。sourceはmonex/rakuten/kabumap/minkabu、allは4取得元を選ぶ。同sourceのlocalとActions取得を重ねない。scheduleの対象日は元runから固定し、受領日をmaster/cron日へ替えない。

```bash
gh workflow run cloud-external-assessment.yml --ref main \
  -f source=monex -f batch_id=<LABEL> -f master_asof=<YYYY-MM-DD>
```

scheduled再開は既存external-assessment-daily-<source>-YYYY-MM-DDをそのまま指定し、欠落planを現在masterで再作成しない。手動labelはsource prefixを重ねない。stage確定済みentry/受領時刻を変えず未試行pairだけを再開する。保存失敗pairの再取得は別labelにする。collector binding変更で拒否されたplanを手書きhashで通さない。

### 原本handoffと公開

raw bundleをstageより先にuploadする。未確定GETはhard kill後に繰り返され得る。external-assessment/raw/は3日期限の短期handoffであり、厳密72時間でも長期L1でもない。実lifecycleを確認し、期限切れ原本の復元を保証しない。publisherがmarketへ取り込み固定lakeを読戻して初めて長期L1となる。

取得はlease外、公開はcloud-publishと共通lease内でfresh pull/hydrateして行う。readyなexact source planのraw/typed/historyを照合し、一回の共通releaseへ公開する。history_digestsは保存後baselineで、更新前の全履歴保全まで証明したとは説明しない。原本・stage破損を自動隔離して残りだけ公開せず、source指定で切り分ける。

readback後にsource別terminalを保存する。pointer成功後のterminal失敗は未反映と断定せず、既存terminalを読んで再開する。sealed sourceは以前の検証としてno-op、未sealedはfresh hydrateからidempotentに再適用する。未確認件数を0にしない。

403/429・未完了sourceを自動で閉じない。確定分だけで閉じる明示操作はsource単独で行い、取得を再実行しない。

```bash
gh workflow run cloud-external-assessment.yml --ref main \
  -f source=minkabu -f batch_id=<LABEL> -f master_asof=<YYYY-MM-DD> \
  -f operation=publish_confirmed_partial
```

固定pair、正常null、失敗・未試行、完全/部分公開、release/hash・readback・lease解放を確認する。lake反映とWeb更新は別で、通常daily/materializeを使う。通知・CI緑・stage保存だけで取得成功や画面反映を認定しない。

<a id="tradingview-expectations"></a>
## TradingView観測

slotは既存workflowを正本とする。営業日は15:30以降の当日master、休場日は09:00以降の直近営業日の保存済みexact-date masterを使う。calendar/master不足や引け前は取得せず、時間guardをmaster指定で解除しない。targetは実開始時のJST当日、fetched_atは実受領でprovider公表日ではない。

同日最初の正常row・null・unresolvedを保持し、後続は未保存銘柄だけ取得する。全件account済みは全metric利用可能の意味ではない。新rowがあればpartialを公開し、日跨ぎresponseを前日へ保存しない。過去日の穴を現在値で補わない。TVはmarket releaseだけを更新し、remote market.sqlite/machine receipt/servingは更新しない。

### 当日の手動観測

```bash
gh workflow run cloud-tradingview-snapshot.yml --ref main \
  -f observation_master_asof=<YYYY-MM-DD> -f interval=15
```

同日再開は同じmaster日を指定する。target/master、first/last受領、追加/残件、stop_reason、releaseとlease解放を確認する。失敗runでも原受領だけを公開する経路があるため、L1と正常L2の件数を混同しない。storage/internal障害や強制終了を取得成功としない。

### 初期設定と認証の復旧

対話commandを起動したまま認証URLを開く。callbackは127.0.0.1:8765で、接続拒否なら待受やWindows/WSLの接続を確認する。stateはrepo外に置く。

```bash
uv run baibai-engine tradingview authorize --state-file "$HOME/.cache/baibai-loop/tradingview/oauth.json"
gh secret set TRADINGVIEW_OAUTH_STATE --repo koumatsumoto/baibai-loop --env tradingview-runtime \
  < "$HOME/.cache/baibai-loop/tradingview/oauth.json"
```

GitHub側のstate正本はtradingview-runtime environment secret。repositoryのEnvironments Read and writeを持つ専用TRADINGVIEW_SECRET_WRITER_TOKENでrotationを取得前に書戻す。失敗・期限切れなら取得を止め再認証する。初期投入fileはrunner更新後の最新版ではないので再投入しない。stateを複数processで更新せず、置換はcloud-publishの非実行時に行う。

tradingview-runtimeはjob開始時に最新のrotating stateを読むためのenvironmentで、required reviewers・wait timer・deployment approvalを設定しない。

継続認証だけの明示確認はmainのCIでtradingview_oauth_smoke=trueを指定する。2銘柄の取得とrotationを行うがsnapshotは保存しない。通常開発で自動起動しない。

### 独立したローカル認証で取得する

専用clean runtime checkoutを使い、fresh pull/hydrateで置換されるstoreを編集中のcheckoutと共用しない。既存R2/J-Quants接続、AWS CLI、httpfsを用意する。GitHub stateをコピーせず別fileへ対話認証する。

```bash
uv run baibai-engine tradingview authorize --state-file "$HOME/.cache/baibai-loop/tradingview-local/oauth.json"
uv run baibai-batch tradingview --state-file "$HOME/.cache/baibai-loop/tradingview-local/oauth.json"
```

同fileの取得・smoke・再認証を並行せず、GitHub smokeとも重ねない。runnerがlease→pull/hydrate→preflight→未保存取得→release公開→lease解放を行う。部分保存、公開release/hash、残件を読戻す。no-opとpartial、取得・公開成功を分ける。公開失敗後のlocalだけを保存済み根拠とせず次回はfreshから始める。

local認証失敗はlocal fileを再認証しGitHub secretを変えない。secret_persistenceは書込権限・容量を確認して再認証し、古いrotation fileでretryしない。継続性確認を明示された時だけ、非取得中にtradingview smoke --state-fileで同fileの次process利用を確かめる。

### 保存と再試行

MCP transport失敗とtool内provider失敗、正常null/unresolvedを分ける。取得不良batchは未保存のまま次へ進み、連続不良や429で新requestを止める。即時同batch retryをせず通常intervalを守る。正常batchを公開し、次slotは未保存分から再開する。

Retry-Afterがある場合は期限を守る。Actions cooldownの保存失敗や強制終了で記録不明ならworkflowを止め直前応答と期限を確認し、盲目的に再実行しない。local手動取得も同じ待機指示を守る。authは対話認証、provider_responseはsafeなvalidation reason、storageはcalendar/master/schemaを起点に調べる。例外本文・raw・秘密をIssueへ出さない。

保存L1の一日分だけをoffline再構築する時は、同日・master・tool・Universe・受領時刻のreceiptを指定する。

```bash
uv run baibai-engine tradingview rebuild --sqlite /tmp/market-rebuild.sqlite \
  --asof <YYYY-MM-DD> --receipt-id <RECEIPT_ID>
```

正常receiptなし・銘柄重複なら置換せず、明示missingはunresolvedへ再現する。元受領時刻を維持し、拒否応答を正常値で補わない。


## Storeをseedする

初回だけ、4 keyすべて不存在を確認してseed.shを使う。既存keyが一件でもあれば停止し、通常更新/復旧へ戻る。1〜3 keyの部分seedは、供用前・writerなし・対象exact keyを確認できた場合だけ初回復旧として扱い、通常pull/pushや無条件上書きで直さない。seed後のapplication更新はpull-appで正本と世代を取り直す。

## Workerをdeployする

read model変更時は旧daily/materializeの完了後、対象mainでmaterializeして全views/metaと対象JSONを読戻してからWebを配信する。純粋UI変更にmaterializeを必須にしない。field削除/renameの一時不整合は、この順序だけで無停止とは保証しない。

```bash
gh workflow run cloud-materialize.yml --ref main
gh workflow run web.yml --ref main
```

二つを続けて発行せず、先の成功確認後に次へ進む。Webのdeploy stepの成功と配信対象のcommitを照合し、古いtreeのdeploy skipを配信成功としない。認証後の画面確認やユーザーによる手動確認は必要時の補助とし、完了条件に含めない。local生成はpython -m baibai_web.materializeを使い、store/schema失敗の生成物を公開しない。

## Password rotation

password manager等で生成したCSPRNG値をpromptへ入力する。

```bash
(cd web/edge && npx wrangler secret put VIEW_PASSWORD)
```

旧値401と新値成功を確認する。secret設定に再deployは不要。初回Worker作成後はsecret→materialize→verify-deployment.shの順で、認証と存在/不存在keyを確認する。TTYなしの未検証を成功へ読み替えない。

<a id="shared-read-setup"></a>
## 共有readとlake接続

production stores bucketだけのObject Read only credentialを作り、publisher用を流用しない。この権限はbucket全体を読め、外部へのnamespace制限はWorker allowlistが担う。canonical R2へのwrite bindingは追加しない。READ_ACCESS_TOKENはowner passwordと別の32 random bytes以上とし、実値・共有URLは非公開経路で扱う。

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

base URLはbucketのjurisdictionに合うaccount-endpoint/bucketで、query・credential・object keyを入れない。observability/logs/traces・Logpush・tail consumerを無効にし、query redactionを設定する。共有request中にwrangler tail/Live Logsへ接続しない。

owner/共有Bearerと共有queryで具体JSONと時点を確認する。未認証・旧/誤値は401、複数shareは400、UIでは最初のAPI前にURLからshareが消えowner保存値を保つ。lakeは[固定release](#fixed-release-read)の実Parquetまでdecode/照合する。HTTP 200だけで受入完了としない。

token rotationは同じsecret put後に旧値拒否・新値成功を確認し、撤回はsecret delete READ_ACCESS_TOKEN。R2 credentialは新read-onlyを設定してGET確認後に旧tokenを失効する。lake共有停止はL1_R2_ACCESS_KEY_IDを削除し専用tokenも失効する。設定失敗時はowner・canonical data・dailyを変更せず接続設定を直す。

## 定期maintenance

既存のdue taskを使い、同じ期限・対象・問いのopen taskを重複登録しない。実行後は完了を記録し、必要な次回分だけ作る。開発変更はGitHub Issueで管理する。

calibrationは月初、前月の完全月末まで同じrulesでbuild→evaluateする。forceは全再構築の時だけ使う。

```bash
uv run baibai-engine screening calibration-build --start <YYYY-MM-DD> --end <YYYY-MM-DD>
uv run baibai-engine screening calibration-evaluate --out /tmp/calibration-evaluation.yaml
```

判断面の文脈を更新する場合は目的・required月・必要metricを先に固定し、同じ評価にcontext-outを加える。[較正の意味](../docs/reference/estimate-calibration.md)に従い、記述context成功をformal evidence成立としない。

PMIは公表翌週、TSE資本政策とJPX廃止は翌月15日以降に既存refresh/buildを行い、必要なlake/store公開へ進む。全履歴監査を定例に増やさない。注文結果の計測が依頼された場合は既存measure_limit_outcomesの対象と限界を確認し、価格差を逸失利益や指値規律の因果効果にしない。

通知webhookのrotationはDiscordで新規/再生成→repository secret DISCORD_WEBHOOK_URL更新→通常runの通知確認。旧webhookはDiscord側で消すまで有効。rotationだけで過去Screeningを再実行しない。
