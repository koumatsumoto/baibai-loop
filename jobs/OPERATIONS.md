# 運用手順

machine処理、store反映、lakeの公開と復旧を扱う。repository rootのBashから、依頼された対象だけを操作する。全optionと内部判定は各CLIのhelp・実装が正本。責務とデータ正本は[architecture](../docs/architecture.md#store-authority)、開発準備は[development](../docs/development.md)にある。

<a id="actions-write-boundary"></a>
## 実行環境と接続

通常の取得・公開は既存Actionsを優先する。個別に依頼されたapplication更新は、認可された運用環境で取得済み作業コピーを正規CLIで更新し、条件付き反映する。物理的なPCだけに限定せず、対応OS・依存・接続を確認する。開発変更を本番操作・新課金・権限拡大の許可へ読み替えない。

baibai-storesは非公開の正本。既存Web/APIのコード撤去は、配備済みWorker・domain・credential・旧表示objectの停止や削除を実施するものではない。それらの実環境変更は対象と権限を確認した別操作とし、原本・application DB・台帳を巻き込まない。

| 実行主体 | 設定の所在と範囲 |
| --- | --- |
| 取得・公開Actions | R2_ACCOUNT_ID、R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY、JQUANTS_API_KEY/ESTAT_APP_ID/EDINET_API_KEY。必要stepだけへ渡す |
| 通知 | repository secret DISCORD_WEBHOOK_URL。通知stepだけが使う |
| 認可されたlocal運用 | repo外で扱う秘密と.env。stores用writerを通常開発へ流用しない |

秘密値・endpoint・私的なDB/原稿をGit・Issue・log・公開artifactへ置かない。認証失敗を別経路や権限拡大で迂回せず、設定の有無と対象接続を確認する。運用内容の許可はbroker操作や未報告取引の入力を含まない。

## 日次実行と結果確認

code修正が必要ならmain反映を終えてから、必要な取得・再計算を実行する。対象run IDを固定し、監視timeoutだけで終了と決めて重複dispatchしない。

```bash
gh workflow run cloud-daily-batch.yml --ref main
gh run list --workflow cloud-daily-batch.yml --limit 5
gh run watch <RUN_ID> --exit-status --interval 60
```

過去日を明示する場合だけdispatchへasofを渡す。通常cronは元の対象日を保ち、queue遅延で翌日へ進めない。非営業日も新しい経済・外部評価を統合できる。生成as_ofと価格観測日は別で、休場日に価格日を繰り上げない。原本の取得は独立scheduleのsource workflowが所有する。

共通統合の`cloud-daily-batch`は毎日13:43 UTC（22:43 JST）に開始時点のready受領一覧を固定する。全providerの完了を待たず、fresh canonicalのpull/hydrateを一回だけ行い、source所有のprojection/rebuildとEDINET offline抽出を適用する。`model-integration`はmarketを一回sealし、独立したeconomic snapshotを固定して五モデルと候補を保存した後、同じmarket releaseを公開する。外部取得やWeb生成は呼ばない。成功後にmachine storeを反映し、受領をreadback確認してsealする。source失敗では前回の適格投影を保ち、正常sourceを進める。モデル生成・lake公開の失敗では後続のmachine反映を止める。workflow全体の成否と、既に公開されたrelease/storeの到達点は分けて確認する。

取得期限は各source workflowがexport/private stagingの余白を残して設定する。retry/deadline停止で完了済みL1を取り消さず、次runで成功chunkから再開する。Retry-Afterを短縮せず、認証・403・解析失敗へtransport retryを広げない。日次に移行・全期間backfill・較正再構築を代行させない。

取得／private受領の通知と、共通統合の正本反映／読戻し／lease解放を区別し、run log、store push、release、generation/selection IDを照合する。通知配送失敗はjobを赤にしないため、通知がない場合もrun自体を確認する。watchdogは取得を12:00 UTC、共通統合を16:00 UTCに別々のschedule/asofで監視する。限定sourceの成功を全体成功へ数えず、経済は取得jobの受領markerを読む。MISSINGは対象日の成功run不足で、過去日確認は次を使う。

```bash
gh workflow run cloud-batch-watchdog.yml -f check_date=<YYYY-MM-DD>
```

過去日再計算でも、その日のmaster・価格・財務の対象日と欠測を保持する。現在の受領からの再計算を完全な当時point-in-time再現とは扱わない。

EDINETは取得日と抽出日を分ける。当日一覧・必要archiveの未到着は `applied.json` の `deferred_inputs` と通知の「入力延期」に残し、適格な保存値で統合を続ける。原本が届けば次回統合で再抽出する。別文書の未到着を理由に、保存済み原本の破損・解析失敗を隠さない。

Screening成功は全source成功を意味しない。旧snapshot日を当日へ書き換えず、`edinet_snapshot_asof` / `edinet_snapshot_recorded_at_utc`、文書ID・提出日時・対象期間で鮮度を確認する。採用条件は[Screeningの入力適格性](../docs/reference/screening-runtime.md#入力不足と品質)を参照する。入力延期だけなら `source-check` は成功、不正受領・解析・公開・readbackの失敗はエラーになる。check無効化や全取得の重複起動ではなく、失敗したsourceだけを修復する。

### マクロ原本の独立更新

`cloud-economic-sources`は日本株の営業日に関係なく毎日実行する。source catalogのproviderを共有quota単位にまとめ、各groupの取得→非公開R2受領を独立させる。取得側は担当groupだけの再生成可能なread-only inputを読み、正本全体をpullしない。成功sourceは同groupの失敗後も受領を残し、groupの取得結果は赤のまま報告する。handoffは既存stores bucketのcoordination/economic-sources/へ今回の経済受領だけを保存し、application・market storeや秘密を含めない。public repositoryのActions artifactへ原本を出さない。

groupと取得範囲は `data-collection economic groups` とData Collectionのsource catalogを正本とする。共有quota・取得方式ごとの制約を保ち、月次・四半期を一律「直前2か月」に縮めない。通常改訂や公表遅延を落とすためで、古い年次改訂は別の明示取得で扱う。全体ファイル配信では比較範囲を絞っても通信量は減らない。松井の現在ページから過去履歴を回復したとは扱わない。PMIの新配信元・速報採用は #1477 の未完了範囲。

`--recent`は通常窓に加え、既存source runの完了した取得範囲／通常窓の失敗以後に残った未確認区間を成功範囲のunionから回復する。初回は最近窓から開始する。過去の失敗した広範囲取得を通常runの起点にはせず、実在するnative日次受領とPDF対象月で回復済みを判定する。未取得が残る広範囲失敗は通知の「全履歴の限定回復待ち」に出し、明示的な期間指定refreshまたは`--all-history`で別に回復する。初期history seed・古い年次改訂照合も別操作である。過去の全改訂や完全な当時PITの再現を保証しない。

```bash
uv run baibai-engine data-collection economic groups
gh workflow run cloud-economic-sources.yml --ref main -f group=fred_csv -f source_id=fred-payems
```

限定再実行はgroupとその中のsource IDを指定する。取得失敗とprivate受領成功は併存し、原本未保存と正本未反映を混同しない。経済の取得開始は07:43 UTCで、土日も実行する。45分の取得期限と60分のjob上限を分け、通常の時間切れでも成功分をexport/stageする。全groupの完了を待たず、その日の共通統合が利用可能な受領と前回適格値からL2の読取材料を更新する。保存済みのL3判断を書き換えたり、新しい投資判断を自動作成したりしない。遅着は次回統合へ入る。

### source入力・市場原本の引渡し

共通統合だけが`cloud-publish`と共通leaseを持つ。取得側はsource別のread-only inputと既存private stagingを使い、market/application全体のhydrateや統合計算をしない。inputは正本generationと原本hashへ結び付く再生成物で、第二の正本ではない。source自身の履歴とcoverage（正常な空も含む）を一緒に保持し、部分storeへfull lake originを付けない。同じsource/groupは取得からstageまで直列化し、J-Quantsの市場・財務・TV master補完・経済取得は共有quotaを守る。

市場取得は`cloud-market-sources`で07:43 UTCにJ-Quants市場／財務・JPX決算予定／規制・EDINET一覧、08:43 UTCにEDINET metrics用archive、09:13 UTCにEDINET facts用archiveを別々に起動する。土日は一覧とarchiveの取得を続ける。metricsとfactsは一覧から独立して分岐し、factsの未初期化を成功した全件取得と扱わない。J-Quants需給は保存済みcalendar/barを使う。TradingViewは既存slotとexact-date master方針を保ち、自分の受領だけをprivate stageへ渡す。

市場の反映はJ-Quantsの範囲／単日projection、JPXのcollection固有の置換、EDINET retained receiptのrebuild、TradingViewの同日最初の正常選択を使う。`merge_market_store`は汎用fact mergerにしない。J-Quantsの古い範囲と後続範囲の再投影は一つのtransactionで確定し、途中失敗時は以前の投影を残す。破損handoffやsource固有のdecode失敗は記録して他sourceを続け、正本破損や全体のR2障害は停止する。外部評価を含め、失敗前に保持した原本も公開・読戻しの対象とし、不正typed値は採用しない。

初回input生成は既存正本から共通統合を一回実行する。`coordination/source-inputs/` のinput不足を全DB取得や空seedで迂回しない。独立取得の受領、共通統合のrelease/readback、モデル生成・候補公開時刻、watchdogを確認する。取得workflowと共通統合の責務を分け、削除済みの複合dailyを復活させない。

通常の遅着・未統合受領は次回共通統合、必要なら既存dailyの手動実行で拾う。一つのinput exportが失敗しても前回pointerを保ち、正常groupは更新する。経済の実取得はseedと同値でも一時受領へ残し、統合時にfresh canonicalと比較して変更だけ追記する。同値no-opを統合したstageもsource watermarkを進める。

すでに後続stageを統合した後に古いstageが現れた場合は`needs_explicit_recovery`として保全し、自動で現在値を巻き戻さない。stage ID、理由、対象source/期間、最新integrated markerを確認する。現行値の回復なら当該sourceを新runで再取得し、次回統合する。過去vintageそのものの補完が必要なら通常dailyへ再投入せず、原本を固定した個別回復として主担当が範囲と再投影を確認する。marker削除、受領日時の書換え、古いDBの丸ごとpushで通さない。全履歴rebuildは通常のrecent確認から分ける。

## クラウド正本をローカルへ取得する

分析用pull前にdailyのqueued/in_progressを確認し、通常は完了後に取得する。ローカルpublisherとも重ねない。TradingViewの未統合stageは共通統合後にreleaseへ入る。

```bash
jobs/scripts/pull.sh
```

machine receipt/ETagとhydrate開始時のmarket releaseを照合する。両者を同一transactionでは固定しないため、競合・検査失敗後のlocal stateを分析へ使わずfreshに取得し直す。applicationはbulk pullせずpull-appを使う。

新しいモデル入力の補修は固定generationの未生成・欠測理由と該当source受領から対象を限定し、取得欠落を同じas-ofのsource取得で補う。verify-cache-coverageは旧較正入力の保守診断で、新候補の共通資格gateにしない。提供元のnullやL2計算不能を取得失敗とせず、値が埋まるまで全期間を再取得しない。

## ローカルからクラウドを更新する

変更codeをmainへ反映し、lake対象tableは**変更前**に現行releaseからhydrateする。変更後のhydrateは成果を上書きする。長い取得・準備を先に済ませ、production read/merge/publishは同じpublication lease内で行う。

```bash
with_publication_lease() (
  handle="$(mktemp "${TMPDIR:-/tmp}/baibai-publication-lease.XXXXXX.json")"
  rm -f "$handle"
  uv run python -m baibai_jobs.storage.publication_lease acquire --purpose operator --handle "$handle" || return $?
  if PUBLICATION_LEASE_HANDLE="$handle" "$@"; then result=0; else result=$?; fi
  uv run python -m baibai_jobs.storage.publication_lease release --handle "$handle" || return $?
  return "$result"
)
```

失敗時もreleaseするため関数内でset -eを使わない。busyならowner・expiryとrun/processを確認し、blind retryしない。release失敗はhandleを保つ。GitHubのcloud-publish queueだけではlocal writerを排除しない。push-app/publish.shと初回seedはこのlease対象ではない。

対象に合う一経路だけを使う。lake変更はpublish-lake→push-market、store-localだけならpush-market、経済原本だけならpush-indicators。直前のbundleからlocal daily全体を実行した時はpublish-lake→push-machineとし、3 storeを個別pullで組み替えない。

```bash
with_publication_lease bash -c 'jobs/scripts/r2_transfer.sh publish-lake && jobs/scripts/r2_transfer.sh push-market'
with_publication_lease jobs/scripts/r2_transfer.sh push-indicators
```

merge・origin・schema・取得ETag/CASの失敗では公開を止める。lake→machine store→受領sealの順を保ち、coverageだけが先に進む状態を作らない。

malformed leaseは自動修復しない。GitHubとlocalにactive/pending writerがないと確認した場合だけexact coordination/publication-lease.jsonを調べ、対象1keyの削除後に通常acquireで再作成する。prefix削除や手書きJSON上書きを通常運用へ入れない。

## application DB を反映する

作業開始前に、空の作業領域へpull-appしてDBとstores/.r2-generations/baibai.sqlite.etagを一組で取得する。そのDBを正規CLIで更新し、取得ETagを保持する。

```bash
jobs/scripts/r2_transfer.sh pull-app
```

更新済みDBと取得ETagがある場合は、再pullせず次だけを実行する。既存DBがあればpull-appは拒否する。DBだけを別世代へ替えてsidecarを流用しない。

```bash
jobs/scripts/publish.sh
```

publish.shは引数なしで使う。台帳preflight、取得世代への条件付きbackup/PUT、次更新用ETag記録を行う。保有quote/basis不足による時価未評価は実取引保存の障害と区別する。未知世代を現在HEADで補わない。

application backupは実行時JST日付の日別keyで、同日keyを置換する。古い世代の整理を行わない依頼ではR2_APP_BACKUP_PRUNE=skipを指定できるが、同日置換は変わらない。元世代の保全や置換が依頼範囲に含まれるか確認する。

保存、R2反映、読戻しを別々に確認する。対象Proposalのimmutable IDと参照評価を読み戻し、最新候補へ固定Selectionを付け替えない。競合なら未公開変更を退避し、最新正本へ必要差分を正規serviceで再適用する。PUT後の記録失敗を未反映と決めて旧DBを再送しない。

current schemaだけを開く。schema変更はsource versionを限定した一回移行を別コピーで検証し、対応codeとstoreの反映を同じdeliveryで終えたらtoolを撤去する。旧DB/keyや完了済みcutoverを現行正本へ再適用しない。

### application DB を復元する

application DBは判断とledgerを保持するため再生成しない。通常の判断更新は前節の取得→正規CLI更新→反映に従う。この節は、明示的に選んだ過去snapshotへクラウド正本を戻す復旧である。
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

ledgerを復元対象の時点と照合し、不一致なら手順5へ進む。照合成功後は取得世代を保ったまま再pullせず、前節の`publish.sh`で反映してから判断業務を再開する。公開途中の失敗では検証済みlocalを戻さず、upload・読戻しの到達点を確認して反映側を復旧する。ledgerを`head`へpipeしない。

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


<a id="research-input-preparation"></a>
## Investment Judgment入力の準備

新規・再開の入口は[Investment Judgment skill](../.agents/skills/investment-judgment/SKILL.md)。企業成長は固定releaseの財務・masterとmanifestを配置したローカルmirrorを読む。通常は既存`hydrate-market`を使い、別配置なら`investment-judgment prepare --company-growth-mirror <LOCAL_MIRROR>`を指定する。query自体はネットワーク取得をせず、mirror不足を更新中のSQLite行で代替しない。[固定release](#fixed-release-read)の手順で準備し、取得/coverageの不足と補助機能の未提供は[企業成長の契約](../docs/reference/company-growth.md)に従って区別する。

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
    accepted_dataset,
    materialize_partitions,
    resolve_release,
    selected_partitions,
)

mirror = Path("<COMPLETE_LOCAL_MIRROR>")
source = LocalMirrorSource(mirror)
release = resolve_release(source, "<RELEASE_ID>", manifest_sha256="<MANIFEST_SHA256>")
name = "edinet.documents"
paths = materialize_partitions(
    LakeObjectCache(root=mirror, source=source),
    dataset=accepted_dataset(release, name),
    partitions=selected_partitions(release, name),
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

### 部分 push からの復旧

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

原本が誤っていればsource訂正取得または明示再構築。local DELETEをcloud mergeによる訂正に使わない。系列追加/退役/改名や式変更はL2定義を直し、外部取得条件を変える場合だけData Collectionも直す。原本から再計算できる変更へ再取得・scalar撤回・registry世代台帳を要求しない。

```bash
uv run baibai-engine data-collection economic list
uv run baibai-engine data-collection economic refresh <SOURCE_ID> --all-history --end <YYYY-MM-DD>
uv run baibai-engine market-context reading --asof <YYYY-MM-DD>
uv run baibai-jobs validate-market-context-stores
```

最後のcommandは原本storeのpayload・record count等を確認する。L2数式の証明やL3全履歴検査ではない。原本更新だけpush-indicatorsへ進み、式変更だけなら不要。取得失敗でcommit済み原本を取り消さず、L2欠測を直すため全取得を繰り返さない。

optionsのall-historyは保存済み古い日を再利用して直近と不足日を取得する。古い訂正は日付範囲を明示する。購読rolling境界の提供不能を示し、空応答と途中失敗を区別する。式変更だけでrawを再取得しない。

PMI manifestは公表翌週にdry-run→更新→対象月refresh→公表値照合→commitする。

```bash
uv run python -m tools.pmi_manifest --dry-run
uv run python -m tools.pmi_manifest
```

PDF自身のPMI名・embargo月・対象headline月を確認し、月の穴を飛び越えない。indexの最新月以前はarchive snapshotからrelease IDを辿り、既存月と照合する。翌月PDFの明示restateを使う時も元の対象月を保つ。URL訂正と同URL本文差替えを区別し、抽出式だけなら保存PDFを再読する。

<a id="edinet-metrics-revision"></a>
## EDINET metricsの意味変更

[抽出意味revision](../docs/reference/data-sources.md#edinet-metric-revision)を変える時だけ実施する。Company Research facts revisionとは別。隔離コピーで保持CSV ZIPを再抽出し、期待差分・quality/failure・coverageを確認する。

main反映とwriterの非競合を確認し、同じlease内でfresh pull/hydrate→比較元backup→extract-edinet-metrics→publish-lake→push-market→固定release読戻しを行う。原本不足で取得が必要な時だけrefresh-edinet-metricsを使う。対象as-ofを指定し、保存値とrevision・store-local情報の整合を確認する。

期待外差分、取得失敗、世代競合、lease失効で後段を止める。準備コピーを本番へ戻さず、再抽出後にhydrateせず、旧値のrevisionだけを付け替えない。通常dailyへ意味移行を代行させない。

<a id="edinet-source-archives"></a>
## EDINET原本とCompany Research facts

原本再利用は明示したmarket SQLiteのedinet_source_archivesから行い、以前の端末ZIPを暗黙に取り込まない。stored_onは初回保存UTC日で開示・初回利用可能日ではない。受領ZIPとL2の選択文書・metricsを混同せず、取得エラーで既存ZIPを上書きしない。

<a id="edinet-research-facts"></a>
未初期化storeだけ、整合する隔離marketとdocument inventoryを指定して初期抽出する。

```bash
uv run baibai-engine data-collection market refresh-edinet-facts \
  --asof <YYYY-MM-DD> --initialize --sqlite-path <STAGED_MARKET_SQLITE>
```

失敗後は同commandで再開し、ok書類を再取得しない。保存原本だけの再計算はextract-edinet-facts。ticker限定は全体initializedを作らない。未対応形式の理由付きokと取得/破損のfailedを区別し、代表原典、missing reasons、export/hydrateを確認してcode・L1・coverageを一組で反映する。

dailyは未初期化をskipし、初期化後の新規年次/訂正を独立処理する。最新訂正失敗を旧版で埋めず、failed docIDの原因を直して再開する。利用側の選択は[固定release](#edinet-research-query)に従う。


<a id="external-assessment-acquisition"></a>
## 外部評価の取得・再開

限定collectは明示した少数銘柄の公開HTTPを直列で読み、private captureへ保存する。通常TLS/proxyを使い、403/429でそのsourceを止める。captureの保存先は試行ごとに別名にし、部分captureを再取得で上書きしない。既存のcurrent-schema隔離marketへimportする。

```bash
uv run baibai-engine external-assessment collect --source minkabu --tickers 7203 4436 --output /tmp/private-external-assessment/responses.json
uv run baibai-engine external-assessment import --input /tmp/private-external-assessment/responses.json --db /tmp/private-external-assessment/market.sqlite
```

importは原本を保持して選択attemptを再解析する。parse失敗時にL1は残り得るがL2を部分追記しない。原本/観測の挿入件数と再投入0を確認し、HTTP・parse・未試行・正常nullを分ける。新規DB初期化や旧schema移行は行わない。

### 日次取得と固定コホートの再開

source別planは固定release/calendar/masterとparser/request条件へ束縛する。19:13 JSTの新規plan作成時に当日exact masterがまだlake未反映なら、J-Quants/TVのready handoffが公開する小さなmaster/calendar依存入力を使う。価格を含むSQLite全体は取得せず、原受領ID・元の受領時刻・dependency/handoff hashとbase releaseをplanへ固定する。過去masterを当日へ読み替えない。継続pagination・不完全master・calendarの欠落は不適格として取得前に失敗し、既存planの再開は最新依存を読み直さない。初回移行ではinput生成後に新しいJ-Quants/TV取得がdependency付きreadyを保存したことを確認してから外部評価を受け入れる。masterが遅延した場合はそのsourceを限定再実行し、共通統合の先行公開で帳尻を合わせない。sourceはmonex/rakuten/kabumap/minkabu、allは4取得元を選ぶ。同sourceのlocalとActions取得を重ねない。scheduleの対象日は元runから固定し、受領日をmaster/cron日へ替えない。

```bash
gh workflow run cloud-external-assessment.yml --ref main \
  -f source=monex -f batch_id=<LABEL> -f master_asof=<YYYY-MM-DD>
```

scheduled再開は既存external-assessment-daily-<source>-YYYY-MM-DDをそのまま指定し、欠落planを現在masterで再作成しない。手動labelはsource prefixを重ねない。stage確定済みentry/受領時刻を変えず未試行pairだけを再開する。保存失敗pairの再取得は別labelにする。collector binding変更で拒否されたplanを手書きhashで通さない。

### 原本handoffと公開

raw bundleをstageより先にuploadする。未確定GETはhard kill後に繰り返され得る。external-assessment/raw/は3日期限の短期handoffであり、厳密72時間でも長期L1でもない。実lifecycleを確認し、期限切れ原本の復元を保証しない。publisherがmarketへ取り込み固定lakeを読戻して初めて長期L1となる。

取得はlease外でsourceごとにacquire→inspect→immutable readyを完了する。共通統合scheduleはその時点のready exact planだけを固定し、raw/typed/historyを照合して一回の共通releaseへ公開する。mutable checkpointの更新を同じrun途中で読み直さない。history_digestsは保存後baselineで、更新前の全履歴保全まで証明したとは説明しない。原本・stage破損はsource別の失敗として報告し、正常sourceの統合を進める。失敗sourceのterminalは閉じない。

readback後にsource別terminalを保存する。pointer成功後のterminal失敗は未反映と断定せず、既存terminalを読んで再開する。sealed sourceは以前の検証としてno-op、未sealedはfresh hydrateからidempotentに再適用する。未確認件数を0にしない。

403/429・未完了sourceを自動で閉じない。確定分だけを明示する操作はsource単独で行い、取得を再実行しない。`stage_confirmed_partial`は確定stageをprivate readyへ固定し、実際の公開とterminalは次回共通統合で行う。

```bash
gh workflow run cloud-external-assessment.yml --ref main \
  -f source=minkabu -f batch_id=<LABEL> -f master_asof=<YYYY-MM-DD> \
  -f operation=stage_confirmed_partial
```

固定pair、正常null、失敗・未試行、完全/部分公開、release/hash・readback・lease解放を確認する。lake反映は共通統合が担当する。通知・CI緑・stage保存だけで正本反映成功を認定しない。

<a id="tradingview-expectations"></a>
## TradingView観測

slotは既存workflowを正本とする。営業日は15:30以降の当日master、休場日は09:00以降の直近営業日の保存済みexact-date masterを使う。calendar/master不足や引け前は取得せず、時間guardをmaster指定で解除しない。targetは実開始時のJST当日、fetched_atは実受領でprovider公表日ではない。

同日最初の正常row・null・unresolvedを保持し、後続は未保存銘柄だけ取得する。全件account済みは全metric利用可能の意味ではない。新rowを含むpartial受領をprivate stageへ保存し、日跨ぎresponseを前日へ保存しない。過去日の穴を現在値で補わない。TV取得jobはreleaseを直接公開せず、共通統合が既存の正常選択を狭めずに反映する。

### 当日の手動観測

```bash
gh workflow run cloud-tradingview-snapshot.yml --ref main \
  -f observation_master_asof=<YYYY-MM-DD> -f interval=15
```

同日再開は同じmaster日を指定する。target/master、first/last受領、追加/残件、stop_reason、private stageを確認する。共通統合側でrelease/readbackとlease解放を確認する。失敗runでも原受領だけを保存する経路があるため、L1と正常L2の件数を混同しない。storage/internal障害や強制終了を取得成功としない。

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
uv run baibai-jobs tradingview --state-file "$HOME/.cache/baibai-loop/tradingview-local/oauth.json"
```

同fileの取得・smoke・再認証を並行せず、GitHub smokeとも重ねない。runnerがlease→pull/hydrate→preflight→未保存取得→release公開→lease解放を行う。部分保存、公開release/hash、残件を読戻す。no-opとpartial、取得・公開成功を分ける。公開失敗後のlocalだけを保存済み根拠とせず次回はfreshから始める。

local認証失敗はlocal fileを再認証しGitHub secretを変えない。secret_persistenceは書込権限・容量を確認して再認証し、古いrotation fileでretryしない。継続性確認を明示された時だけ、非取得中にtradingview smoke --state-fileで同fileの次process利用を確かめる。

### 保存と再試行

MCP transport失敗とtool内provider失敗、正常null/unresolvedを分ける。取得不良の要求単位は未保存のまま次へ進み、連続不良や429で新requestを止める。同じ要求単位の即時retryをせず通常intervalを守る。正常な受領分を公開し、次slotは未保存分から再開する。

Retry-Afterがある場合は期限を守る。Actions cooldownの保存失敗や強制終了で記録不明ならworkflowを止め直前応答と期限を確認し、盲目的に再実行しない。local手動取得も同じ待機指示を守る。authは対話認証、provider_responseはsafeなvalidation reason、storageはcalendar/master/schemaを起点に調べる。例外本文・raw・秘密をIssueへ出さない。

保存L1の一日分だけをoffline再構築する時は、同日・master・tool・Universe・受領時刻のreceiptを指定する。

```bash
uv run baibai-engine tradingview rebuild --sqlite /tmp/market-rebuild.sqlite \
  --asof <YYYY-MM-DD> --receipt-id <RECEIPT_ID>
```

正常receiptなし・銘柄重複なら置換せず、明示missingはunresolvedへ再現する。元受領時刻を維持し、拒否応答を正常値で補わない。


## Storeをseedする

初回だけ、4 keyすべて不存在を確認してseed.shを使う。既存keyが一件でもあれば停止し、通常更新/復旧へ戻る。1〜3 keyの部分seedは、供用前・writerなし・対象exact keyを確認できた場合だけ初回復旧として扱い、通常pull/pushや無条件上書きで直さない。seed後のapplication更新はpull-appで正本と世代を取り直す。

## 定期maintenance

既存のdue taskを使い、同じ期限・対象・問いのopen taskを重複登録しない。実行後は完了を記録し、必要な次回分だけ作る。開発変更はGitHub Issueで管理する。

calibrationは月初、前月の完全月末まで同じrulesでbuild→evaluateする。forceは全再構築の時だけ使う。

```bash
uv run baibai-engine screening calibration-build --start <YYYY-MM-DD> --end <YYYY-MM-DD>
uv run baibai-engine screening calibration-evaluate --out /tmp/calibration-evaluation.yaml
```

判断面の文脈を更新する場合は目的・required月・必要metricを先に固定し、同じ評価に明示的な出力先を加える。`--context-out`に既定の発行先はなく、省略時に配布artifactを更新しない。

```bash
uv run baibai-engine screening calibration-evaluate \
  --run-purpose empirical_change_evidence --decision-subject estimator_policy \
  --required-asof <YYYY-MM-DD> --required-metric er_level_calibration \
  --out /tmp/calibration-evaluation.yaml \
  --context-out engine/src/baibai_engine/machine_analysis/company_valuation/calibration/published/er-level-calibration-latest.yaml
```

required-asofは事前に選んだ全対象月分を繰り返す。配布前にoperative runのrules/model identity・期限・不足を照合し、Gitでreviewする。[較正の意味](../docs/reference/estimate-calibration.md)に従い、記述context成功をformal evidence成立としない。古いcontextの日付・hashだけを編集しない。

PMIは公表翌週、TSE資本政策とJPX廃止は翌月15日以降に既存refresh/buildを行い、必要なlake/store公開へ進む。全履歴監査を定例に増やさない。注文結果の計測が依頼された場合は既存measure_limit_outcomesの対象と限界を確認し、価格差を逸失利益や指値規律の因果効果にしない。

通知webhookのrotationはDiscordで新規/再生成→repository secret DISCORD_WEBHOOK_URL更新→通常runの通知確認。旧webhookはDiscord側で消すまで有効。rotationだけで過去Screeningを再実行しない。

<a id="web-serving"></a>
## 保有・運用Webの表示更新

表示はdashboard・outcomes・tasksの3 viewだけ。旧候補・macro・調査一覧・L1公開取得APIを復活させない。coreの日次統合が保存した後に同じローカルmarketとapplicationから生成・配信する。表示失敗で生成モデルやsource封印を巻き戻さない。台帳・taskの明示更新後は既存のpublish.shがapplication条件付き反映後にcloud-materializeを起動する。

手動再生成はmain上のcloud-materializeを実行する。marketだけをpullし、固定releaseからhydrate、applicationをpullして3 viewを生成する。旧run・economic storeは不要。Web切替前に3 viewの保存・認証付き読戻しとUIの対象HEADを確認し、web workflowをmainで明示dispatchする。PRのCI成功は本番更新ではない。旧object・Worker secretは削除しない。

```bash
uv run python -m baibai_web.materialize --output-dir /tmp/baibai-serving
```

これはローカル生成だけであり公開しない。cloud workflow内だけが `jobs/scripts/r2_transfer.sh upload-portfolio-views /tmp/baibai-serving` で3 viewを公開する。資本・損益・保有の欠測とエラー、タスク、運用成績を読戻しで照合する。
