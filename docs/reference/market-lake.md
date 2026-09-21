---
title: "Market lake operations"
summary: "R2のimmutable L1 releaseをpublishし、固定releaseからmarket storeを復元する運用契約。"
doc_type: reference
status: active
---

# Market lake operations

この文書はmarket factのL1公開、固定releaseの読み取り、hydrateと保持を定める。storeの正本は[architecture](../architecture.md#store-authority)、sourceの選択は[data sources](./data-sources.md)、指標の意味は[valuation metrics](./valuation-metrics.md)が所有する。calibration結果はlakeへ載せず、`stores/screening/calibration/current.sqlite`に置く。

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

<a id="shared-raw-read"></a>

## 共有gatewayからの固定release読み

Webの共有認証から、次の順序で既存manifestとParquetをraw GETします。認証の意味は
[Web](../../web/README.md#共有read)、credential設定と受入手順は
[運用手順](../../batch/OPERATIONS.md#shared-read-setup)を参照してください。

1. `GET /api/lake/current`でexact `lake/pointers/l1/current.json`を一度だけ取得する。
2. pointerが指すrelease manifest keyを`GET /api/lake/object?key=<key>`へ渡す。
3. そのreleaseが列挙したdataset manifestを同じobject routeで取得する。
4. そのdataset manifestが列挙したpartitionだけを取得する。

object routeは`lake/manifests/releases/l1/`と`lake/manifests/datasets/`の`.json`、
`lake/l1/canonical/`の`.parquet`だけを許可します。pointerはcurrent routeだけで公開し、store snapshot、
application DB、staging、GCには到達できません。keyはquery parserで一度decodeした文字列として判定し、
callerがbucketやupstream hostを指定することはできません。

同一分析中にcurrentが更新されても乗り換えません。取得後のbyte数・SHA-256・row数・Arrow schemaを
manifestと照合する責務は利用側にあり、gatewayは再検証・変換・全体bufferingを行いません。
fixed releaseのobjectがGC等で404になった場合はその分析を止め、別releaseのpartitionで穴埋めしません。
再試行するなら新しい分析としてcurrentから取り直します。

HEAD、Range、listing、filter、SQL、provider fetch、JSON/CSV変換は提供しません。上流404は404、
上流redirect・認証エラー・5xx・通信失敗は内容を開示せず502、接続設定不足は503です。
JSONは`application/json; charset=utf-8`、Parquetは`application/vnd.apache.parquet`とcanonical objectのbasenameをdownload名として
返します（headerに安全でない文字だけ`_`へ置換）。すべてno-storeです。HTTP 200はdecode・分析成功の証明ではありません。

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

<a id="edinet-research-query"></a>

## Researchのセグメント・負債満期照会

最初に`l1.current`でreleaseを固定する。2 datasetはoptionalであり、`l1.describe`で存在を確かめる。導入前のreleaseも他datasetを読み続けられるが、存在しないdatasetへのqueryは行わず原典へ戻る。両datasetのpartitionは提出月、日付filterは`disclosed_on`である。企業別・期間別の行の存在は、questionの解決や全債務のcoverageを意味しない。

1. `l1.query`のsource alias `docs`に`edinet.documents`を指定し、[書類選択SQL](./queries/edinet-research-filing.sql)へ`ticker`と`cutoff`（JST日付、当日末まで）を渡す。sourceのfrom/toは保持したdocumentの全範囲を含める。狭い範囲にすると訂正のparentや取下げイベントを落とす。最新年次family内の提出版を先に選び、抽出済みかどうかで古い版へ戻らない。
2. `validity=usable`の書類だけについて、同じreleaseで下記のfact queryを行う。選択0件・`validity_unknown`・抽出行なしは原典確認へ戻り、訂正前の値で補わない。現行inventoryが履歴を確定できない情報修正・不開示・取下げも、選択familyに紐づく場合、またはfamily不明の年次イベントがそれ以降の年次に影響し得る場合はunknownに倒す。既知の別familyだけに紐づくイベントでは選択familyを止めない。年次familyに関連しない非年次イベントだけでは止めない。日中時点のPIT再現にはこの日次例を使わない。
3. 年次表の期末と現在日を併記する。`edinet.metrics`と合わせる際はtickerだけでJOINせず、metricsのasof・原典期間・連結basis・通貨を揃える。新しいBSとの時点差、事業再編、直近の借入れ・返済は別途確認する。

`edinet.metrics`の年次書類120/130には、売上債権・棚卸資産・仕入債務・契約負債・前受金・未払金のOCF寄与と、報告された有形・無形CapEx内訳をnullableで持つ。OCF内訳は報告符号、CapEx内訳はpositive expenditure magnitudeである。内訳を0と仮定した値は原因分解の感応度に過ぎず、「正常FCF」や維持投資の機械判定として扱わない。nullはゼロではなく、原典factの不存在または同一contextで一意に決められない状態を表す。

セグメントsourceを`segments`として、選ばれた`doc`をparameterに渡す。sourceの日付範囲は選択書類の提出日を含める。

```sql
SELECT period_start, period_end, consolidation_basis, segment_key, segment_name,
       metric, profit_basis, value, currency, source_locator
FROM segments
WHERE source_doc_id = $doc AND segment_kind = 'segment'
ORDER BY period_end, segment_key, metric
```

負債sourceを`debt`として読む。nullを合計で黙って無視せず、categoryごとの欠測bucketも確認する。categoryがない場合は債務0としない。

```sql
SELECT balance_sheet_date, consolidation_basis, debt_category,
       due_from_months, due_to_months, principal, currency, source_locator
FROM debt WHERE source_doc_id = $doc
ORDER BY balance_sheet_date, consolidation_basis, debt_category, due_from_months
```

原典に戻る範囲とsource意味は[data sources](./data-sources.md#edinet-research-facts)が所有する。ここから資金余命を評価する際の将来FCF・維持投資・借換えはResearchの見積りであり、L1 factへ保存しない。
