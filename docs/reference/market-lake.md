---
title: "Market lake operations"
summary: "R2 の immutable object と manifest で market fact を publish し、固定 release から local projection を作る運用契約。"
doc_type: reference
status: active
---

# Market lake operations

authority、manifest、version 語彙は [`../architecture.md`](../architecture.md#market-lake-publication-contract)
を正本とする。この文書は publish と read の実操作を持つ。

対象 dataset は `jquants.daily_bars` と `jquants.short_sale_reports` の 2 つで、production
screening が読む他の table はまだ L1 化されていない。screening / web の cutover はこの 2 dataset
だけでは成立しないので、**不足 dataset を legacy store で暗黙に埋めない**。比較は
[Shadow parity](#shadow-parity) の統制された shadow 実行だけで行い、その report が release 由来の
table と legacy 由来の table を必ず両方列挙する。

## Build

初回 seed は全期間を export する。現行 provider / Premium backfill は coverage を SQLite に
commit し、lake export はその SQLite を `legacy_sqlite_import` として月 partition へ変換する。
provider 取得と Parquet writer の二重 canonical write は行わない。contract v1はfixed legacy
SQLite snapshotからauthorityを移すcompatibility boundaryであり、Rawだけにあるfield、decimal
precision、publication / effective / retrieved time、revision/cancellation semanticsを完全には表さない。
これらのmappingを確定しRaw→canonical semantic parityを満たした時点をv2 rebuild triggerとする。

`export-pilot`は開始時にSQLite backup APIでWALを含むsealed snapshotを1回作り、snapshot digest・
schema version・`quick_check`を確定してから、両datasetのexport、source-state、parityを同じsnapshot
から導出する。release作成時にも全partitionが両datasetでexactに1 snapshot generationへ閉じることを
検証する。snapshotはcontent-addressedな`local_build_input`であり、daily releaseのremote closureには
含めない。`sqlite_authority`期間のrestore checkpointは既存のcloud `market.sqlite`を正本とし、lake
authority cutover前に別retention classのinitial checkpointを一度検証する。日次buildごとにfull
SQLiteをR2へ再送しない。

```bash
uv run baibai-engine lake export-pilot \
  --sqlite stores/market/market.sqlite \
  --mirror <local-mirror>
```

中断した Premium CSV / API backfill は既存 `source_coverage` から再開する。直前 manifest を
渡すと SQLite facts + coverage の state hash を比較し、commit 済みの追加・訂正月だけを
export して未変更月を再利用する。transform fingerprintはschema/configに加えてwriter・dataset
contract sourceのdigestを含む。CLIは実装sourceが属するrepositoryを固定し、tracked worktreeがdirty、
git identityが取得不能、unknown zero commitの場合にbuildを開始しない。

```bash
uv run baibai-engine lake export-pilot \
  --sqlite stores/market/market.sqlite \
  --mirror <local-mirror> \
  --base-manifest <previous-daily-bars-manifest> \
  --base-manifest <previous-short-sale-manifest>
```

`coverage_status`は固定値ではない。daily barsは保存行のdate coverage、short sale reportsは
`source_coverage`の連続した`ok` windowをauthorityとしてsealed snapshotから判定する。pilot policyは
[Phase 0 baseline](../../reports/studies/2026-08-12-market-lake-baseline/report.md)のhistory startを必須
境界とし、観測rows・ticker populationの95%をregression floorにする。新鮮でも1日・1rowだけのstore、
leading history欠損、大幅なpopulation縮小はcurrent候補にならない。

Raw は取得直後の bytes を変換せず archive する。Premium CSV と長期 backfill response は
`preserve`、再取得可能な routine response は `buffer` を指定する。endpoint の query と
credential は metadata に保存しない。

```bash
uv run baibai-engine lake archive-raw \
  --source-file <provider-response> \
  --mirror <local-mirror> \
  --dataset jquants.daily_bars \
  --ingest-id <immutable-id> \
  --suffix json.gz \
  --retention preserve \
  --from <request-start> \
  --to <request-end>
```

検証済み dataset manifest を release に固定する。

```bash
uv run baibai-engine lake release create \
  --mirror <local-mirror> \
  --dataset-manifest <daily-bars-manifest> \
  --dataset-manifest <short-sale-manifest>
```

### local pipeline の実測

remote への転送が差分でも、local 側は毎 run sealed snapshot を作り、affected month を
判定し、全 history の SQLite ↔ Parquet parity を検証する。その時間は主張ではなく計測で持つ。

```bash
uv run python -m tools.diagnostics.benchmark_l1_export \
  --sqlite stores/market/market.sqlite --report <report.json>
```

production store（2,013,155,328 bytes、schema v23、11,554,322 rows = daily bars 10,141,309 +
short sale 1,413,013）を Linux/WSL2 の一時 directory で実測した結果は次のとおり。

| 局面 | wall time | 生成 object | 生成 bytes |
| --- | --- | --- | --- |
| full export（121 か月 × 2 dataset） | 367.6 秒 | 242 | 252,387,406 |
| 1 か月訂正の再 export | 262.2 秒 | 1 | 848,197 |

peak RSS は 1,013,817,344 bytes（967 MiB）。**1 か月の訂正で書き換わるのは 0.85 MB だが、
local 側は 262 秒かかる。** その大半は 2 GB の sealed snapshot 作成と、carry する 120 か月分を
含む full parity 検証である。これは correctness gate を測定前に弱めない選択の代価であり、
daily pipeline の予算はこの実測値を前提に置く。fast path と scheduled full audit の分離は、
この時間が daily の制約になった時点で検討する。

この計測は commit ではなく実装 digest（`4f0dc6be…`: writer / models / immutable / snapshot /
benchmark tool）へ結ぶ。それらに触れない変更では証跡は有効なままで、触れた変更は再計測になる。

<!-- AP-02: full=367.56988125501084 秒、incremental=262.16346760702436 秒、
peak RSS=1013817344 / 1048576 = 966.85546875 MiB、
source sha256=703e3fab403489726708fc83c07fe1975e9f0ddad5ba492834ad2f1ec33144ce、
implementation sha256=4f0dc6bee8dacafdc70c0958a0a5ed4d4ed43ebe3e996a35ab8d4479cdf62895、
producer commit=b87e61fba32448766fb2d9cd7d2114e2d1ad6a56。 -->

## R2 publish

publish は immutable object、dataset manifest、release manifest の順に `If-None-Match: *` で
転送する。各sourceをsealed copyへ固定してR2が検証する`Content-MD5`付きPUTを行い、logical
SHA-256、transport marker、size、content typeを同じimmutable PUTのmetadataへ固定する。

**このrunが書いたobjectだけをstreaming GETで読み戻す。** 既にstoreにあるobjectは、immutable PUTが
`Content-MD5`で束ねたidentity metadataで証明する。1 publicationにつき1 keyは1度だけ証明し、pointer
直前の再走査は行わない — overwriteを拒否するstoreでは、その間にkeyが差し替わることがないので、
2度目の全streamは同じ結論のためにhistory全体のbytesを動かすだけになる。remote bytesの差し替えを
探す全stream監査は`--verify-bytes`の別実行が持ち、publication hot pathとはSLOを分ける。
report は `uploaded_bytes` / `downloaded_bytes` / `head_requests` / `get_requests` を出すので、
「差分転送になっている」は主張ではなく観測になる。

SQLite `local_build_input`はdigest・schema・capture時刻をmanifestへ記録するがuploadしないため、
日次remote bytesはchanged Parquet/Raw/manifestへ比例する。最後に`lake/pointers/l1/current.json`を
ETag `If-Match`で切り替え、pointer bytesだけをGETで読み戻す。409/412のCAS conflictはretryせず
fail-closeし、current releaseを再解決する。subprocessのdeadlineはobject sizeから導く（base 120秒 +
実測を下回る4 MiB/秒での転送時間）ので、大きなobjectがtimeoutで曖昧な結果になることを避ける。

current を previous へ降格する前に、その release graph（release manifest → dataset manifest →
object → Raw closure）をremoteで解決して検証する。検証できないcurrentはpublishを止める。previous
への切り戻しは`--rollback-l1`が`If-Match`で行い、対象closureを検証してからpointerを交換する。
pointer が previous を名乗るなら、それは復元できるという主張であり、必要になった日に初めて確かめる
ものではない。previous の closure が欠けている間は publish が止まる。error は欠けた key を名指す
ので、その release を local mirror から `--release-manifest` で publish し直して closure を戻してから
新しい release を publish する。

production authority化ではmutable pointer prefixを除くimmutable prefixへ
[R2 Bucket Lock](https://developers.cloudflare.com/r2/buckets/bucket-locks/)を設定し、lock期間を
previous/pin/restoreの最長保持期間以上にする。R2の
[S3互換checksum](https://developers.cloudflare.com/r2/api/s3/api/#checksum-types)はfull-object SHA-256を
提供しないため、existing objectの再利用はcontent-addressed key、immutable PUT metadata、Bucket Lock、
readerのSHA-256検証、そして`--verify-bytes`監査の組合せで閉じる。Bucket Lock設定確認と
tamper→reader拒否→previous rollback drillはcutover acceptanceの必須項目である。

Raw object と metadata は release publication より前に個別 publish する。

```bash
uv run python -m baibai_batch.storage.lake_publish \
  --mirror <local-mirror> \
  --raw-metadata <raw-metadata>
```

```bash
uv run python -m baibai_batch.storage.lake_publish \
  --mirror <local-mirror> \
  --release-manifest <release-manifest>
```

remote bytesのtamperを探す監査と、previousへの切り戻しは別実行として持つ。

```bash
uv run python -m baibai_batch.storage.lake_publish \
  --mirror <local-mirror> \
  --release-manifest <release-manifest> \
  --verify-bytes
```

```bash
uv run python -m baibai_batch.storage.lake_publish --rollback-l1
```

必要な環境変数は既存 transfer と同じ `R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、
`R2_SECRET_ACCESS_KEY` である。実データ backfill と R2 publish は data license と対象 release
を確認した後にだけ実行する。

<a id="fixed-release-read"></a>

## Fixed release read

読み取りは実行の最初に current pointer を 1 度だけ解決し、以後は固定した `release_id` と
immutable object key だけを読む。実行途中に pointer が切り替わっても、その実行の入力 release は
変わらない。current operational readは解決時刻に対してprofileのfreshness/skew/coverage policyを
再評価し、staleならscreening開始前にfail-closeする。named/pinned/previousのhistorical readは現在
時刻のfreshnessを要求せず、固定されたidentity chainだけを検証する。

```bash
uv run baibai-engine lake resolve --mirror <local-mirror>
uv run baibai-engine lake resolve --mirror <local-mirror> \
  --release <release-id> --manifest-sha256 <release-manifest-sha256>
```

`--release` を渡すとpointerを一切読まないが、同じpinに記録した`--manifest-sha256`を必須とする。
`--previous`はcurrent pointerに対で保存されたprevious release ID / manifest digestを使う。永続pinは
typed `L1ReleaseSourceRef`（ID・key・SHA-256）として保存し、`--release-ref`で解決する。IDだけのnamed /
rollback / study pinは同じkeyの差し替えを検出できないため受理しない。

identity は各辺を digest で閉じる。pointer が release manifest の SHA-256 を、release manifest が
各 dataset manifest の SHA-256 を、dataset manifest が各 object の SHA-256 を持つ。dataset
manifest の key は `(dataset, build_id)` から導けるので、この digest を辿らなければ、release を
一切変えないまま同じ key を差し替えて別のデータを指させられる。

fail-close する条件は次のとおりで、いずれも degrade しない。

- release manifest の SHA-256 が pointer の値と違う
- pointer の `manifest_key` が `release_id` から導く key と違う
- dataset manifest の SHA-256 が release entry の値と違う
- dataset manifest の `dataset` / `build_id` / `contract_version` が release entry と違う
- reader が受け入れる `contract_version` と違う contract を dataset が publish している
- object の SHA-256 / byte 数 / row 数 / Arrow schema が manifest と違う
- 要求した month を release が publish していない

reader は明示された object key の列だけを `read_parquet` へ渡す。bucket の glob、prefix listing、
「最新 object を探し直す」処理、`union_by_name` による schema 吸収はどれも使わない。DuckDB は
渡された path を glob として展開するので、mirror root に glob metacharacter が含まれる場合も
拒否する。extension の autoload / autoinstall は切ってあり、runtimeは`LOAD httpfs`だけを行う。
deployment image / host environmentは、networkを許可したprovisioning stepで同じDuckDB versionの
`uv run python -m tools.diagnostics.provision_duckdb_httpfs`を一度実行する。このcommandはinstall後に
autoload / autoinstallを無効にした別connectionで`LOAD httpfs`までsmoke-checkする。
runtimeがextensionをdownloadするfallbackは持たず、未installなら
R2 sessionをfail-closeする。credentialは非 persistent secretとしてbind parameterで渡し、SQL文・
例外・metadataに残さない。

row は bounded batch で読む。dataset は 10 年分の日足であり、全 row を一度に Python object へ
変換すると build が終わる前に memory を使い切る。partition（1 か月）ごとに object を取得・検証し、
その中を batch で流し込むので、peak memory は dataset の大きさではなく batch 幅に従う。

## Local projection

projection は固定 release から作る使い捨ての SQLite で、authority ではない。削除しても release
から再構築できる。

```bash
uv run baibai-engine lake projection build \
  --mirror <local-mirror> \
  --projection stores/market/projection.sqlite
```

current以外は`--previous`、または`--release <id> --manifest-sha256 <sha256>`、または
typed pin fileを渡す`--release-ref <path>`で固定する。IDだけのprojection buildは受理しない。

`--bucket baibai-stores` を足すと、mirror に無い object だけを R2 から取得して mirror へ
content-addressed に格納する。object key は content hash なので、変わらなかった partition は
既に手元にあり転送量に乗らない。出力の `fetched_bytes` / `reused_bytes` がその内訳になる。

再利用は完全一致でだけ起きる。`release_id`、release manifest digest、全 dataset manifest digest、
全 object digest、projection contract fingerprint のいずれかが違えば再構築する。fingerprintには
table / column / index契約に加え、projectionを作る実装（`projection.py` / `datasets.py` /
`reader.py`）のdigestが入るので、bytesを動かす変更は必ず再構築になる。
identity一致後もtable schema、PK、secondary index、row count、PK順の全row content digest、SQLite
`quick_check`を再計算する。同じrow数のvalue mutation、column/indexの追加・削除、identity tableだけを
残した改変は再利用しない。
`built_at` と build した commit は identity に含めない。`built_at` は同じ入力の 2 回の build で必ず
違い、commit は docs や web だけの変更でも動くので、含めると 10M row の再構築が projection の
bytes と無関係な理由で起きる。commit は `builder_git_commit` として projection の meta に残す。
CLIとbenchmarkは同じidentityを使うので、benchmarkのwarm reuseは実運用の挙動を表す。build は一意な一時 file へ書いて 1 回の rename で公開するので、途中状態が
読まれることはなく、失敗しても直前の projection は壊れない。

production scaleではsecondary indexをbulk insert後に作る。開始前にpublished object bytesの5倍
（最低64 MiB）の同一filesystem空き容量を要求し、10,136,873 daily-bar rowsと1,412,135 short-sale
rowsのbaselineをbounded 20,000-row batchで処理し、index作成後に`ANALYZE`する。受入は次を実行し、
reportの`status: passed`をrelease ID / manifest digestと一緒に保存する。

```bash
uv run python -m tools.diagnostics.benchmark_lake_projection \
  --mirror <local-mirror> --projection <temporary-projection> \
  --release <release-id> --manifest-sha256 <release-manifest-sha256> \
  --report <acceptance-report.json>
```

budgetはcold build 1,800秒、unchanged reuse検証300秒、peak RSS 4 GiB、build中の残空き2 GiB、
代表index queryのp95 100 ms、3年後を現在row/output/timeの1.5倍とする線形stressでoutput 8 GiB・
cold/reuseを同じ時間上限以内とする。11,549,008 row未満のfixtureはproduction-scale証拠として受理しない。
reportはoutput bytes、`quick_check`、table rows、`sqlite_stat1`、query planも記録する。

contract v2のreference acceptanceはLinux/WSL2、DuckDB 1.5.5、SQLite 3.50.4で、release
`pr946-scale-acceptance` / manifest SHA-256
`e77f5b0d6cf031913061969f8a2a092d9044afb89714f5c3d276f2af49e95ab8`の11,554,322 rowsを用いる。
cold 81.98秒、reuse 50.27秒、peak RSS 430 MiB、output 1.33 GiB、代表query p95最大0.065 msで、
全budget、`quick_check`、4件の`sqlite_stat1`を満たす。projection fingerprintは
`sha256:dc46b22ffb3fa654d50f796888ad03ad4f1e78937e66a21c9134b5704fe8d6f0`、generatorとlake codeの
implementation SHA-256は`ea4ce94911dca44345e333db4522593bdadc4f0c43e22f62a0c2ba0ea3f46e6c`である。
<!-- AP-02: cold=81.97515426500468、reuse=50.27278014700278、
peak RSS=450834432 / 1048576 = 429.9453125 MiB、
output=1430007808 / 1073741824 = 1.3317985534667969 GiB、query p95最大=0.06491201929748058 ms。 -->
これはproduction storeをSQLite `mode=ro`でsealed snapshotへ複製し、一時directoryだけにmirror /
projectionを作った結果である。

projectionのatomic publicationが対応するfilesystemは、case-sensitiveでhard link、同一directory内の
`os.replace`、file fsync、directory fsyncを提供するLinux / WSL上のlocal POSIX filesystem
（CIのext4/overlayfsを含む）である。buildは小さなprobe fileでこれらをload開始前に検査する。temporaryと
destinationは必ず同じdirectoryに置く。NFS/CIFS、FUSE/DrvFS、directory fsyncを提供しないfilesystem、
Windows native pathは未対応であり、projection destinationに使わない。replace失敗時はtemporaryを
除去して直前projectionを保持する。

<a id="l2-calibration"></a>

## L2 calibration builds

calibration の cohort（panel・panel diagnostics・forward outcome）は typed Parquet の L2 dataset
として publish する。Arrow schema は `PanelRow` / `PanelDiagnostics` / `ForwardReturnRow` から
導くので、行の契約と保存列がずれない。partition は cohort の as-of の `year/month`。

cohort を 1 つ書くと3 datasetのimmutable buildを先に完成させ、dataset manifestの
`cohort_inventory`へ`complete / empty / partial / not_computed`、row数、typed source digest、
入力cutoffをcohort・role別に固定する。panel/diagnosticsのcutoffはcohort as-ofと一致し、forwardは
実際に観測したmarket data cutoffを持つ。古いpanelを保持したままforwardだけ後日のsnapshotで更新でき、
dataset全体へ過去の全source世代を累積しない。最後に3 manifestを
`CalibrationBundleManifest`へ束ね、`lake/pointers/calibration/current.json`を1回だけ切り替える。
consumerはdataset pointerを読まないため、途中失敗したpanelと旧diagnostics/forwardが混ざらない。
書き換わるのは対象cohortの月partitionだけで、他の月はcontent-addressed objectを引き継ぐ。

bundleは組み立てのtransaction identityとして`assembled_by_git_commit`を持つ。3 datasetのproducer
commitが一致することは要求しない — 既存panelを再計算せずmatured forwardだけを更新する通常運用が、
無関係なcommitを1つ挟むだけで止まってしまう。datasetがcarryできるかは、そのdatasetの
`transform_fingerprint`、cohort source、cutoffで判定する。

forwardの観測規則（control-event exitを使うかなど）は`ForwardObservationPolicy`としてforwardの
`transform_fingerprint`へ入る。1つのstoreは1つのpolicyしか持てず、別policyで作られた月をcarryする
buildは拒否される。storeが名乗るpolicyは`calibration.meta.yaml`にあり、readerがどのidentityを期待するか
だけを決める（rowsがそのpolicyで作られた証明はbuild自身のfingerprintが持つので、書き換えは拒否を
生んでも受理を生まない）。比較用baselineの`--without-control-event-exits`はdefault storeでは拒否し、
別`--calibration-dir`を要求する。

build identityはcohort別typed `SourceRef`、その dataset を最後に作った`producer_git_commit`、
row値・status・membershipを決めるsemantic dependency closure（`panel.py`だけでなくcandidate build、
selection、estimates、rules、universe、SQLite reader、market storeなど。forwardはbars、benchmark、
horizon）のSHA-256とforward observation policyを含む`transform_fingerprint`、
`contract_version`、full primary key、partition/object hashである。panelは`(asof,ticker)`、diagnosticsは
`(asof)`、forwardは`(asof,ticker,horizon)`を一意にし、全rowのyear/month所属をwrite/read両側で
検査する。write APIはsource refのclosureを先に解決し、source省略を受け入れない。`local_operation`
sourceは明示したtest-only gateだけで使う。cohort書き込みは生成中のgenerationに対して行い、
canonical currentへ進むのはgeneration adoptionの1経路だけである。adoptionは全partitionの
digest・size・schema・row countをpointerの前に検証する。cohortごとのcarry検査がpresence/sizeで
止まるのはこのためで、月を1つ触るたびにdataset全体をhashすると書き込み回数の二乗に比例する。readerはbundle pointerを開始時に1回だけ固定し、explicit `empty`の0 rowsだけを`[]`として
返す。inventoryに無いcohortと`partial / not_computed`はfail-closeする。

cohortのinput cutoffとsealed snapshotが保証するのは**同じ結果を後日再生できること**であって、
その値が当時同じ形で入手できたことではない。J-Quantsのadjusted price、master、JPX flagは
revisionを含み、完全なvintageではない（[`data-sources.md`](./data-sources.md)）。較正結果を
live deploy可能なhistorical alphaとして読まず、PIT不完全なfieldに依存するmetricはその前提込みで
保守的に解釈する。

retention の root は 3 種類で、そこから到達できる object は齢によらず残す。

- calibration bundle の current と previous（各3 datasetの完全closure）
- L1 の current release と previous release
- 明示 pin

```bash
uv run baibai-engine lake pin create \
  --mirror <local-mirror> --pin-id <id> \
  --bundle <bundle-id> \
  --reason "adopted as calibration evidence" --owner <owner>

uv run baibai-engine lake gc --mirror <local-mirror>
uv run baibai-engine lake gc --mirror <local-mirror> --apply --plan-hash <hash>
```

calibrationのrootはbundle pointerだけである。pinはbundle manifest keyとSHA-256を固定し、作成時に
target closureを検証する。create/removeは`lake/audit/pins/`へappend-only eventを残す。

`gc` は既定がdry-runで、pointer/pin exact bytes、全root manifest/object digest、candidate identityを
plan hashへ閉じる。`--apply`はpublisher/pinと共通のlocal writer lock取得後に再planする。初回applyは
candidateをmarkするだけで、7日後のsecond sweepが同じidentityを再検証してから削除する。rootが
未解決、object不足、pointer/pin更新、candidate差替えのいずれでも削除を拒否する。

`lake/build-inputs/`のsealed SQLite snapshotも通常GCの対象domainである。1 buildにつきlegacy store
全体と同じ大きさのsnapshotを1つ作るので、prefixを対象外にするとlocal storageがrun数に比例して
増える。current / previous / pinのcohort sourceから到達できる限り残り、到達しなくなってから
30日 + 7日のsecond sweepで回収する。

`calibration-legacy` exact archiveはこのsweepの対象外である。

容量目標は1つのpolicyをclassへ分けて持つ。`lake inventory`の`capacity`が全classを同じ表で出す
ので、あるclassがdesign上の理由で増えたことを、そのclassが対して測られている目標に対して読める。

| class | 内容 | soft budget |
| --- | --- | --- |
| `published` | canonical / analytical Parquet、manifest、pointer。R2が日常的に持つ graph | 10 GiB |
| `raw_preserve` | 再取得できない provider 原本 (Premium CSV、長期 backfill) | 500 GiB |
| `raw_buffer` | 再取得で再現できる routine response | 50 GiB |
| `build_inputs` | sealed legacy snapshot。local のみで upload しない | 10 GiB |

Issue #917 が置いた「R2 は原則 10 GB 前後」は `published` classの目標である。再取得できない原本を
同じ数字に押し込むと保存自体を諦めることになるので、`raw_preserve`は別に承認した budget として持つ。
単一の数字で報告すると、大きい方の budget が小さい方の超過を隠す — 500 GiB の枠の下では、
published graph が目標を超えても、cohort ごとに 2 GB の snapshot が積まれても、何も警告しない。

`lake inventory`は加えて`preserve / buffer`別のobject数、bytes、oldest retrievalを出す。
metadata sidecarを持たないRaw payloadは`raw_unclassified`と`raw_inventory_errors`へ分離し、正常な
retention classの容量へ混ぜない。`preserve`はGC候補にせず、`buffer`はcurrent/previous/pin closureから
未到達かつretrieved-atから90日以上の場合だけ通常GCの候補にする。object/metadata pairを同じplan hashへ
固定し、他のcandidateと同じ7日second sweepを通してlocal mirrorから削除する。R2側の削除はBucket Lock
満了後にDelete専用retention finalizerが同じcandidate identityを検証する運用境界とする。

R2へのpublishは3 datasetのobject/source/manifestとbundle manifestを`If-None-Match: *`で転送し、
最後にbundle pointerだけをETag `If-Match`で切り替える。

**remote calibration lineageはcompactな`calibration_input` packageだけを受け付ける。** sealed full
SQLite snapshotはbuildを1つの一貫した読みへ固定するためのlocal build inputであり、durableな
remote sourceにはしない。cohort generationごとに約2 GBのobjectが増えるので、数世代でこのlakeの
容量目標そのものを使い切る一方、cohortが実際に必要とする行はそのごく一部である。production
calibrationのremote publishは、必要なtableがcompact input packageまたはL1 releaseへ移るまで
fail-closeする。

```bash
uv run python -m baibai_batch.storage.lake_publish \
  --mirror <local-mirror> \
  --calibration-bundle <bundle-manifest>
```

current bundleに問題がある場合は、remote pointer bytes/ETagとpreviousの完全closureを検証し、
current graphが壊れていてもcurrentとpreviousを1回のCASで交換して退避できる。退避した壊れた
generationはpreviousとしてidentityだけを保持し、再度currentへ戻す前には完全closureを要求する。

```bash
uv run python -m baibai_batch.storage.lake_publish \
  --rollback-calibration \
  --bucket <r2-bucket>
```

旧CSVは通常readerのfallbackにしない。`screening calibration-migrate-legacy`がpanel/meta/forwardの
全cohortを列挙し、元bytesをcontent-addressed archiveへ保存してtyped `calibration_input` SourceRefへ
全digestを固定する。現cache contractと互換な履歴だけをfield単位でL2へ変換し、全cohort parity後に
bundle pointerを1回切り替える。非互換履歴は`archived_incompatible`としてarchive/reportだけを残し、
現在手法での再計算と同一視しない。pointer切替後のreport/cleanup失敗は
`completion: committed_with_warnings`として成功済みgenerationを返し、同じinput digestのretryは
`already_migrated`として冪等に完了する。

R2 credentialはroleを分ける。readerはGet/Headだけ、publisherはGet/Head/Putだけ（Deleteなし）、
retention finalizerだけがDeleteを持つ。Bucket Locksはimmutable object/manifest/archive prefixへ適用し、
mutableな`lake/pointers/`、`lake/staging/`、retention markは対象外にする。pinはapplication reachabilityを
表し、Bucket Locksのrule上限・prefix粒度をpin代替に使わない。

merge gateは各stack headの通常CIに加え、`.github/workflows/lake-acceptance.yml`を実行する。
workflowがdefault branchへ入る前はrepository ownerがsame-repository PRへ
`lake-acceptance-approved` labelを付け、eventのexact head SHAをcheckoutして検証する。default branchへ
入った後の再検証はexact 40文字SHAでmanual dispatchする。workflowは`acceptance`を名前に含む
専用bucket以外を拒否し、bundle graphの
実PUT、pointer read-back、current→previousへの実CAS rollback、rollback先bundle read-back、stale ETagの
412/409 fail-closeに加え、tiny L1 publish→remote closure download→reader→projection、2 GiB objectの
PUT/read-back時間、同じsize/偽SHA metadataを持つtampered bytesの拒否、wrong credential errorのredactionを
検査する。
credentialはvalidation/setupへ渡さず、actual R2 stepだけが専用publisher tokenを持つ。production-size
export/projectionは上記reference acceptance report、legacy migrationは元bytesをread-onlyで扱う
archive/parity reportをhead SHAと一緒に保存する。production bucketとcanonical storeをacceptanceに使わない。

## Shadow parity

release から作った projection と legacy store で screening を 2 回実行し、candidate / metric /
selection を突き合わせる。差分があれば非ゼロ終了する。legacy と lake が併存する間だけ必要な
比較なので、stable CLI ではなく diagnostic script に置く。

```bash
uv run python -m tools.diagnostics.verify_lake_release_parity \
  --asof <YYYY-MM-DD> \
  --projection stores/market/projection.sqlite \
  --mirror <local-mirror>
```

両側は同じ as-of、同じ rules、同じ時刻、同じ application DB で走り、provider は cache-only に
固定する。legacy sideはlive `market.sqlite`を読み直さず、projection identityが固定したreleaseから
`SQLiteSnapshotSourceRef`を解決し、lake buildと同じsealed generationを使う。reportはsnapshotの
key/digest/schema/capture時刻とrelease manifest digestを必須出力する。snapshot digest不一致は
screeningを始める前に拒否する。fetch できる provider が 1 つでもあると、release に欠けた行が裏で補われて「一致」が
間違った理由で成立するので、release 側に穴があれば実行そのものを失敗させる。

値が違ってよいのは publication ごとに新しく発行される識別子（`run_revision_id`、`selection_id`、
`selection.input_refs.candidates_ref`）だけで、順序を含む他の全 field は一致しなければならない。
両側とも候補 0 件なら `compared_nothing` を立てて不一致として扱う（空同士は自明に一致するので、
それを一致と報告すると壊れた入力が証拠になってしまう）。report は `release_sourced_tables` と
`legacy_sourced_tables` を必ず両方出す。後者が cutover の残作業そのものである。
