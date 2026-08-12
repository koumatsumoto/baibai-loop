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
provider 取得と Parquet writer の二重 canonical write は行わない。

```bash
uv run baibai-engine lake export-legacy \
  --dataset jquants.daily_bars \
  --sqlite stores/market/market.sqlite \
  --mirror <local-mirror>
```

中断した Premium CSV / API backfill は既存 `source_coverage` から再開する。直前 manifest を
渡すと SQLite facts + coverage の state hash を比較し、commit 済みの追加・訂正月だけを
export して未変更月を再利用する。transform fingerprint が違う base は再利用せず拒否する。

```bash
uv run baibai-engine lake export-legacy \
  --dataset jquants.short_sale_reports \
  --sqlite stores/market/market.sqlite \
  --mirror <local-mirror> \
  --base-manifest <previous-dataset-manifest>
```

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
転送し、最後に `lake/pointers/l1/current.json` を ETag `If-Match` で切り替える。CAS conflict
は retry せず fail-close し、current release を再解決して build をやり直す。

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
uv run baibai-engine lake resolve --mirror <local-mirror> --release <release-id>
```

`--release` を渡すと pointer を一切読まない。rollback と pin 済み study はこの経路で読む。

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
拒否する。extension の autoload / autoinstall は切ってあり、HTTP へ出られるのは credential を
渡したときの明示 `INSTALL httpfs` 経由だけである。credential は非 persistent secret として bind
parameter で渡し、SQL 文・例外・metadata に残さない。

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

`--bucket baibai-stores` を足すと、mirror に無い object だけを R2 から取得して mirror へ
content-addressed に格納する。object key は content hash なので、変わらなかった partition は
既に手元にあり転送量に乗らない。出力の `fetched_bytes` / `reused_bytes` がその内訳になる。

再利用は完全一致でだけ起きる。`release_id`、release manifest digest、全 dataset manifest digest、
全 object digest、projection contract fingerprint、producer commit のいずれかが違えば再構築する。
`built_at` は identity に含めない（同じ入力の 2 回の build で必ず違い、含めると再利用契約が
成立しないため）。build は一意な一時 file へ書いて 1 回の rename で公開するので、途中状態が
読まれることはなく、失敗しても直前の projection は壊れない。

<a id="l2-calibration"></a>

## L2 calibration builds

calibration の cohort（panel・panel diagnostics・forward outcome）は typed Parquet の L2 dataset
として publish する。Arrow schema は `PanelRow` / `PanelDiagnostics` / `ForwardReturnRow` から
導くので、行の契約と保存列がずれない。partition は cohort の as-of の `year/month`。

cohort を 1 つ書くと immutable な新 build を publish し、dataset pointer を CAS で進める。
書き換わるのはその cohort の月の partition だけで、他の月は publish 済み object をそのまま
引き継ぐ。`calibration-build` は cohort ごとにこの経路を通るので、再構築の単位は partition に
なる。

build が記録する identity は `source_release_id`、`producer_git_commit`、`transform_fingerprint`、
`contract_version`、partition と object の hash である。calibration の入力がまだ legacy store から
読まれる間、`source_release_id` は L1 release ではなくそれを明示する固定値になる（束縛していない
release の id を書くと、build が持っていない provenance を主張することになる）。読み取りは transform fingerprint 不一致、
source release 不在、schema 不一致、object digest 不一致をすべて fail-close する。contract を
変える場合は published object を書き換えず、新しい `contract_version` の immutable build を作る。

retention の root は 3 種類で、そこから到達できる object は齢によらず残す。

- 各 L2 dataset の current build と previous build
- L1 の current release と previous release
- 明示 pin

```bash
uv run baibai-engine lake pin create \
  --mirror <local-mirror> --pin-id <id> \
  --build <build-id> --dataset calibration.panel \
  --reason "adopted as calibration evidence" --owner <owner>

uv run baibai-engine lake gc --mirror <local-mirror>
uv run baibai-engine lake gc --mirror <local-mirror> --apply --plan-hash <hash>
```

L2 の root は `lake/pointers/l2/` を列挙して store から導くので、dataset 名を挙げる必要はない。
`--l2-dataset` はその dataset に current pointer が在ることを要求する追加の assertion で、
無ければ root 未解決として扱う。

`gc` は既定が dry-run で、root closure と削除候補と plan hash を出す。`--apply` はその plan hash
を要求するので、別の状態で作った計画は適用できない。root が 1 つでも解決できない場合は削除を
拒否する（読めない pointer や、object は在るのに pointer が無い状態を「root が無い」と扱うと、
それが守っていたものが未参照に見える）。

Raw archive はこの sweep の対象外である。保持の判断が manifest 到達性ではなく retention class と
齢で決まるので、到達性の sweep が候補に挙げてはならない。

R2 への publish は L1 と同じ順序で、object と manifest を `If-None-Match: *` で immutable に
転送してから `lake/pointers/l2/<dataset>/current.json` を `If-Match` で切り替える。

```bash
uv run python -m baibai_batch.storage.lake_publish \
  --mirror <local-mirror> \
  --l2-manifest <dataset-manifest>
```

L2 契約より前に書かれた CSV cache は `calibration.legacy_csv` の read-only adapter で読める。
adapter は書き込まず、そこから build も作らないので、cohort の正本は publish 済み build だけで
ある。

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
