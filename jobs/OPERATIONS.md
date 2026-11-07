# 運用手順

repository rootのBashから依頼された対象だけを操作する。責務は[architecture](../docs/architecture.md#store-authority)、環境は[development](../docs/development.md)、option/判定はCLIのhelpと実装へ。本書は開始・成功境界・復旧を扱う。

<a id="actions-write-boundary"></a>
## 実行前の確認

通常取得・公開は既存Actions、application更新は認可環境の作業コピーと正規CLIを使う。環境接続/開発変更は本番更新・課金・権限拡大・broker操作・未報告取引入力の許可ではない。課金禁止ならrunner・storage・API・R2も確認する。code撤去で配備済みWorker/domain/credential/旧objectを削除しない。

秘密・endpoint・原文・私有DB/原稿はGit・Issue・公開log/artifactへ出さない。既存のR2・J-Quants/e-Stat/EDINET secretは取得/公開step、DISCORD_WEBHOOK_URLは通知stepだけへ渡す。通常開発へのwriter credential流用、認証/403/429の別経路・権限拡大による迂回は禁止。baibai-storesは非公開正本。

- 受領・取得・L2・公開/readback・seal・次input発行は別々に確認する。正常空/partial/未試行/失敗を分け、L2失敗で保存L1を戻さない。
- 固定plan/receipt/ID/hash・原bytes・実受領時計を保つ。結果不明は現objectから未反映/同候補/第三の内容を確認し、新UUID・日付付替え・marker手修正・古いcopyの盲再送で解消しない。
- schema/origin/CAS/必要なlease不一致は停止。旧release・stage/input・参照原本を必要な間保持し、通常dailyへ移行/全期間再取得を代行させない。反映範囲は[共通公開](#machine-publication)、接続と未完移行はsource別手順に従う。

## 操作を選ぶ

| 目的 | 入口と到達点 |
| --- | --- |
| 日次取得/統合 | [日次](#daily)。source受領と公開世代を別確認。 |
| 特定sourceの不足/再抽出 | [source操作](#sources)。正常nullやL2計算不能を再GETで埋めない。 |
| 保存Generationへ現政策 | [候補政策だけ](#screening-only)。モデル再生成・旧ID変更なし。 |
| 固定断面の分析 | [読取](#fixed-release-read)。保存入力と現在条件のcopyを混ぜない。 |
| machine/経済の反映 | [共通公開](#machine-publication)。lake→store→seal/input。 |
| application更新/復元 | [更新](#application-db-を反映する)・[復元](#application-db-を復元する)。DBと取得ETagを一組にする。 |
| 初期化/未完移行 | [移行](#cutovers)。通常取得の不足を空seedで迂回しない。 |
| 表示 | [Web](#web-serving)。core処理の成功条件にしない。 |

<a id="daily"></a>
<a id="日次実行と結果確認"></a>
## 日次と到達点

修正をmainへ反映してから必要な範囲だけを実行する。実scheduleは[workflows](../.github/workflows/)、対象日は[schedule owner](src/baibai_jobs/schedule.py)で確認し、コメントcron/手動成功を定期稼働の証拠にしない。run IDを固定し、監視timeoutだけで重複dispatchしない。

```bash
gh workflow run cloud-daily-batch.yml --ref main -f asof=<YYYY-MM-DD>
gh run list --workflow cloud-daily-batch.yml --limit 5
gh run watch <RUN_ID> --exit-status --interval 60
```

手動asof空欄は元run作成JST日。寄前/場中に完了済み日次が必要なら前日を指定し、前営業日へ戻して週末情報を落とさない。cronも元run時計/slotをqueue・rerunへ保持するが、run作成自体が次slotを越す24時間以上の遅延はasofを明示する。生成対象日と価格日は別で、休場の価格日を繰り上げない。価格/株数不足の候補0を投資機会なしとせず、[不足理由](../docs/reference/screening-runtime.md#入力不足と品質)を見る。既定読取は発行時計より新しいas_ofを優先する。過去再計算はexact generation/selectionで読み、古い非空候補へfallbackしない。

共通統合は全provider完了を待たず、到着済み受領を固定する。

1. 経済L1を単独公開/readback/sealし、次inputを発行。
2. marketの固定受領をsource別投影/EDINET抽出し、固定market release＋固定経済selectionからモデル/候補を保存・公開。
3. 公開成功後に生成cache/候補を整理し、machine反映/readbackと受領sealへ進む。

source失敗は前回適格投影を保って他sourceを進めるが、モデル/公開失敗は後段machine反映を止める。model-integrationは外部取得/Web生成を呼ばない。停止時はmodel-integrationの最後の未完phaseから調べる。開始flush/成功doneと所要秒は診断用で、反映の証明ではない。通知失敗だけではjobが赤にならないため、run log・store/release・generation/selection・lease解放を確認する。限定source成功を全体成功にしない。

取得期限にはexport/stageの余白を残す。Retry-Afterを短縮せず、認証/403/解析失敗へtransport retryを広げない。継続不能429は同quota groupのそのpassだけを止める。経済nativeは失敗1 GETだけ再試行し、H.15 browser起動不能をsource拒否とせず、別取得で再試行できる。[watchdog](../.github/workflows/cloud-batch-watchdog.yml)のMISSINGは対象日を固定して確認する。

```bash
gh workflow run cloud-batch-watchdog.yml -f check_date=<YYYY-MM-DD>
```

<a id="screening-only"></a>
### 保存済みGenerationへ政策だけ適用

fresh runsを同じlease内で取得し、保存V2のexact generationと通常と同じ政策/Selection IDを使う。同ID同内容はno-op、異内容は競合で止める。

```bash
with_publication_lease bash -c '
  jobs/scripts/r2_transfer.sh pull-runs &&
  uv run baibai-jobs screening-selection --generation-id <GENERATION_ID> --runs stores/screening/runs.sqlite &&
  jobs/scripts/r2_transfer.sh push-runs
'
```

fresh正本でID・政策method・候補を読戻す。結果不明は先に反映先を確認し、他store・旧Selectionを書き換えない。保存済み旧推計の読取は[旧成果reader](../docs/reference/estimate-calibration.md)へ。

<a id="fixed-release-read"></a>
## 固定releaseを読む

currentを一度resolveし、固定release ID/manifest SHA-256が列挙するobjectを通常readerで照合する。resolveだけではdataを検証していない。hash・bytes・row数・schemaを確認し、optional未収録を別release/providerで埋めない。

```bash
uv run baibai-jobs snapshots resolve --mirror stores --bucket baibai-stores --format json
uv run baibai-jobs snapshots resolve --mirror stores --bucket baibai-stores \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

daily/local publisher終了後に`jobs/scripts/pull.sh`でbulk作業copyを取得し、machine receipt/ETagとhydrate開始時のmarket origin/releaseを照合する。競合/途中失敗のlocalは分析に使わずfreshに取り直す。applicationはpull-appで別取得。保存L2のInputSnapshotを現在quote用copy/currentへ付け替えない。

<a id="local-release-hydrate"></a>
### 完全local mirrorからのhydrate

認可済みR2読取か完全mirrorとDB/一時snapshot容量を用意する。旧L1公開API・部分JSONを正規readerの代用にしない。

```bash
uv run baibai-jobs snapshots hydrate --mirror <COMPLETE_LOCAL_MIRROR> --store <LOCAL_MARKET_COPY> \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

local mirrorはbucketなし。EDINET一覧cache除去の[原本照合](#edinet-list-native-l1)だけはbucketも指定する。hydrateは対象tableを置換するため、変更後に再実行しない。store-local取得coverageは作らない。旧contractは元commitのcodeで読み、不足/破損を別releaseで埋めない。

Parquet直接読取は[reader](../engine/src/baibai_engine/storage/snapshots/reader.py)のresolve_release→accepted_dataset→selected_partitions→materialize_partitionsで返る検証済みpathだけをlake_sessionへ渡す。glob、別release、remote拡張は使わない。

<a id="machine-publication"></a>
<a id="ローカルからクラウドを更新する"></a>
## 共通公開とmachineの復旧

market/runs・経済は同じpublication lease内でfresh正本/取得ETagを照合し、read/merge/publishする。長い準備は先に済ませ、lake対象tableは変更前にhydrateする。applicationのpublish.sh・初回seedは対象外。独立native L1はsource固有のCAS/並行制御を使い、共通leaseやL2成功を課さない。

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

releaseを保証するため関数内でset -eを使わない。busyはowner/expiry/runを確認し、release失敗はhandleを保つ。cloud-publish queueだけではlocal writerを排除しない。malformed leaseは自動修復せず、全active/pending writer不在を確認した明示復旧だけexact lease keyを調べて削除→通常acquireとする。prefix削除/手書きJSONはしない。

日次は準備・生成後の公開と後処理にも時間を残すため、job枠170分・新daily lease180分とする。他purposeの120分leaseは延ばさない。旧120分のlease/handleも元の期限のまま読取・releaseできる。旧codeはreleased後も新180分leaseを読めないため、切替前に旧writerの新規起動を止め、旧active/queued runの完了とleaseを確認してから修正mainの新runを起動する。手動/local operatorも修正codeへ揃え、途中更新・旧runのrerun・強制unlockで延長しない。hard cancelでreleaseできなければ元のexpiryまでbusyが残り得る。これは速度改善や完走保証ではなく、共通queueの占有が最長60分伸びる。中断時は下記の部分公開を読戻してから再開する。

| 変更 | 反映経路 |
| --- | --- |
| lake table | publish-lake → push-market |
| market store-localだけ | push-market |
| 経済原本 | push-indicators。L2利用のために再pushしない。 |
| 元bundleからlocal daily全体 | publish-lake → push-machine。market/runsを別pullで組み替えない。 |
| runsだけ | 取得世代が合うpush-runs |

```bash
with_publication_lease bash -c 'jobs/scripts/r2_transfer.sh publish-lake && jobs/scripts/r2_transfer.sh push-market'
with_publication_lease jobs/scripts/r2_transfer.sh push-indicators
```

lake→store→seal/inputの順を保つ。machineはmarket/runsの2keyで非原子的にPUTし、全成功後にmachine-manifest.jsonを書く。経済は別CAS/readback、各machine前世代backupはkey.bakの1世代で次回置換される。

### 部分 push からの復旧

先行keyだけ成功すると旧receiptと不整合になりpullが止まる。同じpush/dailyを再送せずlogと現objectを確認し、local成果/origin/取得世代が成立する上表の反映で完了する。経済pushはmachine部分反映を修復しない。

receipt再生成が必要な明示復旧だけ、対象がbucket直下のmutable一件であることを確認する。

```bash
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  aws s3api delete-object --bucket baibai-stores --key machine-manifest.json \
    --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
)
```

次pullは未検証setとなる。完全pushでreceiptを作り直し、一致を確認するまでcross-section保証は回復していない。失敗時に追加削除/dispatchを反復しない。前世代復元が必要ならwriter停止、bucket/exact key/key.bak確認後にaws s3api copy-object --cli-read-timeout 300を使い、fresh pullとreceipt/世代を再検証する。schema修正で全storeを古いcopyへ戻して後続事実を失わせない。

## application DB を反映する

空の作業領域へpullし、DBとstores/.r2-generations/baibai.sqlite.etagを同じ組で使う。更新済みの組があれば再pullしない。

```bash
jobs/scripts/r2_transfer.sh pull-app
# 正規CLIでこのDBを更新した後
jobs/scripts/publish.sh
```

publish.shは引数なし。台帳preflight→取得世代への条件付きbackup/PUT→次回ETag記録→Web生成dispatchの順。時価/quote不足と取引保存障害を分け、DBだけの世代交換・未知世代の現HEAD補完はしない。backupは実行JST日の日別keyを同日置換する。R2_APP_BACKUP_PRUNE=skipでも同日置換は止まらないため、保全/置換の依頼範囲を確認する。

保存・反映・readbackを分け、対象Proposal/評価のexact IDを確認する。競合時は未公開差分を退避し、最新正本へ正規serviceで再適用する。PUT後の記録失敗で旧DBを再送しない。dispatchだけ失敗/不明ならActionsを確認し、run未作成時だけcloud-materializeを再dispatchする。publish.sh全体は繰り返さない。current schemaだけを開き、移行は限定source version・別copyで検証後、code/store反映後に一回toolを撤去する。

### application DB を復元する

判断/ledgerの正本を再生成せず、明示選択した過去snapshotへ戻す手順。通常更新は前節を使う。

全writer停止→未公開copyのconsistent snapshot退避→空の作業領域へ`pull-app`し、現在の正本/世代を取得・検査してから同じshellで進める。取得・検査不能なら停止。key消失は[初回seed](#storeをseedする)の既存key確認を含む別判断とし、無条件PUTで迂回しない。候補はR2日別世代か、出所・時点を照合できるlocal checkpoint。localを自動的に正本より新しいとみなさない。

**1. 復元前backup。** 現正本のWAL込みconsistent snapshotを作り、失敗なら停止する。

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

**2. 候補選択。** backup commandが保持する検証済みlocal snapshotは直近10世代。

```bash
ls -t stores/application/backups/ || exit 1
read -r -p '復元するcheckpoint path: ' target || exit 1
test -f "$target" || exit 1
```

R2候補なら上の選択に代えて一覧を確認する。

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

一覧取得成功後、exact keyを一意なstaging fileへ取得する。

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

**3. 共通検査。** integrityがexact `ok`、foreign key違反0行、user_versionとcurrent schema一致を出力で確認する。exit 0だけでは合格にせず、自動移行もしない。

```bash
(
  set -e
  test -f "$target"
  sqlite3 "$target" 'PRAGMA integrity_check;'
  sqlite3 "$target" 'PRAGMA foreign_key_check;'
  sqlite3 "$target" 'PRAGMA user_version;'
)
```

**4. 配置と照合。** 検査合格・writer停止後、copy成功まで現copyを動かさない。明示復旧として現クラウド世代を保持し、検査済み候補へ置換する。

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

ledgerを`head`へpipeせず復元対象時点と照合する。不一致は手順5へ。成功なら取得世代を保ち、再pullせず`publish.sh`で反映後に判断を再開する。公開途中の失敗では検証済みlocalを戻さず、upload/readbackの到達点から反映側を復旧する。

**5. ローカル復元の検証失敗時。** 復元前backupがある場合だけ次で戻す。なければ判断を再開せず、別候補の取得・検査へ戻る。

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

<a id="sources"></a>
## source取得と再開

独立取得はsource固有の原本/publicationを使い、全market/applicationをhydrateしない。handoff対象はprivate stageへ保存する。同source/groupの取得〜stageを直列化し、J-Quants共有quotaを守る。J-Quantsは共有inputを使わずnative head、未移行sourceは固定L1/未統合原本から計画する。

共有[source input](src/baibai_jobs/acquisition/source_inputs.py)・記事planningを使う経路だけ、取得有効化前に共通統合から初回発行する。inputは原本履歴/coverageに基づく再生成物で、正本ではない。移管後も元bindingで再開し、欠落を全DB取得/空seedで迂回せず、部分storeにfull lake originを付けない。

経済planning inputは計画用の範囲/成功失敗/確認を持ち、原文/URL/実時計はhandoff、同値/訂正/遅着の受入はcanonical原本が担う。Matsuiは現在pageの明示空seed、TSRはrun/coverage（latest成功日だけに縮めない）、PMIはURL/月/phase、UMich/ECBは受領日集合を使う。空seedを原本/input欠落の自動初期化にしない。

新形式inputの配布は両形式readerを先行させ、非対応の実行中/queued runを終えてから行う。旧SHAで再実行しない。全15経済groupは受領なしでも再発行し、group失敗は旧pointerを保って次cycleへ。旧input/stageを削除せず、形式移管をFRED schema切替と一括にしない。

破損handoff/decodeは当該sourceを報告して他を進め、正本破損/R2全体障害は停止する。遅着/未統合は次統合または既存dailyへ。後続統合後の旧stageはneeds_explicit_recoveryのID・理由・期間・markerを確認し、現値は新取得、旧vintageはexact回復で扱う。正常nullを全期間取得で埋めない。

<a id="economic-acquisition"></a>
### 経済原本

[経済workflow](../.github/workflows/cloud-economic-sources.yml)は休場日もgroup別に取得し、部分受領を非公開stageへ保存する。source限定再実行は所属groupとsource IDを指定する。

```bash
uv run baibai-jobs acquire-economic groups
gh workflow run cloud-economic-sources.yml --ref main -f group=fred_csv -f source_id=fred-payems
uv run baibai-jobs acquire-economic refresh <SOURCE_ID> --all-history --end <YYYY-MM-DD>
```

通常窓は[policy](../engine/src/baibai_engine/data_collection/acquisition/policy.py)に従い、一律2か月へ縮めない。FRED全CSV・BOJ四半期APIは期間指定なしの応答を読み、窓外の返却期間も保持する。窓を狭めても通信は減らず、BOJの未返却期間を撤回・全履歴確認とはしない。他sourceの旧年改訂/history seedは明示取得。recentは通常窓と未確認区間を補うが、同じ無期間指定応答を回復用に再処理しない。広範囲失敗の残りはhistory_recovery_requiredを見て別の期間指定/all-historyで回復する。

FREDは全CSV比較で同値なら確認時計だけを進める。本文変更・実確認・公表時計を分け、完全応答の期間集合と原本hashを保つ。headerだけ/短い行を成功とせず、人工空日をprovider欠測に変えない。[confirmation owner](../engine/src/baibai_engine/data_collection/receipts/native_sources/confirmations.py)が比較を行う。確認欠落/遅着で通常pushを拒否されたときだけ、fresh正本を比較基点に実GETする。

```bash
with_publication_lease jobs/scripts/r2_transfer.sh push-indicators --recheck-source <FRED_SOURCE_ID>
```

同じ受領で確認を作りGET/run/countを増やさず、失敗時は両copyを保つ。採用履歴消失時に復元できるのは現値/完全応答/実確認時計/保持原本で、過去の採用順やlast-changeではない。他sourceへ全source置換proofやFRED専用recheckを広げない。

FRED以外の登録済record sourceは、受入時に観測日ごとの実確認時計と採用原本参照を残す。同値本文を複写せず、後着別値と同時計衝突をその時計で判定する。これは取得済range内の確認であり、source全体の置換・providerの明示nullを証明しない。旧rawの実時計を使い、過去に捨てた確認時計をrun終了時刻から補わない。保持stageからの有限回復は通常取得と分ける。新しい確認を保存・固定する前に、受入・merge・固定入力readerを揃え、旧codeのactive/queued実行を終える。旧input/stage・原本は保持する。

current-onlyだけは遅延時の実取得日を使い、他source/handoff対象日は動かさない。松井の過去asofを現在pageで埋めない。options all-historyは保存古日を再利用するため、古い訂正は期間を明示する。rolling提供不能/正常空/途中失敗を分ける。

<a id="pmi-public-articles"></a>
PMIはS&P公式PDFの日本/米国製造業headline・サービスBusiness Activity Index。TE記事adapterは未接続で同測定の別配信であり、取得/coverage/利用範囲の受入なしに替えない。原文受領/抽出、速報/確報・対象月・公表/配信時計を分け、完全PITとしない。ISM/Output/Composite、旧値/snippet、拒否後fallbackを代用しない。[採否](https://github.com/koumatsumoto/baibai-loop/issues/1477)を参照する。

<a id="指標観測とregistryの修正"></a>
原本訂正は再取得/明示再構築、系列追加/退役/改名/式はL2定義、外部取得条件だけData Collectionを変更する。local DELETEをcloud mergeへ流さず、式変更だけで再取得/新registry台帳を要求しない。

```bash
uv run baibai-jobs acquire-economic list
uv run baibai-engine market-context reading --asof <YYYY-MM-DD>
uv run baibai-jobs validate-market-context-stores
uv run python -m tools.pmi_manifest --dry-run
uv run python -m tools.pmi_manifest
```

validateは原本件数等の検査で、L2算式/L3履歴の証明でない。原本更新時だけ経済pushへ進む。PMIは公表翌週にdry-run→manifest更新→対象月refresh→公表値照合→commit。PMI名/embargo/対象月、archive内の穴と翌月restate、URL訂正/同URL差替えを分け、抽出式だけなら保存PDFを再読する。HTTP403だけの失敗でprivate受領保存済みなら通知が省略され得るが、UNAVAILABLE/Actions失敗は残る。403を未公表と推定しない。

<a id="article-acquisition"></a>
### 記事

起動は[workflow](../.github/workflows/cloud-article-sources.yml)、source範囲/予算は[契約](../engine/src/baibai_engine/data_collection/providers/articles/contracts.py)、cycle/再開は[planning](src/baibai_jobs/acquisition/article_planning.py)に従う。group直列化と共有host queueを保つ。TSR日次は正常index受領で充足し、BOJ/MOFは同slotでも保持indexの再発見/未処理本文を進める。試行時計だけで取得済みにせず、改訂確認GETとoffline再解析を分ける。

初回planningは取得有効化前にintegration.sources inputsで発行する。原本stage保存後だけplanningをCAS更新する。v3はcursor/validator/最新stageを持ち、L2未処理件数では取得を止めない。旧v1/v2からの切替時はpendingを最初の新stageへ引継ぎ、非対応旧writerを先にdrainする。統合は不変stage/markerから発見し、sealでplanningを変更しない。L2停止中も原本/worksetを保つ。親ID/hashの連鎖を照合するが、失ったroot・CAS未確定孤立stage・導入前pending外の全原本復元は保証しない。参照中stageを期限削除しない。CAS失敗原本の統合とcursor回復は別で、進んだcursorを戻さない。

v3 planning保存・L1受領・全取得完了・L2統合は別である。continuation_only=trueのpartialだけは正常な継続終了で、全取得完了ではない。HTTP/parse/deadline/CAS/保存失敗は区別し、正常sourceは進める。HTTP失敗はsource不備としてDEGRADEDに残し、受領後のparser失敗だけを投影失敗とする。v3保存だけで失敗を消さない。200同値は別受領、304は具体的200 bodyへの参照。validatorのbody欠落で新GETする場合も過去原本の復元としない。取得worksetの上限では未完を捨てず限定回復へ進む。

案内/転送/添付は親子の別受領。トウシル同hostの公開301/308はLocationを保持して既存予算内で取得し、一般redirect/認証URL/未許可hostへ拡大しない。財務省rolling一覧から過去全PDFへ巡回しない。未保存Locationは推定せず、本文増加を理由にgroup予算を増やさない。sealは固定releaseの今回receiptと投影/抽出artifactを読戻してから行い、原本受領の成功とは別にする。metadataの列挙費用は履歴に比例し、上限や部分logを無料の実証にしない。

統合前の原本はexact stage/receiptをprivateに読む。再GET/planning変更は行わず、引用packetも保つ。旧market schemaのstageもArticle専用shapeとreceipt/body digestで検証し、原SQLiteの版数・bytesを変換しない。

```bash
uv run baibai-jobs article-evidence --source <SOURCE> --stage-id <STAGE_ID> --manifest-sha256 <HASH> \
  --receipt <RECEIPT_ID> <RECEIPT_SHA256> --directory <PRIVATE_OUTPUT>
```

これはstage原本で、release所属/抽出成功/長期保持の証明でない。統合後は同じID/hashを固定releaseで照合する。

### 記事のexact遅着回復

```bash
uv run baibai-jobs article-recovery prepare --source <ARTICLE_SOURCE_ID> \
  --stage-id <EXACT_STAGE_ID> --manifest-sha256 <EXACT_MANIFEST_SHA256> --directory <PRIVATE_RECOVERY_DIRECTORY>
uv run python -m baibai_jobs.integration.sources apply --asof <YYYY-MM-DD> --directory <PRIVATE_RECOVERY_DIRECTORY>
```

plan.jsonを通常planで上書きしない。同じfresh canonical/leaseでapply→model-integration→push-machine→seal/inputsへ進む。prepareだけでは取得/公開しない。元receiptはappend-only、成功はrecovered-<hash>.jsonへ記録し、feed再GET/元時刻変更/integrated.json削除をしない。CAS失敗で原本だけ残った場合も先にこのexact統合を行い、cursor回復とは分ける。

BOJ/MOFのoffline再発見は同ownerのregister-discoveryへstage/hashと--receipt-idを渡す。新stageをplanningへCAS登録する書込で、HTTPなし。同要求はno-op、元受領/validator/slot/未処理workを保ち、登録後に通常取得を続ける。

<a id="edinet-source-archives"></a>
### JPX PDFとEDINET

JPX決算予定/規制は過去factを取得計画に使わず、fresh source DBから部分受領をprivate stageへ残す。明示PDFは既知URLで取得済みの原本だけを使い、daily自動取得へ追加しない。

`baibai-jobs financial-pdf-source`へ`--receipts <JSON>`と`--directory <NEW_PRIVATE_DIRECTORY>`を渡す。HTTPは行わない。JSONは1〜8件、requested_url・file（相対path）・sha256・request_started_at/captured_at（実UTC）・http_status:200・content_type:application/pdfを照合する。captured_atは本文保存・hash計算を含み得る完了時刻。mtime/公表日から時計を作らない。stageは`uv run python -m baibai_jobs.integration.source_handoff stage --source jpx-financial-pdfs --directory <DIRECTORY> --stage-id <NEW_ID>`、完了転換は--collection capital_actions/--source jpx-capital-actionsを使う。不対応も原本を保ち、株数/自己株を推定しない。

PDFの選択/近似/訂正basisは[観測reference](../docs/reference/model-observations.md#reported-comparisons)と[選択owner](../engine/src/baibai_engine/machine_analysis/observations/financials/pdf_selection.py)に従い、新generationで反映する。fixture、実PDF、母集団、固定readbackを別に確認する。

EDINET一覧→保存原ZIP→L2抽出の順。手動source=allは一覧取得/stage成功後にarchiveへ分岐し、未統合一覧も投影して共通統合を待たない。一覧失敗時はall内archiveを止め、有効一覧を確認できる場合だけ同asofの必要archiveを再実行する。他sourceは進める。保持docID/typeを再GETせず、L2成否/revisionを原本取得条件にしない。holder/訂正原本は既存facts取得の新しい未保存分を1回100件/10秒間隔で扱い、未選択を次回へ残す。403/認証/制限で止めても部分原本は保つ。

一覧/archive未到着はdeferred_inputsで適格保存値を使って継続し、到着後に再抽出する。解析失敗/原本破損を延期で隠さない。不正dayは正常投影を保ちながらsource-check失敗を継続し、正常別日の訂正/取消を進める。延期だけはsource-check成功だが、partialは日別成功/厳密読取成功でない。誤orphanは現年度CSVを確保して次統合で回復し、全期間GET/revision変更を代行しない。

<a id="edinet-research-facts"></a>
<a id="edinet-factsの取得範囲schema-50"></a>
<a id="edinet-research-query"></a>
<a id="edinet-factsの書類選択"></a>
### EDINET factsの取得と読取

固定releaseをhydrateし、[facts reader](../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_facts/)のdocument_inventoryへstoreと対象JST日を渡す。得た文書とtickerをlatest_annual_filingへ渡し、usable_documentを確認する。cutoffは日末でintraday PITではない。未選択は書類不存在ではなく、未抽出/公開不明なら原典へ戻る。segment/debtはsource_doc_idで取り、期・連結・通貨を照合する。全債務/資金余命を抽出行だけで確定しない。

facts scopeは取得範囲のoperational stateで、L2品質からglobal開始日/docID集合を狭めない。対象family IDはGET前に保持し、失敗/未提供/交代でも消さず、stale input/stageは出所とunionする。未初期化では通常annual取得を始めずholderは独立。明示initializeの取得成功後だけglobalを有効化し、後のL2失敗で戻さない。GET途中失敗はID/ZIPを保ち同じrefreshで再開、ticker限定は他社/globalを置換しない。offline extractのinitializeは将来GETを有効化しない。

```bash
uv run baibai-jobs maintain-market refresh-edinet-facts --asof <YYYY-MM-DD> --initialize --sqlite-path <STAGED_MARKET_SQLITE>
```

再抽出だけならextract-edinet-facts。stored_onは初回保存UTC日で公表/利用可能時刻でない。未対応0行とfailedを分け、文書期間/訂正/snapshot時刻で鮮度を見る。通常取得とSQL投影/固定consumerの境界は[独立L1](#edinet-list-native-l1)に従う。

<a id="edinet-metrics-revision"></a>
抽出/選択の意味変更時だけmetric revisionを更新し、facts revisionと分ける。配置/import/retryでは変えず、旧値/時計/hashを付け替えない。producer commitを各行の元抽出commitと同一視しない。隔離コピーで保存CSVを再抽出・比較後、同じleaseでfresh pull/hydrate→backup→extract-edinet-metrics→publish-lake→push-market→固定readback。取得が必要なときだけrefreshする。期待外差/失敗は止め、変更後hydrateや旧copyの本番復元をしない。

<a id="external-assessment-acquisition"></a>
### 外部評価

通常TLS/proxyで有限銘柄を直列取得し、試行別private captureをcurrent-schemaの隔離marketへimportする。初期化/移行は行わず、parse失敗時もL1は保ちL2は部分追記しない。再投入0件を確認する。

```bash
uv run baibai-jobs external-assessment collect --source minkabu --tickers 7203 4436 --output <NEW_PRIVATE_CAPTURE>
uv run baibai-jobs external-assessment import --input <PRIVATE_CAPTURE> --db <LOCAL_MARKET_COPY>
gh workflow run cloud-external-assessment.yml --ref main -f source=monex -f batch_id=<LABEL> -f master_asof=<YYYY-MM-DD>
```

日次は[owner](src/baibai_jobs/acquisition/external_assessments/daily.py)の固定master/calendar/parser条件を保つ。native後は独立入力、旧経路は固定lake/readyを使い、依存不足を全market/TradingView起動で迂回しない。同sourceを重ねずqueue後cycleを再確認。scheduled IDはexternal-assessment-daily-<source>-YYYY-MM-DD、手動labelにsource prefix不要。plan再作成/hash手修正はせず、期限内未試行pairだけ再開。保存失敗pairの再取得は別labelとする。

新原本はretained、旧rawだけが3日削除の短期handoffで厳密72時間/長期L1ではない。実残存を確認し、元key/stage/IDを保つ同bytesの保全だけ行う。

```bash
uv run python -m baibai_jobs.acquisition.external_assessments.raw --preserve-run-id <EXACT_RUN_ID>
```

lease外でacquire→inspect→ready --cumulative、共通統合が固定集合を公開/readbackしてackする。後着小集合で後退させない。確定分で明示終了する場合だけworkflowに-f operation=stage_confirmed_partialを付け、取得せず次統合でterminalを確定する。403/429/未完を自動closeしない。pointer後terminal失敗は既存状態を読み、未sealedならfresh hydrateから再適用する。hard kill前の未保存試行の再GETや期限切れ原本の復元は保証しない。history_digestsは保存後baselineで、更新前の全履歴保全を証明しない。

<a id="tradingview-expectations"></a>
### TradingView

本人確認済み利用範囲・毎日取得の指示と既存tradingview-runtime認証を使い、一般FAQだけで停止/除外しない。新credential/課金/再配布へ広げず、認証失敗を充足へ変えない。

[日次](../.github/workflows/cloud-tradingview-snapshot.yml)は未取得原応答だけを再開する。L2行だけでは充足でなく、originals_savedは再GETせずrebuild。L2だけ残るcopyは不足原本を受領して既存L2を保ち、native_added/rows_addedを分ける。後受領を旧L2の生成元とせず、過去cycleを現在応答で埋めない。cycleは09:00 JST切替、営業日15:30/休日09:00から翌09:00未満。休日は直前営業日のexact masterと連続calendarを固定し、翌朝受領を前日既知へ付け替えない。

要求銘柄・fieldの揃った原応答はL2の数値/型検証失敗でも取得の前進として残し、後続batchを止めない。L2不足はpartial/診断に残し、無効応答の連続停止・HTTP/認証・時刻/予算・保存失敗の境界は維持する。

receiveはHTTPなしでNativeToolCaptureをcurrent-schema copyへ保存する。SDK応答全体・失敗も保持し、値から原本を作らない。同IDは全内容一致の再送だけ、別呼出しは別ID。

```bash
uv run baibai-jobs tradingview-source receive --sqlite <LOCAL_MARKET_COPY> --capture <PRIVATE_NATIVE_CAPTURE_JSON>
uv run python -m baibai_jobs.integration.source_handoff export --source tradingview \
  --asof <JST_RECEPTION_DAY> --since <BEFORE_RECEPTION_UTC> --database <LOCAL_MARKET_COPY> --directory <NEW_PRIVATE_HANDOFF>
uv run baibai-jobs tradingview-source rebuild --sqlite <LOCAL_MARKET_COPY> --asof <YYYY-MM-DD> --receipt-id <RECEIPT_ID>
uv run baibai-engine external-assessment show --ticker <TICKER> --cutoff <UTC_TIMESTAMP> --db <LOCAL_MARKET_COPY>
```

新受領は共通統合へ渡し、取得copy全体をpushしない。元master/calendarと最初の正常行/receiptを保つ。rebuildは一日L2を選択receipt集合で置換するため、部分追加だけで既存日を置換しない。L1保存/L2/公開と不足の別を確認し、過去判断/除外履歴へ遡及しない。

### 判断直前の多市場補助観測

[market-evidence](src/baibai_jobs/acquisition/market_context.py)はquote/5分足のprivate原受領で、日次充足・価格master・正式提案終値とは別。保存参照か明示取得を使い、実tool/引数/要求受領時計/応答を保持する。

1. market-evidence prepare --capture <CAPTURE> ... --directory <NEW_DIR>、stage --directory <DIR> --stage-id tradingview_market-<ID>。
2. 別sessionは返却ID/hashでdownload、read --receipt-id <RECEIPT> --sha256 <CAPTURE_SHA>を使う。再GETせず同じAssessment入力を渡し、未保存captureに耐久参照を付けない。

固定economic入力の同じ参照/時計/欠測を使い、最新失敗を旧成功で埋めない。評価時刻と基準JST日末の両cutoffを守り、前日基準へ当朝夜間受領を加えるなら別入力とする。休場/遅延/限月/basisを揃え、受領時刻を取引時刻、bar tを最終約定、update_mode不明をリアルタイム、末尾足を確定値にしない。新価格で判断を変えるなら新入力/再Reviewとし、旧判断へ挿入しない。

<a id="jquants-native-operations"></a>
## J-Quants native L1

7sourceは取得・原本保持とL2再生成を分ける。財務・日足・master・calendarは既定/Actionsともtrue、alerts/short-saleは既定falseで本番切替未実施、margin-interestは明示CLIだけである。現在の設定は各cutover ownerとActionsを確認し、flagやmergeを移行完了としない。切替済み4sourceのfalseは旧入力の有限検証用で本番rollbackには使わない。

`jquants-<source>-native initialize`は新規の空namespaceだけに一度行う。plan/acquireは新directory、応答不明はresume、保存済みattemptは同じdirectoryのcommit-workを使う。head消失や移行失敗を空初期化で迂回しない。

| source | plan/acquireの範囲 | 固有条件 |
| --- | --- | --- |
| financial / price | --start / --end | 日別原本・訂正確認 |
| master / calendar | --start / --end | 明示訂正だけ--force。日足overlapを流用しない |
| margin-alerts / short-sale-reports | --asof、任意--start | 休場も公表確認 |
| margin-interest | --asof、有限backfillは--start / --end | 固定calendar。--forceは明示訂正区間だけ |

rebuild-<source>-nativeは--work-directory / --start / --end / --outputから新規観測とinput.jsonを作る。calendarだけは下記専用引数。既存outputを上書きせず、保存generation/Selectionの再生はexact InputSnapshotを--saved-inputへ渡す。summary・最新headで代用しない。

### sourceごとの通常読取

<a id="price-native-l1"></a>
**日足。** [取得owner](src/baibai_jobs/acquisition/jquants_price_native.py)の期間/訂正規則を使い、旧bootstrap-cache/backfill-historyでは取得しない。保存inputs.priceを使う。正常空L1と公開policyの価格0行拒否は別。native暦で休場と証明できる未試行日だけ公開coverageから除き、raw未取得は不足として残す。旧v1へ遡及しない。初回はidentity・補正係数・coverage/lineage、月契約の日別objectによるrelease全体のobject数/manifest容量を確認し、旧objectを消さない。

<a id="financial-native-l1"></a>
**財務。** [取得](src/baibai_jobs/acquisition/jquants_financial_native.py)・[固定reader](src/baibai_jobs/integration/financial_native.py)を使う。旧stage resume/replay-copy・bootstrap/backfillを使わず、未移行原本を保持して回復する。保存JSONのinputs.generation.inputs.financial（generation headerはinputs.financial）をdescriptor付きで渡す。旧inline/共有固定入力も同readerで読み、旧Selectionを再署名しない。通常Operation再開は保存モデルで、cold再計算とは別。旧pendingは新取得を止め、大きすぎるrequestも空初期化で通さない。

<a id="master-native-l1"></a>
**master。** [疎なsnapshot](../engine/src/baibai_engine/data_collection/providers/jquants/master_native.py)で、受領済み非空日を通常再GETしない。保存inputs.masterと暦で再生する。旧bootstrapの代わりにbackfill-master/native CLIを使い、月末cohort日のbackfillだけ既存bars gridを要求する。TradingView/外部評価はGET前にnative masterを固定して元bindingで再開し、空/failed/heldを旧masterで埋めない。休場判定前の取得なので休日GETも確認する。

<a id="calendar-native-l1"></a>
**calendar。** 通常モデル/公開への切替は旧exporter依存を残さないため財務・日足・masterと同時に有効化する。明示取得は他source/L2/release不要。[range原本と補修計画](../engine/src/baibai_engine/data_collection/providers/jquants/calendar_native.py)を使い、未知HolDiv・不正日・partialを休場としない。不足時はmaster休日basisを確認する。commit-workは実受領を再確認し、空/partialを成功へ変えない。

```bash
uv run baibai-jobs rebuild-calendar-native --saved-input <SAVED_INPUT_JSON> --output <NEW_OUTPUT>
```

<a id="margin-alerts-native-l1"></a>
<a id="short-sale-reports-native-l1"></a>
**日々公表銘柄/空売り報告。** 両sourceの通常source/dailyはnative L1だけを使う。maintain-marketのbootstrap/backfillは両sourceを扱わず、上記native取得入口を使う。旧market/L2の品質をGET条件にせず、8暦日の公表確認と保持範囲の不足を調べる。休日の追加GET・provider予算を確認する。固定selection/sealをreader・モデル入力・公開lineageへ渡し、次hydrateまで読戻す。

[cache](src/baibai_jobs/integration/native_projection_cache.py)はcompleteかつParquet/SQLite全行一致時だけ再利用し、欠損/改変/partialは固定L1から作る。取得側にcache依存や毎日の全履歴copyを加えない。固定readerの全履歴0行は正常空だが、現manifestは観測行を要求するため公開不可。両sourceの切替は非空旧履歴の有限移行が前提で、架空行で通さない。

- [alerts投影](src/baibai_jobs/integration/margin_alerts_native.py)は変更公表日を投影し、year/v2の月objectを内容共有する。PubDate/AppDate・正常空訂正・局所partial、inputs.margin_alertsを確認する。
- [short-sale投影](src/baibai_jobs/integration/short_sale_reports_native.py)はDiscDate/CalcDate・page/行順/重複・開示日別coverage/確認時計、inputs.short_sale_reportsを保つ。同値確認でrawを再読せず、source_coverageはpaired machineが保持する。alertsのidentityや全銘柄信用残へ読み替えない。

<a id="margin-interest-native-l1"></a>
**週次/全銘柄日次信用残。** 通常source/dailyはnative L1だけを使い、移行flagを持たない。有限移行のverify/2dataset compareと後着受領の移送が未完なら通常公開へ進めない。native暦が必須で、旧maintenance writerは使わない。必要なnative暦を先に保持し、通常初回53日窓だけで週次230日/日次2026-09-25以降を充足したとしない。unknown_calendar_days/unknown_incomplete_requestsを休場へ変えない。

週次は230日窓、ISO週最終取引日、2営業日公表lagで2026-09-18残高まで。空の充足には実確認JST日より前の後続2営業日の暦が必要。公表日前の空と最終週を再確認し、最終週は終端後の暦がなくても公表日以降に再確認してrebuild/compareで未充足を残す。有限backfillから今日までの全暦は要求しない。

日次は2026-09-25〜asofの取引日、後続1営業日を待ち、空は未充足。7日overlap/230日へ狭めず9月19〜24日の系列を作らない。[planner](../engine/src/baibai_engine/data_collection/providers/jquants/margin_interest_native.py)は固定暦・実確認時計・raw件数を使い、非空のL2 partialをGET条件へ戻さない。plan.json/calendar-fixed.jsonをcommit-workでも保ち、同値確認は時計だけを更新して元HTTP時計を変えない。

```bash
uv run baibai-jobs jquants-margin-interest-native acquire \
  --directory <NEW_WORK> --asof 2026-11-05 --start 2026-09-18 --end 2026-10-30
uv run baibai-jobs rebuild-margin-interest-native --work-directory <NEW_READ_WORK> \
  --calendar-head <NEW_WORK>/calendar-fixed.json --start 2026-09-18 --end 2026-10-30 --output <NEW_OUTPUT>
```

終端を含む有限copy、coverage、input.json/result.jsonを確認する。saved-inputは固定margin/calendarだけを読み、最新head/旧releaseや歴史promptの完全再現を意味しない。

通常consumerは同じ固定margin/calendarから両tableを投影し、同sealを公開・inputs.margin_interestへ渡す。completeな前公開の全行parityが成立した残高日は行を再利用するが、coverageは固定暦・確認/評価時計から毎回判定する。L2欠損/改変は固定L1から再生成し、旧mixed stageは移送済みreceiptのUUID/時計/全bytes一致時だけ除外する。全market sourceのnative化後は旧release/handoffを取得条件にしない。初回公開と次hydrateを読戻し、原本・旧release・stageは保持する。

<a id="edinet-list-native-l1"></a>
## EDINETの独立L1

一覧とZIP/scopeは既定/ActionsともBAIBAI_EDINET_LIST_NATIVE=true。falseは有限検証だけで、本番旧writerを並行稼働させない。[取得](src/baibai_jobs/acquisition/edinet_list_native.py)・[保存](../engine/src/baibai_engine/data_collection/providers/edinet/list_native.py)が日別CAS・UUID/実受領時計・同値gzipを管理する。旧normalized値から原本を作らない。

日次は開始時headを固定し、全受領から一覧/文書を作る。不正dayを報告しつつ正常投影を保ち、metrics/facts plannerは必要範囲で不足を判定する。archive/L2失敗は一覧L1を取り消さない。releaseへlist_responsesを複写せず、dehydrate/次hydrateで全cache UUID・日付・時刻・gzipを原本照合した場合だけ空にする。この検証はofflineでも--bucketを指定する。他dataset/旧release/stageの保持は変えない。

一覧headの欠落を空のinitializeで埋めない。edinet-list-native initializeは既存原本を持たない新環境だけで使う。

旧一覧取得のrefresh-edinet-documents、backfill-edinet-identity、bootstrap-cacheのedinet-list groupは撤去した。旧EDINETRefresh.list_documentsはnative設定でHTTP前に拒否する。必要な一覧はedinet-list-native acquire --start <DATE> --asof <DATE> --directory <NEW_WORK>、続けてrefresh-edinet-metrics --lookback-days Nで不足CSVを取得する。Nは0/540日超を含め任意で、一覧未取得を成功へ補わない。保存原本再抽出はextractを使い、旧flagへ戻さない。

### 一覧公開失敗の再開

同workのedinet-list-native commit-workで保存attemptを公開/readbackする。workflowがstage_ready=trueを報告して失敗終了した場合は、private failure stage IDを確認し、次で同じUUIDの原本・読戻し・integrated markerを完了する。

```bash
uv run baibai-jobs edinet-list-native commit-stage --stage-id <ID> --directory <NEW_WORK>
```

旧形式stageは原本を保全し、固定native headとの不足を調査する。PUT成功後の応答不明も同UUIDで確認する。kill・disk故障・private stage保存失敗の回復までは保証せず、失敗を成功通知へ変えない。CSV/XBRL ZIP・facts scopeも独立ownerへ保存し、通常取得は旧source-input/L2公開を待たない。既存SQL consumerとlake原本Parquetは移行中の読取経路として残る。全L2のcold再生成・原本の重複配布解消・過去PIT保証は別で、残作業は[#1820](https://github.com/koumatsumoto/baibai-loop/issues/1820)に残る。

### ZIP/scopeの通常取得と固定consumer

原本を持たない隔離環境だけで次の新規取得検証を行う。本番の既存headをinitializeで置き換えず、失ったheadを部分workから再作成しない。一覧とZIP/scopeは同じBAIBAI_EDINET_LIST_NATIVEで選び、新native受領後に旧writerだけへ戻さない。

```bash
uv run baibai-jobs edinet-archives-native initialize --bucket <ISOLATED_BUCKET> --directory <NEW_WORK>
uv run baibai-jobs edinet-archives-native acquire --bucket <ISOLATED_BUCKET> --directory <WORK> \
  --group edinet-metrics --asof <DATE> --lookback-days 540
uv run baibai-jobs edinet-archives-native acquire --bucket <ISOLATED_BUCKET> --directory <WORK> \
  --group edinet-facts --asof <DATE> --initialize-facts
uv run baibai-jobs edinet-archives-native freeze --bucket <ISOLATED_BUCKET> --directory <READ_DIR>
uv run baibai-jobs edinet-archives-native commit-work --bucket <ISOLATED_BUCKET> --directory <SAME_WORK>
```

[planner](src/baibai_jobs/acquisition/edinet_archive_native.py)は固定一覧/scopeを使い、旧market/L2成功/全ZIP本文を要求しない。input.jsonの両headを保持し、各ZIPを次GET前に確定する。[保存owner](../engine/src/baibai_engine/data_collection/providers/edinet/archive_native.py)は初回bytes/stored_onを保ち、新取得だけ実UTCを持つ。pendingがあれば新HTTPを止め、同workのcommit-workへ戻る。CASは最新headとのunionで明示再試行し、部分workから失ったheadを再作成しない。通常workflowの公開失敗は未確定ZIP/実UTC/scopeだけを既存private handoffへ保全する。edinet-archives-native commit-stage --bucket <BUCKET> --directory <NEW_WORK> --stage-id <ID>で同受領を公開/readbackし、その後だけstageを確定する。通常L2は未確定stageをsealせず、旧形式stageは原本を保全して停止・調査する。killやlocal/private両保存失敗まで回復を保証しない。

手動metrics/TOBもZIPをnativeへ先に確定し、factsはscopeと原本の確定後にSQLへ投影する。--initialize/--tickerと任意lookbackを保ち、明示annual refreshへ日次holder batchを追加しない。

共通統合はplanで一覧/archive両headを固定し、途中で最新headを補わない。実行copyの既存ZIP bytes・stored_on・scopeを照合し、不足ZIPだけを取り込み、comparison/holder/TOB/factsへ同じSQLを渡す。同bytesで固定headの保存日が実行copyより早い場合だけ、copyの日付を揃える。固定head外の原本/scopeは消さず停止する。L2失敗はnative取得を妨げない。全原本cold copyはしないが、既存BLOB全hashとlake原本Parquet再exportの費用は残る。

旧canonical/release/stageと移送・受入の証拠は保持する。未移送原本や旧形式stageを通常経路へ混ぜず、固定headと原本を照合して対処を決める。

固定consumerは保存両headと新規outputだけを使い、原本欠落/改変なら公開しない。

- [SQL adapter](src/baibai_jobs/integration/edinet_archive_native.py)のrebuild_fixed_edinet_inputは全ZIP・stored_on・scopeと一覧を再構成し、EDINETReader/comparison/holder/TOBへ渡す。全ZIPを読むためplannerには使わない。L2/成功履歴を複写せず、日付変換を時計精度向上としない。
- [metrics比較](src/baibai_jobs/integration/edinet_metric_rebuild.py)のrebuild_fixed_edinet_metricsは選択CSVだけで、旧metrics/facts scope/全ZIP複写を入力にしない。completed/partialだけoutputを作り、全未取得/一覧不足は延期、parse失敗は失敗、宣言原本破損は例外とする。

一覧とmetricsを別々に確認する。過去正常一覧で計算できても最新一覧失敗は残り、quarantined_eventsのidentityを保つ。旧metricsだけの銘柄対応を推測せず、partial採用日/匿名操作隔離などwarmとの差を比較する。再計算時計を過去へ付替えず、候補生成を通常公開への採用としない。

<a id="cutovers"></a>
## 有限移行と旧受領の回復

以下は完了が確認できた環境へ再適用しない。writer停止/drain、原本・旧release/stage・planの保全、固定候補の公開/readback、consumer/inputの切替、次通常実行までを一組で確認する。codeの既定値/合成testだけでこの手順を撤去しない。

### J-Quantsの共通切替

1. 実bucketの原本/共有固定input prefixについて必要read/write権限と期限削除対象外を確認する。設定未読を初回読戻し成功で代用しない。
2. 対象旧writer/daily/queuedを止め、旧release ID/hash・UTC境界と統合済みを含む全stageを固定する。prepareのblockedを解消して原本・宣言ID・scopeを照合し、後着stageがあれば境界を固定し直す。
3. 空targetを一度initializeし、同じplanでapply→全pack cold verify→旧投影compare。対象sourceだけ移し、mixed stageの他sourceを保つ。差/coverage不足を確認し、意味差の採用は明示する。
4. 全writer/consumer設定を揃え、初回公開の固定input・coverage/native lineageと次hydrateを読戻してcronを再開する。異常時は止め、確定head/plan/旧releaseから再開先を決める。通常経路へ旧writerを戻さない。

```bash
uv run baibai-jobs margin-alerts-native-cutover prepare --directory <PLAN_DIR> \
  --work-directory <NEW_MIRROR> --release <FIXED_OLD_RELEASE> \
  --manifest-sha256 <FIXED_OLD_SHA256> --known-at <UTC_BOUNDARY>
uv run baibai-jobs margin-alerts-native-cutover apply --directory <PLAN_DIR> --work-directory <APPLY_WORK>
uv run baibai-jobs margin-alerts-native-cutover verify --directory <PLAN_DIR> --work-directory <COLD_WORK>
```

未完sourceのcutover名へ置き換える。compareも同directoryと別workを使い、alerts/short-saleは初回日--asof、margin-interestは比較終端--asofと固定--calendar-headを加える。切替後mixed stageのskipは全page fingerprint/bytes・宣言ID一致時だけで、未移行/不足/改変は止める。

| source | 共通手順に加える確認 |
| --- | --- |
| alerts / short-sale | 全market stageと予定日までの保持履歴、非空公開条件。alertsはPubDate/AppDate、short-saleは開示日/行順でcompare。初回の独立head前進と旧stage対象sourceの除外。 |
| margin-interest | 全source_pages partition/market stage。境界後・filtered request・page/宣言ID欠落・未知失敗scopeはblocked。daily_margin_incompleteは実在空/partial原本を要す。固定旧release/暦を使い、UTC境界後の確認を拒否して週次/日次全行とcoverageを別比較。通常writer接続/本番実行は別途。 |

<a id="market-schema-484950の限定cutover"></a>
### market 48/49→50

daily/open/通常pushは移行しない。source/daily/materialize/CLIとqueued writerを止め旧stageを有限drainし、旧codeでfresh machine bundle/ETagと旧releaseからhydrateしたcopyを保つ。50対応codeへ替え、同じleaseで進める。

```bash
uv run baibai-jobs market-cutover prepare-copy \
  --source <PRESERVED_HYDRATED_SCHEMA48_OR_49_COPY> --output stores/market/market.sqlite \
  --receipt <PRIVATE_CUTOVER_RECEIPT_JSON> --kind canonical
uv run baibai-jobs model-integration --asof <YYYY-MM-DD> \
  --lease-handle "$PUBLICATION_LEASE_HANDLE" --notice-output <PRIVATE_NOTICE_JSON>
jobs/scripts/r2_transfer.sh push-market-cutover <PRIVATE_CUTOVER_RECEIPT_JSON>
```

scope変換は旧initialized.sinceと全docID key（failed/origin不在含む）、保持type1 docIDを一度採り、出所digestを付ける。since欠落/不正を推測で埋めず、未初期化/ticker限定/旧stageからglobalを有効化しない。全一覧をscopeにせず、L2値/revision/errorをsource stateへ移さない。元table/bytes/時計は別copyに保持する。

outputは不存在。48は49中間copyを経由し、旧article cutoverの部分公開は旧code/元receiptで先に完了する。専用pushは旧全ETag・lease・新origin/モデル世代とupload候補digestを照合するadditive変換で、空DB再取得置換ではない。market seal/inputは公開machine receipt・両HEAD・lake releaseを、経済は単独読戻しETagを使う。取得ETagをpush結果で手書きしない。

残存stageはprepare-copy --kind stageで別pathへ変換し、result.jsonと共にintegration.source_handoff replay-copy --source <OLD_OWNER> --database <HYDRATED_SCHEMA50_COPY> --directory <CONVERTED_STAGE_DIRECTORY>で限定再生する。元stage keyへ再uploadせず、切替中の後着新旧stage/pendingも照合する。元時計/identityを保ち、全group inputを現正本から再発行してschemaを確認してから取得を再開する。

| 到達点 | 再開 |
| --- | --- |
| pointer前 | 旧公開を保ち、元ETag/originで未公開処理だけ再実行。 |
| pointer後/store前 | 新release/origin/モデルを照合し、元ETag一致の間に専用pushで完了。旧codeへ戻さない。 |
| machine部分PUT/応答不明 | 同receipt/成果の`jobs/scripts/r2_transfer.sh resume-market-cutover <RECEIPT>`を使う。現ETagと実downloadの候補digestを照合し未到達keyだけCAS。第三の値なら停止。盲再送/receipt削除で代用しない。 |
| machine成功/seal失敗 | 公開release/原本を読み、同じapplied.jsonからseal。再GETしない。 |
| seal成功/input失敗 | 公開世代を保ち、下記で対象groupのinputを再発行してから取得を再開。 |

同じlease・fresh正本を使い、経済groupは単独L1 ownerへ戻す。

```bash
uv run python -m baibai_jobs.integration.sources inputs --directory <NEW_INPUT_WORK> \
  --group <GROUP> --identity <NEW_EXECUTION_ID> --asof <YYYY-MM-DD> \
  --lease-handle "$PUBLICATION_LEASE_HANDLE"
```

optional新datasetも旧reader/catalogは拒否する。新公開後は50/catalogを保つ前向き修正とし、新モデル停止の際も保持記事を落とさない。通常push/専用cutoverを混ぜず、停止/drain/変換/公開/machine/seal/inputを別記録する。


### 経済単独公開と失敗再開

経済正本はindicators.sqlite、machine bundleはmarket/runsの2key。旧bytes/stage/backupと切替receiptは保持し、旧3key切替receiptを通常の2key公開へ読み替えない。

通常経済公開はfresh正本＋固定handoffを受入→1世代backup→条件付きPUT/全bytes読戻し→seal→15取得group inputの順。TradingView明示MCP原本も受入れるが取得groupは増やさない。seal/input直前にも同じETag/leaseを確認し、読戻し経済入力からモデルを作る。後段L2失敗で経済L1を再PUT/rollbackしない。

| 到達点 | 再開 |
| --- | --- |
| 公開/readback失敗 | 後段を止め、実bytesを照合する。未確認ならsealしない。空DB/旧値へfallbackしない。 |
| PUT済み、seal前 | 次のfresh runnerが未sealの同じ不変stageを再適用する。seal失敗を飛ばして同sourceの後続watermarkを進めない。 |
| seal済み、input途中 | 次cycleのfresh copyと新attempt identityで全groupを再発行する。旧local出力を上書きしない。 |
| A→同値A→Bの一括公開後に中断 | 本文省略の中間stageがneeds_explicit_recoveryになり得る。stage ID/hash、中断runの公開readback report、現正本/次inputを照合し、元時刻/markerを変更せず要確認として残す。最新B/inputと次の通常取得は前進できる。 |

最後のcaseは新取得成功を旧stage採用証明にしない。現在値は新inputから通常取得し、FRED確認state自体の欠落だけ[通常のrecheck-source](#経済原本)を使う。全sourceに同じ確認方式を広げず、source別object化とFRED HTTP bytes保存は#1820の残作業とする。

## 保存済み旧runを読む

旧ER/FV・四Approach/較正は再生成・再配布せず、exact runと[保存readerの意味](../docs/reference/estimate-calibration.md)を使う。現在の推計で埋めず、過去L3/Review/台帳は保持する。

```bash
uv run baibai-engine historical-evaluation run-show --run-revision-id <RUN_REVISION_ID> --runs-db <PRIVATE_RUNS_COPY>
uv run baibai-engine historical-evaluation candidates show --run-revision-id <RUN_REVISION_ID> --runs-db <PRIVATE_RUNS_COPY>
```

<a id="financials-identity-cutover"></a>
<a id="company-financialsの保存idと公開時入力の読取"></a>
## 保存IDと古いworkspace

Company Financialsはcompany_financials、runs 8/application 29で、通常open/dailyは旧schemaを変換しない。完了済みconverter/publisherは撤去済みで、固定Git履歴と私有backup/planを保持する。

Operationのselection_identity_migrationは不変証拠、published_selection_canonicalは元公開hashの原文。現在の名称正規化したSelectionへ過去Proposal/Review/本人報告/訂正/台帳を再署名しない。履歴readerは同transactionの現hash/receipt/指定ProposalのReview/exact scopeを照合し、旧canonicalをopaqueに返す。通常readerへ旧名alias/hash例外を加えない。

```bash
uv run baibai-engine operation --db <APP29> selection-evidence <OPERATION_ID> \
  --selection-sha256 <PUBLISHED_SELECTION_SHA256> --proposal-id <EXACT_PROPOSAL_ID>
uv run python -m tools.financials_identity_workspace --db <APP29> \
  --operation-id <OPERATION_ID> --proposal-id <EXACT_PROPOSAL_ID> \
  --source <ORIGINAL_WORKSPACE> --archive <NEW_ARCHIVE> --output <NEW_WORKSPACE>
```

helperは現DB読取とlocal保全/生成だけ。旧manifest/domain-inputs/draftをまとめてarchiveし、新directoryへcompleted運用投影だけ復元する。旧出典indexは元canonical/domain-inputsと一組で解決し、新manifestへ旧draftをコピー/force転用しない。

<a id="保持とgc"></a>
## ローカルmirrorの整理

GCはlocal lake mirrorだけ。currentから到達しない対象prefixをgrace後にplanし、applyの再plan hash一致時だけ削除する。native L1/共有固定input/旧較正成果は別寿命で、R2は削除しない。

```bash
uv run baibai-jobs snapshots inventory --root stores
uv run baibai-jobs snapshots gc --mirror stores
uv run baibai-jobs snapshots gc --mirror stores --apply --plan-hash <HASH>
```

immutable Bucket Lockとmutable pointer/stagingを区別する。rollback pointer/全履歴監査を追加せず、問題releaseは正しいstoreから前向き発行する。

## Storeをseedする

初回だけ4 keyすべて不存在を確認してseed.shを使う。既存一件でも通常更新/復旧へ戻る。1〜3 keyの部分seedは供用前・writerなし・exact key確認済みの場合だけ初回復旧とし、無条件上書きで直さない。次のapplication更新はpull-appで世代ごと取得する。

## 定期maintenance

PMIは公表翌週、TSE資本政策/JPX廃止は翌月15日以降に既存refresh/buildと必要な公開を行う。全履歴監査を定例化しない。依頼された注文結果だけmeasure_limit_outcomesで測り、価格差を逸失利益や指値規律の因果効果としない。保存済み旧評価の読取は[旧成果reader](../docs/reference/estimate-calibration.md)へ。

通知webhookのrotationはDiscord新規/再生成→DISCORD_WEBHOOK_URL更新→通常通知確認。旧webhookはDiscord側で消すまで有効で、過去Screening再実行は不要。実credential変更は別の依頼範囲に従う。

<a id="web-serving"></a>
## 保有・運用Webの表示更新

表示対象は[Web契約](../web/backend/src/baibai_web/materialize.py)を正本とし、旧候補/macro/調査一覧/L1公開APIを復活させない。core保存後にapplicationと同local marketから生成し、表示失敗でモデル/source sealを止めたり戻したりしない。明示application更新後はpublish.shが条件付き反映後にcloud-materializeを起動する。

廃止したTaskの既存schema29表/行とviews/tasks.jsonは未参照のまま保持し、通常初期化/公開で削除・移行しない。Operation・台帳の予約期限判定・JPX原本取得は存続する。

mainのcloud-materializeは検証済みapplicationを必須とし、marketを固定releaseからhydrateする。market失敗は赤いrunとして残すが、未検証marketを読ませず、時価等を未評価にして最新ledger fact/保存成績/提案を配信する。market復旧と表示公開を別確認する。旧run/economic storeは不要で、portfolio-dashboard.jsonは旧dashboard.jsonを上書きしない。

公開view全てをR2から読戻し、生成JSON全bytes一致を同じlease内で確認する。UI切替前に成功したmain runを確認し、未実施なら再生成/公開/読戻しする。その後UI対象HEADを確認してweb workflowをmainで明示dispatchし、認証API/画面を確認する。PR CIだけを本番更新とせず、旧object/secretは削除しない。

```bash
uv run python -m baibai_web.materialize --output-dir /tmp/baibai-serving
```

これはlocal生成だけ。cloud workflow内だけが`jobs/scripts/r2_transfer.sh upload-portfolio-views /tmp/baibai-serving`で公開する。資本/損益/保有の不足/エラー、成績、提案のready/empty/unavailableを照合する。提案0件のempty成功と保存ID/基準日/発行時刻付き本文カードの受入は別。

<a id="web-authentication"></a>
VIEW_PASSWORDはowner Bearer、READ_ACCESS_TOKENは共有Bearerまたは?share=で、同じviewを読める独立credential。追加password/権限区分を作らず、R2 credentialと混ぜない。owner変更やUI再配備で共有tokenは失効しない。実Workerの対象secret更新/削除/空値と、旧拒否・新/owner読取維持を別途認可された操作として確認する。秘密や共有URLを本文/shell引数/logへ残さない。
