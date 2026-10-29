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

手動実行では目的の基準日を明示する。以下は2026-10-26の寄前・場中に、完了済みの前日までを再計算する例。

```bash
gh workflow run cloud-daily-batch.yml --ref main -f asof=2026-10-25
gh run list --workflow cloud-daily-batch.yml --limit 5
gh run watch <RUN_ID> --exit-status --interval 60
```

手動asofの空欄は元run作成時刻のJST日であり、直近の完了日や利用可能な終値を自動選択しない。寄前・場中に完了済みの日次モデルを使うならJSTの前日を指定し、前営業日まで戻さず週末の経済・外部評価も対象に残す。同日引後でも共通calendarが要求する価格日と有効な株式分母が揃わなければ価格座標を計算できず、候補0件になり得る。この0件を「買う対象がない」という投資判断に読み替えず、入力不足の理由を確認する。通常cronは元の対象日を保ち、queue遅延で翌日へ進めない。生成as_ofと価格観測日は別で、休場日に価格日を繰り上げない。原本の取得は独立scheduleのsource workflowが所有する。

既定読取は発行時刻よりas_ofの新しさを優先し、古い非空候補へ自動fallbackしない。過去日を後から再計算しても新しいas_ofの結果は置換しないため、その日を分析するときは対象のgeneration/selection IDを明示する。

共通統合の`cloud-daily-batch`は毎日13:43 UTC（22:43 JST）に開始時点のready受領一覧を固定する。全providerの完了を待たず、fresh canonicalのpull/hydrateを一回だけ行い、source所有のprojection/rebuildとEDINET offline抽出を適用する。`model-integration`はmarketを一回sealし、独立したeconomic snapshotを固定して五モデルと候補を保存した後、同じmarket releaseを公開する。外部取得やWeb生成は呼ばない。成功後にmachine storeを反映し、受領をreadback確認してsealする。source失敗では前回の適格投影を保ち、正常sourceを進める。モデル生成・lake公開の失敗では後続のmachine反映を止める。workflow全体の成否と、既に公開されたrelease/storeの到達点は分けて確認する。

取得期限は各source workflowがexport/private stagingの余白を残して設定する。retry/deadline停止で完了済みL1を取り消さず、次runで成功chunkから再開する。Retry-Afterを短縮せず、認証・403・解析失敗へtransport retryを広げない。429の再試行を続けられないときは、その取得pass内の同quota groupだけ新規GETを止め、既受領と他groupの取得を保つ。経済native取得の再試行は失敗した1 GETだけで行い、成功済みの別月やprovider全体の解析をやり直さない。H.15も1回の取得でplain→browserの選択を繰り返さず、browser起動不能はsource拒否として記憶しないため、後の独立取得で再試行できる。文書取得の再試行方針は変えない。日次に移行・全期間backfill・較正再構築を代行させない。

取得／private受領の通知と、共通統合の正本反映／読戻し／lease解放を区別し、run log、store push、release、generation/selection IDを照合する。通知配送失敗はjobを赤にしないため、通知がない場合もrun自体を確認する。watchdogは取得を12:00 UTC、共通統合を16:00 UTCに別々のschedule/asofで監視する。限定sourceの成功を全体成功へ数えず、経済は取得jobの受領markerを読む。MISSINGは対象日の成功run不足で、過去日確認は次を使う。

```bash
gh workflow run cloud-batch-watchdog.yml -f check_date=<YYYY-MM-DD>
```

過去日再計算でも、その日のmaster・価格・財務の対象日と欠測を保持する。現在の受領からの再計算を完全な当時point-in-time再現とは扱わない。

EDINETは取得日と抽出日を分ける。当日一覧・必要archiveの未到着は `applied.json` の `deferred_inputs` と通知の「入力延期」に残し、適格な保存値で統合を続ける。原本が届けば次回統合で再抽出する。別文書の未到着を理由に、保存済み原本の破損・解析失敗を隠さない。

metricsのlookbackは取得coverageと抽出候補の範囲であり、archive取得計画・明示抽出・共通統合とも訂正・取下げの解決に使う保持origin/祖先は切り捨てない。旧年度operationを誤ってorphan扱いしたquarantine行は再利用せず、archive取得計画で選択された現年度文書のCSVを確保し、次回統合で回復する。CSV未到着なら入力延期を保ち、古い正常baselineへ戻さない。保持証拠の範囲だけを直すこの回復に、全期間再取得・全件再抽出や抽出意味revisionの付替えは不要。

一覧の不正再受領では既存のok日の投影・受領時計を保つ。未適格日の不正受領は、選択できた正常原典由来の証拠とfailed coverageを保存し、`edinet.documents`だけはpartialでも公開して正常な別sourceと原本保存を止めない。partial公開は日別取得の完了認定ではなく、厳密な日付読取はfailed日を拒否する。正常な別日の訂正・取下げは反映し、不正日は新しいhandoffがなくても失敗報告を続け、正常受領で回復するまで`source-check`は成功にしない。

Screening成功は全source成功を意味しない。旧snapshot日を当日へ書き換えず、`edinet_snapshot_asof` / `edinet_snapshot_recorded_at_utc`、文書ID・提出日時・対象期間で鮮度を確認する。採用条件は[Screeningの入力適格性](../docs/reference/screening-runtime.md#入力不足と品質)を参照する。入力延期だけなら `source-check` は成功、不正受領・解析・公開・readbackの失敗はエラーになる。check無効化や全取得の重複起動ではなく、失敗したsourceだけを修復する。

### マクロ原本の独立更新

`cloud-economic-sources`は日本株の営業日に関係なく毎日実行する。source catalogのproviderを共有quota単位にまとめ、各groupの取得→非公開R2受領を独立させる。取得側は担当groupだけの再生成可能なread-only inputを読み、正本全体をpullしない。成功sourceは同groupの失敗後も受領を残し、groupの取得結果は赤のまま報告する。handoffは既存stores bucketのcoordination/economic-sources/へ今回の経済受領だけを保存し、application・market storeや秘密を含めない。public repositoryのActions artifactへ原本を出さない。

groupと取得範囲は `baibai-jobs acquire-economic groups` とData Collectionのsource catalogを正本とする。共有quota・取得方式ごとの制約を保ち、月次・四半期を一律「直前2か月」に縮めない。通常改訂や公表遅延を落とすためで、古い年次改訂は別の明示取得で扱う。全体ファイル配信では比較範囲を絞っても通信量は減らない。松井の現在ページから過去履歴を回復したとは扱わない。PMIの新配信元・速報採用は #1477 の未完了範囲。

`--recent`は通常窓に加え、既存source runの完了した取得範囲／通常窓の失敗以後に残った未確認区間を成功範囲のunionから回復する。初回は最近窓から開始する。過去の失敗した広範囲取得を通常runの起点にはせず、実在するnative日次受領とPDF対象月で回復済みを判定する。未取得が残る広範囲失敗はhandoffの`result.json`とrun logの`history_recovery_required`で確認し、明示的な期間指定refreshまたは`--all-history`で別に回復する。初期history seed・古い年次改訂照合も別操作である。過去の全改訂や完全な当時PITの再現を保証しない。

```bash
uv run baibai-jobs acquire-economic groups
gh workflow run cloud-economic-sources.yml --ref main -f group=fred_csv -f source_id=fred-payems
```

限定再実行はgroupとその中のsource IDを指定する。取得失敗とprivate受領成功は併存し、原本未保存と正本未反映を混同しない。経済の取得開始は07:43 UTCで、土日も実行する。45分の取得期限と60分のjob上限を分け、通常の時間切れでも成功分をexport/stageする。全groupの完了を待たず、その日の共通統合が利用可能な受領と前回適格値からL2の読取材料を更新する。保存済みのL3判断を書き換えたり、新しい投資判断を自動作成したりしない。遅着は次回統合へ入る。

### source入力・市場原本の引渡し

日次の独立source取得Actionsと共通統合では、共通統合だけが`cloud-publish`と共通leaseを使う。取得側はsource別のread-only inputと既存private stagingを使い、market/application全体のhydrateや統合計算をしない。TradingViewの新規取得は下記の[提供元条件](#tradingview-expectations)に従い、batchへ組み込まない。

inputは正本generationと原本hashへ結び付く再生成物で、第二の正本ではない。source自身の履歴とcoverage（正常な空も含む）を一緒に保持し、部分storeへfull lake originを付けない。同じsource/groupは取得からstageまで直列化し、J-Quantsの市場・財務・経済取得は共有quotaを守る。

市場取得は`cloud-market-sources`で07:43 UTCにJ-Quants市場／財務・JPX決算予定／規制・EDINET一覧、08:43 UTCにEDINET metrics用archive、09:13 UTCにEDINET facts用archiveを別々に起動する。土日は一覧とarchiveの取得を続ける。metricsとfactsは一覧から独立して分岐し、factsの未初期化を成功した全件取得と扱わない。J-Quants需給は保存済みcalendar/barを使う。TradingViewの定期取得は設けず、保存済み受領の保持と履歴読取を新規取得から分離する。

定時market sourceの対象日と曜日分岐は、元run作成時計以前の直近の該当cron slotで固定し、JST日跨ぎ・作成後のqueue待機・rerunで翌日へ進めない。手動は明示asofを優先し、空欄だけ元run作成時のJST日を使う。本来の予定日時はAPIから得られないため、run作成自体が次の同slotを越える24時間以上の遅延は復元できず、対象日を確認して明示asofで扱う。

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
  uv run python -m baibai_jobs.publication.lease acquire --purpose operator --handle "$handle" || return $?
  if PUBLICATION_LEASE_HANDLE="$handle" "$@"; then result=0; else result=$?; fi
  uv run python -m baibai_jobs.publication.lease release --handle "$handle" || return $?
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

### Research保存のschema 25→26切替

この切替は通常runtimeの自動移行ではない。取得ETagと対応するschema25作業コピーを保全し、`tools/cutover_application_v25_to_v26.py --source <schema25.sqlite> --destination <new-schema26.sqlite>`で別ファイルを作る。旧行・payload・hashを再計算せず、新しいResearch領域だけを追加する。

共通のpublication leaseはapplication writerを排他しない。切替前に全application writerの更新を止める段取りを確定し、対応コードとschema26正本を一組で反映する。取得ETagへの条件付き反映が競合した場合は、最新schema25コピーから移行し直し、古い移行コピーを押し込まない。反映前に既存の同日backupを独立した退避先へ保全するか、上書きしない方法を確定する。`R2_APP_BACKUP_PRUNE=skip`だけでは同日backup置換を防げない。

反映後にschema26と既存評価・Selection・台帳の保持、新Research保存・再読、viewの読戻しを確認してwriterを再開する。コードmergeだけをstore切替完了とは扱わない。移行完了後はこの一回用toolと手順を撤去し、経緯をIssue #1652へ残す。


### 通常のapplication更新

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
  uv run baibai-engine portfolio ledger --db stores/application/baibai.sqlite
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
  uv run baibai-engine portfolio ledger --db stores/application/baibai.sqlite
)
```

途中停止では残存pathと失敗commandを確認し、blockを無条件反復しない。


<a id="fixed-release-read"></a>
## 固定releaseを読む

最初にcurrentを一度解決し、release IDとmanifest SHA-256を固定する。以後は同じmanifestが列挙したdataset/objectだけを読む。optional datasetなしは未収録で、他releaseのpartitionやprovider値で穴埋めしない。

```bash
uv run baibai-jobs snapshots resolve --mirror stores --bucket baibai-stores --format json
uv run baibai-jobs snapshots resolve --mirror stores --bucket baibai-stores \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

resolveはmanifestの解決で、data objectの検証や任意SQL実行ではない。

objectのdigest・bytes・row数・schemaを通常のlake readerで照合する。Webの旧L1 gatewayは廃止したため、認可済みR2読取か完全なローカルmirrorを使う。固定objectを取得できなければ別releaseの値で埋めず、再開時に同じ固定releaseを復元できるか確認する。新しいcurrentを採る場合は別の分析入力として固定し直す。

<a id="local-release-hydrate"></a>
### remote拡張なしのhydrate

必要量とsnapshot用容量を確認し、manifest/objectの完全mirrorを認可済み読取で揃える。部分JSON exportで正規market readerを代用しない。

```bash
uv run baibai-jobs snapshots hydrate \
  --mirror <COMPLETE_LOCAL_MIRROR> --store <LOCAL_MARKET_COPY> \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

ローカルmirrorではbucketを付けない。不足・破損・容量不足なら停止し、途中でreleaseを替えない。hydrateは対象tableを置換するが、store-local coverageの取得記録は生成しない。旧contractのreleaseは元commitのcodeで読み、runtime互換を増やさない。

完全mirrorからParquetを読む最小例:

```python
from pathlib import Path
from baibai_engine.machine_analysis.observations.disclosures.snapshot_contracts import (
    EDINET_DOCUMENTS,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import (
    accepted_dataset,
    materialize_partitions,
    resolve_release,
    selected_partitions,
)

mirror = Path("<COMPLETE_LOCAL_MIRROR>")
source = LocalMirrorSource(mirror)
release = resolve_release(source, "<RELEASE_ID>", manifest_sha256="<MANIFEST_SHA256>")
name = EDINET_DOCUMENTS.name
paths = materialize_partitions(
    LakeObjectCache(root=mirror, source=source),
    dataset=accepted_dataset(release, EDINET_DOCUMENTS),
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
from baibai_engine.machine_analysis.observations.financials.edinet_facts.selection import (
    latest_annual_filing,
    usable_document,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.store import (
    document_inventory,
)

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
uv run baibai-jobs snapshots inventory --root stores
uv run baibai-jobs snapshots gc --mirror stores
uv run baibai-jobs snapshots gc --mirror stores --apply --plan-hash <HASH>
```

immutable prefixのBucket Lockとmutable pointer/stagingを区別する。rollback pointerや全履歴監査を追加せず、問題releaseは正しいstoreから新releaseを前向きに発行する。

## R2 transferの安全境界

machine pushは前世代をkey.bakに1世代保存し、次pushで置換する。applicationだけ日別backupを保持する。3 storeは原子的反映ではなく、全PUT後にmachine-manifest.json receiptを書く。schema修正でstore全体を古いcopyへ戻すと後続事実を失うため、sourceを限定した移行を使う。

machine前世代を復元する場合はwriterを止め、対象bucketとexact key/key.bakを確認し、aws s3api copy-object --cli-read-timeout 300を使う。復元後はfresh pullし、receipt・generationの整合を確認する。盲目的なcopyやprefix削除は行わない。

### 部分 push からの復旧

先行keyだけPUT成功すると旧receiptと不整合になりpullが停止する。同じpush/dailyを再送せず、到達点をlogで確認する。local成果を残す必要があり現行origin/merge条件を満たすならpush-market/push-indicatorsで反映してreceiptを更新する。runsだけの成果は取得世代条件を満たすpush-runsを使う。全storeの成果を反映する場合は元bundleの世代条件を満たすpush-machineを使う。

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
uv run baibai-jobs acquire-economic list
uv run baibai-jobs acquire-economic refresh <SOURCE_ID> --all-history --end <YYYY-MM-DD>
uv run baibai-engine market-context reading --asof <YYYY-MM-DD>
uv run baibai-jobs validate-market-context-stores
```

最後のcommandは原本storeのpayload・record count等を確認する。L2数式の証明やL3全履歴検査ではない。原本更新だけpush-indicatorsへ進み、式変更だけなら不要。明示CLI取得では作業copyと同値の実受領も保存・発行する。長い取得中に正本が更新された場合や古い受領が後から統合される場合も、実際の最新受領を失わないためである。全履歴取得では同値・空応答の行も増えるため、必要なsourceと期間へ限定し、通常dailyの変更時のみ保存とは区別する。取得失敗でcommit済み原本を取り消さず、L2欠測を直すため全取得を繰り返さない。

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
uv run baibai-jobs maintain-market refresh-edinet-facts \
  --asof <YYYY-MM-DD> --initialize --sqlite-path <STAGED_MARKET_SQLITE>
```

失敗後は同commandで再開し、ok書類を再取得しない。保存原本だけの再計算はextract-edinet-facts。ticker限定は全体initializedを作らない。未対応形式の理由付きokと取得/破損のfailedを区別し、代表原典、missing reasons、export/hydrateを確認してcode・L1・coverageを一組で反映する。

dailyは未初期化をskipし、初期化後の新規年次/訂正を独立処理する。最新訂正失敗を旧版で埋めず、failed docIDの原因を直して再開する。利用側の選択は[固定release](#edinet-research-query)に従う。


<a id="external-assessment-acquisition"></a>
## 外部評価の取得・再開

限定collectは明示した少数銘柄の公開HTTPを直列で読み、private captureへ保存する。通常TLS/proxyを使い、403/429でそのsourceを止める。captureの保存先は試行ごとに別名にし、部分captureを再取得で上書きしない。既存のcurrent-schema隔離marketへimportする。

```bash
uv run baibai-jobs external-assessment collect --source minkabu --tickers 7203 4436 --output /tmp/private-external-assessment/responses.json
uv run baibai-jobs external-assessment import --input /tmp/private-external-assessment/responses.json --db /tmp/private-external-assessment/market.sqlite
```

importは原本を保持して選択attemptを再解析する。parse失敗時にL1は残り得るがL2を部分追記しない。原本/観測の挿入件数と再投入0を確認し、HTTP・parse・未試行・正常nullを分ける。新規DB初期化や旧schema移行は行わない。

### 日次取得と固定コホートの再開

source別planは固定release/calendar/masterとparser/request条件へ束縛する。19:13 JSTの新規plan作成時に当日exact masterがまだlake未反映なら、J-Quantsのready handoffが公開する小さなmaster/calendar依存入力を使う。価格を含むSQLite全体は取得せず、原受領ID・元の受領時刻・dependency/handoff hashとbase releaseをplanへ固定する。過去masterを当日へ読み替えない。継続pagination・不完全master・calendarの欠落は不適格として取得前に失敗し、既存planの再開は最新依存を読み直さない。初回移行ではinput生成後に新しいJ-Quants取得がdependency付きreadyを保存したことを確認してから外部評価を受け入れる。保存済みTradingView handoff内のJ-Quants原本由来master/calendar依存入力は、日付条件が合えば新しい外部評価planにも再利用できる。TradingView自体の新規取得は行わない。masterが遅延した場合はそのsourceを限定再実行し、共通統合の先行公開で帳尻を合わせない。sourceはmonex/rakuten/kabumap/minkabu、allは4取得元を選ぶ。同sourceのlocalとActions取得を重ねない。scheduleの対象日は元runから固定し、受領日をmaster/cron日へ替えない。

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
## TradingViewの利用範囲

2026-10-29に[公式接続案内のPermitted use](https://www.tradingview.com/support/solutions/43000795699-mcp-server-connect-your-ai-assistant-to-tradingview/)と[公式FAQ](https://www.tradingview.com/support/solutions/43000795700-mcp-server-faq/)を確認。公式MCPは個人の対話利用向けで、無人・定期・bulk取得と自動判断システムへの入力は認められていない。cloud-hosted custom clientもbetaの対応範囲外で、OAuthの継続有効性は保証されない。[汎用clientの接続仕様](https://www.tradingview.com/mcp/docs)や一度の取得成功は、その運用の許諾を意味しない。上位プラン、手動のworkflow起動、別client/IPによって解決した扱いにしない。

### 採用する最小運用

- **個人の対話調査**：既存の公式connectorを使い、ユーザーが関与する個別の問いへ回答する。接続済みでも全銘柄巡回や定期取得へ転用しない。必要な再認証はユーザーが公式client上で行う。新たな契約・認証権限や秘密のコピーは必要ない。
- **新規batch取得**：TradingViewのscheduled/workflow_dispatch全件取得、local全件runner、OAuth smoke、TV専用の新規source input発行を設けない。`tradingview-source`はoffline `rebuild`だけを持つ。手動で起動するbulk取得も対話調査の代替ではない。
- **保存済みデータ**：L1原本・受領日時・immutable release・保存済みモデル/判断を削除・再日付化しない。保存履歴の個人向けread-only確認と明示offline再構築は、新規取得なしに行う。原本や時系列の外部共有はしない。保存済みであること自体を、自動判断への再利用許諾と扱わない。
- **新しいモデル生成**：TradingViewは読み込まず、`external_assessment.tradingview.availability=excluded_by_source_terms`とし、値・比較を入力しない。欠測や正常nullと区別するため、生成revisionは`same-series-v5-no-tradingview`。既存の固定generation/Selection/判断を読む・再開する際は元payload・revision・hashを保ち、現行の空入力へ書き換えない。旧generationを新規判断の入力へ転用する許諾を与えるものではない。

定期更新できなくなるTV由来の項目は、rating/人数、目標株価3種、次四半期/次年度EPS・売上、forward PER/PS/EV-EBITDA、EBITDA/FCF/CAPEX/負債予想の16項目と、参考close/通貨2項目。株価は従来どおりJ-Quantsを正本とし、会社EPS予想・同予想に基づくPERは既存の会社財務モデルで継続する。これらをアナリストconsensusの代用品としてTV列へ詰めない。他の既存sourceも元の出所・対象期のまま使い、埋まらない予想は欠測として判断へ渡す。新たなcrawlerや代替source契約は追加しない。

新規自動利用を再検討する場合は、提供元条件の変更または当該用途を明示して許諾する契約が必要。support回答だけをTermsの改定と扱わない。既存の他source取得・共通統合は継続し、保存済みTV受領のhandoffも削除しない。既存mainで開始済みのrunをこの文書変更だけで中断せず、切替は対象PRの統合時に行う。

### 保存履歴の確認とoffline再構築

履歴を読むときはsnapshot_date・fetched_at_utcをそのまま表示し、現在値と呼ばない。更新がないことを新しい正常nullや0で埋めず、同じ保存値を当日の新観測にしない。技術障害・未取得・正常null・unresolvedは従来どおり分ける。

```bash
uv run baibai-engine external-assessment show --ticker <TICKER> \
  --cutoff <UTC_TIMESTAMP> --db <LOCAL_MARKET_SQLITE>
uv run baibai-jobs tradingview-source rebuild --sqlite <LOCAL_MARKET_SQLITE> \
  --asof <YYYY-MM-DD> --receipt-id <RECEIPT_ID>
```

再構築は同日・master・tool・Universe・実受領時刻の整合する保存receiptだけを明示する。正常receiptなし・銘柄重複なら置換せず、明示missingはunresolvedへ再現する。元の受領時刻を維持し、拒否応答を正常値で補わない。作業用copyへのoffline再構築は、本番データ変更や再公開の許可ではない。

## Storeをseedする

初回だけ、4 keyすべて不存在を確認してseed.shを使う。既存keyが一件でもあれば停止し、通常更新/復旧へ戻る。1〜3 keyの部分seedは、供用前・writerなし・対象exact keyを確認できた場合だけ初回復旧として扱い、通常pull/pushや無条件上書きで直さない。seed後のapplication更新はpull-appで正本と世代を取り直す。

## 定期maintenance

既存のdue taskを使い、同じ期限・対象・問いのopen taskを重複登録しない。実行後は完了を記録し、必要な次回分だけ作る。開発変更はGitHub Issueで管理する。

旧ER/FV・四Approachのcalibrationは、評価する仮説・対象期間・利用先を明示した研究/保守で必要な時だけbuild→evaluateする。新しいモデル生成・候補政策の通常運用に月次再構築や旧contextの期限更新を要求しない。月次比較を計画する場合は前月までの対象を同じrulesで扱い、forceは全再構築の時だけ使う。

```bash
uv run baibai-engine historical-evaluation calibration-build --start <YYYY-MM-DD> --end <YYYY-MM-DD>
uv run baibai-engine historical-evaluation calibration-evaluate --out /tmp/calibration-evaluation.yaml
```

保存済み旧Selectionに対応する較正文脈を更新する場合は目的・required月・必要metricを先に固定し、同じ評価に明示的な出力先を加える。`--context-out`に既定の発行先はなく、省略時に配布artifactを更新しない。

```bash
uv run baibai-engine historical-evaluation calibration-evaluate \
  --run-purpose empirical_change_evidence --decision-subject estimator_policy \
  --required-asof <YYYY-MM-DD> --required-metric er_level_calibration \
  --out /tmp/calibration-evaluation.yaml \
  --context-out engine/src/baibai_engine/machine_analysis/historical_evaluation/context/published/er-level-calibration-latest.yaml
```

required-asofは事前に選んだ全対象月分を繰り返す。配布前にoperative runのrules/model identity・期限・不足を照合し、Gitでreviewする。[較正の意味](../docs/reference/estimate-calibration.md)に従い、記述context成功をformal evidence成立としない。古いcontextの日付・hashだけを編集しない。

PMIは公表翌週、TSE資本政策とJPX廃止は翌月15日以降に既存refresh/buildを行い、必要なlake/store公開へ進む。全履歴監査を定例に増やさない。注文結果の計測が依頼された場合は既存measure_limit_outcomesの対象と限界を確認し、価格差を逸失利益や指値規律の因果効果にしない。

通知webhookのrotationはDiscordで新規/再生成→repository secret DISCORD_WEBHOOK_URL更新→通常runの通知確認。旧webhookはDiscord側で消すまで有効。rotationだけで過去Screeningを再実行しない。

<a id="web-serving"></a>
## 保有・運用Webの表示更新

表示はdashboard・outcomes・tasksの3 viewだけ。旧候補・macro・調査一覧・L1公開取得APIを復活させない。coreの日次統合が保存した後にapplicationを取得し、同じローカルmarketと合わせて生成・配信する。application取得・表示失敗で生成モデルやsource封印を止めたり巻き戻したりしない。台帳・taskの明示更新後は既存のpublish.shがapplication条件付き反映後にcloud-materializeを起動する。

手動再生成はmain上のcloud-materializeを実行する。検証済みapplicationを必須とし、marketをpullして固定releaseからhydrateする。market取得・hydrateの失敗はrunの失敗として残すが、未検証marketを読ませず、時価等を未評価にして最新の台帳fact・task・保存済み運用成績を配信する。赤いrunでもview公開が完了している場合があるため、marketの復旧と公開結果を分けて確認する。旧run・economic storeは不要。新dashboardは`views/portfolio-dashboard.json`へ保存し、旧UIが読む`views/dashboard.json`を上書きしない。公開処理は3 viewをR2から読戻し、生成したJSONとの全bytes一致を同じlease内で確認する。Web切替前にはこの読戻しまで成功したrunを確認する（旧Workerでは新dashboard/outcomesを読めない）。過去のuploadだけで読戻し未実施なら、mainのcloud-materializeで再生成・公開・照合する。その後UIの対象HEADを確認し、web workflowをmainで明示dispatchする。切替後に認証付きAPIと画面を確認する。PRのCI成功は本番更新ではない。旧object・Worker secretは削除しない。

```bash
uv run python -m baibai_web.materialize --output-dir /tmp/baibai-serving
```

これはローカル生成だけであり公開しない。cloud workflow内だけが `jobs/scripts/r2_transfer.sh upload-portfolio-views /tmp/baibai-serving` で3 viewを公開する。資本・損益・保有の欠測とエラー、タスク、運用成績を読戻しで照合する。

<a id="web-authentication"></a>
Worker secretの`VIEW_PASSWORD`はowner用Bearer、`READ_ACCESS_TOKEN`は共有用Bearerまたは`?share=`で、どちらも同じ3 viewを読める独立credentialである。R2転送credentialとは別で、owner passwordの変更やUI再配備だけでは共有tokenを失効できない。共有の失効は配備先Workerの`READ_ACCESS_TOKEN`自体を更新、または削除／空値で無効化し、旧tokenが拒否され、必要な新tokenとownerの読取が維持されることを確認する。owner側も`VIEW_PASSWORD`自体を更新して旧値の拒否を確認する。実環境の変更は別の明示依頼に従い、秘密値や共有URLを本文・shell引数・logへ残さない。
