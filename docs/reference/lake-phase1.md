# L1 pilot writer operations

Issue #917 Phase 1 は `jquants.daily_bars` と `jquants.short_sale_reports` の concrete
contract だけを実装する。現行 provider / Premium backfill は coverage を SQLite に commit し、
lake export はその SQLite を `legacy_sqlite_import` として月 partition へ変換する。provider
取得と Parquet writer の二重 canonical write は行わない。

contract v1 は、fixed legacy SQLite snapshotからauthorityを移すためのcompatibility boundaryで
あり、provider-native canonicalの最終形ではない。date/timeのstring、価格・ratioのfloat、legacy
tableに存在するfieldだけを保持するため、Rawだけにあるfield、decimal precision、publication /
effective / retrieved time、revision/cancellation semanticsを完全には表さない。これらのmapping、
null/zero、timezoneをversioned contractとして確定し、Raw→canonicalのsemantic parityを満たした時点を
v2 rebuild triggerとする。v1/v2は別prefix・別release policyで扱い、同一releaseへ黙って混在させない。
v1はcutover後もprevious/pinのrollback期間だけ保持する。

## Local build

初回 seed は全期間を export する。`export-pilot` は開始時にSQLite backup APIでWALを含むsealed
snapshotを1回作り、snapshot digest・schema version・`quick_check`を確定してから、両datasetの
export、source-state、parityを同じsnapshotから導出する。release作成時にも全partitionのsnapshot
identityが両datasetでexactに1世代へ閉じることを検証し、世代の混在を拒否する。

```bash
uv run baibai-engine lake export-pilot \
  --sqlite stores/market/market.sqlite \
  --mirror <local-mirror>
```

中断した Premium CSV / API backfill は既存 `source_coverage` から再開する。直前 manifest を
渡すと SQLite facts + coverage の state hash を比較し、commit 済みの追加・訂正月だけを
export して未変更月を再利用する。transform fingerprintはschema/configに加えてwriter・dataset
contract sourceのdigestを含み、違うbaseは再利用せず拒否する。`export-pilot --base-manifest`をdataset
ごとに渡すと両datasetを同じ新snapshotから増分更新する。CLIは実装sourceが属するrepositoryを固定し、
tracked worktreeがdirty、git identityが取得不能、unknown zero commitの場合にpublication buildを
開始しない。commit identityをoperator入力で上書きする経路は持たない。

```bash
uv run baibai-engine lake export-pilot \
  --sqlite stores/market/market.sqlite \
  --mirror <local-mirror> \
  --base-manifest <previous-daily-bars-manifest> \
  --base-manifest <previous-short-sale-manifest>
```

`coverage_status`は固定値ではない。daily barsは保存行のdate coverage、short sale reportsは
`source_coverage`の連続した`ok` windowをauthorityとしてsealed snapshotから判定する。hole、unknown、
`partial` / `failed`は`partial`となる。さらにpilot release policyはPhase 0 baseline
（[`2026-08-12 market lake baseline`](../../reports/studies/2026-08-12-market-lake-baseline/report.md)）の
history startを必須境界とし、観測rows・ticker populationの95%をregression floorにする。5%は訂正・
取消による減少を許容する幅であり、floor変更は新しいbaseline計測とpolicy reviewを要する。新鮮でも
1日・1rowだけのstore、leading history欠損、大幅なpopulation縮小はcurrent候補にならない。

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
転送し、各sourceをsealed copyへ固定してR2が検証する`Content-MD5`付きPUTを行う。MD5はtransport
checksumに限定し、logical identityには使わない。logical SHA-256、transport checksum marker、size、
content typeは同じimmutable PUTのmetadataへ固定する。PUT/reuse後にremote identityを検証し、さらに
pointer直前にreleaseから到達可能な全objectのHEAD closureを再検証する。bulk objectを毎回GETして
memoryへ展開せず、immutable conditional writeとtransport validationを信頼境界にする。最後に
`lake/pointers/l1/current.json` を ETag `If-Match` で切り替え、small pointerだけをGET read-backして
exact digestを検証する。409/412のCAS conflictはretryせずfail-closeし、current releaseを再解決する。

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
