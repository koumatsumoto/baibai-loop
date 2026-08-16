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
検証する。snapshotはoperationの一時入力であり、bytesはlakeにもremote closureにも残さない。manifest
が残すのはschema version・content digest・capture時刻という素性だけで、同じstore世代を持っているか
どうかはre-sealして digest を突き合わせれば答えられる（unchangedなstoreに対してsealはbyte決定的）。

`sqlite_authority`期間のrestore checkpointは既存のcloud `market.sqlite`を正本とし、lake
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
| full export（121 か月 × 2 dataset） | 367.0 秒 | 242 | 252,387,406 |
| 1 か月訂正の再 export | 251.5 秒 | 1 | 848,197 |

peak RSS は 993,619,968 bytes（948 MiB）。

**parity は build が書いた月だけを見る。** carried object は自分の bytes の digest で addressing
されているので、「変わっていない」ことは検証対象ではなく恒等式である。全 history を SQLite から
derive し直すのは、この build ではなく前の build を証明する作業になる。同一機・同一 store での A/B:

| 増分 export（変更なし） | wall time |
| --- | --- |
| 既定（書いた月のみ検証） | **132 秒** |
| `--audit`（全 history 再導出） | **309 秒** |

月の inventory 比較（SQLite の月集合 == manifest の月集合）は常に全体で行う。全 history の再導出は
`--audit` で明示的に求める — store 全体がまだ SQLite と一致するかを問う操作であり、日次の書き込み
経路が毎回背負うものではない。

この計測は commit ではなく実装 digest（writer / models / immutable / snapshot / benchmark tool）へ
結ぶ。それらに触れない変更では証跡は有効なままで、触れた変更は再計測になる。

**現在の状態: 参考値。** 上表は `a2005b74` の実測で、現 head では実装が動いている。同じ store
（snapshot digest `703e3fab…`、11,554,322 rows）を現 head で測り直すと full export は 456 秒、
projection は cold 104 秒 / reuse 61 秒で、いずれも記録値より 20〜30% 遅い。入力・行数・出力 bytes は
完全に一致するので差は測定機の負荷であり、**記録値は楽観側に約 25% ずれている**と読むこと。増分
export の A/B（上表）は現 head・同一機での実測である。

<!-- AP-02: full=367.0256703949999 秒、incremental=251.45569620199967 秒、
peak RSS=993619968 / 1048576 = 947.6015625 MiB、
source sha256=703e3fab403489726708fc83c07fe1975e9f0ddad5ba492834ad2f1ec33144ce、
implementation sha256=fa2377139ec3d9ab09f0d2a9fa011677b157072b6a569bbdcd02415c13b8fcb8、
producer commit=bffb199a3119b1f02123626a2096a955185dd1d1。 -->

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

sealed SQLiteはdigest・schema・capture時刻をmanifestへ記録するだけでbytesを持たないため、
日次remote bytesはchanged Parquet/Raw/manifestへ比例する。最後に`lake/pointers/l1/current.json`を
ETag `If-Match`で切り替え、pointer bytesだけをGETで読み戻す。409/412のCAS conflictはretryせず
fail-closeし、current releaseを再解決する。subprocessのdeadlineはobject sizeから導く（base 120秒 +
実測を下回る4 MiB/秒での転送時間）ので、大きなobjectがtimeoutで曖昧な結果になることを避ける。

**pointerはcurrentだけを名乗る。rollbackは無い。** 修理は前へ publish することであり、store が
serve をやめた世代へ戻ることではない。pointer が「この世代は復元できる」と名乗れば、それは publish の
たびに検証し続けなければならない約束になり、実際そうしていた。local mirror が graph 全体を持ち、
writer が 1 つしかないこの構成では、悪い release を publish したときの復旧は良い release を publish
することである。同じ release ID の再 publish だけは中断した publication の retry として受け付け、
identity が違えば拒否する。

production authority化ではmutable pointer prefixを除くimmutable prefixへ
[R2 Bucket Lock](https://developers.cloudflare.com/r2/buckets/bucket-locks/)を設定し、lock期間を
restoreの最長保持期間以上にする。R2の
[S3互換checksum](https://developers.cloudflare.com/r2/api/s3/api/#checksum-types)はfull-object SHA-256を
提供しないため、existing objectの再利用はcontent-addressed key、immutable PUT metadata、Bucket Lock、
readerのSHA-256検証、そして`--verify-bytes`監査の組合せで閉じる。Bucket Lock設定確認と
tamper→reader拒否→前へのre-publish drillはcutover acceptanceの必須項目である。

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

remote bytesのtamperを探す監査は別実行として持つ。

```bash
uv run python -m baibai_batch.storage.lake_publish \
  --mirror <local-mirror> \
  --release-manifest <release-manifest> \
  --verify-bytes
```

必要な環境変数は既存 transfer と同じ `R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、
`R2_SECRET_ACCESS_KEY` である。実データ backfill と R2 publish は data license と対象 release
を確認した後にだけ実行する。

<a id="fixed-release-read"></a>

## Fixed release read

読み取りは実行の最初に current pointer を 1 度だけ解決し、以後は固定した `release_id` と
immutable object key だけを読む。実行途中に pointer が切り替わっても、その実行の入力 release は
変わらない。current operational readは解決時刻に対してprofileのfreshness/skew/coverage policyを
再評価し、staleならscreening開始前にfail-closeする。named releaseのhistorical readは現在
時刻のfreshnessを要求せず、固定されたidentity chainだけを検証する。

```bash
uv run baibai-engine lake resolve --mirror <local-mirror>
uv run baibai-engine lake resolve --mirror <local-mirror> \
  --release <release-id> --manifest-sha256 <release-manifest-sha256>
```

`--release` を渡すとpointerを一切読まないが、`--manifest-sha256`を必須とする。手元のrelease参照は
typed `L1ReleaseSourceRef`（ID・key・SHA-256）として保存し、`--release-ref`で解決する。IDだけの
named readは同じkeyの差し替えを検出できないため受理しない。

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
remote lakeを開くhost environmentは、networkを許可したprovisioning stepで同じDuckDB versionの
`uv run python -m tools.diagnostics.provision_duckdb_httpfs`を一度実行する。このcommandはinstall後に
autoload / autoinstallを無効にした別connectionで`LOAD httpfs`までsmoke-checkする。
provisioningを実行するのは実際にremote lakeを読むjobだけとする。lake pathを通らないjobへ入れると、
extension repositoryの一時障害がそのjobの成功条件になり、lakeの価値を受け取っていない処理を止める。
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

current以外は`--release <id> --manifest-sha256 <sha256>`、または
typed release ref fileを渡す`--release-ref <path>`で固定する。IDだけのprojection buildは受理しない。
publishしていないlocal mirrorにはcurrent pointerが無いので、初回のprojectionは必ずこのどれかで
releaseを名指す。

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

current modeでは、再利用と再構築の**どちらも**成功を返す直前にcurrent pointerのfull identityを
問い直す。再利用は何も書かないが、決めるためにprojection全体を読み返す（production storeで47秒）
ので、その間にpointerが動く窓は再構築と同じだけある。成功の報告は「これがcurrentのprojectionだ」
という主張なので、計算中にcurrentでなくなった releaseについてそれを言わない。

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
`pr946-r5-acceptance` / manifest SHA-256
`e960473ac41235caa5e927e3e031bd621eed86c5527d61ac079bc6b40e5dd0d5`の11,554,322 rowsを用いる。
cold 78.55秒、reuse 47.07秒、peak RSS 433 MiB、output 1.33 GiB、代表query p95最大0.065 msで、
全budget、`quick_check`、4件の`sqlite_stat1`を満たす。projection fingerprintは
`sha256:e53ee0ea9deb62adcb222cb63fefcc623ae8739e64c5d77d9f601f659cfe4e37`、benchmarkの
implementation SHA-256は`6b583b2af13fd7920e01efab10ce6e7409f6b40fd4f28e6f03de533f061db6a1`である。

**現在の状態: stale。** 現 head の projection fingerprint は `sha256:b12d5cb4…` で、記録値とは別の
identity である。fingerprint は projection を作る実装 digest を含むので、reuse identity への
DuckDB / SQLite version 追加と、その後の currency check の変更のたびに動く。数値の桁は変わらないと
見ているが、この head の証跡ではない。再計測が必要。
<!-- AP-02: cold=78.55443349899724、reuse=47.06548080200446、
peak RSS=453734400 / 1048576 = 432.71484375 MiB、
output=1430007808 / 1073741824 = 1.3317985534667969 GiB、query p95最大=0.0654769828543067 ms。 -->
これはproduction storeをsealed snapshotへ複製し、一時directoryだけにmirror /
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
入力cutoff、そしてmeasurement policy（rules hash・panel variant・production authority）を
cohort・role別に固定する。measurement policyがmanifestに居るのは、どのrulesで測ったかが
membershipとstatusを決めるからで、これがdiagnostics rowの中にしか無いとconsumerはParquetを
開かないと世代の正体を知れず、混在したbundleを組み立てても何も反対しない。forwardはpanelの
policyを継承する — 観測している銘柄はそのpanelが選んだ集合なので、別のrulesを名乗ると使って
いないcross-sectionを説明することになる。**bundleは1つのpolicyしか持てず、混在は組み立てで
拒否する。** consumer側で気づく設計だと、誰かが読むまでstoreが混在を抱えたままになる。

**bundle manifestはcohort inventoryを持たない。** 各dataset manifestが自分のbuildが持つcohortを
既に述べているので、bundleへの複製は同じ事実の2つ目の置き場所であり、両者が一致することを確認する
3つ目・4つ目の場所を作る。bundleが解決されるときに3つのdataset manifestから合成し、合成できない
ものを拒否する — 3 datasetのcohort集合が違う、計算済みpanelにdiagnosticsが無い、roleが別のrulesを
名乗る、panelとdiagnosticsの入力が違う、cohort keyが正規のas-ofでない、panelのcutoffがas-ofと違う、
forwardの観測がcross-sectionより前、policyが混ざる。同じ合成をassemblerがpublish前に、remote
publisherがCAS前に通す。複製が無いので、食い違いようがない。panel/diagnosticsのcutoffはcohort as-ofと一致し、forwardは
実際に観測したmarket data cutoffを持つ。古いpanelを保持したままforwardだけ後日のsnapshotで更新でき、
dataset全体へ過去の全source世代を累積しない。最後に3 manifestを
`CalibrationBundleManifest`へ束ね、`lake/pointers/calibration/current.json`を1回だけ切り替える。
**mutableなstateはこのbundle pointer 1つだけである。** cohort writeは新しいdataset manifestの
refを値として返し、触っていないdatasetは現行bundleから引き継ぐ。dataset別のpointerを別に持つと、
公開したgenerationとwriterの継続状態が2つの別々のstateになり、次のwork generationへ運ばれるのは
片方だけになる。同じas-of範囲を2回目に走らせてforwardだけ成熟させる経路は、まさにその引き継がれ
なかった側を読む。
書き換わるのは対象cohortの月partitionだけで、他の月はcontent-addressed objectを引き継ぐ。

bundleは組み立てのtransaction identityとして`assembled_by_git_commit`を持つ。3 datasetのproducer
commitが一致することは要求しない — 既存panelを再計算せずmatured forwardだけを更新する通常運用が、
無関係なcommitを1つ挟むだけで止まってしまう。datasetがcarryできるかは、そのdatasetの
`transform_fingerprint`、cohort source、cutoffで判定する。

forwardの観測規則（control-event exitを使うかなど）は`ForwardObservationPolicy`としてforwardの
`transform_fingerprint`へ入る。1つのstoreは1つのpolicyしか持てず、別policyで作られた月をcarryする
buildは拒否される。storeが名乗るpolicyはbundle manifestの中にあり、readerがどのidentityを期待するか
だけを決める（rowsがそのpolicyで作られた証明はbuild自身のfingerprintが持つので、書き換えは拒否を
生んでも受理を生まない）。pointerが名乗るmanifestの中に置くのは、世代の切替を1つのatomicな行為に
するためである — 契約を別fileに置くと、pointerが動かないままそのfileだけが新しくなり、serveして
いない世代を名乗りながらserveしている世代を読めないstoreができる。比較用baselineの`--without-control-event-exits`はdefault storeでは拒否し、
別`--calibration-dir`を要求する。

`contract_version`はdatasetごとに持つ。object keyへ入る唯一の互換性表示なので、片方のrow型が
列を得たときに同じ`contract=v1`が2つの列構成を指すと、versionだけで判断する外部readerが違う形を
読む。drift gate `check_l2_contract_versions`が記録済みschema signatureと実際のschemaを突き合わせ、
bumpせずにrow型を変えた変更を落とす。signatureは列・key・partitionに加えreaderが実際に比較する
`baibai.*` metadataも署名するので、列を変えずrow型名だけを変えた場合も落ちる。**gateが赤いときの
修復は`contract_version`を上げて新しい版として記録することであり、記録済みsignatureの上書きではない。**
上書きはgateを緑にしたまま同じ版に2つの列構成を持たせる — 版だけで判断する外部readerが違う形を読む、
まさにこのgateが防いでいる状態である。failure messageはその修復を名指す。cache identityも
datasetごとに導く — forwardへ列を1つ足してpanelの81 cohortが再構築になるのは、値を動かせない変更に
数時間と数百MBを払ううえ、「再構築が要る」という信号の意味を薄める。screening閾値と評価式の意味は
panel / diagnosticsの値を決めるが、forwardの観測 (entry / exit / 配当) は決めない。

**契約版を問う場所はrowをdecodeする側だけである。** bundleの解決はdigest edgeを閉じる構造的な行為で、
forwardがv2へ動いた世代もpanelについては真の記述なので、解決時にversionを問うと1 datasetの契約変更が
bundle全体をunresolveにし、完全にdecodeできるpanelが要求される前に拒否される。generationをcanonicalに
するadoptionだけが3 dataset全ての契約版を問う。

到達している範囲は**読み取り側**である。契約が動いたdataset自身はbuild全体を作り直す必要が残る
（`_publish_cohort`が旧契約のpartitionをcarryできず、bundleは3 datasetのcohort集合一致を要求するため、
build途中で1 datasetだけを作り直せない）。段階的なdataset単位upgradeは別phaseとする。

build identityはcohort別typed `SourceRef`、その dataset を最後に作った`producer_git_commit`、
semantic dependency closureのSHA-256とforward observation policyを含む`transform_fingerprint`、
`contract_version`、full primary key、partition/object hashである。panelは`(asof,ticker)`、diagnosticsは
`(asof)`、forwardは`(asof,ticker,horizon)`を一意にし、全rowのyear/month所属をwrite/read両側で
検査する。

semantic dependency closureは手で並べず、その dataset を作る module（panel / forward）から
importで到達するengine moduleを辿って求める。手で並べたlistは、載っているmoduleが新しいhelperを
importした時点で遅れる — 行の値は変わったのにfingerprintが黙るので、旧cohortが新しい意味の行と
同じidentityで並ぶ。この経路で入ってくるのは`foundation/coerce.py`や`screening/metric_quality.py`の
ような、どのcalibration moduleも名指していないが値を決めているhelperである。2つのdatasetは別々の
entryから辿るので、outcomeの観測を変えてもpanelの月は無効化しない（唯一の共有だったentry lagは
horizon契約が持つ）。

write APIはsource refのclosureを先に解決し、source省略を受け入れない。同じ source を何度
名指しても検証は operation ごとに 1 回で、writer lock を持つ間は immutable な source が
動かないことがそれを許す。retired CSV archive のように 1 つの source を全 cohort が指す場合、
参照ごとに払うと 500MB × 81 cohort が 1 回の migration で数百 GB の hash になる。cohort書き込みは生成中の
generationに対して行い、canonical currentへ進むのはgeneration adoptionの1経路だけである。

**部分範囲の再計算は、範囲外の既存cohortを黙って落とさない。** `--force`はstoreをhard linkで
引き継がずgenerationを空から始めるので、1年を直すつもりの実行がその1年だけを持つcurrent bundleを
公開しうる。adoptionの直前に「今serveしている集合」と「これからserveする集合」を比較し、落ちるものが
あれば名指して拒否する。

**壊れたstoreは、その場では直さない。** 解決できないstoreへのbuildは`--force`の有無にかかわらず
拒否し、pointerもmanifestも1バイトも動かさない。復旧は別の`--calibration-dir`へfull buildし、読める
ことを確認してからdirectoryを入れ替える。in-placeで直すには「今serveしている集合」が要るが、それは
まさに壊れて読めないものであり、推定で埋めれば破壊的な再構築が推定の上で走る。別directoryなら
入れ替える前に読めるし、旧directoryはそのまま残るのでrollbackもできる。

これはstate数の判断でもある。in-place復旧を持つと、通常buildは「読めるstoreへのbuild」と「壊れた
storeへのbuild」の2つの意味を持ち、`--force`の意味・drop guardの比較対象・retryの扱いがその分岐ごとに
変わる。1人運用でめったに起きない障害のために、毎日の経路が常時その分岐を抱えることになる。

**pointerを失ったstoreは空のstoreではない。** publishした痕跡（bundle manifest）が残る限り解決は
fail closeする。両者を同じ「まだ何も無い」として扱うと、次のbuildがstoreを新規扱いして書き潰す。
retentionは既にこの区別でsweepを止めており、readerだけが「空」と答える状態が食い違いである。

adoptionはbundleが閉じているものだけを歩く。bundle manifest → dataset manifest → partition object
→ 保持するcohort sourceとそのfileであり、directory treeではない（treeには追い越された世代も居る）。
全partition objectのdigest・size・schema・row countをpointerの前に検証する。generationはstoreをhard linkで複製して作るので、carryされたobjectは最初からstore側と
同じinodeを共有している。同一inodeにinstallもcompareも不要であり、残るのはこのbuildが実際に作った
ものだけになる。CLIはclosure object数、hashしたbytes、installしたobject数とbytes、再利用した
object数を出力するので、更新1回のI/Oがstore全体へ広がったことはwall timeより先に見える。

cohortごとのcarry検査がpresence/sizeで止まるのはこのためで、月を1つ触るたびにdataset全体をhashすると
書き込み回数の二乗に比例する。readerはbundle pointerを開始時に1回だけ固定し、explicit `empty`の
0 rowsだけを`[]`として返す。inventoryに無いcohortと`partial / not_computed`はfail-closeする。

**固定は呼び手が行う。** `published_cohorts` / `read_panel` / `read_forward` は世代を受け取り、
渡されなければcurrentを解決する。1つの測定はcohort列挙・forward rows・rules identity・panel rowsの
4回以上のreadでできているので、それぞれがcurrentを解決すると、途中に入った publication が
「片方の世代のpanel」と「もう片方の世代のoutcome」を1つの効果量へ入れる。個々のreadは全てvalidで
digestもschemaも反対しないため、報告だけが何も生成していない数値になる。calibrationを読む分析tool
（`tools/experiments/measure_*`）は入口で世代を1回固定し、以降のreadへ渡す。

cohortのsourceには2つの保証水準があり、`source_assurance`として区別する。**rebuildable_input**は
producerが読んだ上流入力をlakeが保持していて、producer側の誤りを直してから再導出できる。
**trace_only**は読んだstore世代を名指せるだけで、それができない。判断のaudit — 計算logicの誤りが
後で見つかったときの訂正 — に要るのは前者だけなので、`--run-purpose production_decision`は
rebuildable_inputのcohortだけを許可し、それ以外には`source_not_rebuildable`をblocking reasonとして
立てる。水準はcohortの3 role（panel / diagnostics / forward）が名指すsourceの最弱で決まる。

**現時点でrebuildable_inputに到達するcohortは存在しない。** `CohortSourceRef`が許すのはsealed SQLite
snapshotだけで、そのbytesをlakeは持たないからである。`production_decision`はその間fail-closeする。
この水準が区別として意味を持ち始めるのは、L1 releaseがcohort sourceとして採れるようになった時点で
（Issue #917）、同じ変更が「世代が名指すsourceがcurrentになる前に解決すること」の検査も連れてくる。

cohortのinput cutoffとsealed snapshot identityが保証するのは**どのstore世代を読んだか名指せること**
であって、その値が当時同じ形で入手できたことではない。J-Quantsのadjusted price、master、JPX flagは
revisionを含み、完全なvintageではない（[`data-sources.md`](./data-sources.md)）。較正結果を
live deploy可能なhistorical alphaとして読まず、PIT不完全なfieldに依存するmetricはその前提込みで
保守的に解釈する。

retention の root は 2 種類で、そこから到達できる object は齢によらず残す。

- calibration bundle の current（3 datasetの完全closure）
- L1 の current release

```bash
uv run baibai-engine lake gc --mirror <local-mirror>
uv run baibai-engine lake gc --mirror <local-mirror> --apply --plan-hash <hash>
```

**世代を無期限に到達可能へ留める機構は持たない。** 公開した study をそれが読んだ bytes から再現
する能力は、この store が提供するものではない — 記録は report であり、report が名指した世代を
すべて抱えることは、store が「今何を serve しているか」を言えなくなる道筋そのものである。

`gc` は既定がdry-runで、pointer exact bytes、全root manifest/object digest、candidate identityを
plan hashへ閉じる。`--apply`はpublisherと共通のlocal writer lock取得後に再planし、同じ実行の中で
削除まで終える。plan hashがoperatorの読んだ planへ束縛し、lockが並行publishを排除し、lock内の
再planがrootの実状態に対して候補を計算し直し、削除直前に各candidateのbytesを再検証する。間に
到達可能になったcandidateは再planの結果を変えるのでloopに入らない。markして1週間後に消す二段構えは
何も足さない — 競合は既に排除されており、planner自体の誤りは2回目も同じ答えを計算する。単独運用で
収集を終えるのに2回の実行が要るだけで、それはretention policyが実行されなくなる道筋である。待つ
場所はcandidateになるまでの30日grace側にある。rootが未解決、object不足、pointer更新、candidate
差替えのいずれでも削除を拒否する。

`lake/staging/`もGCの対象domainである。in-flightのstagingとsealed snapshotが置かれる場所で、
killされたoperationも失敗したbuildも自分の後片付けを実行できないため、7日のgrace後に回収する。
manifestから到達しないので、age以外に回収の根拠がない。失敗したbuildのstagingを別の場所へ退避して
長く保持することはしない — 誰も開かない事故調資料である。

calibration storeのwork generationはstoreのsiblingとして作られる（storeをhard linkで複製して
作るため）。これはlakeのどのprefixにも入らないので、次のbuildが — writer lockを持っている以上、
live generationは存在しえない — 起動時に破棄し、回収したbytesを出力する。

容量目標は1つのpolicyをclassへ分けて持つ。`lake inventory`の`capacity`が全classを同じ表で出す
ので、あるclassがdesign上の理由で増えたことを、そのclassが対して測られている目標に対して読める。

| class | 内容 | soft budget |
| --- | --- | --- |
| `published` | canonical / analytical Parquet、manifest、pointer。R2が日常的に持つ graph | 10 GiB |
| `raw_buffer` | 再取得で再現できる routine response | 50 GiB |
| `workspace` | in-flight staging と失敗 build が残したもの。どのmanifestにも属さない | 20 GiB |

Issue #917 が置いた「R2 は原則 10 GB 前後」は `published` classの目標である。classを分けるのは、
単一の数字で報告すると大きい方の budget が小さい方の超過を隠すからで、published graph が目標を
超えても失敗 build が 2 GB 積んでも、合算では何も警告しない。`preserve` classのRawは budget 表を
持たない — 再取得できない原本を「いくらまで」で語ると、超えた日に捨てるか諦めるかしか選べなくなる。
量が問題になった時点で、何を捨てるかを個別に決める。

`lake inventory`は加えて`preserve / buffer`別のobject数、bytes、oldest retrievalを出す。
metadata sidecarを持たないRaw payloadは`raw_unclassified`と`raw_inventory_errors`へ分離し、正常な
retention classの容量へ混ぜない。`preserve`はGC候補にせず、`buffer`はcurrent closureから
未到達かつretrieved-atから90日以上の場合だけ通常GCの候補にする。object/metadata pairを同じplan hashへ
固定し、他のcandidateと同じsweepでlocal mirrorから削除する。R2側の削除はBucket Lock
満了後にDelete専用retention finalizerが同じcandidate identityを検証する運用境界とする。

R2へのpublishは3 datasetのobject/source/manifestとbundle manifestを`If-None-Match: *`で転送し、
最後にbundle pointerだけをETag `If-Match`で切り替える。

cohort sourceのうちbytesを保持するもの（`calibration_input`）はそのbundleと一緒にuploadされ、
durable remote inventoryはcohortが実際に保持する分だけ増える。sealed SQLiteは素性だけなので運ぶ
bytesがなく、通常のcalibration-buildが作ったbundleはそのままremoteへ公開できる。

**引き換えに失うもの**: 過去cohortをbyte単位でrebuildする「保証」は、この段階では持たない。同じ
digestのmarket store世代があれば再現でき、digestで照合もできるが、その世代がまだ入手できることは
lakeが保証しない。保証が戻るのは、cohortが必要とするtableがL1 releaseとして公開され、keyを持つ
`rebuildable_input`になった時点である（Issue #917）。

```bash
uv run python -m baibai_batch.storage.lake_publish \
  --mirror <local-mirror> \
  --calibration-bundle <bundle-manifest>
```

current bundleに問題がある場合も、直すのは前へ publish することである。local storeで作り直した
generationを publish すれば pointer は 1 回のCASでそれを指す。

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

出力へ何を出さないかは、その出力が誰の手に渡るかで決まる。**共有される成果物** — remote publish
report、Discord通知、CI artifact、そこへ載るerror — にはcredential、account ID、bucket URL、
そしてlocal filesystem pathを出さない。publish reportがrelease ID・pointer ETag・転送counterだけで
できているのはこのためである。**operator-local CLI**（`inventory`、`release`、`projection`、
`archive-raw`、immutable installのerror）はlocal pathを出す。operatorが次に触るのはその
pathそのものであり、隠すとdebug可能性を失うだけで誰も守らない。共有される場所へこれらのoutputを
そのまま貼る運用にしない。

R2 credentialはroleを分ける。readerはGet/Headだけ、publisherはGet/Head/Putだけ（Deleteなし）、
retention finalizerだけがDeleteを持つ。Bucket Locksはimmutable object/manifest/archive prefixへ適用し、
mutableな`lake/pointers/`と`lake/staging/`は対象外にする。

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
  --mirror <local-mirror> \
  --sqlite stores/market/market.sqlite
```

両側は同じ as-of、同じ rules、同じ時刻、同じ application DB で走り、provider は cache-only に
固定する。legacy sideは`--sqlite`で渡したstoreをsealし、そのdigestがprojection identityの固定した
releaseが名乗るsnapshot generationと一致することを要求する。sealはunchangedなstoreに対してbyte
決定的なので、この一致は「両側が同じ世代を見ている」ことの証明になる。releaseを作った世代を
もう持っていない場合は、比較を始める前に拒否する。reportはsnapshotのdigest/schema/capture時刻と
release manifest digestを必須出力する。fetch できる provider が 1 つでもあると、release に欠けた行が裏で補われて「一致」が
間違った理由で成立するので、release 側に穴があれば実行そのものを失敗させる。

値が違ってよいのは publication ごとに新しく発行される識別子（`run_revision_id`、`selection_id`、
`selection.input_refs.candidates_ref`）だけで、順序を含む他の全 field は一致しなければならない。
両側とも候補 0 件なら `compared_nothing` を立てて不一致として扱う（空同士は自明に一致するので、
それを一致と報告すると壊れた入力が証拠になってしまう）。report は `release_sourced_tables` と
`legacy_sourced_tables` を必ず両方出す。後者が cutover の残作業そのものである。
