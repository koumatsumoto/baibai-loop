---
title: "Market lake operations"
summary: "R2 の immutable object と manifest で market fact を publish し、固定 release から market store を満たす運用契約。"
doc_type: reference
status: active
---

# Market lake operations

authority、manifest、version 語彙は [`../architecture.md`](../architecture.md#market-lake-publication-contract)
を正本とする。この文書は publish と read の実操作を持つ。

L1 は `market.sqlite` の 17 table を持ち、R2 が持つ `market.sqlite` は残る 2 本だけを運ぶ
（[Daily cutover](#daily-cutover)）。その 2 本は L1 に入らない — `tse_capital_policy_snapshots` は
operator が導出したもので fetch の蓄積ではなく、key merge すると撤回した行が復活する。
`source_coverage` は取得範囲の帳簿であって fact ではない。**不足 dataset を store の残り物で暗黙に
埋めない** — hydrate は積む前に対象 table を空にし、積んだ行数が release manifest の publish 行数と
一致しなければ失敗する。この「空にしてから積む」順序は撤回した行を次世代へ持ち越さないので、
operator が導出する dataset でも L1 へ載せれば key merge の危険は無くなる。

partition の粒度は dataset 契約が宣言する。行数から導出しない — reader は manifest の layout を
契約と突き合わせるので、行数由来だと table が育った日に layout が無言で変わり reader が release を
拒否し始める。行数は粒度を選ぶ根拠であって機構ではない。

| grain | dataset | 月あたり行数 |
| --- | --- | ---: |
| month | `jquants.daily_bars` | 84k |
| month | `edinet.metrics` | 46k |
| month | `jquants.weekly_margin` | 17k |
| month | `jquants.short_sale_reports` | 11.7k |
| month | `edinet.documents` | 6.8k（古い行の lifecycle 更新で書き直しが起きるため細かく） |
| month | `jquants.all_issues_daily_margin` | 2026-09-28 から全銘柄日次 |
| year | `jquants.master_snapshots` / `jquants.fin_summaries` / `edinet.buyback_reports` / `edinet.document_lists` / `jquants.market_calendar` / `jquants.earnings_calendar` / `jquants.margin_alerts` / `jpx.regulation_flags` / `jpx.regulation_sources` / `jpx.delistings` / `edinet.tender_offer_exit_values` | 6〜4.9k |

行を持たない dataset は export が飛ばす。`jquants.all_issues_daily_margin` は JPX の公表制度変更
（2026-09-28、初回は 9/25 残高）を待っているので今は 0 行で、canonical build に partition が無いと
release 入力にならない。飛ばすことで「まだ始まっていない」と「build が失敗した」を分ける。

## Build

初回 seed は全期間を export する。現行 provider / Premium backfill は coverage を SQLite に
commit し、lake export はその SQLite を `legacy_sqlite_import` として月 partition へ変換する。
provider 取得と Parquet writer の二重 canonical write は行わない。contract v1はfixed legacy
SQLite snapshotからauthorityを移すcompatibility boundaryであり、provider responseにしかないfield、
decimal precision、publication / effective / retrieved time、revision/cancellation semanticsを完全には
表さない。これらのmappingを確定しprovider→canonical semantic parityを満たした時点をv2 rebuild
triggerとする。

`export-all`は開始時にSQLite backup APIでWALを含むsealed snapshotを1回作り、snapshot digest・
schema version・`quick_check`を確定してから、全datasetのexport、source-state、parityを同じsnapshotから導出する。
release作成時にも全partitionが全datasetでexactに1 snapshot generationへ閉じることを検証する。snapshotはoperationの一時入力であり、bytesはlakeにもremote closureにも残さない。manifest
が残すのはschema version・content digest・capture時刻という素性だけで、同じstore世代を持っているか
どうかはre-sealして digest を突き合わせれば答えられる（unchangedなstoreに対してsealはbyte決定的）。

日次buildごとにfull SQLiteをR2へ再送しない。restore checkpointはlakeのimmutable object graph
そのものであり、release manifestが全partitionのkey・digest・rowsを列挙するので、release一つから
storeを組み直せる。

```bash
uv run baibai-engine lake export-all \
  --sqlite stores/market/market.sqlite \
  --mirror <local-mirror>
```

中断した Premium CSV / API backfill は既存 `source_coverage` から再開する。直前 manifest を
渡すと SQLite facts + coverage の state hash を比較し、commit 済みの追加・訂正月だけを
export して未変更月を再利用する。transform fingerprintはschema/configに加えてwriter・dataset
contract sourceのdigestを含む。CLIは実装sourceが属するrepositoryを固定し、tracked worktreeがdirty、
git identityが取得不能、unknown zero commitの場合にbuildを開始しない。

```bash
uv run baibai-engine lake export-all \
  --sqlite stores/market/market.sqlite \
  --mirror <local-mirror> \
  --base-manifest <previous-daily-bars-manifest> \
  --base-manifest <previous-short-sale-manifest>
```

`coverage_status`は固定値ではない。完全性は多くのsourceで行から導けない — 提出されなかった書類と
取得しなかった書類は同じ不在を残すので、取得記録が答える。daily barsだけが例外で、全営業日が全市場分の
行を負うため行自体が答える。どちらも持たないsourceは`unproven`として、持っているものは言えるが全部
持っているとは言えない状態を表す。dataset契約が`coverage_authority`でこれを宣言する。

release policyはdatasetごとに、必須性・history境界・rows / population floor・完全性要求・鮮度窓を
持つ。cadenceはdatasetの性質であってprofileの性質ではない — 週次残高と日次barは watermark が2週間
離れていても両方currentで、profile単一の上限は最も遅いdatasetに合わせるしかなく、その時点で最も速い
datasetについて何も言わなくなる。`max_lead_days`は先取り公表を表す（market calendarは未到来の営業日を、
earnings calendarは未発表の announcement を名乗る）。watermark同士のskew上限は持たない — 各watermarkを
同じ評価日に対して自分の窓で測っているので、更新の止まったdatasetは自分の窓が既に拒否する。

floorは観測rows・populationの95%をregression floorにする。新鮮でも1日・1rowだけのstore、
leading history欠損、大幅なpopulation縮小はcurrent候補にならない。population floorを持つdatasetが
populationを報告しなければ、checkをskipせず停止する。日や書類を行とするdatasetにpopulationの問いは
無いので、そこでは floor も報告も持たない。

検証済み dataset manifest を release に固定する。

```bash
uv run baibai-engine lake release create \
  --mirror <local-mirror> \
  --dataset-manifest <manifest> ...    # export-all が出した manifest すべて
```

`--dataset-manifest` は release policy が required とする dataset を全て満たす必要がある。欠けた
まま作ると release 検証が「required dataset を欠く」で停止する。

### local pipeline の実測

remote への転送が差分でも、local 側は毎 run sealed snapshot を作り、affected month を
判定し、全 history の SQLite ↔ Parquet parity を検証する。その時間は主張ではなく計測で持つ。

```bash
uv run python -m tools.diagnostics.benchmark_l1_export \
  --sqlite stores/market/market.sqlite --report <report.json>
```

production store（1,812,189,184 bytes、schema v23、snapshot digest `ef791840…`、14,751,189 rows）
を Linux/WSL2 の一時 directory で実測した結果は次のとおり。

| 局面 | wall time | 生成 object | 生成 bytes |
| --- | --- | --- | --- |
| full export（16 dataset・全 partition） | 359.9 秒 | 448 | 300,587,037 |
| 1 か月訂正の再 export | 125.7 秒 | 1 | 1,144,400 |

peak RSS は 1,030,107,136 bytes（982 MiB）。`jquants.all_issues_daily_margin` は JPX の公表制度
移行まで行を持たないので、export は 17 dataset のうち 16 を書く。

日次 build も base と current SQLite の全 partition を PK 順の row hash と period に clip した
coverageで比較する。したがって日次 window の外側の訂正・削除も次の publish でaffectedとなり、最後の
rowを失った月はmanifestから消える。**parity の Parquet 再導出は build が書いた月だけを見る。** carry
できるのはこの全比較で同一と証明済みのpartitionであり、content-addressed objectを再生成しない。上表の
2行がその差で、1か月の訂正は全量の2.9分の1で済み、生成objectは448分の1になる。`--audit` はcarry
した月もParquetを再導出してSQLiteとのparityを問う明示検査で、mutationを初めて検出する入口ではない。

この計測は commit ではなく実装 digest（writer / models / immutable / snapshot / benchmark tool）へ
結ぶ。それらに触れない変更では証跡は有効なままで、触れた変更は再計測になる。

<!-- AP-02: full=359.8542985210079 秒、incremental=125.72119240899337 秒、
peak RSS=1030107136 / 1048576 = 982.39 MiB、
source sha256=ef79184082ce1e82177d5bffac58e7336eff757f4a41c15d00a17fe461f89a2e、
implementation sha256=20c9cea642c9be2bc2f505d40abe56c39933159952240e73304d87714385d39a、
producer commit=85d3dbdc13c67b05a018448e1be17710ddcb869e、recorded=2026-08-19T23:32:19Z。 -->

## L1 dataset を追加する

新しい table を L1 へ載せる作業が触る場所と、忘れたときに何が言うか。

| 触る場所 | 忘れると |
| --- | --- |
| `market/lake/datasets.py` の `LakeDataset` 定義と `LAKE_DATASETS` | 起点なので忘れられない。要点は下段の fingerprint 規約 |
| `market/lake/models.py` の `PRODUCTION_RELEASE_POLICY` へ `ReleaseDatasetPolicy` 1 件 | `test_every_lake_dataset_states_a_release_policy` が落ちる |
| `market/sqlite/schema.py` と `market/sqlite/migrations.py` の table | `tests/batch/test_cloud_merge_market_store.py` が落ちる。新 table を lake 側か merge 側かに分類するまで通らない |
| provider が `market/sqlite/coverage.py` へ記録する `source_coverage.source` と dataset の `coverage_authority` | 何も言わない。既定の `source_coverage` はその帳簿を読むので、名前がずれた dataset は `partial` を名乗り続ける（`coverage_source` で宣言できる） |
| 本書の grain 表と [`../../stores/README.md`](../../stores/README.md) の table 数 | 何も言わない。ここが唯一の備忘 |

hydrate / dehydrate に個別作業は無い。どちらも `LAKE_DATASETS` から従い、積んだ行数が release
manifest と合わなければ [Store hydration](#store-hydration) が fail-close する。

**`datasets.py` は `transform_fingerprint` の 3 file の 1 つなので、この merge は full rebuild
release の publish までが 1 つの作業である。**手順は
[`AGENTS.md`](../../AGENTS.md#store-の正本とクラウド反映) と
[`batch/OPERATIONS.md`](../../batch/OPERATIONS.md#fingerprint-変更後の-full-rebuild) を正本とする。

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
日次remote bytesはchanged Parquet/manifestへ比例する。最後に`lake/pointers/l1/current.json`を
開始時に検証したETagの`If-Match`（開始時に不在なら`If-None-Match: *`）で切り替え、終了時に見えた
successorのETagへ乗り換えない。pointer HEADのidentity metadataとGET bytesも開始時に照合するため、
export中に別writerがcurrentを動かせば409/412でfail-closeする。remote adapterはin-processのboto3
S3 clientで接続を再利用し、requestごとのprocess起動を行わない。各attemptのread timeoutはobject sizeから導く
（botocoreのretryを含むoperation全体のdeadlineではない）。downloadは同一directoryのtemporary fileへ
書き、expected sizeとfsyncを確認してからatomic replaceする。manifest cacheが期待digestと異なる場合は、
remote bytesをprivate temporaryへ再取得し、digest一致後だけcacheを置換する。
（base 120秒 + 実測を下回る4 MiB/秒での転送時間）。成功時はstderrへ
`lake publish phases: base_resolve=... seal_plan_export=... release_create=... local_graph=... remote_closure=... pointer=...`
を1行出し、stdoutはrelease recordに使うJSON 1行だけを維持する。

incremental export は開始時 current pointer と、exportに使う同じsealed SQLite snapshot内の
`lake_store_origin`を照合し、digest chain を検証した dataset manifest の private snapshot から unchanged
partition を carry する。sidecarはSQLiteと分離してrestoreできるためauthorityにしない。full rebuild は
baseをcarryしないが、同じsealed snapshotのembedded originをcurrent pointerと照合する。currentとoriginが
ともに無いfirst publicationだけは例外である。

exportが返したin-memory manifestはcanonical bytesにしてmirror配下のpublication-private directoryへ
固定し、release作成はそのpathだけを読む。releaseも作成時payloadのSHA-256をpublisherへ渡し、pathが
差し替わっていればpointer切替前に拒否する。全dataset共通のprevious-release row floorは、正常に縮小する
snapshotや取消・訂正と両立しないため持たない。欠損はdatasetごとのproduction policy（history境界、
minimum rows / population、coverage、freshness）で拒否する。

conditional pointer PUTや直後のHEAD/GETが失敗した場合はcurrentを再読込し、exact targetなら成功、別
identityならconflict、読めなければunknown outcomeとして停止する。low-level publisher CLIがcurrentを
変更できるのはfirst publicationだけで、currentとexact targetが一致する場合はretryとして成功する。

**pointerはcurrentだけを名乗る。rollbackは無い。** 修理は前へ publish することであり、store が
serve をやめた世代へ戻ることではない。pointer が「この世代は復元できる」と名乗れば、それは publish の
たびに検証し続けなければならない約束になり、実際そうしていた。local mirror が graph 全体を持ち、
writer が 1 つしかないこの構成では、悪い release を publish したときの復旧は良い release を publish
することである。同じ release ID の再 publish だけは中断した publication の retry として受け付け、
identity が違えば拒否する。

**immutable prefix は Bucket Lock で守る。** `lake/l1/canonical/` と `lake/manifests/` へ
[R2 Bucket Lock](https://developers.cloudflare.com/r2/buckets/bucket-locks/) を age-based で設定し、
削除と上書きの両方を拒否させる。mutable な `lake/pointers/`、GC が回収する `lake/staging/`、bucket
直下の store key は対象にしない。lock 期間は「到達不能 object の R2 側削除は満了を待つ」という
retention 設計と整合する長さにする — lock を外して即時削除する運用は取らない。現在の設定は読み取りで
確かめる:

```bash
npx wrangler r2 bucket lock list baibai-stores
```

R2 の [S3互換checksum](https://developers.cloudflare.com/r2/api/s3/api/#checksum-types)は
full-object SHA-256 を提供しないため、existing object の再利用は content-addressed key、immutable
PUT metadata、Bucket Lock、reader の SHA-256 検証、`--verify-bytes` 監査の組合せで閉じる。設定には
account 単位の権限が要り、日次の publisher token では設定状態を読めない
（`GetObjectLockConfiguration` が `AccessDenied`）。

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

## Daily cutover

日次バッチは lake から store を作り、lake へ publish して終わる。`market.sqlite` 全体の
GET / backup copy / PUT は発生しない。

| 段 | 何をするか |
| --- | --- |
| `r2_transfer.sh pull-machine` | `market.sqlite` を GET する。R2 の copy は lake が持たない 2 本だけを持つ |
| `r2_transfer.sh hydrate-market` | current release を解決し、lake 所有 17 本を store へ積む |
| `baibai-batch daily` | ingest は store へ書き、screening は store を読む。lake は経路に入らない |
| `r2_transfer.sh publish-lake` | 変わった partition だけ export → release → pointer を CAS で切り替え |
| `r2_transfer.sh push-machine` | push 用 copy から lake 所有 17 本を空にして PUT する |

**publish は push より先に置く。** 逆順で publish に失敗すると、クラウドには「今日の coverage を
主張する store」だけが残る。coverage が「取得済み」と言う限り次の run はその範囲を取りに行かないので、
穴が自力で塞がらない唯一の組み合わせになる。

**R2 の key は `market.sqlite` のままにする。** store の同一性は変わっていない — schema version も
19 本という構成も同じで、変わったのは 17 本の権威が lake へ移り、pull のたびに hydrate が復元する
という点だけである。

**両側とも行数で fail-close する。** hydrate は release manifest が publish した行数と一致しなければ
失敗する。積み損ねた store をそのまま screening へ渡すと、空の universe が健全な結果として publish
されるためで、これが cutover が持ち込む唯一の新しい失敗経路である。dehydrate は逆向きに同じ一致を
要求し、release が持たない dataset に行があれば拒否する — registry にあるが release にまだ無い
dataset を空にすると、どの release からも戻せない行を落とすことになる。

dehydrate が走るかどうかは `lake/pointers/l1/current.json` の有無で決まる。権威が lake にあることは
pointer が宣言しているので、それを読む。pointer が無い bucket（初回 seed）では store がまだ唯一の
複製であり、pointer がある以上は空にすることが必須になる。

mirror は `stores/lake/` に置く。key が全て `lake/` で始まるので mirror root は store が並ぶ
directory 自身であり、`--mirror stores` と渡す。mirror は immutable object の fetch-through cache
なので、消しても release から作り直せる。

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
変換すると build が終わる前に memory を使い切る。partition（1 か月）ごとに object を検証してから
その中を batch で流し込み、hydrate の R2 読みでは worker が消費順の先の object を bounded buffer
（既定 16 object）へ先読みする。peak memory は dataset の大きさではなく batch 幅と buffer 上限に
従い、検証・install・transfer 計数は先読みの有無にかかわらず消費側 thread の同じ経路を通る。

## Store hydration

固定 release を SQLite へ実体化するのは hydrate である。`market.sqlite` の lake 所有 17 table を
空にして release の object から積み直し、他の 2 table と schema はそのまま残す。store は満たされた
後も ingest が書き続けるので、契約から導いた形ではなく store 自身の schema — 書き込み時の制約と
index — を運ぶ必要がある。両者は実際に違う（store だけが `week_end` を制約し、契約が宣言しない
secondary index を持つ）ので、契約側の形で作った store は本物が拒否する行を黙って受け入れる。

```bash
uv run baibai-engine lake hydrate \
  --mirror stores \
  --store stores/market/market.sqlite
```

current以外は`--release <id> --manifest-sha256 <sha256>`、または typed release ref fileを渡す
`--release-ref <path>`で固定する。IDだけのhydrateは受理しない。publishしていないlocal mirrorには
current pointerが無いので、初回のhydrateは必ずこのどれかでreleaseを名指す。

`--bucket baibai-stores` を足すと、mirror に無い object だけを R2 から取得して mirror へ
content-addressed に格納する。object key は content hash なので、変わらなかった partition は
既に手元にあり転送量に乗らない。出力の `fetched_bytes` / `reused_bytes` がその内訳になる。

R2 読みは worker（既定 8、各自の DuckDB session）が消費順の先の object を bounded buffer へ
先読みし、stderr に 2 種類の行を出す。`lake prefetch served X/Y planned objects (Z already
mirrored)` は毎回出る要約で、cold fill は served ≈ Y、warm fill は served 0 + mirrored ≈ Y と
読む。`lake prefetch disabled; reads continue sequentially: ...` は worker 側の失敗や待ちの
超過で逐次読みへ退化したときだけ出る — fill は正しく完走するが遅くなるので、hydrate の
所要時間が戻った run ではまずこの行の有無を見る。

hydrate は store の sealed copy へ書き、1 回の rename で公開する。途中状態が読まれることはなく、
失敗しても直前の store は壊れない。current modeでは成功を返す直前にcurrent pointerのfull identityを
問い直す。identity は名前ではなく digest まで見る — 同じ release ID で別の bytes を再 publish した
recovery が、名前比較なら通ってしまうためである。

積む前に table の列並びを dataset 契約と突き合わせ、違えば load 前に停止する。契約はこの schema から
導いたので、両者が一致していることが fill の前提である。secondary index は store 自身の DDL から
読み取って落とし、load 後に同じ DDL で作り直す — index を張ったまま load すると load 自体の数倍
かかる一方、契約側から index を発明することも store の index を失うこともない。primary key の
auto index は DDL を持たず落とせないので残り、それが load の key semantics を保つ。

開始前に同一 filesystem の空き容量を要求する。必要量は 64 MiB、現 store の bytes、published object
bytes の 8 倍のうち最大で、真ん中の項は「一時 copy は store の複製として始まり満たされて終わる」
ことから来る。倍率は実測（release 300,587,037 bytes の Parquet に対し store 1,812,189,184 bytes、
6.03 倍）の上に置く。

hydrate の atomic publication が対応する filesystem は、case-sensitive で hard link、同一 directory
内の `os.replace`、file fsync、directory fsync を提供する Linux / WSL 上の local POSIX filesystem
（CI の ext4/overlayfs を含む）である。開始前に小さな probe file でこれらを検査する。temporary と
destination は必ず同じ directory に置く。NFS/CIFS、FUSE/DrvFS、directory fsync を提供しない
filesystem、Windows native path は未対応であり、market store の置き場に使わない。replace 失敗時は
temporary を除去して直前の store を保持する。

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
semantic dependency closureのAST digestとforward observation policyを含む`transform_fingerprint`、
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

closureの各moduleは、bytesではなく位置とdocstringを落としたASTのdumpをhashする。コメント・
docstring・整形は行の値を動かせないのに、bytes hashではhash対象の31%を占めて全cohortを捨て
させる。parseはbytes hashの216倍かかり、fingerprintはpublishするpartitionごとに取られるので、
digestはsource text自体をkeyにmemoiseする。依存のversionは互換境界（major.minor）までを入れる。

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
立てる。

**cohortは1つの読みを2通りに名乗る。** sealed SQLite snapshotが「どのbytesを読んだか」、L1 releaseが
「それをどこで読み直せるか」で、水準を決めるのは後者があるかどうかである。最弱で決める規則にすると、
読みの記録であるsnapshotが「その読みは再現可能だ」という主張を打ち消すことになる。この読み替えが
成り立つのは、cohortが読むtableが全部lake所有だからである（`jquants.daily_bars`・`fin_summaries`・
`master_snapshots`・`edinet.metrics`・`jpx.delistings`・`edinet.tender_offer_exit_values`）。lakeが
運ばない入力を取るようになったら最弱へ戻す必要がある。

**releaseの名前はhintで、証明は行の突合である。** buildはreleaseを名乗られてから、lake所有17 tableを
そのreleaseが公表するtotalsと1つずつ突き合わせ、全部一致したときだけcohortにそれを述べさせる。
releaseが省くdatasetはstore側も0行でなければならない。名前が違う・世代が古い・publishしていない行を
storeが持つ、のいずれもcohortを`trace_only`のままにする。

**cohortのlineageは別のmirrorを指す。** 較正storeが持つのはL2 objectと自分のmanifest・pointerで、
L1 releaseはmarket mirrorにある。解決先はkeyのnamespaceで決まり、`lake/manifests/releases/l1/`は
buildが渡したmarket mirrorが答える。`lake gc`は自分がpublishしていないnamespaceのsourceを
到達可能にも未解決にもしない — 前者は持っていないbytesを守ると称することになり、後者は別storeの
事実で較正のsweepを止めることになる。ただし不在だけを理由にはしない: そのnamespaceをpublishしている
mirrorでは、解決できないsourceは未解決として報告する。

cohortのinput cutoffとsealed snapshot identityが保証するのは**どのstore世代を読んだか名指せること**
であって、その値が当時同じ形で入手できたことではない。J-Quantsのadjusted price、master、JPX flagは
revisionを含み、完全なvintageではない（[`data-sources.md`](./data-sources.md)）。較正結果を
live deploy可能なhistorical alphaとして読まず、PIT不完全なfieldに依存するmetricはその前提込みで
保守的に解釈する。

retention の root は 2 種類で、そこから到達できる object は齢によらず残す。

- calibration bundle の current（3 datasetの完全closure）
- L1 の current release

`gc` は mirror の形をした directory であれば何に対しても回せるので、この 2 root はどちらも live で
ある。**較正 store（`stores/screening/calibration/`）自身が bundle root を持つ mirror** で、
`lake gc --mirror stores/screening/calibration` は pointer から 3 dataset の closure を辿って全 object を
到達可能にする。bundle root を「R2 publish 用」と読むと、この store 全体が 30 日の grace の後に
削除候補へ変わる。検証は下の dry-run で `roots` に pointer が出ることを見る。

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
差替えのいずれでも削除を拒否する。publish直後の`--apply`は、前世代を固定して読んでいる実行中の
runからその世代を外し得る — 結果はfail-closeの一時errorで、再実行すれば新しいcurrentを読む。

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
| `workspace` | in-flight staging と失敗 build が残したもの。どのmanifestにも属さない | 20 GiB |

budget は class ごとに持ち、合算では持たない。単一の数字で報告すると大きい方の budget が小さい方の
超過を隠すからで、published graph が目標を超えても失敗 build が 2 GB 積んでも、合算では何も
警告しない。

GC の候補は current closure から未到達な object だけで、grace 期間を過ぎたものを同じ plan hash へ
固定し、1 回の sweep で local mirror から削除する。R2側の削除はBucket Lock
満了後にDelete専用retention finalizerが同じcandidate identityを検証する運用境界とする。

**L2 calibration はローカル資産で、R2 へ publish しない。** 較正 store は market/ledger evidence から
再生成できるローカル成果物で、cloud 側にこれを読む consumer が居ない。R2 に calibration bundle
pointer は存在せず、bundle を出す publish 経路も持たない。読者が現れた時点で設計し直す。

ローカル資産でいられる理由は consumer が居ないことであって、cohort source の保証水準とは無関係で
ある。水準の話は次段を読む。

**保証の範囲**: cohort は読んだ bytes（sealed snapshot）と読み直せる場所（L1 release）の両方を
名乗るので、`rebuildable_input` である。ただし **retention が根として固定するのは current release
だけ**で、cohort が名乗る過去世代は根を持たない。object は content-addressed なので、current
release が同じ bytes を名乗る限り到達可能であり続けるが、その世代にしか無い object が残ることを
lake は約束しない。較正 store の sweep はこの点について沈黙する — `lake gc --mirror
stores/screening/calibration` の `roots` は bundle pointer 1 つで、L1 release は較正 store が
publish していない namespace だからである。

current bundleに問題がある場合、直すのは前へ build することである。local storeで作り直した
generationを `calibration-build` が publish すれば、bundle pointer は 1 回のCASでそれを指す。
これは較正 store 内で完結する操作で、R2 は関与しない。

旧CSV storeからの移行機構は持たない。**旧 store を捨てて全 cohort を再構築する。** 実測では、
rulesがその間に動いているため旧storeのcohortは1件もそのまま使えず、移行を作っても達成するのは
「現行codeが読めないbytesを新store内に保存する」ことだけだった。読み返せず・混ぜられず・
再計算もできないbytesは、定義上ゼロ価値である。旧rulesで測った過去の計測値は失われるが、
それは設計自身が「旧rulesのcohortを現行集計に混ぜない」ために拒否していたものである。

出力へ何を出さないかは、その出力が誰の手に渡るかで決まる。**共有される成果物** — remote publish
report、Discord通知、CI artifact、そこへ載るerror — にはcredential、account ID、bucket URL、
そしてlocal filesystem pathを出さない。publish reportがrelease ID・pointer ETag・転送counterだけで
できているのはこのためである。**operator-local CLI**（`inventory`、`release`、`hydrate`、
`dehydrate`、immutable installのerror）はlocal pathを出す。operatorが次に触るのはその
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
実PUT、pointer read-back、前へのre-publishによる実CAS pointer switch、stale ETagの
412/409 fail-closeに加え、tiny L1 publish→remote closure download→reader→hydrate、2 GiB objectの
PUT/read-back時間、同じsize/偽SHA metadataを持つtampered bytesの拒否、wrong credential errorのredactionを
検査する。
credentialはvalidation/setupへ渡さず、actual R2 stepだけが専用publisher tokenを持つ。production-size
exportは上記reference acceptance reportをhead SHAと一緒に保存する。production bucketとcanonical storeをacceptanceに使わない。

**acceptanceは専用bucketと専用tokenだけで走る。** workflowは `R2_LAKE_ACCEPTANCE_BUCKET`
variableと `R2_LAKE_ACCEPTANCE_ACCESS_KEY_ID` / `R2_LAKE_ACCEPTANCE_SECRET_ACCESS_KEY` の 2 secretを
読む。tokenはその bucket だけへ Get/Head/Put/Delete を持つ。**日次のpublisher tokenで代用しない** —
acceptance bucketへは403を返すうえ、role分離の設計がその代用を禁じている。secretが揃っているかは
dispatchで分かる: `Validate exact head without credentials`までは通り、揃っていなければactual R2 stepが
`required environment variable is missing: R2_ACCESS_KEY_ID`で停止する。

```bash
gh secret list | rg R2_LAKE_ACCEPTANCE
gh variable list | rg R2_LAKE_ACCEPTANCE
```

acceptanceが走らせられない間、lakeのread経路を変える変更は実bucketに対する
publish→download→hydrateのround tripで確かめる。
