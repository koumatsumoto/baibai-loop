---
title: "Market lake operations"
summary: "R2のimmutable L1 releaseをpublishし、固定releaseからmarket storeを復元する運用契約。"
doc_type: reference
status: active
---

# Market lake operations

この文書はmarket factのL1 publicationと固定release読みを定める。storeの所有者は
[`architecture.md`](../architecture.md#information-layers)、providerごとの意味とcoverageは
[`data-sources.md`](./data-sources.md)を正本とする。較正結果はlakeへ載せず、
`stores/screening/calibration/current.sqlite`のローカルsnapshotとして扱う。

<a id="market-lake-publication-contract"></a>
<a id="publication-contract"></a>

## 発行契約

market factはR2のcontent-addressed Parquet objectとして保持する。dataset manifestはpartition objectと
row数・schema・digestを列挙し、L1 release manifestは同時に使うdataset buildの集合を固定する。
`lake/pointers/l1/current.json`だけが可変で、それ以外のobjectとmanifestはimmutableである。

- partition grainはdataset契約が宣言し、row数から動的に変えない
- object key、SHA-256、byte数、row数、Arrow schemaを読取時に照合する
- releaseは`production` profileだけを持ち、required dataset・coverage・row/population floorを満たす
- prefix listing、glob、`union_by_name`、provider fallbackで欠損を補わない
- 一つのdatasetへ同時に二つのcanonical writerを持たない
- readerは開始時にcurrentを一度だけ解決し、以後は固定releaseだけを読む

L1 releaseに含まれないstore-local tableはSQLite側が所有する。hydrateはrelease対象tableを空にしてから
積み、manifest row数と一致した場合だけatomic replaceする。したがって、撤回済みrowや旧世代の残り物を
暗黙に引き継がない。

## 構築

`export-all`はSQLite backup APIでWALを含むsealed snapshotを一度作り、全datasetを同じsnapshotから
exportする。毎回全partitionを導出するが、object keyはcontent digestなので変化しないpartitionは同じkeyを
再利用する。snapshot bytesは一時入力で、lakeや監査archiveへ保存しない。

```bash
uv run baibai-engine lake export-all \
  --sqlite stores/market/market.sqlite \
  --mirror stores
```

dataset追加時は次を同じ変更で揃える。

1. `market/lake/datasets.py`のtyped契約
2. SQLite schemaとingest
3. release profileのrequired/optional、coverage、floor
4. export・resolve・hydrateのpositive/negative test
5. `data-sources.md`のsource意味

個別dataset専用の移行runnerは作らない。現行storeからreleaseを再構築し、hydrateで現行schemaへ満たす。

## 発行

publishはimmutable object、dataset manifest、release manifestの順に転送し、最後にcurrent pointerを
conditional PUTで切り替える。開始時pointerが動いていればconflictとして停止し、別writerのsuccessorへ
乗り換えない。pointer切替前にlocal closureをdigest・schema・row数まで検証する。

```bash
batch/scripts/r2_transfer.sh publish-lake
```

必要な環境変数は`R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、
`R2_SECRET_ACCESS_KEY`である。remote publishはローカルbuildと検証が成功した後だけ行う。
このcommandがcurrent pointerの解決、現行market storeのseal/export、CAS publish、local
`lake_store_origin`の更新を一続きで行う。個別release manifestを転送するlow-level moduleは、pointerが
未作成のbootstrapまたはcurrentと同一releaseのretryに限る内部primitiveであり、forward publicationには使わない。

rollback pointerや全履歴bytes監査は持たない。問題のあるreleaseを直すときは、正しいstoreから新しいreleaseを
前向きにpublishする。immutable prefixはBucket Lockで上書きと削除を防ぎ、mutable pointerとstagingは
lock対象外にする。

<a id="daily-cutover"></a>

## 日次切替

日次batchは次の順序でmarket storeを扱う。

| 段 | 処理 |
| --- | --- |
| `r2_transfer.sh pull-machine` | lake外のstore-local tableとoriginを取得 |
| `r2_transfer.sh hydrate-market` | current releaseからlake所有tableを復元 |
| `baibai-batch daily` | ingestとscreeningを実行 |
| `r2_transfer.sh publish-lake` | 新しいL1 releaseをpublish |
| `r2_transfer.sh push-machine` | lake所有tableを除いたstore-local copyを反映 |

publishはpushより先に行う。逆順ではcoverageだけが進み、factがpublishされない状態を作り得る。

<a id="fixed-release-read"></a>

## 解決と読み取り

```bash
uv run baibai-engine lake resolve --mirror stores
uv run baibai-engine lake resolve --mirror stores \
  --release <release-id> --manifest-sha256 <release-manifest-sha256>
```

named releaseはIDだけでは受理せずmanifest SHA-256を必須とする。次の不一致はすべてfail-closeする。

- pointerとrelease manifestのidentity
- release entryとdataset manifestのidentity・contract
- objectのSHA-256、byte数、row数、Arrow schema
- required dataset、coverage、row/population floor

readerはmanifestが列挙したobject keyだけをbounded batchで読む。remote readに必要なDuckDB extensionは
provisioning stepで事前にinstallし、runtime downloadへfallbackしない。

<a id="store-hydration"></a>

## Storeの復元

```bash
uv run baibai-engine lake hydrate \
  --mirror stores \
  --store stores/market/market.sqlite
```

hydrateは同一filesystem上のtemporary storeへ書き、`integrity_check`、foreign key、manifest row数を
確認してから`os.replace`する。途中失敗時は直前のstoreを保持する。current以外を使う場合は
`--release`と`--manifest-sha256`、またはtyped `--release-ref`で固定する。

## 保持とGC

retention rootはL1 current releaseだけである。そこから到達できるmanifest/objectは保持し、未到達objectと
stagingだけをgrace期間後の候補にする。

```bash
uv run baibai-engine lake inventory --root stores
uv run baibai-engine lake gc --mirror stores
uv run baibai-engine lake gc --mirror stores --apply --plan-hash <hash>
```

`gc`は既定dry-runで、apply時はwriter lock内で再planし、operatorが確認したplan hashと一致した候補だけを
削除する。較正storeはlake GCの対象外である。

## 較正store

calibrationは再生成可能なローカルSQLite snapshotである。panel、diagnostics、forward outcomeを
`current.sqlite`へまとめ、build完了後に一度だけatomic replaceする。generation graph、manifest、
pointer、CAS、remote publish、過去generation保持は行わない。

```bash
uv run baibai-engine screening calibration-build \
  --start <YYYY-MM-DD> --end <YYYY-MM-DD> \
  --sqlite-path stores/market/market.sqlite \
  --calibration-dir stores/screening/calibration
```

`--force`は対象cohortを現行入力で再計算する。snapshot contractが変わった場合は旧storeを移行せず、
完全なmarket storeから再構築する。

## セキュリティ

共有されるpublish report、通知、CI artifactへcredential、bucket URL、local pathを出さない。
operator-local CLIは復旧に必要なlocal pathを表示してよい。R2 tokenはreader、publisher、retention
finalizerで権限を分け、production bucketを試験用途に使わない。remote実装のprotocol確認はdeterministic
unit testとlocal fakeで行い、専用acceptance workflowやexact workflow auditを運用条件にしない。
