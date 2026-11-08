# 運用手順

repository rootのBashで、依頼された対象だけを操作する。[architecture](../docs/architecture.md#store-authority)は正本と責務、[development](../docs/development.md)は環境、CLIのhelpはoptionを扱う。本書は開始・到達点・復旧を扱う。

<a id="actions-write-boundary"></a>
## 実行前の確認

通常取得・公開は既存Actions、application更新は認可環境の作業copyと正規CLIを使う。環境接続/開発変更を本番更新・課金・権限拡大・broker操作・未報告取引入力の許可にしない。課金禁止ならrunner/storage/API/R2も確認する。code撤去で配備済みWorker/domain/credential/旧objectを削除しない。

baibai-storesは非公開正本。秘密・endpoint・原文・私有DB/原稿をGit・Issue・公開log/artifactへ出さない。writer secretは取得/公開step、DISCORD_WEBHOOK_URLは通知stepだけへ渡し、通常開発へwriter credentialを流用しない。認証/403/429を別経路や権限拡大で迂回しない。

- 原本受領・L2・公開/readback・seal・次input発行を分け、正常空/partial/未試行/失敗を残す。L2失敗で保存L1を戻さない。
- 固定plan/receipt/ID/hash・原bytes・実受領時計を保つ。結果不明は現objectを照合し、新UUID・日付付替え・marker手修正・古いcopyの盲再送で解消しない。
- schema/origin/CAS/必要なlease不一致は停止する。参照中の旧release・stage/input・原本を保ち、通常dailyへ移行/全期間再取得を代行させない。

## 操作を選ぶ

| 目的 | 入口 |
| --- | --- |
| 日次取得/統合 | [日次](#daily) |
| 特定sourceの取得/再抽出 | [source操作](#sources) |
| 保存Generationへ現政策 | [候補政策だけ](#screening-only) |
| 固定断面の分析 | [読取](#fixed-release-read) |
| machine/経済の反映・復旧 | [共通公開](#machine-publication) |
| application更新/復元 | [更新](#application-db-を反映する)・[復元](#application-db-を復元する) |
| 初期化/未完移行・表示 | [移行](#cutovers)・[Web](#web-serving) |

<a id="daily"></a>
<a id="日次実行と結果確認"></a>
## 日次と到達点

修正をmainへ反映してから必要な範囲だけ実行する。実scheduleは[workflows](../.github/workflows/)、対象日は[schedule owner](src/baibai_jobs/schedule.py)を参照する。run IDを固定し、監視timeoutだけで重複dispatchしない。

```bash
gh workflow run cloud-daily-batch.yml --ref main -f asof=<YYYY-MM-DD>
gh run list --workflow cloud-daily-batch.yml --limit 5
gh run watch <RUN_ID> --exit-status --interval 60
```

asof空欄は元run作成JST日。寄前/場中に完了済み日次が必要なら前日を指定し、前営業日へ戻して週末情報を落とさない。queue/rerunは元時計/slotを保持するが、run作成自体が次slotを越す24時間以上の遅延ではasofを明示する。生成対象日と価格日は別で、休場の価格日を繰り上げない。

共通統合は全providerを待たず、到着済み受領を固定する。経済L1の公開/readback/seal/input発行→marketのsource別投影/EDINET抽出→固定market releaseと経済selectionによるモデル/候補保存→machine反映/readback・sealの順で進む。source失敗は前回適格投影を保って他sourceを進めるが、モデル/公開失敗は後段反映を止める。model-integrationは外部取得/Web生成を呼ばない。

停止時は最後の未完phase、store/release・generation/selection・lease解放を確認する。logのdoneや通知成功は反映の証明でなく、限定source成功を全体成功にしない。候補0を投資機会なしとせず、[不足理由](../docs/reference/screening-runtime.md#入力不足と品質)を確認する。既定読取は発行時計より新しいas_ofを優先する。過去計算はexact IDで読み、古い非空候補へfallbackしない。

取得期限にはexport/stageの余白を残す。Retry-Afterを短縮せず、認証/403/解析失敗へretryを広げない。継続不能429は同quota groupのそのpassを止める。H.15のbrowser起動不能とsource拒否を分ける。[watchdog](../.github/workflows/cloud-batch-watchdog.yml)のMISSINGは対象日を固定して調べる。

```bash
gh workflow run cloud-batch-watchdog.yml -f check_date=<YYYY-MM-DD>
```

<a id="screening-only"></a>
### 保存済みGenerationへ政策だけ適用

同じleaseでfresh runsを取得し、保存V2のexact generationへ通常の政策を適用する。同ID同内容はno-op、異内容は競合で止まる。

```bash
with_publication_lease bash -c '
  jobs/scripts/r2_transfer.sh pull-runs &&
  uv run baibai-jobs screening-selection --generation-id <GENERATION_ID> --runs stores/screening/runs.sqlite &&
  jobs/scripts/r2_transfer.sh push-runs
'
```

fresh正本でID・政策・候補を読戻す。旧Selectionは変更せず、保存旧推計は[旧成果reader](../docs/reference/estimate-calibration.md)を使う。

<a id="fixed-release-read"></a>
## 固定releaseを読む

currentを一度resolveし、固定release ID/manifest SHA-256が列挙するobjectを通常readerで検証する。resolveだけではdata確認にならず、optional未収録を別release/providerで埋めない。

```bash
uv run baibai-jobs snapshots resolve --mirror stores --bucket baibai-stores --format json
uv run baibai-jobs snapshots resolve --mirror stores --bucket baibai-stores \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

daily/local publisher終了後に`jobs/scripts/pull.sh`でbulk作業copyを取得し、machine receipt/ETagとhydrate開始時のmarket origin/releaseを照合する。競合/途中失敗copyは分析に使わずfreshに取り直す。applicationはpull-appで別取得し、保存L2のInputSnapshotを現在quote用copyへ付け替えない。

<a id="local-release-hydrate"></a>
### 完全local mirrorからのhydrate

完全mirrorとDB/一時snapshot容量を用意する。旧contractは元commitで読み、不足/破損を別releaseで補わない。

```bash
uv run baibai-jobs snapshots hydrate --mirror <COMPLETE_LOCAL_MIRROR> --store <LOCAL_MARKET_COPY> \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

local mirrorはbucketなし。ただし旧EDINET原本cacheの[除去照合](#edinet-list-native-l1)にはbucketも指定する。hydrateは対象tableを置換するため変更後に再実行せず、store-local取得coverageを作らない。Parquet直接読取も[reader](../engine/src/baibai_engine/storage/snapshots/reader.py)が検証したpathだけをlake_sessionへ渡し、globやremote拡張は使わない。

<a id="machine-publication"></a>
<a id="ローカルからクラウドを更新する"></a>
## 共通公開とmachineの復旧

market/runs・経済は同じpublication lease内でfresh正本/取得ETagを照合し、read/merge/publishする。長い準備は先に済ませ、lake tableは変更前にhydrateする。applicationのpublish.sh・初回seedは対象外。独立native L1はsource固有CAS/並行制御を使い、共通leaseやL2成功を課さない。

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

releaseを保証するため関数内でset -eを使わない。busyはowner/expiry/runを確認し、release失敗時はhandleを保つ。queueだけではlocal writerを排除しない。malformed leaseは自動修復せず、全active/pending writer不在を確認した明示復旧だけexact lease keyを調べて削除→通常acquireとする。prefix削除/手書きJSONで直さない。

日次job枠210分・新daily lease220分、他purposeは120分。旧120/180分handleは元期限のまま読み/releaseでき、途中延長しない。旧codeはreleasedでも220分leaseを読めないため、切替時は旧active/queued writerをdrainし全writerのcodeを揃える。hard cancel後は元expiryまでbusyが残り得る。

| 変更 | 反映経路 |
| --- | --- |
| lake table | publish-lake → push-market |
| market store-localだけ / runsだけ | push-market / 取得世代が合うpush-runs |
| 経済原本 | push-indicators。L2利用のために再pushしない。 |
| 元bundleからlocal daily全体 | publish-lake → push-machine。market/runsを別pullで組み替えない。 |

```bash
with_publication_lease bash -c 'jobs/scripts/r2_transfer.sh publish-lake && jobs/scripts/r2_transfer.sh push-market'
with_publication_lease jobs/scripts/r2_transfer.sh push-indicators
```

lake→store→seal/inputの順を守る。machineはmarket/runsへ非原子的にPUTし、全成功後にmachine-manifest.jsonを作る。経済は別CAS/readback。machine前世代backupはkey.bakの一世代で次回置換される。

### 部分 push からの復旧

logと現objectを先に確認する。lakeだけ進みmarket/runsの両ETagが旧receiptと一致する場合は、receipt削除/旧store復元をせず通常日次を前向きに実行する。消失したrunner-localモデルへ元ID/hashを付けて再生成しない。通常日次の再計算にprovider workflowの再実行は不要。

片方のkeyだけ進んだ場合は同じpush/dailyを盲再送せず、local成果/origin/取得世代が成立する上表の経路で完了する。経済pushはmachine部分反映を修復しない。receipt再生成が必要な明示復旧だけ、bucket直下のmutable一件を確認して次を行う。

```bash
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  aws s3api delete-object --bucket baibai-stores --key machine-manifest.json \
    --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
)
```

次pullは未検証setとなる。完全pushとreceipt一致までcross-section保証は回復しない。前世代復元が必要ならwriter停止後、exact key/key.bakを確認し`aws s3api copy-object --cli-read-timeout 300`を使い、fresh pullと世代を再検証する。失敗時の追加削除/dispatch反復や全storeの巻戻しをしない。

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

独立取得はsource固有原本を使い、全market/applicationをhydrateしない。L2不足を再GETで埋めず、同source/groupの取得〜stageを直列化し、共有quotaを守る。J-Quantsはnative head、未移行sourceは固定L1/未統合原本から計画する。

共有[source input](src/baibai_jobs/acquisition/source_inputs.py)・記事planningを使う経路だけ、取得有効化前に共通統合から初回発行する。inputは再生成可能な計画材料で正本ではない。元bindingで再開し、欠落を全DB取得/空seedで迂回しない。形式切替では対応readerを先行させ、非対応のactive/queued writerをdrainする。旧input/stageは保持する。

破損handoff/decodeは当該sourceを報告し他を進め、正本破損/R2全体障害は停止する。遅着は次統合へ渡し、needs_explicit_recoveryは元ID/hash/時計/markerを保ってexact回復する。正常nullを全期間取得で埋めない。

<a id="economic-acquisition"></a>
### 経済原本

[workflow](../.github/workflows/cloud-economic-sources.yml)は休場日もgroup別に取得し、部分受領をprivate stageへ保存する。限定再実行は所属groupとsource IDを指定する。

```bash
uv run baibai-jobs acquire-economic groups
gh workflow run cloud-economic-sources.yml --ref main -f group=fred_csv -f source_id=fred-payems
uv run baibai-jobs acquire-economic refresh <SOURCE_ID> --all-history --end <YYYY-MM-DD>
```

取得窓/空seed/確認はsource別[policy](../engine/src/baibai_engine/data_collection/acquisition/policy.py)に従う。空seedを欠落の自動初期化にせず、一律の短期窓へ狭めない。FRED全CSV・BOJ四半期APIは期間を狭めても通信が減らず、返却された窓外期間も保持する。未返却を撤回/全履歴確認とせず、history_recovery_requiredは別の有限期間/all-historyで回復する。同じ無期間応答を回復用に重ねて処理しない。

FRED同値は原本を複写せず実確認時計を進め、本文変更・公表時計と分ける。完全応答の期間集合/hashを保ち、headerだけ/短い行を成功としない。確認欠落/遅着で通常pushを拒否されたときだけ、fresh正本を基点に実GETする。

```bash
with_publication_lease jobs/scripts/r2_transfer.sh push-indicators --recheck-source <FRED_SOURCE_ID>
```

失敗時は両copyを保つ。このrecheckを他sourceへ広げず、現値/完全応答/実確認/保持原本から過去の採用順を作らない。他のrecord sourceの確認は取得range内だけの証拠で、source全体の置換や明示nullを証明しない。旧rawの実時計を使い、捨てた時計をrun終了時刻で補わない。

同値確認欠落は[local診断](../tools/diagnostics/inspect_economic_confirmation_gaps.py)で調べる。閉じたcurrent-schema canonicalと、integrated済みも含む明示stageのhandoff.json/receipts.sqlite/取得済みmarkerを私有copyへ固定する。認可済み環境で既存read_private/_read/download_fixedを有限に使い、manifest SHA・receipt key/bytes・markerの元manifest/source/started_at/status・canonical SHA/ETagとcopy前後の不変を確認する。書込を拒否し、無制限prefix走査・旧schema移行・失敗copy上書きをしない。available_handoffsを使うならinclude_integrated=Trueが必要で、marker未供給を未統合の証明にしない。

```bash
uv run python -m tools.diagnostics.inspect_economic_confirmation_gaps \
  --canonical <LOCAL_CANONICAL> --canonical-sha256 <FIXED_SHA256> \
  --stage <STAGE_ID> <MANIFEST_SHA256> <LOCAL_PACKET_DIRECTORY> \
  --source-id nikkei-pbr --start <OBSERVED_DATE> --end <OBSERVED_DATE> \
  --received-since <RECEIVED_AT_WITH_ZONE> --received-until <CUTOFF_WITH_ZONE>
```

stage指定は必要分を繰り返す。終了0は固定copyの診断完了だけで、全stage網羅・remote headの現在性・保存済みL2への影響を証明しない。canonicalのfetched_atは確認時計がなければ既知の下限にすぎない。欠落を空応答へ変えず、同時計衝突は要確認とする。[#1962](https://github.com/koumatsumoto/baibai-loop/pull/1962)/[#1965](https://github.com/koumatsumoto/baibai-loop/pull/1965)のcode統合と、未実施の本番影響確認・保持stageの有限回復は別である。一回用copy例の撤去もデータ課題の解消を意味しない。

回復時だけcanonical確認時計以降〜固定cutoffのstage欠落/後続受領を人が調べる。原本が揃い一意なら再GETせず既存accept_rechecks/merge/verifyで別copyを準備する。不足時の限定再取得は現在の実確認であり、過去時計の復元や衝突解消の証明ではない。実公開は対応code、lease/CAS・backup・全bytes読戻し・input再発行を別に確認し、元stage/marker・保存L2/L3を変えない。

current-onlyは実取得日を使い、他handoffの日付は動かさない。松井の過去asofを現在pageで埋めず、optionsの古い訂正は明示期間で取得する。

<a id="pmi-public-articles"></a>
PMIはS&P公式PDFの日本/米国製造業headline・サービスBusiness Activity Index。未接続TE adapterやISM/Output/Composite、旧値/snippet、拒否後fallbackで代用しない。原文/抽出・速報/確報・対象月・公表/配信時計を分け、完全PITとしない。[採否](https://github.com/koumatsumoto/baibai-loop/issues/1477)を参照する。

<a id="指標観測とregistryの修正"></a>
原本訂正は再取得/明示再構築、系列/式はL2定義、取得条件はData Collectionで変更する。式変更だけで再取得せず、local DELETEをcloud mergeへ流さない。

```bash
uv run baibai-jobs acquire-economic list
uv run baibai-engine market-context reading --asof <YYYY-MM-DD>
uv run baibai-jobs validate-market-context-stores
uv run python -m tools.pmi_manifest --dry-run
uv run python -m tools.pmi_manifest
```

validateはL2算式/L3履歴の証明でない。原本更新時だけ経済pushする。PMIは公表翌週にdry-run→manifest更新→対象月refresh→公表値照合→commit。URL訂正/同URL差替えと抽出式変更を分け、式だけなら保存PDFを再読する。403で通知が省略されてもUNAVAILABLE/Actions失敗は残り、未公表と推定しない。

<a id="article-acquisition"></a>
### 記事

起動は[workflow](../.github/workflows/cloud-article-sources.yml)、source/予算は[契約](../engine/src/baibai_engine/data_collection/providers/articles/contracts.py)、cycle/再開は[planning](src/baibai_jobs/acquisition/article_planning.py)に従う。group直列化/共有host queueを守り、試行時計を受領成功にしない。TSRの正常indexでの日次充足とBOJ/MOFの再発見/未処理本文を分ける。

初回planningは取得前にintegration.sources inputsで発行し、原本stage保存後だけCAS更新する。旧planningからの切替はpendingを新stageへ引継ぎ、旧writerをdrainする。統合は不変stage/markerから発見し、sealでplanningを変えない。L2停止中も原本/worksetを保つ。連鎖照合は失ったroot・CAS未確定孤立stage・導入前pending外の全原本復元を保証しない。

continuation_onlyのpartialは正常継続で全取得完了ではない。HTTP/parse/deadline/CAS/保存失敗を分け、正常sourceを進める。HTTP失敗はDEGRADEDに残し、受領後parser失敗と分ける。200同値も別受領、304は具体的200 bodyへの参照。新GETで失った過去原本を復元したとしない。

案内/転送/添付は親子の別受領。トウシル同host公開301/308は既存予算内でLocationを保持し、一般redirect/認証URL/未許可hostや財務省過去PDF全巡回へ広げない。未保存Locationを推定せず、workset未完を捨てたり予算を増やしたりしない。sealは固定releaseの今回receiptと抽出artifactの読戻し後に行う。

統合前はexact stage/receiptをprivateに読み、再GET/planning変更をしない。旧market schemaでもArticle専用shapeとbody digestで照合し、元SQLiteのbytes/版数を変換しない。

```bash
uv run baibai-jobs article-evidence --source <SOURCE> --stage-id <STAGE_ID> --manifest-sha256 <HASH> \
  --receipt <RECEIPT_ID> <RECEIPT_SHA256> --directory <PRIVATE_OUTPUT>
```

stage原本の読取はrelease所属/抽出成功/長期保持の証明でなく、統合後は同ID/hashを固定releaseで照合する。

### 記事のexact遅着回復

```bash
uv run baibai-jobs article-recovery prepare --source <ARTICLE_SOURCE_ID> \
  --stage-id <EXACT_STAGE_ID> --manifest-sha256 <EXACT_MANIFEST_SHA256> --directory <PRIVATE_RECOVERY_DIRECTORY>
uv run python -m baibai_jobs.integration.sources apply --asof <YYYY-MM-DD> --directory <PRIVATE_RECOVERY_DIRECTORY>
```

prepareはprovider取得やcanonical公開をせず、指定一件とその時点のEDINET pairだけをplanへ保存する。pair固定のhead読取・immutable input保存は行う。他のpending stageやexternal readyを混ぜず、通常planで上書きしない。同じfresh canonical/leaseでapply→model-integration→push-machine→seal/inputsへ進む。元receipt/時計/markerを変えず、成功はrecovered-<hash>.jsonに残す。CAS失敗原本のexact統合とcursor回復は別で、進んだcursorを戻さない。

BOJ/MOFのoffline再発見はregister-discoveryへstage/hashと--receipt-idを渡す。HTTPなしのplanning CAS書込で、同要求はno-op。元受領/validator/slot/未処理workを保ち、登録後に通常取得を続ける。

<a id="edinet-source-archives"></a>
### JPX PDFとEDINET

JPX決算予定/規制は過去factで取得を止めず、fresh source DBの部分受領をprivate stageへ残す。手動bootstrapは`maintain-market bootstrap-cache --asof <DATE> --source-group jpx-earnings`または`jpx-regulations`だけを指定する。旧all/J-Quants groupやbackfill-historyは使わない。

明示PDFは既知URLで取得済みの原本だけを`financial-pdf-source --receipts <JSON> --directory <NEW_PRIVATE_DIRECTORY>`へ渡し、daily自動取得へ加えない。HTTPなしで実UTC/bytes/hash等を検査するため、mtime/公表日で時計を作らない。生成後はintegration.source_handoff stageへ--source jpx-financial-pdfs（完了転換は--collection capital_actions/--source jpx-capital-actions）を渡す。不対応も原本を保ち、株数/自己株を推定しない。選択/訂正basisは[観測reference](../docs/reference/model-observations.md#reported-comparisons)へ。

EDINET一覧→保存ZIP→L2抽出の順。手動source=allは一覧/stage成功後にarchiveへ進む。一覧失敗時はall内archiveを止め、有効一覧を確認できる場合だけ同asofのarchiveを再実行する。保持docID/typeを再GETせず、L2成否/revisionを取得条件にしない。holder/訂正の未保存分は既存batchを使い、403/認証/制限でも部分原本を保つ。

一覧/archive未到着はdeferred_inputsで適格保存値を使い、到着後に再抽出する。解析失敗/原本破損を延期で隠さず、不正dayは正常投影を保ちながらsource-check失敗を残す。延期だけの継続とpartial/日別成功を分ける。誤orphanは現年度CSVを確保して次統合で回復し、全期間GETを代行しない。

<a id="edinet-research-facts"></a>
<a id="edinet-factsの取得範囲schema-50"></a>
<a id="edinet-research-query"></a>
<a id="edinet-factsの書類選択"></a>
### EDINET factsの取得と読取

固定releaseから[facts reader](../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_facts/)を使い、文書/ticker/対象JST日とusable_documentを確認する。日末cutoffはintraday PITでなく、未選択は書類不存在を意味しない。segment/debtはdocID・期・連結・通貨を揃え、抽出行だけで全債務/資金余命を確定しない。

facts scopeはL1取得範囲。L2品質でglobal開始日/docID集合を狭めず、failed/未提供/交代のfamilyも出所と共に保つ。初回annual取得は明示initialize成功後だけ有効化し、holderは独立。途中失敗は同じrefreshで再開し、ticker限定で他社/globalを置換しない。offline extractのinitializeは将来GETを有効にしない。

```bash
uv run baibai-jobs maintain-market refresh-edinet-facts --asof <YYYY-MM-DD> --initialize --sqlite-path <STAGED_MARKET_SQLITE>
```

再抽出だけならextract-edinet-facts。stored_onは初回保存UTC日で公表時刻でない。未対応0行/failedと鮮度を分ける。

<a id="edinet-metrics-revision"></a>
metric revisionは抽出/選択の意味変更だけで更新し、配置/import/retryやfacts revisionと分ける。隔離copyで保存CSVを再抽出・比較後、同じleaseでfresh pull/hydrate→backup→extract-edinet-metrics→publish-lake→push-market→固定readback。期待外差/失敗は止め、変更後hydrateや旧copyの本番復元はしない。

<a id="external-assessment-acquisition"></a>
### 外部評価

通常TLS/proxyで有限銘柄を直列取得し、試行別private captureをcurrent-schemaの隔離marketへimportする。parse失敗でも原本は保ち、L2を部分追記しない。再投入0件を確認する。

```bash
uv run baibai-jobs external-assessment collect --source minkabu --tickers 7203 4436 --output <NEW_PRIVATE_CAPTURE>
uv run baibai-jobs external-assessment import --input <PRIVATE_CAPTURE> --db <LOCAL_MARKET_COPY>
gh workflow run cloud-external-assessment.yml --ref main -f source=monex -f batch_id=<LABEL> -f master_asof=<YYYY-MM-DD>
```

日次は固定master/calendar/parserの元bindingで再開する。依存不足を全market/TradingView起動で迂回しない。同sourceを重ねずqueue後cycleを確認し、plan再作成/hash手修正をせず期限内未試行pairだけを進める。保存失敗pairの再取得は別labelとする。

新原本はretained。旧rawは3日削除の短期handoffで、厳密72時間/長期L1ではない。残存する同bytes/元IDを保全するときだけ次を使う。

```bash
uv run python -m baibai_jobs.acquisition.external_assessments.raw --preserve-run-id <EXACT_RUN_ID>
```

lease外でacquire→inspect→ready --cumulative、共通統合で固定集合を公開/readbackしてackする。後着小集合で後退させない。確定分で明示終了するときだけworkflowの-f operation=stage_confirmed_partialを使う。403/429/未完を自動closeしない。pointer後terminal失敗は現状態を読み、未sealならfresh hydrateから再適用する。hard kill前の未保存試行・期限切れ原本の復元は保証しない。history_digestsは保存後baselineで、更新前の全履歴保全を証明しない。

<a id="tradingview-expectations"></a>
### TradingView

本人確認済み利用範囲・毎日取得の指示と既存tradingview-runtime認証を使う。一般FAQだけで停止/除外せず、新credential/課金/再配布へ広げない。認証失敗を充足にしない。

[日次](../.github/workflows/cloud-tradingview-snapshot.yml)は未取得原応答だけを再開する。originals_savedは再GETせずrebuildし、L2だけのcopyでは不足原本を受領して既存L2を保つ。後受領を旧L2の生成元にせず、過去cycleを現在応答で埋めない。cycleは09:00 JST切替、営業日15:30/休日09:00から翌09:00未満。休日の前営業日basisは[master/calendar](#master-native-l1)に従う。

必要銘柄/fieldの揃った原応答はL2検証失敗でも保持し後続batchを進める。不足はpartial/診断へ残し、無効応答連続・HTTP/認証・時刻/予算・保存失敗の停止境界を維持する。

receiveはHTTPなしでSDK応答全体/失敗を保存する。同IDは全内容一致の再送だけ、別呼出しは別ID。新受領は共通統合へ渡し、取得copy全体をpushしない。

```bash
uv run baibai-jobs tradingview-source receive --sqlite <LOCAL_MARKET_COPY> --capture <PRIVATE_NATIVE_CAPTURE_JSON>
uv run python -m baibai_jobs.integration.source_handoff export --source tradingview \
  --asof <JST_RECEPTION_DAY> --since <BEFORE_RECEPTION_UTC> --database <LOCAL_MARKET_COPY> --directory <NEW_PRIVATE_HANDOFF>
uv run baibai-jobs tradingview-source rebuild --sqlite <LOCAL_MARKET_COPY> --asof <YYYY-MM-DD> --receipt-id <RECEIPT_ID>
uv run baibai-engine external-assessment show --ticker <TICKER> --cutoff <UTC_TIMESTAMP> --db <LOCAL_MARKET_COPY>
```

rebuildは選択receipt集合で一日L2を置換するため、部分追加だけで既存日を置換しない。元master/calendar・最初の正常行/receiptを保ち、過去判断/除外履歴へ遡及しない。

### 判断直前の多市場補助観測

[market-evidence](src/baibai_jobs/acquisition/market_context.py)はquote/5分足のprivate原受領で、日次充足・価格master・正式提案終値とは別。prepare→stageで保存し、別sessionは返却ID/hashでdownload/readして同じAssessment入力を使う。未保存captureに耐久参照を付けない。

固定economic入力の参照/時計/欠測と、評価時刻/基準JST日末の両cutoffを守る。最新失敗を旧成功で埋めず、前日基準へ当朝夜間受領を加えるなら別入力にする。休場/遅延/限月/basisを揃え、受領時計を取引時計、bar tを最終約定、update_mode不明をリアルタイム、末尾足を確定値にしない。価格で判断を変えるなら新入力/再Reviewへ進める。

<a id="jquants-native-operations"></a>
## J-Quants native L1

7sourceはnative L1だけで取得し、L2生成/公開と分ける。旧writer・移行flagへ戻さない。入口は`jquants-<source>-native`のinitialize/plan/acquire/resume/commit-work。初期化は原本のない新しい空namespaceだけで一度行い、head消失/移行失敗を空initializeで迂回しない。新plan/acquireは新directory、結果不明はresume、保存attemptは同directoryのcommit-workを使う。各source optionはhelpを参照する。

| source | 明示取得範囲 |
| --- | --- |
| financial / price / master / calendar | --start / --end。master/calendarの--forceは明示訂正だけ。 |
| margin-alerts / short-sale-reports | --asof、任意--start。休場も公表確認。 |
| margin-interest | --asof、有限backfillは--start / --end。固定calendarが必要。 |

`rebuild-<source>-native`は--work-directory / --start / --end / --outputから新しいL2とinput.jsonを作る（calendarは下の専用引数）。保存Generation/Selectionの再計算はdescriptor付きexact InputSnapshotを--saved-inputへ渡す。head/summaryに付け替えず、新outputを使う。L2の欠損/改変/partialは固定L1から作り、取得の再GET条件へ戻さない。

<a id="sourceごとの通常読取"></a>

<a id="price-native-l1"></a>
**日足。** 保存inputs.priceを使う。正常空L1と価格0行の公開拒否は別。native暦で休場を証明できる未試行日だけを公開coverageから除き、raw不足は残す。旧v1へ遡及しない。

<a id="financial-native-l1"></a>
**財務。** 保存inputs.financialを[固定reader](src/baibai_jobs/integration/financial_native.py)へ渡す。旧inline/共有固定入力と旧SQL/Parquetの明示reader・cold再構築は保持する。旧mode/旧財務handoffを含む通常planは作り直し、旧Selectionを再署名しない。Operation再開とcold計算を混同しない。

<a id="master-native-l1"></a>
<a id="calendar-native-l1"></a>
**master/calendar。** TradingView/外部評価はcalendarを先に固定し、休場なら直前営業日のbasis masterから銘柄集合を固定する。unknown/partialを休場へ変えず、空/failed/held masterを旧値で補わない。保存packetの元master/calendarはhead削除/訂正後も保持参照から再生し、新しいheadへ再固定しない。

masterの明示日付は過去L2不要、月末backfillだけ既存bars gridを使う。未来日/空gridを拒否する。calendarの明示取得は他source/L2/release不要で、必要範囲を指定する。空/partialを成功へ変えない。

```bash
uv run baibai-jobs maintain-market backfill-master --asof <DATE> --asof <DATE>
uv run baibai-jobs maintain-market backfill-master --month-end-from <DATE> --month-end-to <DATE>
uv run baibai-jobs rebuild-calendar-native --saved-input <SAVED_INPUT_JSON> --output <NEW_OUTPUT>
```

<a id="margin-alerts-native-l1"></a>
<a id="short-sale-reports-native-l1"></a>
**日々公表銘柄/空売り報告。** 通常取得は8暦日の公表確認と保持範囲の不足を扱う。固定selection/sealを公開lineageまで渡し、正常空の全履歴0行と観測行を要求する公開policyを分ける。架空行で公開条件を通さない。

<a id="margin-interest-native-l1"></a>
**週次/全銘柄日次信用残。** native暦を先に保持する。初回53日暦だけで週次230日/日次2026-09-25以降を充足したとせず、unknown_calendar_days/unknown_incomplete_requestsを休場にしない。週次は2026-09-18残高まで、日次は2026-09-25からを扱い、9月19〜24日の系列を作らない。

週次の空充足には実確認JST日より前の後続2営業日、日次には後続1営業日の暦を使い、日次空は不足のままにする。最終週は公表日後に再確認し、未充足を残す。有限backfillへ今日までの全暦を要求しない。

margin-alerts / short-sale-reports / margin-interestのL2 cacheは、numeric projection revision・採用原本・object hash・SQL parityが一致するcomplete投影だけを再利用し、marker欠落/旧revisionやpartialは同じfixed L1から再投影する。markerはParquet footerと派生inputに置き、raw L1・Arrow schema・旧readerの保存形式は維持する。

```bash
uv run baibai-jobs jquants-margin-interest-native acquire \
  --directory <NEW_WORK> --asof <DATE> --start <DATE> --end <DATE>
uv run baibai-jobs rebuild-margin-interest-native --work-directory <NEW_READ_WORK> \
  --calendar-head <NEW_WORK>/calendar-fixed.json --start <DATE> --end <DATE> --output <NEW_OUTPUT>
```

plan/calendar-fixedと実受領時計を保つ。saved-inputは固定margin/calendarの再計算であり、歴史prompt/過去PITの完全復元ではない。旧mixed stageを除外するのは移送済receiptのUUID・query/page順・実時計・全body bytesが一致するときだけ。未移送/不足/改変は停止し、原本・旧release/stageを保持する。

<a id="edinet-list-native-l1"></a>
## EDINETの独立L1

一覧→ZIP/scope→L2を分け、通常source/手動refresh/TOBはnativeへ原本を先に確定する。共通統合・モデル・publisherはplan/applied.jsonの同じ固定一覧/archive pairを使い、欠落を現在headや旧stageで埋めない。旧固定release readerと明示cold再構築は残す。

head欠落を空initializeで埋めない。原本のない隔離環境だけで新規取得を始める。以下は一覧とarchiveを同じ隔離bucketに作る例で、一覧CLIだけは環境変数でbucketを指定する。

```bash
R2_STORES_BUCKET=<ISOLATED_BUCKET> uv run baibai-jobs edinet-list-native initialize --directory <NEW_LIST_INIT_WORK>
R2_STORES_BUCKET=<ISOLATED_BUCKET> uv run baibai-jobs edinet-list-native acquire \
  --start <DATE> --asof <DATE> --directory <NEW_LIST_WORK>
uv run baibai-jobs edinet-archives-native initialize --bucket <ISOLATED_BUCKET> --directory <NEW_ARCHIVE_INIT_WORK>
uv run baibai-jobs edinet-archives-native acquire --bucket <ISOLATED_BUCKET> --directory <NEW_METRICS_WORK> \
  --group edinet-metrics --asof <DATE> --lookback-days 540
uv run baibai-jobs edinet-archives-native acquire --bucket <ISOLATED_BUCKET> --directory <NEW_FACTS_WORK> \
  --group edinet-facts --asof <DATE> --initialize-facts
uv run baibai-jobs edinet-archives-native freeze --bucket <ISOLATED_BUCKET> --directory <READ_DIR>
```

<a id="一覧公開失敗の再開"></a>
### 公開失敗の再開

一覧/ZIPとも保存attemptは同workのcommit-workで公開/readbackする。pendingがあれば新HTTPを止める。workflowがprivate failure stageを残した場合は元stage IDで回復する。

```bash
uv run baibai-jobs edinet-list-native commit-stage --stage-id <ID> --directory <NEW_WORK>
uv run baibai-jobs edinet-archives-native commit-stage --bucket <BUCKET> --directory <NEW_WORK> --stage-id <ID>
```

同UUID・実UTC・初回bytes/stored_on/scopeを保持し、CASは最新headとのunionで明示再試行する。部分workで失ったheadを再作成せず、旧stageは保全して調査する。killやlocal/private両保存失敗まで回復を保証しない。手動factsの--initialize/--ticker・任意lookbackは維持し、annual refreshへholder batchを追加しない。

<a id="zipscopeの通常取得と固定consumer"></a>
### 固定consumerと旧copy

[SQL adapter](src/baibai_jobs/integration/edinet_archive_native.py)のrebuild_fixed_edinet_inputは保存pairから全ZIP/stored_on/scopeを再構成するため、plannerには使わない。必要CSVだけの比較は次を使う。候補の完成/partialと延期/parse失敗を分け、生成を通常公開への採用としない。

```bash
uv run baibai-jobs rebuild-edinet-metrics --saved-input <INPUT.json> --output <NEW.sqlite> --asof <DATE> --lookback-days <N>
```

TOBは`maintain-market build-tender-offer-exits --saved-input <INPUT.json>`で保存pairと必要type=5だけを使う。別の一覧/SQL ZIP/最新headで補わず、原本欠損/破損/decode失敗なら既存退出値を保つ。--asofは提出日/上場廃止日のcutoffで受領時点PITではない。引数なし旧SQL readerと明示refreshは別経路として残す。

publisherは旧SQL原本を固定pairのkey/hash/size/stored_on/scopeと照合した後だけdisposable raw表を空にする。head外の原本/scopeやnativeより早いSQL受領日は消さず停止する。原本clearのため全ZIPを再GETしない。原受領cacheが残る旧releaseのhydrateは照合用--bucketが必要で、初回full hydrate/BLOB hash/必要なcompactは残る。新releaseでは通常のraw SQL copy/Parquet再exportをしない。単独publisherで保存pairを使うときは--saved-edinet-inputを明示する。

旧一覧に匿名取消/編集対象のidentityがなければ、L1だけで旧ticker/採用順を復元できない。旧baselineによる安全側隔離、良好metricの出所/revision/時計、facts成功0行/status/reasonsを保つ。原受領cacheなしの単独hydrate/汎用readerで同じ確認をする場合は、保存pairの一覧を既存ownerで明示materializeする。暗黙R2補完や現在時計で欠落を埋めず、cold出力を旧抽出metadata/warm履歴/PITの完全再現としない。原本・旧canonical/release/stageと移送証拠は保持する。

<a id="cutovers"></a>
## 有限移行と旧受領の回復

完了済み環境へ再適用しない。旧active/queued writerの停止/drain、原本・旧release/stage/planの保全、固定候補の公開/readback、consumer/input切替、次の通常実行を確認する。codeの既定値やfixtureだけで移行完了としない。完了したJ-Quants移送入口は撤去済みだが、原本/移行証拠は保持する。

<a id="market-schema-484950の限定cutover"></a>
### market 48/49→50

daily/open/通常pushは移行しない。全writerを止め、旧codeでfresh machine bundle/ETagと旧releaseをhydrateしたcopyを保つ。旧article cutoverの部分公開は元code/receiptで先に完了する。50対応codeへ替え、同じlease・fresh copy・対象日で進め、plan/apply失敗時は停止する。固定EDINET pairを含むapplied.jsonをモデルへ渡し、後段のseal/inputsも同じintegration directoryを使う。

```bash
uv run baibai-jobs market-cutover prepare-copy \
  --source <PRESERVED_HYDRATED_SCHEMA48_OR_49_COPY> --output stores/market/market.sqlite \
  --receipt <PRIVATE_CUTOVER_RECEIPT_JSON> --kind canonical
uv run python -m baibai_jobs.integration.sources plan --directory <PRIVATE_CUTOVER_INTEGRATION_DIRECTORY>
uv run python -m baibai_jobs.integration.sources apply --asof <YYYY-MM-DD> --directory <PRIVATE_CUTOVER_INTEGRATION_DIRECTORY>
uv run baibai-jobs model-integration --asof <YYYY-MM-DD> \
  --integration-report <PRIVATE_CUTOVER_INTEGRATION_DIRECTORY>/applied.json \
  --lease-handle "$PUBLICATION_LEASE_HANDLE" --notice-output <PRIVATE_NOTICE_JSON>
jobs/scripts/r2_transfer.sh push-market-cutover <PRIVATE_CUTOVER_RECEIPT_JSON>
```

outputは不存在。変換は[owner](src/baibai_jobs/maintenance/market_cutover.py)を使い、元table/bytes/時計を別copyに保つ。scopeのsinceを推測せず、未初期化/ticker限定/旧stageからglobalを有効にしない。専用pushは旧ETag・lease・新origin/モデル・候補digestを照合し、空DB再取得で置換しない。取得ETagをpush結果で手書きしない。

残存stageはprepare-copy --kind stageで別pathへ変換し、result.jsonと共に`integration.source_handoff replay-copy --source <OLD_OWNER> --database <HYDRATED_SCHEMA50_COPY> --directory <CONVERTED_STAGE_DIRECTORY>`で限定再生する。元stageへ再uploadせず、後着新旧stage/pendingも照合する。

| 到達点 | 再開 |
| --- | --- |
| pointer前 | 旧公開を保ち、元ETag/originで未公開処理だけ再実行。 |
| pointer後/store前 | 新release/origin/モデルを照合し、元ETag一致中に専用pushで完了。旧codeへ戻さない。 |
| machine部分PUT/応答不明 | 同receipt/成果の`jobs/scripts/r2_transfer.sh resume-market-cutover <RECEIPT>`を使い、現ETagと実downloadのdigestを照合して未到達keyだけCAS。第三の値なら停止。 |
| machine成功/seal失敗 | 公開release/原本を読み、同じapplied.jsonからseal。再GETしない。 |
| seal成功/input途中 | fresh正本と同leaseから対象inputを再発行して取得を再開。 |

```bash
uv run python -m baibai_jobs.integration.sources inputs --directory <NEW_INPUT_WORK> \
  --group <GROUP> --identity <NEW_EXECUTION_ID> --asof <YYYY-MM-DD> \
  --lease-handle "$PUBLICATION_LEASE_HANDLE"
```

経済groupは独立L1 ownerへ戻す。新datasetも非対応readerは拒否するため、新公開後は50/catalogを保つ前向き修正とする。通常push/専用cutoverを混ぜず、machine/seal/inputを別確認する。

### 経済単独公開と失敗再開

経済正本はindicators.sqlite、machine bundleはmarket/runs。旧3key切替receiptを通常2key公開へ読み替えず、旧bytes/stage/backupを保つ。

fresh正本＋固定handoffの受入→一世代backup→条件付きPUT/全bytes読戻し→seal→15group inputの順。seal/input直前にも同ETag/leaseを確認し、読戻し経済入力からモデルを作る。L2失敗でL1を再PUT/rollbackしない。

- 公開/readback失敗: 後段を止め、実bytesを照合する。未確認ではsealせず空DB/旧値へfallbackしない。
- PUT後/seal前: fresh runnerで未sealの同じ不変stageを再適用する。同sourceの後続watermarkを先に進めない。
- seal後/input途中: 次cycleのfresh copy・新attemptで全groupを再発行する。旧local出力を上書きしない。
- A→同値A→Bの一括公開後の中断: 本文省略の中間stageがneeds_explicit_recoveryになり得る。元stage/hash・中断runのreadback・現正本/次inputを照合し、元時計/markerを変更しない。最新B/inputから前進できるが、新取得成功を旧stage採用の証明にしない。

FRED確認state自体の欠落だけ[通常recheck-source](#経済原本)を使う。全sourceへ同じ確認方式を広げず、source別object化/FRED HTTP bytes保存は[#1820](https://github.com/koumatsumoto/baibai-loop/issues/1820)の残作業とする。

## 保存済み旧runを読む

旧ER/FV・四Approach/較正は再生成・再配布せず、exact runと[保存reader](../docs/reference/estimate-calibration.md)を使う。現推計で埋めず、旧Selection/L3/Review/台帳を保持する。

```bash
uv run baibai-engine historical-evaluation run-show --run-revision-id <RUN_REVISION_ID> --runs-db <PRIVATE_RUNS_COPY>
uv run baibai-engine historical-evaluation candidates show --run-revision-id <RUN_REVISION_ID> --runs-db <PRIVATE_RUNS_COPY>
```

<a id="financials-identity-cutover"></a>
<a id="company-financialsの保存idと公開時入力の読取"></a>
## 保存IDと古いworkspace

通常open/dailyは旧schemaを変換しない。撤去済converter/publisherの代わりに、固定Git履歴・私有backup/planを保つ。元公開hashのcanonicalを現在名へ再署名せず、Operationの不変移行証拠をexact Proposal/Review/scopeと照合して読む。

```bash
uv run baibai-engine operation --db <APP29> selection-evidence <OPERATION_ID> \
  --selection-sha256 <PUBLISHED_SELECTION_SHA256> --proposal-id <EXACT_PROPOSAL_ID>
uv run python -m tools.financials_identity_workspace --db <APP29> \
  --operation-id <OPERATION_ID> --proposal-id <EXACT_PROPOSAL_ID> \
  --source <ORIGINAL_WORKSPACE> --archive <NEW_ARCHIVE> --output <NEW_WORKSPACE>
```

helperは現DB読取とlocal保全/生成だけ。旧manifest/domain-inputs/draftをまとめてarchiveし、新directoryへcompleted投影だけを復元する。旧出典indexは元canonical/domain-inputsと一組で解決し、新manifestへ旧draftをコピー/force転用しない。

<a id="保持とgc"></a>
## ローカルmirrorの整理

GCはlocal lake mirrorだけ。grace後の到達不能prefixをplanし、apply時の再plan hash一致で削除する。native L1/共有固定input/旧較正成果は別寿命で、R2は削除しない。

```bash
uv run baibai-jobs snapshots inventory --root stores
uv run baibai-jobs snapshots gc --mirror stores
uv run baibai-jobs snapshots gc --mirror stores --apply --plan-hash <HASH>
```

問題releaseは正しいstoreから前向き発行し、immutable Bucket Lockとmutable pointer/stagingを混同しない。

## Storeをseedする

初回だけ4keyすべて不存在を確認してseed.shを使う。既存一件でも通常更新/復旧へ戻る。1〜3keyの部分seedは供用前・writerなし・exact key確認済みの場合だけ初回復旧とし、無条件上書きしない。次のapplication更新はpull-appで世代ごと取得する。

## 定期maintenance

TSE資本政策/JPX廃止は翌月15日以降に既存refresh/buildと必要な公開を行う。全履歴監査を定例化しない。依頼された注文結果だけmeasure_limit_outcomesで測り、価格差を逸失利益や指値規律の因果効果としない。

通知webhookのrotationは新規/再生成→DISCORD_WEBHOOK_URL更新→通常通知確認。旧webhookはDiscord側で消すまで有効。実credential変更は別の依頼範囲に従い、過去Screeningを再実行しない。

<a id="web-serving"></a>
## 保有・運用Webの表示更新

[Web契約](../web/backend/src/baibai_web/materialize.py)を使い、core保存後にapplicationと同local marketから生成する。表示失敗でsource/model sealを止めたり戻したりしない。廃止Taskの保存行/旧viewも通常初期化/公開で消さない。

mainのcloud-materializeは検証済applicationと固定releaseを使う。market失敗は赤いrunに残し、未検証値を読ませず時価等を未評価にしてledger fact/保存成績/提案を配信する。market復旧と表示公開を別確認する。

```bash
uv run python -m baibai_web.materialize --output-dir /tmp/baibai-serving
```

これはlocal生成だけ。公開はworkflow内の`jobs/scripts/r2_transfer.sh upload-portfolio-views /tmp/baibai-serving`を使い、全viewの全bytes読戻しを同lease内で確認する。成功main run確認後にUIの対象HEADをmainで配備し、認証API/画面を受入れる。PR CIを本番更新とせず、旧object/secretを削除しない。提案emptyと保存ID/基準日/発行時刻付き本文の表示受入は別。

<a id="web-authentication"></a>
VIEW_PASSWORDはowner Bearer、READ_ACCESS_TOKENは共有Bearerまたは?share=で、同じviewを読める独立credential。owner変更/UI再配備で共有tokenは失効しない。実Workerのsecret更新/削除/空値、旧拒否と新/owner読取の維持を別途認可された操作として確認する。秘密/共有URLを本文・shell引数・logへ残さず、R2 credentialと混ぜない。
