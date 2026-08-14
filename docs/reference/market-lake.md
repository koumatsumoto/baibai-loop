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
検証する。

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
  --retention preserve
```

検証済み dataset manifest を release に固定する。

```bash
uv run baibai-engine lake release create \
  --mirror <local-mirror> \
  --dataset-manifest <daily-bars-manifest> \
  --dataset-manifest <short-sale-manifest>
```

## R2 publish

publish は immutable object、dataset manifest、release manifest の順に `If-None-Match: *` で
転送する。各sourceをsealed copyへ固定してR2が検証する`Content-MD5`付きPUTを行い、logical
SHA-256、transport marker、size、content typeを同じimmutable PUTのmetadataへ固定する。PUT/reuse後、
さらにpointer直前にreleaseから到達可能な全objectのHEAD closureを再検証する。bulk objectを毎回
GETしてmemoryへ展開せず、最後に`lake/pointers/l1/current.json`をETag `If-Match`で切り替える。
small pointerだけをGET read-backしてexact digestを検証する。409/412のCAS conflictはretryせず
fail-closeし、current releaseを再解決する。

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

必要な環境変数は既存 transfer と同じ `R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、
`R2_SECRET_ACCESS_KEY` である。実データ backfill と R2 publish は data license と対象 release
を確認した後にだけ実行する。

<a id="fixed-release-read"></a>

## Fixed release read

読み取りは実行の最初に current pointer を 1 度だけ解決し、以後は固定した `release_id` と
immutable object key だけを読む。実行途中に pointer が切り替わっても、その実行の入力 release は
変わらない。

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
全 object digest、projection contract fingerprint、producer commit のいずれかが違えば再構築する。
identity一致後もtable schema、PK、secondary index、row count、PK順の全row content digest、SQLite
`quick_check`を再計算する。同じrow数のvalue mutation、column/indexの追加・削除、identity tableだけを
残した改変は再利用しない。
`built_at` は identity に含めない（同じ入力の 2 回の build で必ず違い、含めると再利用契約が
成立しないため）。build は一意な一時 file へ書いて 1 回の rename で公開するので、途中状態が
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
`pr944-scale-acceptance` / manifest SHA-256
`aa8d2a8b0a8f5528c0cf1e76d7684de30abe1820f37bef5fb6148d9a4bb6ce53`の11,554,322 rowsを用いる。
cold 77.48秒、reuse 46.52秒、peak RSS 436 MiB、output 1.33 GiB、代表query p95最大0.061 msで、
全budget、`quick_check`、4件の`sqlite_stat1`を満たす。projection fingerprintは
`sha256:ecd5e281e94704c2f6d10f7fef8773c5657aef5f3a393c4041b6a3c0756e1844`、generatorとlake codeの
implementation SHA-256は`838963d0313fe67a4e4d994c458273ad943a78bf7ff79963c0225d27a00ac787`である。
<!-- AP-02: cold=77.48110374299722、reuse=46.52158455800236、
peak RSS=457134080 / 1048576 = 435.95703125 MiB、
output=1430007808 / 1073741824 = 1.3317985534667969 GiB、query p95最大=0.060705999203491956 ms。 -->
これはproduction storeをSQLite `mode=ro`でsealed snapshotへ複製し、一時directoryだけにmirror /
projectionを作った結果である。

projectionのatomic publicationが対応するfilesystemは、case-sensitiveでhard link、同一directory内の
`os.replace`、file fsync、directory fsyncを提供するLinux / WSL上のlocal POSIX filesystem
（CIのext4/overlayfsを含む）である。buildは小さなprobe fileでこれらをload開始前に検査する。temporaryと
destinationは必ず同じdirectoryに置く。NFS/CIFS、FUSE/DrvFS、directory fsyncを提供しないfilesystem、
Windows native pathは未対応であり、projection destinationに使わない。replace失敗時はtemporaryを
除去して直前projectionを保持する。

## Shadow parity

release から作った projection と legacy store で screening を 2 回実行し、candidate / metric /
selection を突き合わせる。差分があれば非ゼロ終了する。legacy と lake が併存する間だけ必要な
比較なので、stable CLI ではなく diagnostic script に置く。

```bash
uv run python -m tools.diagnostics.verify_lake_release_parity \
  --asof <YYYY-MM-DD> \
  --projection stores/market/projection.sqlite
```

両側は同じ as-of、同じ rules、同じ時刻、同じ application DB で走り、provider は cache-only に
固定する。fetch できる provider が 1 つでもあると、release に欠けた行が裏で補われて「一致」が
間違った理由で成立するので、release 側に穴があれば実行そのものを失敗させる。

値が違ってよいのは publication ごとに新しく発行される識別子（`run_revision_id`、`selection_id`、
`selection.input_refs.candidates_ref`）だけで、順序を含む他の全 field は一致しなければならない。
両側とも候補 0 件なら `compared_nothing` を立てて不一致として扱う（空同士は自明に一致するので、
それを一致と報告すると壊れた入力が証拠になってしまう）。report は `release_sourced_tables` と
`legacy_sourced_tables` を必ず両方出す。後者が cutover の残作業そのものである。
