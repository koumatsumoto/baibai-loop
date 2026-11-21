# 運用手順

repository rootのBashで、依頼された対象だけを操作する。[architecture](../docs/architecture.md#store-authority)は正本と責務、[development](../docs/development.md)は環境、CLIのhelpはoptionを扱う。本書は開始・到達点・復旧を扱う。

| 目的 | 実行・確認する箇所 |
| --- | --- |
| 初めて実行する | [権限・秘密・対象storeの確認](#actions-write-boundary)。環境準備は[開発手順](../docs/development.md)。 |
| 日次の進行や失敗を見る | [日次の起動・到達点](#daily) → 最後に成功したphaseと同じrun/attemptで確認する。 |
| 取得・再抽出をやり直す | [経済](#economic-acquisition)／[記事](#article-acquisition)／[JPX PDF](#jpx-pdf)／[外部評価](#external-assessment-acquisition)／[TradingView](#tradingview-expectations)／[J-Quants](#jquants-native-operations)／[EDINET](#edinet-list-native-l1) |
| 公開済みの内容を読む | [固定release](#fixed-release-read)。release IDとmanifest SHAを固定する。 |
| 反映が一部だけ成功した | [market/runsの部分公開](#部分-push-からの復旧)／[seal・次input](#machine公開後の失敗再開)／[経済単独の再開](#経済単独公開と失敗再開)。日次を丸ごと再送しない。 |
| 提案・台帳を保存する | [application DBの更新](#application-db-を反映する)。[バックアップからの復元](#application-db-を復元する)とは異なる。 |
| Webだけを更新する | [表示の生成・原本との照合・配備](#web-serving)。表示失敗でcoreを巻き戻さない。 |

<a id="actions-write-boundary"></a>
## 実行前の確認

通常取得・公開は既存Actions、application更新は認可環境の作業copyと正規CLIを使う。環境接続/開発変更を本番更新・課金・権限拡大・broker操作・未報告取引入力の許可にしない。課金禁止ならrunner/storage/API/R2も確認する。code撤去で配備済みWorker/domain/credential/旧objectを削除しない。

baibai-storesは非公開正本。秘密・endpoint・原文・私有DB/原稿をGit・Issue・公開log/artifactへ出さない。writer secretは取得/公開step、DISCORD_WEBHOOK_URLは通知stepだけへ渡し、通常開発へwriter credentialを流用しない。認証/403/429を別経路や権限拡大で迂回しない。

ローカルで認証付きCLIを直接使う場合、以降の例は必要な変数がshellへexport済みであることを前提とする。`.env`は全CLIが自動読込するわけではなく、`setup-dev.sh`も読み込まない。[`.env.sample`](../.env.sample)から認可済みの値を用意する場合は、Bash構文で値の特殊文字を適切に引用し、repository rootで次を実行する。秘密を表示する`set -x`や環境変数のdumpはしない。

```bash
set -a
source .env
set +a
```

R2転送失敗の診断はstorage ownerが操作名・検証済みHTTP status・keyのSHA-256だけを記録する。status不明をservice codeや本文から推測しない。key hashは固定対象を手元で照合するためで、公開可能性を保証する匿名化ではない。例外causeには私有情報が残り得るためtracebackを公開せず、key・bucket・endpoint・応答本文/headersを追加でdumpしない。診断の追加は429解消や本番受入を意味しない。

shellの存在確認は明示404以外の失敗を欠落扱いせず停止し、対象の固定keyと`HeadObject failed`だけを示す。CLIの生stderrや不正ETagは再表示せず、stderrからHTTP statusを推測しない。この局所診断は他のAWS CLI出力を含むlog全体の公開安全性を保証しない。

- 原本受領・L2・公開/readback・seal・次input発行を分け、正常空/partial/未試行/失敗を残す。L2失敗で保存L1を戻さない。
- 固定plan/receipt/ID/hash・原bytes・実受領時計を保つ。結果不明は現objectを照合し、新UUID・日付付替え・marker手修正・古いcopyの盲再送で解消しない。
- schema/origin/CAS/必要なlease不一致は停止する。通常writerは現schemaだけを扱い、旧作業DB/stageを変換しない。旧release・原本・実受領stageと移送証拠を保ち、保存済み固定入力の読取契約を維持する。完了済み移行の作業copy・取得計画inputは、exact keyを事前に固定し、本人が対象範囲・不可逆性・失う再開能力の説明を受けて削除承認した範囲だけ保持・再開対象から外す。通常dailyへ移行/全期間再取得を代行させない。

<a id="daily"></a>
<a id="日次実行と結果確認"></a>
## 日次と到達点

修正をmainへ反映してから必要な範囲だけ実行する。実scheduleは[workflows](../.github/workflows/)、対象日は[schedule owner](src/baibai_jobs/schedule.py)を参照する。run IDを固定し、監視timeoutだけで重複dispatchしない。

```bash
gh workflow run cloud-daily-batch.yml --ref main -f 'asof=<YYYY-MM-DD>' -f preserve_existing_generations=true
gh run list --workflow cloud-daily-batch.yml --limit 5
gh run watch <RUN_ID> --exit-status --interval 60
```

手動でasofを省くと元run作成日のJST日付、定期は元run作成時点の直近cron枠の対象日。queue/rerunでは元の時計を使う。対象枠をまたぐ登録遅延の復旧は手動でasofを指定する。寄前・場中に完了済みの日次が必要なら前日を指定する（前営業日に戻して週末情報を落とさない）。手動日次は[日足の到着](#price-native-l1)を確認してから起動し、生成対象日と価格日を同一視しない。

共通統合は全providerを待たず、到着済み受領を固定する。経済L1の公開/readback/seal/input発行→marketのsource別投影/EDINET抽出→固定market releaseと経済selectionによるモデル/候補保存→machine反映/readback・sealの順で進む。source別の失敗/欠測を残して他sourceを進めるが、モデル/公開失敗は後段反映を止める。model-integrationは外部取得/Web生成を呼ばない。

[通常生成](src/baibai_jobs/integration/models.py)は候補外を含む全対象モデルを保存する。個社投影失敗では新Generation・Selection・lakeを発行しない。政策だけが失敗した場合は先に保存したGenerationが残る。

scheduleは、比較・再開の元になった既存の全Generation/Selectionを毎回保持する。手動dispatchで同じ保持を行う場合は`-f preserve_existing_generations=true`、localの`model-integration`では`--preserve-existing-generations`を指定する。手動の省略/falseはcache整理を行うため、履歴を保つ本番検証で省略しない。生成前にrunsの既存IDをread-onlyで固定し、読取不能は空扱いせず停止する。既に欠けた旧世代はこの設定では復元されず、単一世代の`.bak`も恒久的な履歴保管ではない。履歴容量の整理は対象と保全を別途確認し、無人のscheduleへ戻して代行させない。

EDINETの意味revisionを明示反映するときは、固定原本の隔離比較後に`edinet_metrics_acceptance`へ[受入owner](src/baibai_jobs/integration/edinet_metric_acceptance.py)のJSON条件を渡せる。明示asofと全世代保持が必須。通常の空欄は変更せず、指定時だけsource-checkと固定list/archive・revision・有限sampleの元原本/保存結果digestをmodel-integration前に照合する。deferred/partial、入力交代、異なる結果では新L2生成とcore公開へ進まない。先行する経済L1公開やplanの不変原本保持は既に起こり得るため、run全体の無書込や全公開の原子性を保証しない。受入条件を現在結果から後付けせず、旧条件のticker/docID/hashを期待値へ書き換えて通さない。新しいselection時計は原本再受領でないためlist/archive identityで比較する。公開後のexact readbackとL3受入は別途必要。

停止時は最後の未完phase、store/release・generation/selection・lease解放を確認する。公開受入の分類済み失敗は`release_acceptance`のreason codeと登録済みdataset名だけをnotice/logへ残し、例外本文・原本・識別子は出さない。未分類の例外は型のみとし、診断情報が増えてもcoverageや履歴保持の条件は緩めない。logのdoneや通知成功は反映の証明でなく、限定source成功を全体成功にしない。候補0を投資機会なしとせず、[不足理由](../docs/reference/screening-runtime.md#入力不足と品質)を確認する。既定読取は発行時計より新しいas_ofを優先する。過去計算はexact IDで読み、古い非空候補へfallbackしない。

経済原本の転送で429を受けたら、同じ枠の新しい要求を止める。Retry-Afterは次回判断の最早時刻で、自動再実行の指示ではない。

通知の明示受入・記事再投影/読戻しの失敗と、既に成功したLake/Store反映を分ける。後段の受入失敗は公開を巻き戻した意味ではなく、通知の成否だけで同じdailyを再実行しない。[部分反映の確認](#部分-push-からの復旧)を先に行う。

取得期限にはexport/stageの余白を残す。Retry-Afterを短縮せず、認証/403/解析失敗へretryを広げない。継続不能429は同quota groupのそのpassを止める。H.15はBoardの自動取得用固定パッケージを1回読み、必要な観測日だけ保存する。空・HTML・拒否は別経路や手動再実行で補わない。Boardの事前定義パッケージ終了予定（2027-03-31）までに後継の出所・単位・観測日・受領時刻を検証する。

hydrateで403/429が返ったら、先読みを別のGETに切り替えて拒否を回避しない。

[watchdog](../.github/workflows/cloud-batch-watchdog.yml)のMISSINGは対象日を固定して調べる。取得履歴は各workflowの最新100runまで。成功/進行中を確認できず履歴が過去日の確認窓へ届かなければ、履歴不足で停止しMISSING通知を送らない。古いrunの再試行・削除前の状態までは復元せず、過去の診断には対象日を含む保存履歴を使う。

dailyの `[SOURCE QUALITY]` はMISSINGでも成功でもない。通知に示す日付・attemptで本体、machine、seal、入力更新、表示、lease解放が成功し、失敗は個社TradingViewの固定受領の応答拒否だけ。元runの赤と既存の取得gapは残る。受領品質は日次全体をやり直しても直らないため対象attemptを確認する。提供元の障害原因、他sourceの完全性、その後の最新世代を保証せず、状態値導入前の赤はstep名だけで推定しない。追加照会はMISSING判定時の新しい候補から最大2件、各5秒まで。より新しい同日または日付不明の失敗・キャンセルや照会不能を、古いattemptの印で上書きしない。

dailyだけは、対象日が明確な初回attemptで同日の成功がなく、queuedの待機が210分を超えると既存通知先へDELAYEDを送る。210分は既存job上限を参照したqueue ageで、実行時間の制限・停止や原因の断定ではない。再実行attemptは今回のqueue開始を確定できないためin_flightのままにし、長時間in_progressと対象日不明の手動runには適用せず、自動cancel/再dispatchもしない。監視は既存scheduleの実行時点だけで、超過直後の通知を保証しない。

```bash
gh workflow run cloud-batch-watchdog.yml -f check_date=<YYYY-MM-DD>
```

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
## 共通公開とmachineの復旧

market/runs・経済は同じpublication lease内でfresh正本/取得ETagを照合し、read/merge/publishする。market/runsは元bundleの組を保ち、別pullで組み替えない。長い準備は先に済ませ、lake tableは変更前にhydrateする。applicationのpublish.sh・初回seedは対象外。独立native L1はsource固有CAS/並行制御を使い、共通leaseやL2成功を課さない。

公開後の市場転送は、作業DBを書き換えず、同じSQLite読取時点のrelease印・Lake所有表の件数・公開外の状態から小さな転送DBを作る。独立EDINET一覧の作業行は固定原本との照合を経るまで除かない。件数は同数の値変更を証明しないため、公開と転送の間にLake所有値を編集しない。未対応スキーマではSQLiteの通常backupに戻り、未知の表を捨てない。失敗した一時出力は再利用せず、正規の取得ETag・leaseで同じ手順を再開する。

push-machine/runs/market/indicatorsとupload-portfolio-viewsにはPUBLICATION_LEASE_HANDLEを渡す。公開途中でowner失効/交代を検出したら次writeを止め、[部分pushの復旧](#部分-push-からの復旧)へ進む。単独確認は`uv run python -m baibai_jobs.publication.lease verify --handle "$PUBLICATION_LEASE_HANDLE"`を使い、延長/再取得はしない。確認とwriteは非原子的である。

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

job上限は[日次workflow](../.github/workflows/cloud-daily-batch.yml)、新規leaseの期限は[lease owner](src/baibai_jobs/publication/lease.py)が正本。既存handleは元期限を保ち、hard cancel後もexpiryまでbusyが残り得る。

leaseの世代は削除・改変して古いwriterを入場させない。公開中のownerが先行した場合は停止し、現行の[lease実装](src/baibai_jobs/publication/lease.py)で確認する。

| 変更 | 反映経路 |
| --- | --- |
| lake table | publish-lake → push-market |
| market store-localだけ / runsだけ | push-market / 取得世代が合うpush-runs |
| 経済原本 | push-indicators。L2利用のために再pushしない。 |
| 元bundleからlocal daily全体 | sources apply → model-integration → push-machine → seal/inputs。同日applied.jsonを--integration-reportへ渡す。 |

```bash
with_publication_lease bash -c 'jobs/scripts/r2_transfer.sh publish-lake && jobs/scripts/r2_transfer.sh push-market'
with_publication_lease jobs/scripts/r2_transfer.sh push-indicators
```

model-integrationは生成に使ったreleaseも公開するため、後からpublish-lakeを挟まない。単独のL2修正は上表のpublish-lakeを使う。machineはmarket/runsへ非原子的にPUTし、全成功後にmachine-manifest.jsonを作る。前世代backupはkey.bakの一世代で次回置換される。

実送信の証拠が必要な切替では、`push-machine --evidence-dir <新しいprivate dir>` を使う。既存dir/再利用を拒否し、market脱水後・runs compact後の実PUT bodyと実manifest、pull由来If-Match、各PUTの応答ETagをcreate-onlyで保持する（dir0700/file0600）。未指定時の動作は変わらない。開始/backup/PUT/停止を記録し、保持失敗やack不明では後続PUTを止め、再送・自動復旧しない。停止recordや不完全な記録を成功へ読み替えず、先行ackと未確認keyを区別する。

このlocal完了は3件のPUT ackまでで、pair/readback受入ではない。実body SHAと別の正規読戻しを結び、新Generation/旧保持・manifest/pointerを別に確認する。保持dirはtemp cleanup対象外だが、hard kill/runner消失後の回復や別環境への配布は保証しない。原body/ETagを公開logへ出さず、切替担当がprivateな保持先の寿命を確保する。

### 部分 push からの復旧

logと現objectを先に確認する。lakeだけ進みmarket/runsの両ETagが旧receiptと一致する場合は、receipt削除/旧store復元をせず通常日次を前向きに実行する。消失したrunner-localモデルへ元ID/hashを付けて再生成しない。通常日次の再計算にprovider workflowの再実行は不要。

片方のkeyだけ進んだ場合は同じpush/dailyを盲再送せず、local成果/origin/取得世代が成立する上表の経路で完了する。経済pushはmachine部分反映を修復しない。

receiptの一致は現storeの組の確認で、新Generation/Selectionの到達を保証しない。market成功/runs失敗の後にpush-marketだけを行うと、receiptが整ってもrunsは旧世代のままである。未公開local成果を別copyで保ち、確認pullで唯一のcopyを上書きせず、意図した新Generation/Selectionと保持対象の旧全rawを公開後に照合する。

receipt再生成が必要な明示復旧だけ、bucket直下のmutable一件を確認して次を行う。

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

### 保持copyから欠落Generationを戻す

現在のrunsを古いcopyで丸ごと戻すと、後発Generation/Selectionを失う。検証済み・静止中のcurrentと旧sourceのSHAを固定し、必要なGeneration IDを明示して、新しい隔離copyへ欠落分だけ追記する。同IDのheader・revision・圧縮payload・Selectionが異なれば全体を停止する。再計算・再圧縮・時点更新はせず、current側の追加政策や他tableも残す。

```bash
uv run baibai-jobs restore-model-history \
  --source <VERIFIED_OLD_RUNS_COPY> --source-sha256 <OLD_SHA256> \
  --current <VERIFIED_CURRENT_RUNS_COPY> --current-sha256 <CURRENT_SHA256> \
  --output <NEW_PRIVATE_RUNS_COPY> \
  --generation-id <EXACT_GENERATION_ID>
```

複数IDは`--generation-id`を繰り返す。既存outputやWAL/journal付き入力は使わない。この成功はlocal copyの準備だけで、L1/共有入力/L3の復元や本番反映を意味しない。元copyは保持し、通常readerで対象と現行の保存値/IDを照合する。本番へ反映する場合は別途writer不在・単一lease・freshなmarket/runsの組とCASを確認し、currentが進んでいればそのfresh copyへ作り直す。取得世代の合う通常`push-runs`と公開読戻しを使い、古いmachine全体への上書きやCAS失敗後の盲再送をしない。

### machine公開後の失敗再開

- machine成功/seal失敗: 公開releaseと原本を読み、同じapplied.jsonからsealする。再GETしない。
- seal成功/input途中: 成功した日次や取得全体を再実行せず、失敗したgroupだけを `inputs --group` で再発行する。`--group` は `plan`・`apply`・`seal`・`check` の範囲を絞る指定ではない。元runの基準日と市場release/manifest・market/runsの対を現在の正本と照合し、TradingViewは対応する検証済み市場copyを使う。元の対を確認できない、または世代が進んだときは同一世代からの復旧と扱わない。

JPXのsealはapplyで固定したbundle原本と、決算予定の全体/規制の対象日を公開releaseから読戻す。未公開applyや異なる本文/投影では成功markerを発行しない。旧applied.jsonに読戻し計画がなければ、元stageを保ってfresh正本へ同じplanを再applyし、通常の生成・公開・読戻しへ進む。現在の値から旧計画を補わない。

```bash
uv run python -m baibai_jobs.integration.sources inputs --directory <NEW_INPUT_WORK> \
  --group <GROUP> --identity <NEW_EXECUTION_ID> --asof <YYYY-MM-DD> \
  --lease-handle "$PUBLICATION_LEASE_HANDLE"
```

経済groupは独立L1 ownerへ戻す。新datasetを非対応readerへ渡さず、現schema/catalogを保つ前向き修正とする。

### 経済単独公開と失敗再開

fresh正本＋固定handoffの受入→一世代backup→条件付きPUT/全bytes読戻し→seal→経済の全group inputの順。byte不変時は同ETag/leaseを確認し、初回検証済みの正本copyを再利用する。seal/input直前にも同ETag/leaseを確認し、読戻し経済入力からモデルを作る。同一日・同じ正本世代で取得グループの計画本文/形式が変わらなければ、既発行のstageをそのまま読み戻す。途中で止まった組や異なる日・世代は通常どおり発行する。L2失敗でL1を再PUT/rollbackしない。

- 公開/readback失敗: 後段を止め、実bytesを照合する。未確認ではsealせず空DB/旧値へfallbackしない。
- PUT後/seal前: fresh runnerで未sealの同じ不変stageを再適用する。同sourceの後続watermarkを先に進めない。
- seal後/input途中: 次cycleのfresh copy・新attemptで全groupを照合し、未完・差分だけを発行する。旧local出力を上書きしない。
- A→同値A→Bの一括公開後の中断: 本文省略の中間stageがneeds_explicit_recoveryになり得る。元stage/hash・中断runのreadback・現正本/次inputを照合し、元時計/markerを変更しない。最新B/inputから前進できるが、新取得成功を旧stage採用の証明にしない。

FRED確認state自体の欠落は[通常recheck-source](#経済原本)へ。他sourceへ同じ確認方式を広げない。

## application DB を反映する

空の作業領域へpullし、DBとstores/.r2-generations/baibai.sqlite.etagを同じ組で使う。更新済みの組があれば再pullしない。

```bash
jobs/scripts/r2_transfer.sh pull-app
# 正規CLIでこのDBを更新した後（通常どおり新backupを作り、旧backupは削除しない）
R2_APP_BACKUP_PRUNE=skip jobs/scripts/publish.sh
```

publish.shは引数なし。台帳preflight→取得世代への条件付きbackup/PUT→次回ETag記録→Web生成dispatchの順。時価/quote不足と取引保存障害を分け、DBだけの世代交換・未知世代の現HEAD補完はしない。App backupは元世代を読戻して実行JST日＋内容SHAのkeyへcreate-onlyで保存し、読戻し照合後に本体PUTへ進む。同日でも既存backupを置換しない。同じ日付・SHAの既存keyだけを本文一致時に再利用する。backup段階の競合・拒否・応答不明では本体PUTに進まない。以前の日付だけのkeyはこの作成・照合で使用せず、そのまま残す。通常pruneは直近14個のbackup日付に属する全世代を保持し、永久削除をしない反映ではR2_APP_BACKUP_PRUNE=skipを指定する。保存量は日数だけでなく反映回数に増え、元Appとbackupの読戻し通信・一時容量も必要になる。正規の単一PUT由来ETagを持たない旧Appはこの経路で更新せず別途確認する。

本人が追加backupを不要と明示し、更新後Appが旧提案・評価・台帳等の全旧行を保持することを旧新の正規copyで照合した追加専用反映では、`R2_APP_BACKUP_CREATE=skip` で新しいbackup生成だけを省略できる。既定はretain。取得ETag照合と最終If-Matchは省略されず、未知世代・競合は停止する。旧backupの削除を止めるフラグは `R2_APP_BACKUP_PRUNE=skip`。両方のskipは、旧backupを削除せず、新backup作成も上記条件で意図して省くときだけ使う。既存情報を書き換える一般更新へこの省略判断を流用しない。

保存・反映・readbackを分け、対象Proposal/評価のexact IDを確認する。競合時は未公開差分を退避し、最新正本へ正規serviceで再適用する。非0・無応答では元App世代と対象IDを読戻し、不明なまま再送しない。PUT済みで表示runが未作成と確認できたときだけ、cloud-materializeを起動する。通常は現行schemaのDBだけを扱い、自動移行しない。古いDBに未公開の事実がある場合は、元copyを保持し、正本へ丸ごと戻さない。

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

共有[source input](src/baibai_jobs/acquisition/source_inputs.py)・記事planningを使う経路だけ、取得有効化前に共通統合から初回発行する。inputは再生成可能な計画材料で正本ではない。元bindingで再開し、欠落を全DB取得/空seedで迂回しない。形式切替では対応readerを先行させ、非対応のactive/queued writerをdrainする。原本stage・現役inputは保持し、廃止した計画を残存pointerから再開しない。

破損handoff/decodeは当該sourceを報告し他を進め、正本破損/R2全体障害は停止する。遅着は次統合へ渡し、needs_explicit_recoveryは元ID/hash/時計/markerを保ってexact回復する。正常nullを全期間取得で埋めない。

<a id="economic-acquisition"></a>
### 経済原本

[workflow](../.github/workflows/cloud-economic-sources.yml)は休場日もgroup別に取得し、部分受領をprivate stageへ保存する。限定再実行は所属groupとsource IDを指定する。

取得summaryのfailedはHTTP試行件数ではない。group停止後の未試行sourceはblocked_by_source_idで最初の失敗を参照し、自身のHTTP statusを持たない。error_typesは例外の型だけで、詳細本文はprivate受領に残す。旧summaryの`browser_download_diagnostic`は許可済みHTTP/MIME・navigation分類だけを保持し、欠落時は補完しない。旧summaryは区別できないため、4系列の失敗を4回の拒否と数えたり、経過時間だけでtimeoutと断定しない。型やHTTP statusが得られても、拒否原因・別経路の許諾や回復成功の証明ではない。

```bash
uv run baibai-jobs acquire-economic groups
gh workflow run cloud-economic-sources.yml --ref main -f group=fred_csv -f source_id=fred-payems
uv run baibai-jobs acquire-economic refresh <SOURCE_ID> --all-history --end <YYYY-MM-DD>
```

取得窓/空seed/確認はsource別[policy](../engine/src/baibai_engine/data_collection/acquisition/policy.py)に従う。空seedを欠落の自動初期化にせず、一律の短期窓へ狭めない。FRED全CSV・BOJ四半期APIは期間を狭めても通信が減らず、返却された窓外期間も保持する。未返却を撤回/全履歴確認とせず、history_recovery_requiredは別の有限期間/all-historyで回復する。同じ無期間応答を回復用に重ねて処理しない。

FRED成功時のL1は従来のnative行とし、同値は原本を複写せず実確認時計を進め、本文変更・公表時計と分ける。完全応答の期間集合/hashを保ち、headerだけ/短い行を成功としない。GET後のdecode/CSV解析失敗でnative行を作れない場合だけ、requestsのresponse entity bodyを既存economic_source_documentsへ保全し、runは失敗のままにする。HTTP header/圧縮wireや成功応答全文のarchiveではなく、旧原文/時計も復元しない。確認欠落/遅着で通常pushを拒否されたときだけ、fresh正本を基点に実GETする。

```bash
with_publication_lease jobs/scripts/r2_transfer.sh push-indicators --recheck-source <FRED_SOURCE_ID>
```

失敗時は両copyを保つ。このrecheckを他sourceへ広げず、現値/完全応答/実確認/保持原本から過去の採用順を作らない。他のrecord sourceの確認は取得range内だけの証拠で、source全体の置換や明示nullを証明しない。旧rawの実時計を使い、捨てた時計をrun終了時刻で補わない。

同値確認欠落は[local診断](../tools/diagnostics/inspect_economic_confirmation_gaps.py)で調べる。閉じたcurrent-schema canonicalと、統合済みを含む明示stageのhandoff.json/receipts.sqlite/取得済みmarkerを私有copyへ固定する。認可済み環境で有限取得し、canonical SHA/ETag・manifest/receipt・markerの元identity/時計/statusを照合する。無制限prefix走査・旧schema移行・失敗copy上書きをせず、marker未供給だけで未統合と決めない。

```bash
uv run python -m tools.diagnostics.inspect_economic_confirmation_gaps \
  --canonical <LOCAL_CANONICAL> --canonical-sha256 <FIXED_SHA256> \
  --stage <STAGE_ID> <MANIFEST_SHA256> <LOCAL_PACKET_DIRECTORY> \
  --source-id nikkei-pbr --start <OBSERVED_DATE> --end <OBSERVED_DATE> \
  --received-since <RECEIVED_AT_WITH_ZONE> --received-until <CUTOFF_WITH_ZONE>
```

stage指定は必要分を繰り返す。終了0は固定copyの診断完了だけで、全stage網羅・remote headの現在性・保存済みL2への影響や回復を証明しない。canonicalのfetched_atは確認時計がなければ既知の下限にすぎない。欠落を空応答へ変えず、同時計衝突は要確認とする。

回復時だけcanonical確認時計以降〜固定cutoffのstage欠落/後続受領を人が調べる。原本が揃い一意なら再GETせず既存accept_rechecks/merge/verifyで別copyを準備する。不足時の限定再取得は現在の実確認であり、過去時計の復元や衝突解消の証明ではない。実公開は対応code、lease/CAS・backup・全bytes読戻し・input再発行を別に確認し、元stage/marker・保存L2/L3を変えない。

current-onlyは実取得日を使い、他handoffの日付は動かさない。松井の過去asofを現在pageで埋めず、optionsの古い訂正は明示期間で取得する。

<a id="pmi-public-articles"></a>
PMIはS&P公式PDFの日本/米国製造業headline・サービスBusiness Activity Index。ISM/Output/Composite、旧値/snippet、拒否後fallbackで代用しない。原文/抽出・速報/確報・対象月・公表/配信時計を分け、完全PITとしない。

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

`minkabu_news`と見出し専用の`rakuten_news`は通常・手動workflowの取得と新規planningの既定対象から外し、[必要時L3調査](../docs/reference/stock-assessment.md#recent-news-research)で読む。外部見解の見出しを含む将来の自動発見・取得時点原本は残らず、Web検索は速報性・全件再発見・過去bytesの復元を保証しない。未取得pendingは自動再開せず、保存済みreceipt/stage/世代とparser・遅着回復は保持する。同hostの数値External Assessmentは別経路であり変更しない。

定期枠は通常モデルに入力を供給するTSRデータインサイト（`tsr_data`）と日銀新着（`boj_news`）の2ソースを取得し、手動`public`と同じqueueを使う。同じ2ソースだけを手動で確認する場合は Actions の `group=scheduled` を選ぶ。定期と同じキュー・時刻・重複抑止・予算が適用され、取得を強制しない。取得はフィード単位で、モデルが採用する記事への絞り込みは取得後に行う。その他の現役記事source（NHK、BBC、FRB・ECB・財務省の更新、TSR倒産速報、トウシル）は既存の手動`public`・`world`・`rakuten`または`all`で取得できる。通常日次は手動用の計画も準備するが、記事本文を取得しない。既存の原本・stage統合・履歴の照合は維持する。定期取得を止めた期間の未受領原本を後から同時点に補完はしない。

初回planningは取得前にintegration.sources inputsで発行し、原本stage保存後だけCAS更新する。統合は不変stage/markerから発見し、sealでplanningを変えない。L2停止中も原本/worksetを保つ。planning/root不明やCAS未確定の孤立stageは新規取得で上書きせず、下記のexact受領から回復する。

continuation_onlyのpartialは正常継続で全取得完了ではない。HTTP/parse/deadline/CAS/保存失敗を分け、正常sourceを進める。HTTP失敗はDEGRADEDに残し、受領後parser失敗と分ける。401/403を受けたsourceはそのpassを終了し、失敗受領と未完worksetを保存して次の通常slotへ引き継ぐ。未完URLはvalidator cacheの128件上限で切り詰めない。既存のplanning 128KiB上限を超えた場合は全workset入りのimmutable handoffを残して明示失敗し、summary更新や次slotへの自動引継ぎの成功とは扱わない。200同値も別受領、304は具体的200 bodyへの参照。新GETで失った過去原本を復元したとしない。

トウシルは定性調査の補助であり、現行の正常な一覧から消えた持越し本文が404なら、次回の自動pendingから外す。失敗受領と当該回の欠測は残し、記事の永久消失や取得成功とは扱わない。再掲載時は通常の発見対象へ戻す。現行掲載中の404、一覧の取得不完了、403/429/5xxはこの扱いに含めない。

[記事summary](src/baibai_jobs/acquisition/articles.py)のbodiesは本文receive呼出しの回数で、HTTP失敗も含み、新規本文の保存数ではない。今回の通信はrequests_used/bytes_used、本文の保持はstage内の受領・body参照で確認し、304の既存本文参照と新規body保存を分ける。private受領の成功はstage保存の成否であり、全本文取得や正本への統合成功を意味しない。

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

意味抽出revisionだけの回復は、選択本文のobservation ID・article version・要求revision・cutoffを先に照合する。本文が保持されても旧revisionのartifactは現行入力に使われず、新しい見出し/概要の抽出だけでは埋まらない。native revisionも変わった場合、stage replayは別article versionを作るため回復を保証しない。

この場合は隔離copyで[既存抽出owner](../engine/src/baibai_engine/machine_analysis/observations/trends/extraction.py)の`extract_observations`へ明示した有限observation IDだけを渡す。native再投影・再GET・日次全件移行はせず、現在UTCの生成時計で新artifactを追記する。同内容/同revisionは再利用し、原受領/本文・旧artifactを保持する。対象IDと手順は私有の一回作業に固定し、source全体を暗黙選択しない。抽出後のartifact IDを[公開読戻しowner](src/baibai_jobs/publication/article_verification.py)へ渡して原本closureも照合する。原本/投影不変と新旧cutoffの選択を隔離copyで確認後、明示承認されたfresh canonical/同一leaseの[共通公開](#machine-publication)へ進む。旧Generationは書き換えず、新しいGenerationの採用を別に読戻す。並行publisherへ割り込まず、CLI/fixture成功を本番反映としない。

次の単一dailyへ同梱するときだけ、既定空の`article_semantic_recovery`へ[有限契約](src/baibai_jobs/integration/article_semantic_recovery.py)を渡す。`as_of`と1〜16件の`observation_id`/`observation_sha256`/`article_version_id`/現行`extractor_revision`/比較済み`result_sha256`を固定し、明示asofと`preserve_existing_generations=true`を必要とする。result hashは既存抽出結果のstatus/method/revision/payloadを固定し、新artifactの生成時計を含めない。未対応結果も固定したまま許し、0件の意味を無出来事へ変えない。取得済みrawを現在のnative parserへ再投影せず、通常hydrate/apply後に対象だけ再抽出し、同じモデル生成/公開の後に原本closureを読戻す。失敗時は新しい全件移行・再GET・盲目的再dispatchをしない。現runの入力や過去のGenerationへ遡及適用しない。

BOJ/MOFのoffline再発見はregister-discoveryへstage/hashと--receipt-idを渡す。HTTPなしのplanning CAS書込で、同要求はno-op。元受領/validator/slot/未処理workを保ち、登録後に通常取得を続ける。

### JPX PDF

JPX決算予定/規制は `uv run python -m baibai_jobs.integration.source_handoff acquire --source <SOURCE> --asof <DATE> --directory <PRIVATE_DIRECTORY>` で原本だけ取得する。SOURCEは `jpx-earnings` または `jpx-regulations`。部分受領も同じモジュールの `stage --source <SOURCE> --stage-id <UNIQUE_ID> --directory <PRIVATE_DIRECTORY>` で保存し、L2は通常の統合で作る。

失敗seal済みのJPX stageを限定回復する場合は、原本とmarkerをread-onlyで照合してからexact source・stage ID・manifest hashを指定する。複数件はoptionを繰り返す。

```bash
uv run python -m baibai_jobs.integration.sources plan --directory <PRIVATE_RECOVERY_DIRECTORY> \
  --recover-jpx-stage <SOURCE> <EXACT_STAGE_ID> <EXACT_MANIFEST_SHA256>
```

他のpending stageや外部評価readyは混ぜず、元ID/hash/受領時計を保つ。fresh canonicalと同じpublication leaseでapply→model-integration→push-machine→readback→sealへ進める。成功は同stageのrecovered-<hash>.jsonに記録し、元の失敗markerを削除/上書きしない。通常plan/resumeは失敗stageを自動で再選択しない。

明示PDFは既知URLで取得済みの原本だけを`financial-pdf-source --receipts <JSON> --directory <NEW_PRIVATE_DIRECTORY>`へ渡し、daily自動取得へ加えない。HTTPなしで実UTC/bytes/hash等を検査するため、mtime/公表日で時計を作らない。生成後はintegration.source_handoff stageへ--source jpx-financial-pdfs（完了転換は--collection capital_actions/--source jpx-capital-actions）を渡す。不対応も原本を保ち、株数/自己株を推定しない。選択/訂正basisは[観測reference](../docs/reference/model-observations.md#reported-comparisons)へ。

EDINETの取得・facts・再抽出は[独立L1の手順](#edinet-list-native-l1)へ。

<a id="external-assessment-acquisition"></a>
### 外部評価

数値の通常・手動取得はMonexと株マップ、TradingViewは[別入口](#tradingview-expectations)で継続する。楽天・みんかぶの数値取得は[拒否の記録と本人の廃止判断](https://github.com/koumatsumoto/baibai-loop/issues/1820#issuecomment-6856119813)に基づき除去し、新しいExternal Assessmentへ保存済み旧値を採用しない。拒否原因や許諾不存在を確定した変更ではない。旧plan/receipt・原本decoder・保存Generation/Selectionの読取は保持し、未試行pairの取得再開や履歴削除はしない。旧HEADで既に起動したrunの停止はmergeだけでは保証されない。ニュースsource・トウシル記事は別範囲である。

通常TLS/proxyで有限銘柄を直列取得し、試行別private captureをcurrent-schemaの隔離marketへimportする。parse失敗でも原本は保ち、L2を部分追記しない。再投入0件を確認する。

```bash
uv run baibai-jobs external-assessment collect --source monex --tickers 7203 4436 --output <NEW_PRIVATE_CAPTURE>
uv run baibai-jobs external-assessment import --input <PRIVATE_CAPTURE> --db <LOCAL_MARKET_COPY>
gh workflow run cloud-external-assessment.yml --ref main -f source=monex -f batch_id=<LABEL> -f master_asof=<YYYY-MM-DD>
```

日次は固定master/calendar/parserの元bindingで再開する。依存不足を全market/TradingView起動で迂回しない。同sourceを重ねずqueue後cycleを確認し、plan再作成/hash手修正をせず期限内未試行pairだけを進める。保存失敗pairの再取得は別labelとする。HTTP 401/403はそのsourceの当該passを止め、次の通常slotでは未試行pairだけを進める。

[外部評価summary](src/baibai_jobs/acquisition/external_assessments/cohorts.py)のstate_counts/reason_countsと取得ログのfailed_http_status_countsは保存状態の集計。failed_http_status_countsは非200で失敗したpairだけの内訳で、正常欠測やHTTP 200の解析失敗を含まない。provider_requestsは今回の呼出し、http_requests/response_content_bytesは再開前を含む[cohortの記録](src/baibai_jobs/acquisition/external_assessments/daily.py)であり、本文の新規保存数とは分けて読む。

新原本はretained。旧rawは切替当時3日保持の短期handoffで、現在の保持設定や削除可否を名前の日付だけで判断しない。元のplanを指定し、残存する同bytes/元IDを保全するときだけ次を使う。

```bash
uv run python -m baibai_jobs.acquisition.external_assessments.raw --preserve-run-id <EXACT_RUN_ID>
```

lease外でacquire→inspect→ready --cumulative、共通統合で固定集合を公開/readbackしてackする。通常の探索は、同じplanにreadyが一つだけでその本文SHAへ正式ackがあるとき、採用済みstageを再監査しない。別のreadyがあれば固定集合の包含を確認し、後着小集合で後退させない。確定分で明示終了するときだけworkflowの-f operation=stage_confirmed_partialを使う。401/403/429/未完を自動closeしない。pointer後terminal失敗は現状態を読み、未sealならfresh hydrateから再適用する。hard kill前の未保存試行・期限切れ原本の復元は保証しない。history_digestsは保存後baselineで、更新前の全履歴保全を証明しない。

保存原本の再解析が必要なときは、現行の隔離copyへ対象attemptだけを `external-assessment rebuild-raw` で再解析する。元の受領日、対象L2の差分、直前/直後cutoffの値を確かめて[共通公開](#machine-publication)へ進む。HTTPを再試行せず、未公開の旧copyは本番へ戻さない。

<a id="tradingview-expectations"></a>
### TradingView

本人確認済み利用範囲・毎日取得の指示と既存tradingview-runtime認証を使う。一般FAQだけで停止/除外せず、新credential/課金/再配布へ広げない。認証失敗を充足にしない。

[日次](../.github/workflows/cloud-tradingview-snapshot.yml)は未取得原応答だけを再開する。originals_savedは再GETせずrebuildし、L2だけのcopyでは不足原本を受領して既存L2を保つ。後受領を旧L2の生成元にせず、過去cycleを現在応答で埋めない。cycleは09:00 JST切替、営業日15:30/休日09:00から翌09:00未満。休日の前営業日basisは[master/calendar](#master-native-l1)に従う。

通常枠を時刻別に登録し、共通統合前に少数callの追加枠を置く。cronは実行時刻/回数の保証でなく、遅着も実時計のcycle・観測窓と同じ共有lock・cooldownに従う。追加枠も完了済みなら照会せず、上限到達は部分進捗であって充足ではない。日次全体を再起動しない。

要求銘柄の対応は取れたが必要fieldの欠けた原応答だけは、同cycleの次枠で未充足銘柄を小さいbatchにまとめ直す。拒否の少ない銘柄から未試行等の通常batchと交互に進め、同じ先頭だけで枠を使い切らない。元応答の一部採用や欠落fieldのnull補完はしない。全体に同じ欠落が続けば連続不備で停止し、そのcycleの未取得を残す。原因・受領形の確認が先で、分割はprovider仕様の修正ではない。

必要銘柄/fieldの揃った原応答はL2検証失敗でも保持し後続batchを進める。不足はpartial/診断へ残し、無効応答連続・HTTP/認証・時刻/予算・保存失敗の停止境界を維持する。

列欠落の広がりは、固定済みの同じ試行のhandoffから一組の `result.json` と、WALを反映済みの `receipts.sqlite` のローカルコピーを使って調べる（追加取得・再解析保存なし）。`result.json` の `snapshot_date`・`receipt_ids` を使い、別の試行のIDや稼働中DBを混ぜない。
`uv run python -m baibai_jobs.integration.tradingview_receipts --sqlite <LOCAL_RECEIPTS_SQLITE> --asof <CYCLE_DATE> --diagnose-receipt <ID>` の末尾を受領ごとに繰り返す（最大100件）。field別の分母は銘柄の重複を含む辞書行で、明示missing・非辞書行・応答照合失敗は別。キーの存在は値の妥当性や評価の採用を意味しない。

新しい観測は[日次workflow](../.github/workflows/cloud-tradingview-snapshot.yml)と同じ順で準備する。空の作業DBへ `source_handoff input --source tradingview` で入力を読み、`master-dependency hydrate` で観測日の暦とmasterを固定し、`source_handoff resume` で同じcycleの受領済み分を戻す。master不足時の追加取得は同workflowのpreflightとcooldownに従う。過去観測の回復では元の固定入力を使い、SQL内の依存表や現在のheadで代用しない。

receiveはHTTPなしでSDK応答全体/失敗を保存する。同IDは全内容一致の再送だけ、別呼出しは別ID。exportには上で固定したsource-input directoryを渡し、共通統合へ渡すのは受領packetだけ。取得copy全体をpushしない。

applyのmaster投影は同じ固定入力の読取で対象日の取得状態も確認し、未採用・failed・heldならmasterを書き換えない。packet自身が参照する元masterの銘柄集合は別に照合する。

共通統合のsealは、applyで固定した対象日の原応答と最終L2を公開releaseから照合してからstageを消費する。追加前から保たれた最初の行とその原本も照合し、受領拒否/投影失敗は原本公開と明示復旧を分ける。旧bundleの整合だけではsealしない。apply未完で照合対象を固定できなかったstageは、raw保持前後の依存/受領context不一致やwatermark以前の遅着も含め、markerを書かずpendingに残して復旧する。readback計画のない旧applied.jsonはfresh hydrateから再applyし、原本stageや受領時計を作り直さない。

```bash
uv run baibai-jobs tradingview-source receive --sqlite <LOCAL_MARKET_COPY> --capture <PRIVATE_NATIVE_CAPTURE_JSON>
uv run python -m baibai_jobs.integration.source_handoff export --source tradingview \
  --asof <JST_RECEPTION_DAY> --since <BEFORE_RECEPTION_UTC> --database <LOCAL_MARKET_COPY> \
  --input-directory <FIXED_SOURCE_INPUT_DIR> --directory <NEW_PRIVATE_HANDOFF>
uv run baibai-jobs tradingview-source rebuild --sqlite <LOCAL_MARKET_COPY> --asof <YYYY-MM-DD> --receipt-id <RECEIPT_ID>
uv run baibai-engine external-assessment show --ticker <TICKER> --cutoff <UTC_TIMESTAMP> --db <LOCAL_MARKET_COPY>
```

rebuildは選択receipt集合で一日L2を置換するため、部分追加だけで既存日を置換しない。元master/calendar・最初の正常行/receiptを保ち、過去判断/除外履歴へ遡及しない。

### 判断直前の多市場補助観測

[market-evidence](src/baibai_jobs/acquisition/market_context.py)はquote/5分足のprivate原受領で、日次充足・価格master・正式提案終値とは別。prepare→stageで保存し、別sessionは返却ID/hashでdownload/readして同じAssessment入力を使う。未保存captureに耐久参照を付けない。

日次market-dailyはjob上限15分、capture上限5分（TERM後30秒でKILL）。capture timeoutは失敗のまま、完了分を既存partial handoffへ渡してcooldownを保存する。setupを含むjob期限や外部cancelでは後段も停止し得る。

固定economic入力の参照/時計/欠測と、評価時刻/基準JST日末の両cutoffを守る。最新失敗を旧成功で埋めず、前日基準へ当朝夜間受領を加えるなら別入力にする。休場/遅延/限月/basisを揃え、受領時計を取引時計、bar tを最終約定、update_mode不明をリアルタイム、末尾足を確定値にしない。価格で判断を変えるなら新入力/再Reviewへ進める。

<a id="jquants-native-operations"></a>
## J-Quants native L1

取得はnative L1へ保存し、L2生成/公開と分ける。入口は`jquants-<source>-native`のinitialize/plan/acquire/resume/commit-work。初期化は原本のない新しい空namespaceだけで一度行い、head消失/移行失敗を空initializeで迂回しない。新plan/acquireは新directory、結果不明はresume、保存attemptは同directoryのcommit-workを使う。各source optionはhelpを参照する。

| source | 明示取得範囲 |
| --- | --- |
| financial / price / master / calendar | --start / --end。master/calendarの--forceは明示訂正だけ。 |
| margin-alerts / short-sale-reports | --asof、任意--start。休場も公表確認。 |
| margin-interest | --asof、有限backfillは--start / --end。固定calendarが必要。 |

`rebuild-<source>-native`は--work-directory / --start / --end / --outputから新しいL2とinput.jsonを作る（calendarは下の専用引数）。保存入力からの再計算はGenerationのinputsにあるdescriptor付きexact InputSnapshotを--saved-inputへ渡す。head/summaryに付け替えず、新outputを使う。欠損/改変/partialのL2や不適格な[投影cache](src/baibai_jobs/integration/native_projection_cache.py)は同じ固定L1から再生成し、原本を再GETしない。J-Quantsの受領はnative L1で完結し、SQLを詰めたhandoffを作成・再生しない。

<a id="price-native-l1"></a>
**日足。** 正常空L1と価格0行の公開拒否は別。native暦で休場を証明できる未試行日だけを公開coverageから除き、raw不足は残す。各日を固定L1から再投影する。同じ作業SQLiteは他のwriterと共有しない。

日足の冷再構成は `input.json` のある新出力だけを完了として扱う。中断でマーカーのない出力が残った場合は別の新規出力先を使う。保存入力に固定暦がないときは未試行日を休場扱いしない。

`--saved-input` を渡さない日足CLIは、開始時に現在のL1 headを一度固定する。`--work-directory` に置いた旧 `head.json` や過去のattemptは入力にならない。元の失敗attemptに結び付く保存InputSnapshotがなければ、元attemptの再現としてはそこで止める。現行headを診断する場合は別の新しい出力先を使い、同じ日付でも過去の選択に付け替えない。

独立原本の内容でなく旧releaseの日足投影そのものを調べるときは、対象releaseを指定した通常の `hydrate-market` を使う。

<a id="financial-native-l1"></a>
**財務。** [`rebuild-financial-native --saved-input`](src/baibai_jobs/integration/financial_native.py)は保存済みの固定native入力から財務の派生表を再構築する。公開済みの投影を確認するときは[そのreleaseを指定](#fixed-release-read)して読む。再構築はOperationの再開ではなく、保存済みSelectionを置き換えない。

定期財務取得は平日18:13 JSTの`financial-edinet-facts`枠へ置き、価格等の16:43枠と分ける。Standard等の[公式更新目安](https://jpx-jquants.com/ja/spec/data-update)は18:00速報・翌00:30確報で、18:13の完全到着や22:43統合への間に合いを保証しない。確報は次回overlapで取り込むため、17時頃の臨時統合では前回取得後の確報が未反映の場合がある。早期更新が必要なら既存受領と進行中runを確認し、不足する財務だけ次で明示取得する。

```bash
gh workflow run cloud-market-sources.yml --ref main -f source=jquants-financial -f asof=<YYYY-MM-DD>
gh run list --workflow cloud-market-sources.yml --branch main --event workflow_dispatch --limit 5
gh run watch <SOURCE_RUN_ID> --exit-status --interval 60
```

作成された対象run IDを固定し、成功後も財務head/受領の対象日・不足を確認してから必要な[日次統合](#daily)へ進む。取得完了とL2利用可能時刻を分け、統合・公開/readbackの時間も見込む。dailyだけの再実行ではproviderの最新値を取得せず、後着入力へ途中で差し替えない。scheduleのrun作成自体も遅れ得るため、枠の追加を定刻到着の保証にしない。明示実行も既存queueを守り、即時完了は保証しない。

財務summaryの[API枠](https://jpx-jquants.com/ja/spec/rate-limits)はプラン共通60要求/分で通常plan枠とは別。現行のJ-Quants共通queueはこれより保守的な実装上の直列化であり、時刻変更では外さない。`financial-edinet-facts`は枠名で、土日は従来どおりEDINET factsだけを取得する。

<a id="master-native-l1"></a>
<a id="calendar-native-l1"></a>
**master/calendar。** TradingView/外部評価はcalendarを先に固定し、休場なら直前営業日のbasis masterから銘柄集合を固定する。unknown/partialを休場へ変えず、空/failed/held masterを旧値で補わない。保存packetの元master/calendarはhead削除/訂正後も保持参照から再生し、新しいheadへ再固定しない。

masterは明示した日付を取得し、過去L2を使わない。`maintain-market backfill-master`は未来日を拒否する。calendarの明示取得は他source/L2/release不要で、必要範囲を指定する。空/partialを成功へ変えない。

```bash
uv run baibai-jobs maintain-market backfill-master --asof <DATE> --asof <DATE>
uv run baibai-jobs rebuild-calendar-native --saved-input <SAVED_INPUT_JSON> --output <NEW_OUTPUT>
```

backfill-masterは成功時だけ一時workを整理する。失敗/未充足時に表示されるworkの日付directoryを保持し、受領済みattemptの公開結果を解決するには`jquants-master-native commit-work --directory <WORK>/<DATE>`を使う。元受領の時計/UUIDを変えず、公開結果が不明なまま新しいbackfillで再GETしない。machine自体が失われた場合のlocal work保持までは保証しない。

<a id="margin-alerts-native-l1"></a>
<a id="short-sale-reports-native-l1"></a>
**日々公表銘柄/空売り報告。** 取得窓と保持範囲の不足はplanで確認する。固定selection/sealを公開lineageまで渡し、正常空の全履歴0行と観測行を要求する公開policyを分ける。架空行で公開条件を通さない。

<a id="margin-interest-native-l1"></a>
**週次/全銘柄日次信用残。** 取得・公表確認に必要なnative暦を先に保持する。planの暦不足を確認し、unknown_calendar_days/unknown_incomplete_requestsを休場にしない。週次は2026-09-18残高まで、日次は2026-09-25からを扱い、9月19〜24日の系列を作らない。

週次の空充足には実確認JST日より前の後続2営業日、日次には後続1営業日の暦を使い、日次空は不足のままにする。最終週は公表日後に再確認し、未充足を残す。有限backfillへ今日までの全暦を要求しない。

```bash
uv run baibai-jobs jquants-margin-interest-native acquire \
  --directory <NEW_WORK> --asof <DATE> --start <DATE> --end <DATE>
uv run baibai-jobs rebuild-margin-interest-native --work-directory <NEW_READ_WORK> \
  --calendar-head <NEW_WORK>/calendar-fixed.json --start <DATE> --end <DATE> --output <NEW_OUTPUT>
```

plan/calendar-fixedと実受領時計を保つ。saved-inputは固定margin/calendarの再計算であり、歴史prompt/過去PITの完全復元ではない。旧mixed stageを除外するのは移送済receiptのUUID・query/page順・実時計・全body bytesが一致するときだけ。未移送/不足/改変は停止し、原本・旧release/stageを保持する。

<a id="edinet-list-native-l1"></a>
## EDINETの独立L1

一覧→ZIP/scope→L2を分け、通常source/手動refresh/TOBはnativeへ原本を先に確定する。通常のnative archive取得はmarket DBを読まず、固定一覧原本とsource scopeからZIP候補を決める。明示の `maintain-market refresh-edinet-metrics` は固定一覧をmarket SQLiteへ投影し、その一覧行からCSV ZIP候補を選ぶ。どちらも抽出済みmetricsに依存しない。共通統合・モデル・publisherはplan/applied.jsonの同じ固定一覧/archive pairを使い、欠落を現在headや旧stageで埋めない。旧固定release readerと明示cold再構築は残す。

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

手動source=allは一覧/stage成功後にarchiveへ進む。一覧失敗時はall内archiveを止め、有効一覧を確認できる場合だけ同asofのarchiveを再実行する。metricsは取得窓の一覧に未受領日があっても、固定一覧で判明した許可CSVを先に保持する。未受領日はlist_unavailable_days、失敗受領日はlist_failed_daysへ残し、取得全体は失敗のままにする。明示refresh-edinet-metricsも既知CSVを先に保存する。要求窓内に未受領日または最新一覧の失敗日が残る場合は、先着の有効一覧があっても既存metrics/coverageを変えず失敗終了する。窓外の失敗は停止理由にしない。L2の完全window条件を緩めず、一覧回復後は保存済みZIPを再利用する。保持docID/typeを再GETせず、L2成否/revisionを取得条件にしない。holder/訂正の未保存分は既存batchを使う。CSV/XBRLともHTTP 401/403と、HTTP 200本文のStatusCode 401で当該passを止め、保存済みの部分原本を保ち、次の通常取得は未保存docID/typeだけを進める。

一覧/archive未到着はdeferred_inputsで適格保存値を使い、到着後に再抽出する。解析失敗/原本破損を延期で隠さず、不正dayは正常投影を保ちながらsource-check失敗を残す。延期だけの継続とpartial/日別成功を分ける。誤orphanは現年度CSVを確保して次統合で回復し、全期間GETを代行しない。

<a id="edinet-research-facts"></a>
### EDINET factsの取得と読取

固定releaseから[facts reader](../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_facts/)を使い、文書/ticker/対象JST日とusable_documentを確認する。日末cutoffはintraday PITでなく、未選択は書類不存在を意味しない。segment/debtはdocID・期・連結・通貨を揃え、抽出行だけで全債務/資金余命を確定しない。

facts scopeはL1取得範囲。L2品質でglobal開始日/docID集合を狭めず、failed/未提供/交代のfamilyも出所と共に保つ。初回annual取得は明示initialize成功後だけ有効化し、holderは独立。途中失敗は同じrefreshで再開し、ticker限定で他社/globalを置換しない。offline extractのinitializeは将来GETを有効にしない。

```bash
uv run baibai-jobs maintain-market refresh-edinet-facts --asof <YYYY-MM-DD> --initialize --sqlite-path <STAGED_MARKET_SQLITE>
```

手動refreshのSQLには年次scopeの保存済みXBRLを残すので、再抽出だけなら同じSQLでextract-edinet-factsを使う。大量保有報告のXBRLや財務指標CSVを新規に複写しない。全原本が必要なら[明示cold再構築](#zipscopeの通常取得と固定consumer)を使う。stored_onは初回保存UTC日で公表時刻でない。未対応0行/failedと鮮度を分ける。

<a id="edinet-metrics-revision"></a>
metric revisionは抽出/選択の意味変更だけで更新し、配置/import/retryやfacts revisionと分ける。隔離copyで保存CSVを再抽出・比較後、同じleaseでfresh pull/hydrate→backup→extract-edinet-metrics→publish-lake→push-market→固定readback。期待外差/失敗は止め、変更後hydrateや旧copyの本番復元はしない。

### 公開失敗の再開

一覧/ZIPとも保存attemptは同workのcommit-workで公開/readbackする。pendingがあれば新HTTPを止める。workflowがprivate failure stageを残した場合は元stage IDで回復する。

```bash
uv run baibai-jobs edinet-list-native commit-stage --stage-id <ID> --directory <NEW_WORK>
uv run baibai-jobs edinet-archives-native commit-stage --bucket <BUCKET> --directory <NEW_WORK> --stage-id <ID>
```

同UUID・実UTC・初回bytes/stored_on/scopeを保持し、CASは最新headとのunionで明示再試行する。部分workで失ったheadを再作成せず、旧stageは保全して調査する。killやlocal/private両保存失敗まで回復を保証しない。

<a id="zipscopeの通常取得と固定consumer"></a>
### 固定consumerと旧copy

[SQL adapter](src/baibai_jobs/integration/edinet_archive_native.py)のrebuild_fixed_edinet_inputは保存pairから全ZIP/stored_on/scopeを再構成するため、plannerには使わない。必要CSVだけの比較は次を使う。候補の完成/partialと延期/parse失敗を分け、生成を通常公開への採用としない。

```bash
uv run baibai-jobs rebuild-edinet-metrics --saved-input <INPUT.json> --output <NEW.sqlite> --asof <DATE> --lookback-days <N>
```

TOBは`maintain-market build-tender-offer-exits --saved-input <INPUT.json>`で保存pairと必要type=5だけを使う。別の一覧/SQL ZIP/最新headで補わず、原本欠損/破損/decode失敗なら既存退出値を保つ。--asofは提出日/上場廃止日のcutoffで受領時点PITではない。引数なし旧SQL readerと明示refreshは別経路として残す。

publisherは旧SQL原本を固定pairのkey/hash/size/stored_on/scopeと照合した後だけdisposable raw表を空にする。head外の原本/scopeやnativeより早いSQL受領日は消さず停止する。原本clearのため全ZIPを再GETしない。通常日次は一覧の固定原受領を一度読んで投影・失敗日を作り、作業SQLiteに原受領を複写しない。匿名の取下げ・書類情報修正で過去の発行体同定が必要なときは、同じ日次処理内の検証済み受領参照で補う。原受領cacheが残る旧releaseのhydrateは照合用--bucketが必要で、初回full hydrate/BLOB hash/必要なcompactは残る。新releaseでは通常のraw SQL copy/Parquet再exportをしない。移行後に旧ZIPが空なら、sealに十分な空きがありDBの空きページが全体の1/8未満のときは、他の市場表の細かな空きを詰めるためだけにDB全体をVACUUMしない。その後の処理でseal直前の容量が不足した場合だけ、見送った圧縮を実行してから通常の容量検査に進む。旧releaseからの移行・再開では引き続きcompactする。単独publisherで保存pairを使うときは--saved-edinet-inputを明示する。

旧一覧に匿名取消/編集対象のidentityがなければ、L1だけで旧ticker/採用順を復元できない。旧baselineによる安全側隔離、良好metricの出所/revision/時計、facts成功0行/status/reasonsを保つ。原受領cacheなしの単独hydrate/汎用readerで同じ確認をする場合は、保存pairの一覧を既存ownerで明示materializeする。暗黙R2補完や現在時計で欠落を埋めず、cold出力を旧抽出metadata/warm履歴/PITの完全再現としない。原本・旧canonical/release/stageと移送証拠は保持する。

<a id="保持とgc"></a>
## ローカルmirrorの整理

GCはlocal lake mirrorだけ。grace後の到達不能prefixをplanし、apply時の再plan hash一致で削除する。native L1/共有固定inputは別寿命で、R2は削除しない。

rootはlocal currentだけで、保存run/Researchが参照する旧releaseを自動保護しない。必要な旧releaseを保持するmirrorではapplyしない。またlockはGC同士だけを直列化し、公開・hydrate等は共有しない。同じmirrorの読取・生成・公開を止めた状態でplan/applyし、再planの後も静止を保つ。R2のcurrentやpublication leaseをlocal mirrorの排他保証に読み替えない。

```bash
uv run baibai-jobs snapshots inventory --root stores
uv run baibai-jobs snapshots gc --mirror stores
uv run baibai-jobs snapshots gc --mirror stores --apply --plan-hash <HASH>
```

問題releaseは正しいstoreから前向き発行し、immutable Bucket Lockとmutable pointer/stagingを混同しない。

## Storeをseedする

初回だけ4keyすべて不存在を確認して`jobs/scripts/r2_transfer.sh seed-all`を使う。既存一件でも通常更新/復旧へ戻る。1〜3keyの部分seedは供用前・writerなし・exact key確認済みの場合だけ初回復旧とし、無条件上書きしない。次のapplication更新はpull-appで世代ごと取得する。

## 定期maintenance

TSE資本政策/JPX廃止は翌月15日以降に既存refresh/buildと必要な公開を行う。全履歴監査を定例化しない。依頼された注文結果だけmeasure_limit_outcomesで測り、価格差を逸失利益や指値規律の因果効果としない。

通知webhookのrotationは新規/再生成→DISCORD_WEBHOOK_URL更新→通常通知確認。旧webhookはDiscord側で消すまで有効。実credential変更は別の依頼範囲に従い、過去Screeningを再実行しない。

<a id="web-serving"></a>
## 保有・運用Webの表示更新

[Web契約](../web/backend/src/baibai_web/materialize.py)を使い、core保存後にapplicationと同local marketから生成する。表示失敗でsource/model sealを止めたり戻したりしない。

mainのcloud-materializeは検証済applicationと固定releaseを使う。単独表示更新は同じmarket originの価格・calendar・masterだけをhydrateし、無関係な原本cacheを取得・消去しない。別releaseへの部分更新はせず、originが合わなければmarketを未検証として扱う。通常dailyの実行用復元とは別である。market失敗は赤いrunに残し、未検証値を読ませず時価等を未評価にしてledger fact/保存成績/提案を配信する。market復旧と表示公開を別確認する。

**日次の表示だけを復旧する前提**は、対象runの同一attemptで `Generate models and candidates, then publish the same fixed release` と `Upload the core machine stores` の両方が成功したこと。片方が不明なら、この復旧は使わず、対象のModelGenerationとそれに属するScreeningSelectionを正本のrunsからIDで読戻して状態を調べる。読戻しは元runのreceiptの代わりにはならない。`push-market`でも既存のrunsと新しいmarketの対を再確定できるため、現在のmachine receiptだけでは、その日次の生成・選定が公開された証明にならない。

同じrun・attemptのモデル公開結果から`release_id`と`release_manifest_sha256`、成功したcore uploadの `machine bundle receipt written:` 行から`machine_receipt_sha256`を取り、元のrun ID-attemptと合わせて4つを渡す。正規の日次は同じattemptのモデル公開成功通知と手元のmarket originを転送前に照合し、その出所をreceiptに同封する。旧receiptや単独のpush-market／push-machineには日次の出所がないので、hashを知っていても復旧できない。古いrunなどで証拠が欠ける場合はここで止める。現在のR2や別attemptから値を補わない。空の手動実行・application更新からの呼出しは現在の表示を生成する従来の縮退動作で、過去runの復旧の代用にはしない。

```bash
gh workflow run cloud-materialize.yml --ref main \
  -f market_release_id="${RELEASE_ID:?公開release IDを指定}" \
  -f market_manifest_sha256="${MANIFEST_SHA256:?同じ公開結果のmanifest SHAを指定}" \
  -f original_daily_run="${ORIGINAL_DAILY_RUN:?元のrun ID-attemptを指定（例12345-1）}" \
  -f machine_receipt_sha256="${MACHINE_RECEIPT_SHA256:?同じrunのcore upload成功行のSHAを指定}"
```

指定時は新しいlease内で現行pointer、元runと同じmachine receiptの実bytesと埋め込まれた日次run/attempt・release、market/runs両ETagを確認し、同じoriginのmarketで価格・calendar・masterのみを復元する。表示公開直前にもreceipt・世代・ETag対・手元のoriginを照合する。同じlakeでもreceiptのbytesが変わった場合（時刻だけが違う再発行も含む）、4項目の一部だけの指定、market取得・照合失敗では縮退表示も公開しない。再取得・モデル生成・正本の再公開はせず、現在のapplicationと実行時計で4表示を作り直すため、以前の表示bytesの再現ではない。4件の転送は順次で一括commitではない。転送途中の失敗時は公開済みの範囲を確認してから同じ4項目で表示だけを再実行する。

```bash
uv run python -m baibai_web.materialize --output-dir /tmp/baibai-serving
```

これはlocal生成だけ。公開はworkflow内の`jobs/scripts/r2_transfer.sh upload-portfolio-views /tmp/baibai-serving`を使い、全viewの全bytes読戻しを同lease内で確認する。

固定Appの正式な最新提案と、実際に配信された2本文を一度だけ照合する場合は、表示JSONを生成した版で以下を使い、稼働CLI・Worker/UIの配備版とは別に控える。非公開の空の作業領域に正規の `pull-app` で固定Appと取得ETagを対で保ち、既に認証した本番の `/api/proposal/latest` と `/api/proposal/history` の成功JSONを非公開ファイルとして採取する。採取を挟むApp正本と2つの表示objectのETagをそれぞれ前後で照合し、Appは取得印の世代に、Workerのbucketとrouteは該当する表示objectに対応することを確認する。WorkerはETagを返さないのでレスポンスから推測しない。採取中の世代が安定していても、以前と同じ表示内容ならそのApp世代から生成した証明にはならない。同じApp取得ETagにひも付く実materializer runと、そのrunが生成・公開して読戻した表示objectの証拠を別に確認する。得られなければ「本文一致、生成元世代は未確認」と区別する。変化・認証・出所の不明を成功にしない。ローカル生成物は認証APIの代用にならず、本文・閲覧秘密・銘柄・ETagをログやIssueへ載せない。

```bash
uv run python -m tools.diagnostics.check_served_proposal \
  --db "$APP_COPY" --latest-json "$AUTH_LATEST_JSON" --history-json "$AUTH_HISTORY_JSON"
```

履歴先頭でなく訂正を反映した正式latestを固定し、現行投影のその一件の全項目、履歴の各行が表示形式に適合し全IDに欠落・重複がないこと、同じ生成時刻、固定Caseの対象・結果・Selectionを別々に照合する。他の履歴本文全件の同値性は検査しない。既知のIDで固定したいときは `--proposal-id` を加える。`--require-completed` は「この提案自体がCaseの完了参照」まで必要なときだけ使う。後日の訂正で正式latestと当時の完了参照が異なることがある。出力は各段階の合否だけで、終了0もApp世代の取得・配備版・実画面・新形式の実発行や売買の証明ではない。

必要な表示データを公開・読戻しした成功main runを確認後、[web workflow](../.github/workflows/web.yml)をmainで明示dispatchする。push/PRのweb runは検証だけで、Worker/UIを配備しない。

```bash
gh workflow run web.yml --ref main
gh run list --workflow web.yml --branch main --event workflow_dispatch --limit 5
gh run watch <WEB_RUN_ID> --exit-status --interval 60
```

対象SHAの`Deploy Worker and UI assets`が実行・成功したことを確認する。待機中にmainのWeb入力が変わると配備はskipされるため、run成功だけで配備済みとしない。認証APIは対話terminalで`bash web/edge/scripts/verify-deployment.sh`を実行し、閲覧パスワードはpromptへ入力する。`WORKER_BASE_URL`で確認先を変える場合はHTTPS originだけを指定し、userinfo・path（末尾slashを含む）・query・fragment・空白を含めない。画面のDashboard・提案emptyまたは保存ID/基準日/発行時刻付き本文の表示受入は別に確認する。旧object/secretは削除しない。

<a id="web-authentication"></a>
VIEW_PASSWORDをBearerとして使う。閲覧画面は空白なしの半角英数字・記号（印字可能ASCII）だけを受け付け、入力の前後空白を除くため、secretもこの範囲で用意する。実Workerのsecret更新は別途認可された操作として行い、秘密を本文・shell引数・logへ残さず、R2 credentialと混ぜない。
