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
- coverageはdatasetごとの運用判定である。日次株価は保存日の端点・間隔による再取得判定を使い、全営業日・全銘柄の網羅を証明しない。row/population floorも別の下限検査であり、完全性の証明とはしない
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

1. `storage/lake/datasets.py`のtyped契約
2. SQLite schemaとingest
3. release profileのrequired/optional、coverage、floor
4. export・resolve・hydrateのpositive/negative test
5. `data-sources.md`のsource意味

`jquants.fin_summaries`はcontract version 3で、主キーは`ticker / disclosed_at / record_key`である。索引用の3列と、提供元recordの全fieldを保持する必須`document_payload`だけを保存する。APIからdecodeしたrecordやBulk CSVから読んだrecordをJSON化したものであり、原HTTPレスポンスやCSV bytesそのものとは呼ばない。空欄・当期/翌期slot・未使用fieldも保持し、財務数値の展開列を別の正本にしない。

DocNoがある文書はその番号、未取得ならpayload digestで識別する。同じidentityの同一recordは一度だけ保存し、内容が異なる場合は入力順で選ばず拒否する。digestやDocNoはrevision順序ではない。型変換・当期/翌期予想の選択・財務計算は読込後の共通純粋投影で行う。予想の意味は[Valuation metrics](./valuation-metrics.md#21-採用ソース)、物理schemaは[観測store](../../engine/src/baibai_engine/storage/observations/schema.py)を正本とする。

旧contractのreleaseは対応codeと組み合わせて保持し、runtimeの旧schema読替え・自動移行は行わない。原本を欠く旧flattened行を原本recordに見せかけて変換しない。schema変更時の再構築・検証・明示切替と、通常の取得・発行・復旧を分ける。運用は[batch運用](../../batch/OPERATIONS.md)に従う。

個別dataset専用の移行runnerは作らない。現行storeからreleaseを再構築し、hydrateで現行schemaへ満たす。

## 発行

publishはimmutable object、dataset manifest、release manifestの順に転送し、最後にcurrent pointerを
conditional PUTで切り替える。開始時pointerが動いていればconflictとして停止し、別writerのsuccessorへ
乗り換えない。pointer切替前にlocal closureをdigest・schema・row数まで検証する。

実行command、credential、成功確認と停止・復旧は[ローカル発行手順](../../batch/OPERATIONS.md#ローカルからクラウドを更新する)を正本とする。

rollback pointerや全履歴bytes監査は持たない。問題のあるreleaseを直すときは、正しいstoreから新しいreleaseを
前向きにpublishする。immutable prefixはBucket Lockで上書きと削除を防ぎ、mutable pointerとstagingは
lock対象外にする。

<a id="daily-cutover"></a>

## 日次切替

実行順は[batch運用](../../batch/OPERATIONS.md#日次機械工程--baibai-batch-daily)を正本とする。L1 publishはmachine store pushより先に行う。逆順ではcoverageだけが進み、factがcurrent releaseに無い状態を作り得る。readerは開始時にreleaseを固定する。

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

`lake resolve`はmanifestを解決してidentityとdataset一覧を表示する。任意SQLの実行やdata objectの検証は行わない。
Pythonで同じreleaseを固定し、必要なdatasetのmanifestに列挙されたobjectを検証してDuckDBへ渡す。
次は`uv run python`で使うローカルmirrorの例で、`name / alias`を`edinet.documents / docs`、
`edinet.segment_facts / segments`、`edinet.debt_schedule / debt`、`tradingview.forecast_snapshots / tv`から選ぶ。

```python
from pathlib import Path
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import (
    accepted_dataset,
    materialize_partitions,
    resolve_release,
    selected_partitions,
)

mirror = Path("stores")
source = LocalMirrorSource(mirror)
release = resolve_release(source, "<release-id>", manifest_sha256="<release-manifest-sha256>")
cache = LakeObjectCache(root=mirror, source=source)
name, alias = "edinet.documents", "docs"
paths = materialize_partitions(
    cache,
    dataset=accepted_dataset(release, name),
    partitions=selected_partitions(release, name),
)
with lake_session() as session:
    session.connection.read_parquet([str(path) for path in paths]).create_view(alias)
    sql = Path("docs/reference/queries/edinet-research-filing.sql").read_text()
    rows = session.connection.execute(
        sql, {"ticker": "<TICKER>", "cutoff": "<YYYY-MM-DD>"}
    ).fetchall()
```

同じsessionへ必要なviewを追加し、以下のSQL例もparameterをbindして実行する。optional datasetが一覧にない場合や
objectが0件の場合は収録なしとして原典へ戻り、別releaseで補わない。`selected_partitions`の`periods`で対象月を
絞れるが、提出版選択用の`docs`は全保持範囲を読む。readerはmanifestが列挙したobject keyだけをbounded batchで読む。
remote readに必要なDuckDB extensionはprovisioning stepで事前にinstallし、runtime downloadへfallbackしない。

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

hydrateは同一filesystem上のtemporary storeへ書き、SQLiteの`quick_check`・対象列・manifest row数を
確認してからatomic replaceする。途中失敗時は直前のstoreを保持する。current以外を使う場合は
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

## EDINET原本archive

`edinet.source_archives`はCSV/XBRLの取得ZIP bytesを保持する任意datasetである。旧releaseの抽出値から原本を捏造せず、新規取得・既存cache再利用から増やす。日付partitionは初回保存日であり、書類の観測日ではない。抽出値の正本と混同せず、[原本保存の運用](../../batch/OPERATIONS.md#edinet-source-archives)に従って不足確認・再計算・公開受入を行う。

<a id="edinet-research-query"></a>

## Researchのセグメント・負債満期照会

最初に[解決と読み取り](#fixed-release-read)の`lake resolve`でrelease IDとmanifest SHA-256を固定する。2 datasetはoptionalであり、同じreleaseのdataset一覧で存在を確かめる。導入前のreleaseも他datasetを読み続けられるが、存在しないdatasetへのqueryは行わず原典へ戻る。両datasetのpartitionは提出月、日付filterは`disclosed_on`である。企業別・期間別の行の存在は、questionの解決や全債務のcoverageを意味しない。

1. 固定releaseの`edinet.documents`からDuckDB view `docs`を作り、[書類選択SQL](./queries/edinet-research-filing.sql)へ`ticker`と`cutoff`（JST日付、当日末まで）を渡す。viewには保持したdocumentの全範囲を含める。狭い範囲にすると訂正のparentや取下げイベントを落とす。最新年次family内の提出版を先に選び、抽出済みかどうかで古い版へ戻らない。
2. `validity=usable`の書類だけについて、同じreleaseで下記のfact queryを行う。選択0件・`validity_unknown`・抽出行なしは原典確認へ戻り、訂正前の値で補わない。現行inventoryが履歴を確定できない情報修正・不開示・取下げも、選択familyに紐づく場合、またはfamily不明の年次イベントがそれ以降の年次に影響し得る場合はunknownに倒す。既知の別familyだけに紐づくイベントでは選択familyを止めない。年次familyに関連しない非年次イベントだけでは止めない。日中時点のPIT再現にはこの日次例を使わない。
3. 年次表の期末と現在日を併記する。`edinet.metrics`と合わせる際はtickerだけでJOINせず、metricsのasof・原典期間・連結basis・通貨を揃える。新しいBSとの時点差、事業再編、直近の借入れ・返済は別途確認する。

`edinet.metrics`の年次書類120/130には、売上債権・棚卸資産・仕入債務・契約負債・前受金・未払金のOCF寄与と、報告された有形・無形CapEx内訳をnullableで持つ。OCF内訳は報告符号、CapEx内訳はpositive expenditure magnitudeである。内訳を0と仮定した値は原因分解の感応度に過ぎず、「正常FCF」や維持投資の機械判定として扱わない。nullはゼロではなく、対象標準conceptの不存在、context・unitの不一致や曖昧さ、またはv1対象外conceptを含み得る。必要な場合は原典を確認する。

同じreleaseの`edinet.segment_facts`をview `segments`として、選ばれた`doc`をparameterに渡す。partitionを絞る場合は選択書類の提出月を含め、`disclosed_on`のcutoffも照合する。

```sql
SELECT period_start, period_end, consolidation_basis, segment_key, segment_name,
       metric, profit_basis, value, currency, source_locator
FROM segments
WHERE source_doc_id = $doc AND segment_kind = 'segment'
ORDER BY period_end, segment_key, metric
```

同じreleaseの`edinet.debt_schedule`をview `debt`として読む。nullを合計で黙って無視せず、categoryごとの欠測bucketも確認する。categoryがない場合は債務0としない。

```sql
SELECT balance_sheet_date, consolidation_basis, debt_category,
       due_from_months, due_to_months, principal, currency, source_locator
FROM debt WHERE source_doc_id = $doc
ORDER BY balance_sheet_date, consolidation_basis, debt_category, due_from_months
```

原典に戻る範囲とsource意味は[data sources](./data-sources.md#edinet-research-facts)が所有する。ここから資金余命を評価する際の将来FCF・維持投資・借換えはResearchの見積りであり、L1 factへ保存しない。

<a id="external-assessment-query"></a>

## External Assessmentの保存済み照会

`external_assessment.model_valuation_observations`、`external_assessment.analyst_rating_observations`、`external_assessment.quants_score_observations`は意味ごとのtyped datasetで、month partition、任意収録、履歴保持、coverage未証明である。既存のmarket storeとlake releaseを使う。External Assessmentを含まないreleaseをcurrent-schema作業コピーへhydrateした場合、そのtableは空となる。旧retail dataset名を含む固定releaseは元commitとの組合せで読み、新runtimeへ透過的に読み替えない。保存名の切替は完了しており、通常の取得・反映・復旧は[運用手順](../../batch/OPERATIONS.md#store-identity-recovery)に従う。旧SQLiteの形をruntime readerが読み替えることはしない。

同じ銘柄を同日に複数回受信しても、`fetched_at_utc`が違えば別行にする。保存済みstageの再投入は元の受信時刻を維持し、同じキー・同じ内容なら無変更、異なる内容ならtransaction全体を拒否する。新しいHTTP受信の同値は改定とは数えない。将来別配信元を同じsemantic tableへ追加するときは、その配信元を含むキーへ明示的にcontractを変更する。

領域は銘柄・cutoffについて、各datasetの配信元ごとに最新受領と直前受領を選ぶ。同じtableの別配信元の直近2行を比較相手にしない。最新の正常nullも受領として保持し、過去のnonnullへfallbackしない。未取得、受領済みで値がない、未知/変更された比較basisを区別する。最新・直前を含むL1値の再保存・再取得は不要で、株マップの取得済みscoreも同じ入口から利用できる。原評価元の関係は値の種類ごとに引用を束ねるだけで、独立票数や合成scoreを作らない。

領域入口は`external_assessment.query.external_assessment`、Web・toolsの公開facadeは`read_api.external_assessment`であり、銘柄・受領cutoff・評価論理時刻を明示する。Webの銘柄詳細・ticker profile・次のCLIが同じ領域結果を読む。Web exportは外部観測だけを持つtickerも含め、一つの受領cutoffをexport全体で固定する。ticker profileは要求日のJST末を論理cutoffとし、profile全体のportfolio基準日は[Market Context読取](./market-context.md#market-context-reads)に従う。lakeの固定releaseでも受信時刻のcutoffを併用する。

```bash
uv run baibai-engine external-assessment show --ticker <TICKER> --cutoff <ISO_DATETIME_WITH_TIMEZONE> --db stores/market/market.sqlite
```

`show`は取得・書込を行わない。`--evaluated-at`省略時はcutoffを評価論理時刻とする。実取得・stage importは明示された`collect / import`だけが既存L1入口へ委譲し、通常のbatch run・DB/R2 identityは変えない。値の意味と未確認metadataは[data sources](./data-sources.md#external-assessment-acquisition)、手動取得・stage importは[batch運用](../../batch/OPERATIONS.md#external-assessment-acquisition)が所有する。

<a id="tradingview-query"></a>

## TradingView snapshotの照会

`tradingview.forecast_snapshots`はmonth partitionの任意datasetであり、未収録のreleaseには存在しない。[解決と読み取り](#fixed-release-read)でcurrentを一度解決し、同じrelease IDとmanifest SHA-256のdataset一覧とPython readerを使う。過去snapshotには期間filter、特定企業にはticker parameterを指定する。

例えば、対象月のobjectからview `tv`を作り、`snapshot_date`の期間条件とともに、次のSQLで取得状態と実取得時刻を確認できる。

```sql
SELECT snapshot_date, ticker, fetched_at_utc, fetch_status,
       eps_forecast_next_fy, quote_currency, estimate_currency
FROM tv
WHERE ticker = $ticker AND snapshot_date BETWEEN $start AND $end
ORDER BY snapshot_date DESC
LIMIT 30
```

External AssessmentはTradingViewも同じ保存済み読取へ含める。正常受領の予測欄あり、正常受領で全予測欄null、`unresolved`、未取得を別状態で返す。quoteや人数だけを有効な予測の証拠にせず、正常受領を比較可能な企業評価と同一視しない。元のquote通貨と未確認のestimate通貨・単位・株式基準・絶対対象期を区別し、既存の未知metadataから数値deltaや対価格の割安判定を作らない。行単位の`target_period_label/end`は各metricとの対象期対応を保証しない。年度metadataが埋まっていても四半期・次年度・倍率等へ割り当てず、現行TradingViewの数値差分は`metric_period_mapping_unconfirmed`で比較不能に保つ。最新・直前の提示値と未取得・欠測・復帰の受領状態は保持する。銘柄列挙と値の読取は同じaware時刻比較を使い、optionalな秒以下表現やcutoffのUTC offsetで選択を変えない。

最新行も`snapshot_date`と`fetched_at_utc`を併記する。同じ日でもbatchや後続slotで取得時刻が異なる。対象日に行がないこと、`unresolved`の行、正常rowの個別指標nullを別々に読む。部分公開の場合、rowがない銘柄は未取得であり、coverage不存在とは判定しない。取得前の履歴、provider公表時刻、翌日取得による前日補完はこのdatasetから復元できない。値の意味は[data sources](./data-sources.md#tradingviewの市場期待)に従う。
