# 運用手順

machine処理、store反映、lakeの公開と復旧を扱う。repository rootのBashから、依頼された対象だけを操作する。全optionと内部判定は各CLIのhelp・実装が正本。責務とデータ正本は[architecture](../docs/architecture.md#store-authority)、開発準備は[development](../docs/development.md)にある。

<a id="actions-write-boundary"></a>
EDINETの大量保有・訂正報告書原本は既存の `edinet-facts` 取得に含める。保存済みIDを再取得せず、未保存分を新しい順に1回100件まで、10秒間隔で取得する。未選択数は次回の対象として残し、既存の年次原本や一覧を削除しない。認証拒否・403・レート制限では打切り、保存済みの部分結果を維持する。

## 実行環境と接続

通常の取得・公開は既存Actionsを優先する。個別に依頼されたapplication更新は、認可された運用環境で取得済み作業コピーを正規CLIで更新し、条件付き反映する。物理的なPCだけに限定せず、対応OS・依存・接続を確認する。開発変更を本番操作・新課金・権限拡大の許可へ読み替えない。

baibai-storesは非公開の正本。既存Web/APIのコード撤去は、配備済みWorker・domain・credential・旧表示objectの停止や削除を実施するものではない。それらの実環境変更は対象と権限を確認した別操作とし、原本・application DB・台帳を巻き込まない。

| 実行主体 | 設定の所在と範囲 |
| --- | --- |
| 取得・公開Actions | R2_ACCOUNT_ID、R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY、JQUANTS_API_KEY/ESTAT_APP_ID/EDINET_API_KEY。必要stepだけへ渡す |
| 通知 | repository secret DISCORD_WEBHOOK_URL。通知stepだけが使う |
| 認可されたlocal運用 | repo外で扱う秘密と.env。stores用writerを通常開発へ流用しない |

秘密値・endpoint・私的なDB/原稿をGit・Issue・log・公開artifactへ置かない。認証失敗を別経路や権限拡大で迂回せず、設定の有無と対象接続を確認する。運用内容の許可はbroker操作や未報告取引の入力を含まない。

## 日次実行と結果確認

code修正をmainへ反映してから、必要な取得・再計算だけを実行する。定期稼働は[workflow](../.github/workflows/)の実 `on.schedule`、対象日は[Jobs schedule](src/baibai_jobs/schedule.py)で確認する。コメント内cronや手動成功は定期稼働の証拠でない。run IDを固定し、監視timeoutだけで重複dispatchしない。

```bash
gh workflow run cloud-daily-batch.yml --ref main -f asof=<YYYY-MM-DD>
gh run list --workflow cloud-daily-batch.yml --limit 5
gh run watch <RUN_ID> --exit-status --interval 60
```

手動asof空欄は元run作成時のJST日。寄前・場中に完了済み日次を使うなら前日を指定し、前営業日へ戻して週末の経済・外部評価を落とさない。cronは元run作成時計と該当slotから対象日を固定し、queue/rerunでも保つ。生成as_ofと価格日は別で、休場日に価格日を繰り上げない。引後でも必要な価格日・株式分母が不足すると候補0件になり得るため、買う対象なしと解釈せず[入力不足](../docs/reference/screening-runtime.md#入力不足と品質)を確認する。

既定読取は発行時計より新しいas_ofを優先し、古い非空候補へ戻らない。過去日の再計算はexact generation/selection IDで読む。現在受領からの再計算を完全な当時PIT再現としない。

共通統合は全providerの完了を待たず、利用可能な受領を固定する。

1. 経済L1を単独公開・読戻し・sealし、次回取得inputを発行する。
2. marketのready受領を固定してsource別投影/EDINET抽出を行い、[model-integration](src/baibai_jobs/integration/models.py)が固定market releaseと経済原本の固定selectionからモデル/候補を保存し、同じreleaseを公開する。
3. 公開成功後だけ生成cache/候補を整理し、machine反映・readback・受領sealへ進む。

source失敗は前回適格投影を保って他sourceを進め、モデル生成/公開失敗は後続machine反映を止める。model-integrationは外部取得/Web生成を呼ばず、後続のWeb表示もcore成功条件ではない。workflowの成否と公開済みrelease/storeの到達点を分けて確認する。

model-integrationの`phase`ログは開始時にflushし、成功した処理だけ`done`と所要秒を出す。停止時は最後の未完了phaseから調べるが、ログだけを原本・正本の反映成功の証拠にしない。

### 保存済み世代への候補政策の発行

モデルを再生成せず保存V2 generationへ現行政策だけを適用する場合は、main反映後、[公開lease](#ローカルからクラウドを更新する)内でfresh runsを取得する。exact generationを指定し、通常経路と同じ政策/Selection IDを使う。同ID・同内容はno-op、異内容は競合で止め、旧IDを改名しない。

```bash
with_publication_lease bash -c '
  jobs/scripts/r2_transfer.sh pull-runs &&
  uv run baibai-jobs screening-selection --generation-id <GENERATION_ID> \
    --runs stores/screening/runs.sqlite &&
  jobs/scripts/r2_transfer.sh push-runs
'
```

これはrunsだけの条件付き反映。fresh正本でSelection/generation ID・政策method・候補数を読戻す。結果不明は先に反映先を確認し、同じcopyを盲再送しない。旧Selection/generationや他storeを書き換えない。

### 取得と統合の結果確認

取得期限にはexport/stageの余白を残す。停止前の成功L1を保ち、次runは成功chunkから再開する。[通信owner](../engine/src/baibai_engine/data_collection/transport/context.py)のRetry-Afterを短縮せず、認証・403・解析失敗へtransport retryを広げない。継続不能な429はそのpassの同quota groupだけを止め、経済nativeは失敗した1 GETだけを再試行する。H.15のbrowser起動不能はsource拒否ではなく、別取得で再試行できる。日次へ移行・全期間backfill・較正再構築を代行させない。

取得/private受領、正本反映/readback、lease解放を別々に照合する。通知失敗でjobは赤にならないため、run log・store push・release・generation/selectionも確認する。[watchdog](../.github/workflows/cloud-batch-watchdog.yml)は取得と統合を別schedule/asofで監視し、経済は受領markerを読む。限定source成功を全体成功にせず、MISSINGの過去日確認には次を使う。

```bash
gh workflow run cloud-batch-watchdog.yml -f check_date=<YYYY-MM-DD>
```

### EDINETの未到着・抽出失敗

[CSV取得計画](src/baibai_jobs/acquisition/edinet_archives.py)は既定540日前〜asofの保存一覧とnative訂正/取消から選び、保持docID/typeで原ZIP不足を判断する。L2の成否/revision/破損でGETを減らさず、取得成功をL2採用としない。一覧不足/失敗・候補0は取得失敗で、plannerはL2 failureを書かず、origin/ticker不明をbaselineで推測しない。保持type5 ZIPを再取得せず、lookback外の保持origin/祖先も訂正/取下げの解決に使う。

一覧/archive未到着は `applied.json` の `deferred_inputs` に残し、適格保存値で統合を続け、到着後に[再抽出](../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_integration.py)する。他文書の未到着で保存原本の破損/解析失敗を隠さない。不正一覧の再受領は既存ok日/時計を保つ。未適格日は正常部分とfailed coverageをpartial公開できるが、日別成功ではなく厳密読取は拒否する。正常別日の訂正/取下げは進め、不正日は新handoffがなくても正常受領までsource-check失敗を保つ。延期だけはsource-check成功、解析/公開/readback等の失敗はエラーとし当該sourceを修復する。

誤orphan隔離は現年度の選択CSVを確保して次回統合で回復し、未到着を古い正常baselineで埋めない。全期間GET・全件再抽出・意味revision変更は不要。明示 `maintain-market refresh-edinet-metrics` も一覧保存→原ZIP確保→L2抽出の順で、原本取得失敗なら抽出しない。後続失敗でも受領済みZIPを保つ。

metric本文/coverageを除いたinputは次回通常発行で更新し、旧余分欄を読まない。schema/stage/原本identityを変えず、未統合原ZIP/一覧を保持する。facts未初期化やinput不足を迂回せず、[schema50切替](#edinet-factsの取得範囲schema-50)・[一覧native切替](#edinet-list-native-l1)へ進む。archive input/scope・一覧L2のmarket公開依存は残る（refs #1820）。

Screening成功を全source成功としない。旧snapshot日を当日にせず、`edinet_snapshot_asof` / `edinet_snapshot_recorded_at_utc`、文書ID・提出日時・対象期間で鮮度を確認する。

<a id="financials-identity-cutover"></a>
### Company Financialsの保存IDと公開時入力の読取

現行の保存IDは`company_financials`、schemaはruns 8 / application 29。通常open/dailyは旧schemaを変換しない。一度限りのDB converterと本番publish入口は現行codeから撤去し、当時の実装・手順は固定Git履歴に残す。原DB backup・移行plan等の私的保全記録は削除せず、完了したcutoverを現行正本へ再適用しない。

`selection_identity_migration`はOperationごとの不変な証拠で、`published_selection_canonical`は元の公開hashが指す原文。現在のOperation内Selectionは名称を正規化した運用入力であり、原Proposal/Review/本人報告/訂正/台帳を再発行・再署名して新hashへ付け替えない。履歴readerは同transactionで現在hash・receipt・指定Proposalの正規Reviewとexact scopeを照合し、旧canonicalをopaqueな文字列で返す。通常readerへ旧名aliasや旧hash例外を設けない。

```bash
uv run baibai-engine operation --db <APP29> selection-evidence <OPERATION_ID> \
  --selection-sha256 <PUBLISHED_SELECTION_SHA256> --proposal-id <EXACT_PROPOSAL_ID>
```

旧workspaceを使う場合はmanifest・domain-inputs・執筆済みdraftをまとめてarchiveし、別の新directoryへcompletedの運用投影だけを復元する。下記helperは現行DBを読取り、ローカルfileだけを保全・生成する。旧出典indexは原canonical/原domain-inputsと一組で解決し、新manifest下へ旧draftをコピーしない。旧workspaceを`--force`で新hashへ転用しない。

```bash
uv run python -m tools.financials_identity_workspace --db <APP29> \
  --operation-id <OPERATION_ID> --proposal-id <EXACT_PROPOSAL_ID> \
  --source <ORIGINAL_WORKSPACE> --archive <NEW_ARCHIVE> --output <NEW_WORKSPACE>
```

### マクロ原本の独立更新

[cloud-economic-sources](../.github/workflows/cloud-economic-sources.yml)は日本株休場日も取得する。共有quotaごとのgroupが担当sourceのread-only inputを使い、成功分を非公開 `coordination/economic-sources/` へ独立stageする。失敗groupは赤のまま部分受領を保ち、application/market全体や秘密をhandoff・公開artifactへ含めない。

```bash
uv run baibai-jobs acquire-economic groups
gh workflow run cloud-economic-sources.yml --ref main -f group=fred_csv -f source_id=fred-payems
```

限定再実行はgroup内のsource IDを指定する。[取得policy](../engine/src/baibai_engine/data_collection/acquisition/policy.py)のsource別窓を一律2か月へ縮めない。FREDは全CSVの全期間を比較して旧期改訂を残す。全体配信は比較窓を狭めても通信量が減らない。他sourceの古い年次改訂・初回history seedは明示取得とする。

`--recent`は通常窓と、既存成功範囲/通常窓失敗からの未確認区間を回復する。広範囲失敗は通常runの起点にせず、実受領の成功分を除いた残りを `result.json` / logの `history_recovery_required` で確認し、期間指定refreshまたは `--all-history` で別に回復する。全改訂・完全な当時PITは保証しない。

定時/asof空欄の遅延ではcurrent-onlyだけ実取得日を記録し、他sourceとhandoff対象日は動かさない。松井の現在ページや明示過去asofを現在値で埋めて履歴回復としない。PMIの配信元変更/速報採用は[別途受入](../docs/reference/data-sources.md#pmi-public-articles)で扱う。

取得成功と正本反映は別に確認する。保存L3を書き換えたり投資判断を自動作成したりしない。

### FRED確認stateと経済schema 12→13の切替

FREDの同値応答は本文を追加せず、実際の確認時刻と採用した変更受領への参照を更新する。値の変更時刻と鮮度を混同しない。全CSVに含まれる期間と過去にnative行が存在した期間だけを比較し、旧schemaの人工空日をproviderが明示した欠測へ昇格させない。headerだけの全履歴応答や短い行は取得失敗として既存内容を保つ。完全応答の実受領時刻と、返却period・native本文hashの集合をsource単位の確認stateへ同時に記録する。本文は既存period原本を参照し、同値応答の全履歴を複製しない。scoped handoffにも全anchorを含め、fresh正本にだけある実在periodの消失とsource全体の遅着を判定する。旧runや人工空日から完全集合を推測しない。明示回復ではfresh正本を実再取得の比較基点とし、local本文の保全unionを取得後に行う。同じ実応答・同じ受領時計でlocalにだけある実在periodの不在を補い、GET・run・countを増やさない。

完全応答確認tableがない旧draft13 copy/inputは自動補完せず拒否する。保持したschema12原本から新receipt/outputを作り直し、実FRED再取得で確認を確立する。旧draft copyにだけある未公開原本は削除せず個別保全し、空のsource確認を足して通常経路へ通さない。

通常open/dailyはschema 12を自動変換せず、12/13混在の通常merge・pushも拒否する。次は一回限りの明示切替であり、通常`push-indicators`で代用しない。code/store/inputの読戻しと通常取得・統合の確認までを一組として扱い、完了済み環境へ再適用しない。

1. 経済取得・共通統合と手動writerを停止し、実行中のwriterが終わったことを確認する。同じpublication leaseを保持し、旧codeで停止時の旧ready一覧を有限drainする。失敗・pendingは成功扱いせず、残る旧schema受領の処置を決める。旧stage/keyは上書き変換しない。
2. 旧codeのpull-machineでfreshな3 storeとETagを取得する（新codeではpull-machineとpull-indicatorsを分ける）。`machine-manifest.json`と3 HEADが一致する集合を使い、schema12原本を閉じたまま保持する。次の`prepare-copy`はその原本bytes・3 ETagと別schema13 copyを結び付け、旧本文・受領時刻・run・人工空行を保持する。過去の確認時刻は作らない。
3. 新codeで別copyを明示FRED再取得する。受信は全CSVで、実受領時刻だけに確認を記録する。失敗sourceは失敗のまま既存内容を保つ。既存期間で確認stateがない場合、実再確認まで後着handoffを自動採用しない。FREDの一部が失敗しても保持済み原本を公開できるが、未確認の既存期間への後着受入は保留を維持し、そのsourceの回復・通常運用確認が終わるまで全体の切替完了としない。結果と不足を確認してから`freeze`する。これは全旧行保持と確認anchorを検証し、以後再送するSQLite snapshotを固定する。`unconfirmed_native_periods`はFRED以外も含む不足数で、ゼロだけを本番受入の判定には使わない。

```bash
uv run python -m baibai_jobs.maintenance.economic_cutover prepare-copy \
  --source <SCHEMA12_COPY> --output <SCHEMA13_COPY> --receipt <CUTOVER_RECEIPT> \
  --generation-directory <PULLED_GENERATION_DIRECTORY>
uv run baibai-jobs acquire-economic refresh --group fred_csv --recent \
  --end <ACTUAL_DATE> --db <SCHEMA13_COPY>
uv run python -m baibai_jobs.maintenance.economic_cutover freeze --receipt <CUTOVER_RECEIPT>
jobs/scripts/r2_transfer.sh push-economic-cutover <CUTOVER_RECEIPT>
uv run python -m baibai_jobs.maintenance.economic_cutover reissue-inputs \
  --receipt <CUTOVER_RECEIPT> --lease-handle "$PUBLICATION_LEASE_HANDLE"
```

4. 同じlease内でschema13 codeを反映し、専用`push-economic-cutover`を実行する。最初にfresh正本bytes・3 ETag・bundle manifestを照合し、固定uploadのintentを保存してから`indicators.sqlite`だけを旧ETag条件でPUTし、実bytesを全読戻しする。market/runsを更新せず、両HEADが未変更であることとleaseを再確認し、元manifest ETagを条件に新bundle manifestをPUT・全読戻しする。L2/modelの再生成や成功は前提にしない。
5. `reissue-inputs`は公開済みの同一schema13 bytesを再照合し、全経済groupのsource inputだけを再発行・読戻しする。lake・前回L2・market DBを読まない。途中失敗ならwriterを停止したまま同じcommandを再実行し、全groupのschema・economic ETag一致を確認する。新inputの公開は、旧inputや旧stageの削除ではない。
6. fresh pullでschema13正本を取得し、旧pendingが残っていないことと新inputを確認してからwriter/consumerを再開する。通常のFRED取得→handoff受入→同値時の本文不増加、変更/消失時の正本反映、経済L2再生成・読戻しを確認する。publisherの合成test成功を本番切替完了やL2同一性の実証とは扱わない。

PUT応答不明やmanifest更新前の停止では、receipt・元schema12 copy・固定upload・manifest候補をそのまま保持する。同じ`push-economic-cutover`が現objectを全bytes照合し、元の世代なら同じcandidateだけを再送、candidateなら再送せず次へ進む。第三の値やpeer変更では停止し、通常pushやETagの手書き変更で解消しない。lease期限切れならwriter停止を維持し、正規手順でleaseを取り直して同じreceiptを使う。固定後の作業copy編集はuploadへ反映されない。`freeze`前に残った未登録uploadはremote未変更なので内容を確認し、新しいreceipt/outputで準備し直す。

converter・専用publisherとこの一回手順は、本番切替・全input/通常経路の読戻し・残存旧stageの処置を確認した後に撤去する。通常`push-indicators`もfresh正本に対するhandoff受入を使う。正本・作業copyの原本を保ち、採用確認だけを受入結果へ揃える。FREDの遅着や確認欠落で受入できない場合はcopyを変えずに停止し、明示再取得してから再実行する。他sourceは従来の原本union・最新受領の読取を保つ。確認stateを失った正本へ任意の古いlocal確認を採用しない。回復時は`with_publication_lease jobs/scripts/r2_transfer.sh push-indicators --recheck-source <FRED_SOURCE_ID>`を明示実行する。fresh正本とETagを固定し、そのcopyへ実GETしてから対象sourceのlocal原本を保全し、同じETag条件で公開する。失敗時は両copyを変えず、現在の完全応答からのみ確認を作る。採用stateを全て失い、正本にも未採用原本が混在していた場合、回復が保証するのは現在値・完全集合・実確認時刻と原本保持であり、失われた過去の採用順やlast-change時刻ではない。`adopted_fetched_at`は実在原本への参照で、欠落区間の変更時計を復元した意味にはしない。

### 経済L1の単独公開への切替

経済正本は既存の`indicators.sqlite`一つで、market/runsのbundleから独立する。通常dailyの`economic_sources`はfresh正本と固定handoffだけを読み、採用・1世代backup・条件付きPUT・全bytes読戻しを終えてからsealと15取得groupのinputを発行する。TradingViewの明示MCP原本も経済namespaceの受入対象だが、定期取得inputには追加しない。各seal/input PUT直前に同じ経済ETagとleaseを再確認する。取得失敗runを保持できたことは取得成功ではなく、source失敗・明示history回復の必要はsource gapsとしてcheck/通知へ残す。モデルは読戻しcopyから[固定selection](../docs/architecture.md#store-authority)を保存・再計算し、metadataや原本の検証失敗時はモデル/lakeを公開しない。後段L2失敗で経済L1を戻したり、経済を再PUTしたりしない。

切替前に全旧writer（daily・手動push・待機中run）を停止し、実行中の終了を確認する。旧codeで未統合stageと部分machine pushを有限整理し、必要なschema12→13の上記cutover・input読戻しを完了してから、全writer/consumerを新codeへ同時に切り替える。旧writerと新writerを混在させない。新`pull-machine`/`push-machine`とmanifestはmarket/runsの2keyだけを扱う。旧manifestの余分なindicators欄は正本の根拠にせず、次の正常market公開で省かれる。経済の旧bytes・stage・1世代backupは削除しない。schema12 cutoverは旧3keyを照合する有限入口なので、この単独公開の再開後に再実行しない。未完の旧article cutover receiptも切替前に完了・整理し、新2keyのreceiptへ黙って読み替えない。

PUTの応答不明は実bytes読戻しで確かめ、確認できなければsealしない。PUT後・seal前の中断は次のfresh runnerが未sealの不変stageを現正本へ再適用する。sealの書込失敗を飛ばして同sourceの後続watermarkを進めない。input途中の中断はstageが既にseal済みでも次cycleで新attempt identityを使い全groupを再発行する。新cycleは既存local出力を上書きせず、fresh実行copyを要求する。経済公開・読戻しの失敗では後段を止め、空の経済DBや古い値へ自動fallbackしない。L1公開後にseal/inputだけが失敗した到達点と、L2失敗は通知・`applied.json`で分ける。

同じsourceのA→同値A→Bを一括公開し、seal前に中断した場合、差分保存で本文を省いた中間の同値stageは、再開時に未見の遅着と区別できず`needs_explicit_recovery`になることがある。完全な自動再開のための新しい監査stateは持たない。このstageを成功扱いにせずcheck/通知へ残すが、正本の最新Bと全groupの次inputは公開でき、次の通常取得も前進する。運用者は対象stage ID/hash・中断runの公開読戻し時report log・現正本と次inputの値を照合し、元stage/時刻/markerを変更せず要確認の記録を保つ。markerを手で成功へ変えたり、古いcopyをpushしたりしない。現在値が必要なら新inputからの通常取得で確かめる。FREDの確認state自体が欠落した場合だけ上記の明示再確認入口を使う。いずれも新しい取得成功を古いstageの採用証明へ読み替えない。

FRED以外の比較窓・人工空・確認方式は変更しない。経済SQLite/handoffからsource別objectへの移行とFREDのHTTP bytes保存は #1820 の残作業である。

### source入力・市場原本の引渡し

共通統合だけが `cloud-publish` と共通leaseを使う。独立取得はsourceごとの原本受領とprivate stagingを担当し、market/application全体のhydrate・統合をしない。同source/groupは取得〜stageを直列化し、J-Quantsは市場/財務/経済の共有quotaを守る。TradingViewも毎日独立取得し、[取得運用](#tradingview-expectations)に従う。

[source input](src/baibai_jobs/acquisition/source_inputs.py)は正本generation/原本hashに結び付く再生成物で、正本ではない。計画に必要なsource履歴/coverage（正常空を含む）を保持し、部分storeにfull lake originを付けない。初回は共通統合から発行し、`coordination/source-inputs/` 不足を全DB取得・空seedで迂回しない。J-Quantsはこれを使わず、未移行sourceは固定L1 release/未統合原本、nativeは独立headから計画する。

JPX決算予定・規制は過去のfactを取得計画に使わないため、scoped inputや取得前resumeを要求しない。fresh source DBで受領し、失敗前の部分原本も従来のprivate stageへ保存する。共通統合でのcollection別replayは変えず、旧input・stageは削除しない。

Matsui/TSRは原document全履歴をinputへ複写しない。schema13 envelopeと旧全履歴inputとの双方向reader互換を保ち、次の通常input発行で更新する。schema移行・再GETは不要で、同値inputは再利用する。

- **Matsui:** 実取得日のcurrent pageを1 GETするため、document・受領履歴・runなしの明示空seed。正本schemaを検証して発行し、原本不在やinput欠落の自動初期化を意味しない。
- **TSR倒産件数:** complete JSONの本文を除き、run範囲・成功/失敗・native coverageを残す。recent/outage/未完了広範囲の判定を最新成功日だけへ縮約しない。run metadataが増えれば再発行し、固定bytes量とはしない。

PMI/UMich/ECBは先行配布した両形式readerに対してv1 planning inputを発行する。PMIはURL/月/phase、UMich/ECBは空を含む受領日集合とrun/confirmation metadataから同じ取得計画を作り、過去本文はcanonical原本に残す。発行切替前に旧readerを使う実行中・待機runを終え、運用CLIをreader-first版以降へ揃える。旧SHAを再実行しない。両形式readerへ配布済みなら、旧writerによる旧形式の再発行も読める。共通経済統合は新受領がなくても全15groupを再発行し、groupごとの失敗では旧pointerを保って次の通常統合で再開する。原本・旧input/stageを削除せず、FRED packやschema移行と一括にしない。

通常発行後はgeneration・同値再利用・次受領とfresh正本の受入を確認する。新受領の原文/URL/params/実時計はhandoff、同値/訂正/遅着の比較とL2再生成は保持正本が担う。旧input/stageと参照先は有効な間保持し、削除・retention変更を伴わない。経済正本全体の転送・source別object化は残作業（refs #1820）。

[cloud-market-sources](../.github/workflows/cloud-market-sources.yml)は市場/財務・JPX・EDINET一覧とmetrics/facts archiveを別slotにする。週末も一覧/archiveを続け、facts未初期化を全件取得成功にしない。需給の取得日は固定calendar原本を使う。

手動 `source=all` は一覧取得/stage成功後にmetrics/factsへ分岐する。archiveは未統合一覧も作業copyへ投影するので共通統合を待たない。一覧失敗時はall内のarchiveを止め、有効一覧を確認できる場合だけ同asofの必要archive sourceを再実行する。他sourceは待たせない。

marketも元run時計/該当cronから日付・曜日を固定する。ただしrun作成自体が次の同slotを越す24時間以上の遅延は復元できず、対象日を確認してasofを明示する。

反映は[source所有の投影](src/baibai_jobs/integration/sources.py)を使う。破損handoff/decode失敗は当該sourceを報告して他を進めるが、正本破損/R2全体障害は停止する。失敗前の原本は公開/readback対象とし、不正typed値を採用しない。input発行失敗も前pointerを保ち正常groupを更新する。

遅着・未統合受領は次回統合、必要なら既存dailyの手動実行で拾う。同値no-opもstage watermarkを進める。後続統合後に現れた旧stageは `needs_explicit_recovery` として保持し、ID・理由・source/期間・最新markerを照合する。現在値は新run取得→統合、旧vintageの補完は原本固定の個別回復で扱う。marker削除・受領日改変・古いDB全体pushで通さず、通常recentに全履歴rebuildを混ぜない。

## クラウド正本をローカルへ取得する

分析用pull前にdailyのqueued/in_progressを確認し、通常は完了後に取得する。ローカルpublisherとも重ねない。TradingViewの未統合stageは共通統合後にreleaseへ入る。

```bash
jobs/scripts/pull.sh
```

machine receipt/ETagとhydrate開始時のmarket releaseを照合する。両者を同一transactionでは固定しないため、競合・検査失敗後のlocal stateを分析へ使わずfreshに取得し直す。applicationはbulk pullせずpull-appを使う。

新しいモデル入力の補修は固定generationの未生成・欠測理由と該当source受領から対象を限定し、取得欠落を同じas-ofのsource取得で補う。verify-cache-coverageは旧較正入力の保守診断で、新候補の共通資格gateにしない。提供元のnullやL2計算不能を取得失敗とせず、値が埋まるまで全期間を再取得しない。

<a id="article-acquisition"></a>
### 記事sourceのslot・差分統合（schema 49）

起動枠は[cloud-article-sources](../.github/workflows/cloud-article-sources.yml)、sourceの範囲・応答上限・取得予算は[契約](../engine/src/baibai_engine/data_collection/providers/articles/contracts.py)、cycleと計画上限は[planning](src/baibai_jobs/acquisition/article_planning.py)を正本とする。groupの直列化と共有hostのqueueを保ち、実受領日時・公表日時・対象slotを別に確認する。

- TSRは正常なnative index受領で日次cycleの充足を判断する。host queue後の実clockを使い、indexが充足済みなら未処理本文だけを続ける。試行時刻だけで当日取得済みにしない。
- BOJ/MOFは元runのslotを保持するが、未処理URLまたは新しい発見revisionで再解析できる原本があれば同slotでも再開する。同slotの再開ではindexを取り直さず、保持原本からの発見と未処理本文を進める。
- その他はsourceごとのslot・validator・worksetで再開する。設定された再検証GETは改訂確認であり、保持原本をoffline再解析する処理と区別する。充足の確認は現在のownerが保持する要求範囲と再開状態を読む。

初回planningは`integration.sources inputs`で取得有効化前に発行する。不足を空DBや全market pullで迂回しない。原本stageを保存した後にだけ計画をCAS更新する。未処理workはvalidator cacheから独立して保持し、未統合stageのacknowledgementでworksetやindex原本を消さない。容量上限では未完了項目を捨てず、統合または限定回復へ進む。

planning CAS失敗で残った原本の統合と、最新cursorの復元は別である。exact stage/hashから原本を回復できても、全sourceのcursorを自動修復できる保証ではない。後続取得が進めた計画を古いcursorで巻き戻さない。参照worksetの不足と、validatorの200 body不足も別である。後者では通常予算内の新しい無条件GETを行う場合があるが、過去原本を復元したとは扱わない。

原本の表現・添付・リンク範囲は[記事sourceの意味](../docs/reference/data-sources.md#public-articles)を参照する。トウシルの同一host内の公開記事移転（301/308）はLocationを保持し、既存予算内で転送先を別受領として取得する。一般のHTTP redirect、認証付きURL、未許可hostへ巡回を広げない。旧受領に未保存のLocationは推測して補わない。HTTP失敗を含むL1の保存成功と、取得成功・本文投影成功を区別する。受領失敗はsource不備としてDEGRADED通知に残し、本文を受領した後のparser失敗だけを投影失敗として扱う。財務省の許可された案内は親子の別受領を保ち、rolling一覧から過去全PDFへ巡回しない。応答の種類別上限は契約に従い、本文が大きいからfeed・source・groupの総予算まで増やさない。

結果の`partial`には未処理本文の継続も含む。原本・cursor保存が完了した`continuation_only=true`だけは正常な継続終了であり、全取得完了ではない。HTTP・parse・deadline・cursor省略・保存/CAS失敗を成功扱いせず、保存済み原本と失敗理由を分ける。source Aの失敗でも正常なBは進め、共通統合は開始時に到着済みのstageを固定する。

200同値は別受領、304は具体的200 bodyへの参照、正常空と失敗は別状態として保持する。原文・概要はprivate原本に限定する。sealは固定releaseから今回のreceiptとprojection/抽出artifactを読戻して一致した後に行い、原本保存・投影成功・正本公開を同じ成功件数にしない。

設定上限は実測値や無料枠の保証ではない。runではsource別requests/bytes/seconds、metadata操作、input/raw転送と保存、runner時間、readbackを確認する。setupを含むActions時間とR2全体の費用を部分logだけでゼロと報告しない。旧formatを読めないcodeへ戻して同じplanningを書かせず、切替・回復では稼働中writerを確認する。

### 日次統合前の記事原本を読む

固定 stage の manifest hash と receipt ID/hash を指定して、[記事原本reader](src/baibai_jobs/acquisition/article_evidence.py) から非公開の作業先へ取得する。provider への再GET、planning変更、正本への書込は行わない。

```bash
uv run baibai-jobs article-evidence \
  --source <SOURCE> --stage-id <STAGE_ID> --manifest-sha256 <HASH> \
  --receipt <RECEIPT_ID> <RECEIPT_SHA256> --directory <PRIVATE_OUTPUT>
```

出力は原受領・原本と stage 出典で、通常 release 所属や本文抽出成功を意味しない。引用時は出力packetも保持する。日次統合後は同じ receipt/body の ID・hash を固定 release で照合し、保存先の変更を新しい受領時刻へ読み替えない。stage URL だけで長期保持を保証しない。全文を公開logやIssueへ転記しない。

### 記事のexact遅着回復

記事ownerに限定した回復planを作り、通常と同じfresh canonical/leaseでapply→モデル生成/公開→machine反映→sealを行う。最新feed再取得、元受領時刻変更、`integrated.json`削除は行わない。旧stageの原受領はappend-onlyで追記し、現在版選択はownerに任せる。回復成功は別の`recovered-<manifest hash>.json`へ記録する。

```bash
uv run baibai-jobs article-recovery prepare --source <ARTICLE_SOURCE_ID> \
  --stage-id <EXACT_STAGE_ID> --manifest-sha256 <EXACT_MANIFEST_SHA256> \
  --directory <PRIVATE_RECOVERY_DIRECTORY>
# このdirectoryのplan.jsonを保持し、通常の日次planで上書きしない。
uv run python -m baibai_jobs.integration.sources apply --asof <YYYY-MM-DD> \
  --directory <PRIVATE_RECOVERY_DIRECTORY>
```

続くmodel-integration/push-machine/seal/inputsは同じ作業copyと公開leaseを使う。`prepare`はplan作成だけで、本番公開やsource取得を暗黙に実行しない。summary CASに失敗して原本だけ保存された場合も、まずこのexact入口で受領を統合する。取得計画の回復は別に確認し、統合成功だけでcursorが復元されたとは扱わない。

BOJ/MOFの保存済み文書からリンク発見をやり直す場合は、同じ[回復owner](src/baibai_jobs/maintenance/article_recovery.py)の`register-discovery`へexact stage/hashと`--receipt-id`を指定する。これは元原本を保持した別stageを作り、現在のplanningへCAS登録する書込である。HTTPは行わず、元受領時刻・既存未処理work・validator・slotを保つ。同じ要求はno-opとなり、登録後は通常取得が未処理のリンクを処理する。原本統合だけの`prepare`と取り違えない。

### EDINET factsの取得範囲（schema 50）

annual XBRLの取得範囲はsource所有の`edinet_facts_scope`が持つ。global開始日と保持docID集合、旧scopeの出所digestだけを保存し、L2 facts/revision/成功状態をGET計画へ使わない。通常取得とL2再生成は同じ範囲を読む。対象familyのIDはGET前に保持するため、取得失敗・現在のXBRL未提供・後続familyへの交代で消えない。実際のGET可否、訂正/取消・orphanの扱いは既存native一覧の選択規則に従い、type1 ZIPは既存identityで再利用する。stale input/stageはIDと出所をunionし、既存開始日を狭めない。

global未初期化は開始日NULLで表し、通常annual取得を開始しない（独立したholder取得は従来通り）。新規有効化は明示`refresh-edinet-facts --initialize`が取得可能な対象原本を確保した時だけ行い、その後のL2失敗でも戻さない。途中GET失敗ではglobalを有効化せず、対象IDと既存ZIPを保ち、同じ明示refreshで再開する。`--ticker`限定はglobalを有効化せず他社の範囲も置換しない。offline`extract-edinet-facts --initialize`は保存入力の局所再計算であり、将来のGETを有効化しない。

有限converterだけが旧`edinet_research_facts`の`initialized.since`と全docID key（failed/現在origin不在を含む）と保持type1原本のdocIDを一度読み、scopeの出所digestを付ける。L2値/revision/error本文はsource stateへ複写しない。旧initializedのsince欠落/型違い/不正日は推測で補わず変換を止める。未初期化・ticker限定由来の旧keyだけならglobal無効を維持する。canonical/stageとも原ZIP受領後にL2が中断したfamilyを保持するが、全一覧を対象集合にはしない。stageの旧initializedからglobalを有効化しない。旧table/原本bytes/時刻は別copyに完全保持し、旧coverageは通常取得で読まない。移行済みL2を消してもsource範囲は変わらず、L2だけを再生成できる。

これは取得範囲の責務分離であり、EDINET L1公開独立化の完了ではない。scopeはprovider原本datasetではなくmarket SQLiteのoperational stateとしてhydrate/dehydrateで残り、source input/handoffと通常mergeが保持する。通常input発行はまだmarket正本・一覧投影/coverage・共通公開へ依存する。独立scope objectだけを先に増やしても一覧依存が残るため、一覧原本/ZIPと一緒に独立source storeへ移す段階を別件として残す（refs #1820）。

### market schema 48/49→50の限定cutover

通常daily/open_connectionは旧schemaを移行しない。通常`push-market`のremote/local current-schema検査も緩めない。writer（source/daily/materialize/運用CLI）を停止し、待機中runも確認して旧stageを有限drainする。旧codeでfresh machine bundleとETagをpullし、旧releaseからhydrateした48/49正本の作業copyを保全する。codeを50対応へ切り替え、同じpublication leaseで以下を実施する。旧source inputsは再使用せず、seal後に全groupを再発行してから取得を再開する。

```bash
uv run baibai-jobs market-cutover prepare-copy \
  --source <PRESERVED_HYDRATED_SCHEMA48_OR_49_COPY> --output stores/market/market.sqlite \
  --receipt <PRIVATE_CUTOVER_RECEIPT_JSON> --kind canonical
# outputは不存在が条件。元copy・元ETag・receiptを反映完了まで保持する。
uv run baibai-jobs model-integration --asof <YYYY-MM-DD> \
  --lease-handle "$PUBLICATION_LEASE_HANDLE" --notice-output <PRIVATE_NOTICE_JSON>
jobs/scripts/r2_transfer.sh push-market-cutover <PRIVATE_CUTOVER_RECEIPT_JSON>
```

48は既存記事48→49変換を中間copyへ行ってから49→50へ進み、49はscope変換だけを行う。旧`article-cutover`command/receiptは新形式へ黙って読み替えない。旧形式の部分公開は対応旧codeと元receiptで先に完了し、fresh copyから新cutoverを開始する。

専用pushは元bundleの全ETag、lease、公開済み新origin、対応モデル世代を照合し、各keyの条件付きPUTと通常machine receiptを使う。PUT直前のupload候補digestをprivate cutover receiptに保存する。空DBの再取得置換ではなく、旧原本/coverage/operator-owned値を残したadditive変換である。
push後のmarket `source-seal`/`source-inputs`は公開済み`machine-manifest.json`とmarket/runsのHEAD一致、固定lake releaseを確認する。経済のseal/inputは単独L1 ownerが読戻した経済ETagだけに結び付ける。pull時の`.r2-generations/*.etag`はCAS baseとcutover再開の根拠なので、push結果へ手で書き換えない。

残存48/49 stageはimmutable原本を直接編集しない。`market-cutover prepare-copy --kind stage`で別pathへ変換し、stageのresult.jsonを同じ作業directoryへ保って`integration.source_handoff replay-copy --source <OLD_OWNER> --database <HYDRATED_SCHEMA50_COPY> --directory <CONVERTED_STAGE_DIRECTORY>`で旧ownerの限定再生を行う。変換済みcopyを原stage keyへ再uploadしない。切替中に旧writerが動いた場合も新旧stageを区別し、48/49作業copyの明示変換か旧owner再生で取り込む。取得時刻/receipt identityは変えず、再開前にpending一覧を照合する。旧source inputは現正本から全groupを再発行し、そのschemaを確認してから取得を再開する。

| 失敗地点 | 継続方法 |
| --- | --- |
| pointer前 | 旧公開を維持。元ETag・originと作業copyを再確認し、未公開処理だけをやり直す。 |
| pointer後/store前 | 新releaseとlocal origin・モデル世代を照合。元ETagが一致する間に専用cutoverで対応store反映を完了。旧codeへ戻さない。 |
| machine部分PUT/PUT応答不明 | 同じcutover receiptと成果を保全。`r2_transfer.sh resume-market-cutover <RECEIPT>`は現ETagを読み、変化したkeyを実downloadして記録済み候補digestと照合し、未到達keyだけCAS反映。候補不一致なら停止して新writerの差分を調査する。古いbundleを盲再送・machine receipt削除で代替しない。 |
| machine成功/seal失敗 | 既に保存した原本と公開releaseを読戻し、同じapplied.jsonからsealを完了する。再取得しない。 |
| seal成功/input失敗 | 正本・公開世代を保ち、`integration.sources inputs --group <GROUP> --identity <NEW_EXECUTION_ID>`で対象groupのinputだけ再発行。取得再開は該当input発行後。 |

新datasetはoptionalでも旧reader/catalogが拒否する。新公開後は50/catalogを保った前向き修正を基本とし、新モデルを止める場合も保持記事datasetを落とさない。通常pushと専用cutoverを混ぜず、実行済みの停止/drain/変換/公開/machine/seal/inputを個別に記録する。本実装に本番GC・削除・追加課金は含めない。

## ローカルからクラウドを更新する

変更codeをmainへ反映し、lake対象tableは**変更前**に現行releaseからhydrateする。変更後のhydrateは成果を上書きする。長い取得・準備を先に済ませ、production read/merge/publishは同じpublication lease内で行う。

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

失敗時もreleaseするため関数内でset -eを使わない。busyならowner・expiryとrun/processを確認し、blind retryしない。release失敗はhandleを保つ。GitHubのcloud-publish queueだけではlocal writerを排除しない。push-app/publish.shと初回seedはこのlease対象ではない。

対象に合う一経路だけを使う。lake変更はpublish-lake→push-market、store-localだけならpush-market、経済原本だけならpush-indicators。直前のbundleからlocal daily全体を実行した時はpublish-lake→push-machineとし、market/runsの2 storeを個別pullで組み替えない。経済の実行copyが必要なら別にpull-indicatorsを行い、L2用途で経済を再pushしない。

```bash
with_publication_lease bash -c 'jobs/scripts/r2_transfer.sh publish-lake && jobs/scripts/r2_transfer.sh push-market'
with_publication_lease jobs/scripts/r2_transfer.sh push-indicators
```

merge・origin・schema・取得ETag/CASの失敗では公開を止める。lake→machine store→受領sealの順を保ち、coverageだけが先に進む状態を作らない。

malformed leaseは自動修復しない。GitHubとlocalにactive/pending writerがないと確認した場合だけexact coordination/publication-lease.jsonを調べ、対象1keyの削除後に通常acquireで再作成する。prefix削除や手書きJSON上書きを通常運用へ入れない。

## application DB を反映する

### 通常のapplication更新

作業開始前に、空の作業領域へpull-appしてDBとstores/.r2-generations/baibai.sqlite.etagを一組で取得する。そのDBを正規CLIで更新し、取得ETagを保持する。

```bash
jobs/scripts/r2_transfer.sh pull-app
```

更新済みDBと取得ETagがある場合は、再pullせず次だけを実行する。既存DBがあればpull-appは拒否する。DBだけを別世代へ替えてsidecarを流用しない。

```bash
jobs/scripts/publish.sh
```

publish.shは引数なしで使う。台帳preflight、取得世代への条件付きbackup/PUT、次更新用ETag記録を行う。保有quote/basis不足による時価未評価は実取引保存の障害と区別する。未知世代を現在HEADで補わない。

application backupは実行時JST日付の日別keyで、同日keyを置換する。古い世代の整理を行わない依頼ではR2_APP_BACKUP_PRUNE=skipを指定できるが、同日置換は変わらない。元世代の保全や置換が依頼範囲に含まれるか確認する。

保存、R2反映、読戻しを別々に確認する。対象Proposalのimmutable IDと参照評価を読み戻し、最新候補へ固定Selectionを付け替えない。競合なら未公開変更を退避し、最新正本へ必要差分を正規serviceで再適用する。PUT後の記録失敗を未反映と決めて旧DBを再送しない。

current schemaだけを開く。schema変更はsource versionを限定した一回移行を別コピーで検証し、対応codeとstoreの反映を同じdeliveryで終えたらtoolを撤去する。旧DB/keyや完了済みcutoverを現行正本へ再適用しない。

### application DB を復元する

application DBは判断とledgerを保持するため再生成しない。通常の判断更新は前節の取得→正規CLI更新→反映に従う。この節は、明示的に選んだ過去snapshotへクラウド正本を戻す復旧である。
全writerを停止し、未公開の作業コピーをconsistent snapshotで退避する。次に空の作業領域へ`pull-app`し、復旧で置き換える現在のクラウド世代とDBを確保してから以下を同じshell sessionで実行する。現在の正本が取得・検査できなければ通常更新経路での復旧を止める。key消失は[初回seed](#storeをseedする)の既存key確認を含む別の復旧判断に戻し、無条件PUTへ迂回しない。
復旧候補はR2の日別世代、または出所・時点を照合できるローカルcheckpointから選ぶ。ローカル候補を自動的に正本より新しいとみなさない。

**1. 復元前backup。** 取得した現在の正本のWAL込みconsistent snapshotを作る。backup失敗なら復旧へ進まない。

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

**2. 候補選択。** ローカルcheckpointはbackup commandが検証済みsnapshotを直近10世代保持する。

```bash
ls -t stores/application/backups/ || exit 1
read -r -p '復元するcheckpoint path: ' target || exit 1
test -f "$target" || exit 1
```

R2候補を選ぶ場合は上の選択を行わず、R2の一覧を確認する。

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

一覧取得成功後、選んだexact keyを一意なstaging fileへ取得する。

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

**3. 共通検査。** 出力を読み、integrityがexact `ok`、foreign key違反が0行、user_versionがcurrent codeのschemaと一致する場合だけ進む。sqlite3のexit 0だけでは合格にしない。runtime自動移行はない。

```bash
(
  set -e
  test -f "$target"
  sqlite3 "$target" 'PRAGMA integrity_check;'
  sqlite3 "$target" 'PRAGMA foreign_key_check;'
  sqlite3 "$target" 'PRAGMA user_version;'
)
```

**4. 配置と照合。** 検査合格とwriter停止を確認後、copy成功前に現在の作業コピーを動かさない。ここだけは明示的な復旧として、取得した現在のクラウド世代を保持したまま検査済み候補へ置換する。

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

ledgerを復元対象の時点と照合し、不一致なら手順5へ進む。照合成功後は取得世代を保ったまま再pullせず、前節の`publish.sh`で反映してから判断業務を再開する。公開途中の失敗では検証済みlocalを戻さず、upload・読戻しの到達点を確認して反映側を復旧する。ledgerを`head`へpipeしない。

**5. ローカル復元の検証失敗時。** 復元前backupがある場合だけ、次で戻す。backupがない場合は判断を再開せず、別候補の取得・検査へ戻る。

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


<a id="fixed-release-read"></a>
## 固定releaseを読む

最初にcurrentを一度解決し、release IDとmanifest SHA-256を固定する。以後は同じmanifestが列挙したdataset/objectだけを読む。optional datasetなしは未収録で、他releaseのpartitionやprovider値で穴埋めしない。

```bash
uv run baibai-jobs snapshots resolve --mirror stores --bucket baibai-stores --format json
uv run baibai-jobs snapshots resolve --mirror stores --bucket baibai-stores \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

resolveはmanifestの解決で、data objectの検証や任意SQL実行ではない。

objectのdigest・bytes・row数・schemaを通常のlake readerで照合する。R2転送は共通object store、DuckDBのqueryは検証済みlocal fileが担当し、remote拡張の導入は不要。Webの旧L1 gatewayは廃止したため、認可済みR2読取か完全なローカルmirrorを使う。固定objectを取得できなければ別releaseの値で埋めず、再開時に同じ固定releaseを復元できるか確認する。新しいcurrentを採る場合は別の分析入力として固定し直す。

<a id="local-release-hydrate"></a>
### 完全local mirrorからのhydrate

必要量とsnapshot用容量を確認し、manifest/objectの完全mirrorを認可済み読取で揃える。部分JSON exportで正規market readerを代用しない。

```bash
uv run baibai-jobs snapshots hydrate \
  --mirror <COMPLETE_LOCAL_MIRROR> --store <LOCAL_MARKET_COPY> \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

通常の完全ローカルmirrorはbucketなしで読む。EDINET一覧nativeのcache除去検証を伴う場合は[専用条件](#edinet-list-native-l1)に従い `--bucket` も指定する。不足・破損・容量不足なら停止し、途中でreleaseを替えない。hydrateは対象tableを置換するが、store-local coverageの取得記録は生成しない。旧contractのreleaseは元commitのcodeで読み、runtime互換を増やさない。

Parquetを直接読む場合も[固定reader](../engine/src/baibai_engine/storage/snapshots/reader.py)の `resolve_release` → owner datasetの `accepted_dataset` → `selected_partitions` → `materialize_partitions` を使い、返された検証済みpathだけを `lake_session` へ渡す。globや別releaseのobjectを混ぜない。通常のSQLite利用は上のhydrateを使う。

<a id="edinet-research-query"></a>
### EDINET factsの書類選択

同じ固定releaseを作業SQLiteへhydrateし、保存inventoryからlatest annualと訂正を選ぶ。

```python
from datetime import date
from pathlib import Path
from baibai_engine.machine_analysis.observations.financials.edinet_facts.selection import (
    latest_annual_filing,
    usable_document,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.store import (
    document_inventory,
)

doc = latest_annual_filing(
    document_inventory(Path("/tmp/research-fixed.sqlite"), date.fromisoformat("<YYYY-MM-DD>")),
    "<TICKER>",
)
if doc is None or not usable_document(doc):
    raise ValueError("年次書類を確定できません。原典を確認してください。")
```

cutoffはJST日末でintraday PITではない。選択なしは書類不存在でなく、未抽出・公開状態未確認なら原典へ戻る。segment/debtをsource_doc_idで選び、metadata期間・連結・通貨を揃える。全債務や資金余命を抽出行だけで確定しない。

### 保持とGC

このGCはローカルlake mirrorだけを対象とし、current releaseから到達しない対象prefixのobjectをgrace後の候補にする。native L1・共有固定入力・calibrationは別寿命で、R2は削除しない。dry-runを読み、apply時に再planしたhashが一致した場合だけ削除する。

```bash
uv run baibai-jobs snapshots inventory --root stores
uv run baibai-jobs snapshots gc --mirror stores
uv run baibai-jobs snapshots gc --mirror stores --apply --plan-hash <HASH>
```

immutable prefixのBucket Lockとmutable pointer/stagingを区別する。rollback pointerや全履歴監査を追加せず、問題releaseは正しいstoreから新releaseを前向きに発行する。

## R2 transferの安全境界

machine pushは前世代をkey.bakに1世代保存し、次pushで置換する。applicationだけ日別backupを保持する。market/runsの2 storeは原子的反映ではなく、全PUT後にmachine-manifest.json receiptを書く。経済L1は単独CASと読戻しで公開し、このreceiptへ含めない。schema修正でstore全体を古いcopyへ戻すと後続事実を失うため、sourceを限定した移行を使う。

machine前世代を復元する場合はwriterを止め、対象bucketとexact key/key.bakを確認し、aws s3api copy-object --cli-read-timeout 300を使う。復元後はfresh pullし、receipt・generationの整合を確認する。盲目的なcopyやprefix削除は行わない。

### 部分 push からの復旧

先行keyだけPUT成功すると旧receiptと不整合になりpullが停止する。同じpush/dailyを再送せず、到達点をlogで確認する。local成果を残す必要があり現行origin/merge条件を満たすならpush-marketで反映してreceiptを更新する。経済のpush-indicatorsはmarket/runsの部分pushを修復も追認もしない。runsだけの成果は取得世代条件を満たすpush-runsを使う。全storeの成果を反映する場合は元bundleの世代条件を満たすpush-machineを使う。

receipt再生成で復旧する場合だけ、対象がbucket直下のmutable machine-manifest.json一件であることを確認する。

```bash
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  aws s3api delete-object --bucket baibai-stores --key machine-manifest.json \
    --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
)
```

次pullは未検証setとして現世代を取得し、次の完全pushで新receiptを作る。その一周のcross-section保証が落ちるため、receipt一致を再確認するまでを復旧とする。失敗時に追加削除・再dispatchを繰り返さない。

## 指標観測とregistryの修正

原本訂正はsourceの再取得か明示再構築で行い、local DELETEをcloud mergeへ流さない。系列の追加・退役・改名や式変更はL2定義、外部取得条件の変更だけData Collectionを直す。式変更だけで再取得・scalar撤回・registry世代台帳を要求しない。

```bash
uv run baibai-jobs acquire-economic list
uv run baibai-jobs acquire-economic refresh <SOURCE_ID> --all-history --end <YYYY-MM-DD>
uv run baibai-engine market-context reading --asof <YYYY-MM-DD>
uv run baibai-jobs validate-market-context-stores
```

validateは原本payload・件数等の検査で、L2数式やL3全履歴の証明ではない。原本更新時だけpush-indicatorsへ進む。[FREDの同値確認](#fred確認stateと経済schema-1213の切替)はfresh正本へ統合し、他sourceは同値・空応答も実受領として発行する。全履歴取得は必要なsourceと期間に限り、失敗でcommit済み原本を取り消さず、L2欠測のため全取得を繰り返さない。

optionsのall-historyは保存済み古い日を再利用し、直近と不足日を取得する。古い訂正は日付範囲を明示する。購読rolling境界の提供不能、空応答、途中失敗を区別する。

PMI manifestは公表翌週にdry-run→更新→対象月refresh→公表値照合→commitする。

```bash
uv run python -m tools.pmi_manifest --dry-run
uv run python -m tools.pmi_manifest
```

PDFのPMI名・embargo月・headline対象月を照合する。indexの最新月以前はarchive snapshotのrelease IDで既存月を確認し、穴を飛ばさない。翌月PDFのrestateも元対象月を保つ。URL訂正と同URL本文差替えを分け、抽出式だけなら保存PDFを再読する。

provider別jobは、失敗が確認済みHTTP 403だけでprivate受領保存に成功した場合のみDiscord通知を省略する。失敗status・欠測・Actions失敗は残し、ログはUNAVAILABLE。403を未公表と推測せず、他の失敗・取消は通知する。公表前の当月値を要求するjobや日程台帳は足さない。

<a id="edinet-metrics-revision"></a>
## EDINET metricsの意味変更

[抽出意味revision](../docs/reference/data-sources.md#edinet-metric-revision)変更時だけ実施し、Company Research facts revisionと区別する。隔離コピーで保持CSV ZIPを再抽出し、期待差分・quality/failure・coverageを確認する。

main反映とwriter非競合を確認し、同じlease内でfresh pull/hydrate→比較元backup→extract-edinet-metrics→publish-lake→push-market→固定release読戻しを行う。原本取得が必要な時だけrefresh-edinet-metricsを使う。対象as-ofと保存値・revision・store-local情報を照合する。

期待外差分、取得失敗、世代競合、lease失効で後段を止める。準備コピーの本番復元、再抽出後のhydrate、旧値のrevision付替え、通常dailyによる意味移行は行わない。

<a id="edinet-source-archives"></a>
## JPX決算PDFの表示額による補完

契約を変えず、既知の公開URLから取得済みのPDFを明示取り込みする。通常dailyの自動取得には追加しない。

`financial-pdf-source --receipts <JSON> --directory <NEW_PRIVATE_DIRECTORY>`はHTTPなしで新しい有限handoffを作る。JSONは1〜8件の配列で、各要素は`requested_url`、`file`（JSONからの相対path）、`sha256`、`request_started_at`、`captured_at`、`http_status: 200`、`content_type: application/pdf`。原本・URL・hashを事前照合する。時刻は実取得ログのtimezone付きUTCで、captured_atは本文保存・hash計算を含み得る完了時刻。mtime・公表日・移行時刻から作らない。schema移行・既存原本書換えは不要。

既存JPX原受領bundleの`financial_statements`を`python -m baibai_jobs.integration.source_handoff stage --source jpx-financial-pdfs --directory <DIRECTORY> --stage-id <NEW_ID>`で共通統合・固定release・モデル生成へ渡す。株式交付・普通株転換の完了開示は同じreceipt形式に`--collection capital_actions`と`--source jpx-capital-actions`を使う。不対応PDFも保持し未解決とする。取得網羅性を保証せず、発行済株数だけで同日時点の自己株数を推定しない。

対応形式・不受入理由は[抽出owner](../engine/src/baibai_engine/machine_analysis/observations/financials/pdf_statements.py)、採用は[選択owner](../engine/src/baibai_engine/machine_analysis/observations/financials/pdf_selection.py)に従う。予想を実績へ転用せず、丸め方不明は残す。文書vintageの補助表示と最新期・訂正を先に固定する選択を区別し、企業財務/成長で同じ選択を使う。表示額由来の近似で、EPSは円利益合成後に自己株控除後株数で一度だけ割る。

同日順序不明・本文競合・最新訂正欠測を旧PDF/比較で埋めず、後続期へ旧TTMをcarryしない。年度の当期額だけは、選択ownerで不変を確認した旧PDFを使える。最新訂正の比較欄まで確認済みとしない。未照合vintageを混ぜず、独立した当期報告・株数・価格は残す。選択は保持入力内で、未受領訂正の網羅を保証しない。

旧保存結果を再serializeせず、新generationで反映する。fixture CI、保存実PDFとの照合、母集団差分、固定release読戻しは別に確認する。

## EDINET原本とCompany Research facts

明示market SQLiteのedinet_source_archivesを使い、端末ZIPを暗黙に取り込まない。stored_onは初回保存UTC日で、公表・初回利用可能日ではない。受領ZIPとL2の選択文書・metricsを分け、取得失敗で既存ZIPを上書きしない。

<a id="edinet-research-facts"></a>
未初期化storeだけ、整合する隔離marketとdocument inventoryで初期抽出する。

```bash
uv run baibai-jobs maintain-market refresh-edinet-facts \
  --asof <YYYY-MM-DD> --initialize --sqlite-path <STAGED_MARKET_SQLITE>
```

失敗後は同commandで再開してok書類を再取得せず、保存原本だけの再計算はextract-edinet-factsを使う。ticker限定では全体initializedを作らない。未対応形式の理由付きokと取得/破損のfailedを分け、代表原典・missing reasons・export/hydrateを確認してcode・L1・coverageを一組で反映する。

dailyは未初期化をskipし、初期化後の新規年次/訂正を独立処理する。最新訂正失敗を旧版で埋めず、failed docIDの原因を直して再開する。利用側の選択は[固定release](#edinet-research-query)に従う。


<a id="external-assessment-acquisition"></a>
## 外部評価の取得・再開

限定取得は通常TLS/proxyで少数銘柄を直列に読み、403/429を迂回しない。試行ごとに別のprivate captureを保存し、既存current-schemaの隔離marketへimportする。

```bash
uv run baibai-jobs external-assessment collect --source minkabu --tickers 7203 4436 --output <NEW_PRIVATE_CAPTURE>
uv run baibai-jobs external-assessment import --input <PRIVATE_CAPTURE> --db <LOCAL_MARKET_COPY>
```

importは初期化・移行をしない。parse失敗でL1が残ってもL2は部分追記しない。原本/観測件数と再投入0を確認し、正常null・未試行・HTTP/parse失敗を分ける。

### 日次取得と固定コホートの再開

起動枠・cycle・期限は[workflow](../.github/workflows/cloud-external-assessment.yml)、固定依存・再開・attempt履歴は[取得owner](src/baibai_jobs/acquisition/external_assessments/daily.py)を参照する。元master/calendar・parser/request条件を保持する。native切替後は独立入力、旧経路は固定lake/readyを使い、依存不足を全market取得・日次公開・TradingView起動や過去masterの読み替えで迂回しない。

```bash
gh workflow run cloud-external-assessment.yml --ref main \
  -f source=monex -f batch_id=<LABEL> -f master_asof=<YYYY-MM-DD>
```

同sourceのlocal/Actionsを重ねず、queue後にcycleを再確認する。scheduled再開は保存済み `external-assessment-daily-<source>-YYYY-MM-DD`、手動labelはsource prefixなし。欠落planの再作成やbinding hashの手修正はしない。期限内の未試行pairだけを再開し、保存受領を再GET・再日付化しない。保存済みの失敗pairを再取得する場合は別labelにする。

### 原本handoffと公開

新原本は `external-assessment/retained/`。旧 `raw/` だけが3日削除の短期handoffで、厳密72時間や長期L1ではない。実lifecycle・残存を確認し、[保全](src/baibai_jobs/acquisition/external_assessments/raw.py)は同一bytesのcopy/読戻しだけを行う。元key/stage/IDを変えず、期限切れの復元を保証しない。retainedの自動削除やlifecycle変更はしない。

```bash
uv run python -m baibai_jobs.acquisition.external_assessments.raw --preserve-run-id <EXACT_RUN_ID>
```

取得はlease外でacquire→inspect→immutable ready。通常 `ready --cumulative` はplanを閉じず、[共通統合](#actions-write-boundary)が固定集合を公開・読戻してackする。後着の小集合で後退せず、ack済み原本はcanonical hydrateのID/hashで照合する。market取込と固定lake読戻しまで長期L1反映成功にしない。

403/429・未完了を自動で閉じない。確定分で終える明示partialだけ、上のworkflowをsource単独で `-f operation=stage_confirmed_partial` として実行する。取得は再実行せず、実公開とterminal確定は次回共通統合が行う。

hard kill前の未保存試行は再GETされ得る。pointer成功後のterminal失敗は既存terminalを読み、sealedはno-op、未sealedはfresh hydrateから再適用する。破損sourceを閉じず正常sourceを進め、未確認件数を0にしない。完全/部分、原本/観測、release/hash・readback・lease解放を照合し、通知やCI緑を成功としない。`history_digests` は保存後baselineで、更新前の全履歴保全の証明ではない。

<a id="tradingview-expectations"></a>
## TradingViewの毎日取得・明示受領・保存・分析

本人確認済みの利用範囲と毎日取得の指示に従い、[公式MCP](https://www.tradingview.com/mcp/docs)と既存 `tradingview-runtime` 認証を使う。一般FAQだけで停止・削除・入力除外せず、新client/credential・権限・課金・再配布へ許可を広げない。認証変更は別途確認し、認証失敗を成功/保存済み省略にしない。

[日次workflow](../.github/workflows/cloud-tradingview-snapshot.yml)は一snapshot完成までの再開枠。上限・間隔・cooldownは[collector](src/baibai_jobs/acquisition/tradingview/collector.py)に従う。原応答の未取得銘柄だけを取得し、L2行だけでは取得済みとしない。正常原応答が充足してL2だけ不足する `originals_saved` は再GETせずrebuildする。L2だけ残るcopyでは当cycleの不足原本を受領して既存L2を保ち、`native_added` と `rows_added` を分ける。後から受け取った原本を既存L2の生成元とみなさず、過去cycleの穴も現在応答で埋めない。HTTP/tool失敗や銘柄/要求列不足を充足にしない。

`snapshot_date` は09:00 JST切替のcycle日。保存暦に従い営業日15:30、休日09:00から翌09:00未満まで取得する。休日は直前営業日のexact masterと連続calendarを使う。日跨ぎも同cycleを続け、翌09:00以降に旧不足を現在応答で埋めない。実受領UTC・既存key/bytesを付け替えず、翌朝受領を前日の既知情報にしない。

### 原応答をL1へ保持する

`receive` は外部通信せず、既存current-schema copyへ[NativeToolCapture](src/baibai_jobs/maintenance/tradingview_capture.py)を保存する。SDK応答全体をそのまま保存し、金融値から原応答を作らず、失敗応答も保持する。同IDは全内容一致の再送だけ、別呼出しは別IDとする。capture/金融値を公開artifact/logへ出さない。

```bash
uv run baibai-jobs tradingview-source receive --sqlite <LOCAL_MARKET_COPY> --capture <PRIVATE_NATIVE_CAPTURE_JSON>
```

### L2検証と正本への反映

新受領は[共通統合](#actions-write-boundary)へ渡し、同cycle最初の正常行と原receiptを保つ。元master/calendar不一致を黙って採用せず、取得copy全体を正本へpushしない。

```bash
uv run python -m baibai_jobs.integration.source_handoff export --source tradingview \
  --asof <JST_RECEPTION_DAY> --since <BEFORE_RECEPTION_UTC> \
  --database <LOCAL_MARKET_COPY> --directory <NEW_PRIVATE_HANDOFF>
```

[offline rebuild](src/baibai_jobs/maintenance/tradingview_rebuild.py)は選択receiptで一日分のL2を置換する。隔離copyに必要な受領集合を渡し、部分受領の追加で既存日を一部receiptに置換しない。

```bash
uv run baibai-jobs tradingview-source rebuild --sqlite <LOCAL_MARKET_COPY> --asof <YYYY-MM-DD> --receipt-id <RECEIPT_ID>
uv run baibai-engine external-assessment show --ticker <TICKER> --cutoff <UTC_TIMESTAMP> --db <LOCAL_MARKET_COPY>
```

欠測・通貨・対象期・cutoffの解釈は[取得元](../docs/reference/data-sources.md#tradingview)へ。L1保存・L2成功・公開を区別し、取得成功・省略・部分・原受領のみ・統合未達を確認する。保存判断と旧除外履歴を改変せず、新生成へ旧除外を流用しない。

## Storeをseedする

初回だけ、4 keyすべて不存在を確認してseed.shを使う。既存keyが一件でもあれば停止し、通常更新/復旧へ戻る。1〜3 keyの部分seedは、供用前・writerなし・対象exact keyを確認できた場合だけ初回復旧として扱い、通常pull/pushや無条件上書きで直さない。seed後のapplication更新はpull-appで正本と世代を取り直す。

## 定期maintenance

既存due taskを完了して必要な次回分だけ作り、同じ期限・対象・問いを重複登録しない。開発変更はIssueで扱う。

較正は仮説・期間・利用先を明示した旧ER/FV・四Approachの研究/保守用で、新モデル/候補政策に月次再構築や旧context更新を要求しない。[意味・force・配布条件](../docs/reference/estimate-calibration.md)に従い、月次比較は前月までを同じrulesで扱う。

```bash
uv run baibai-engine historical-evaluation calibration-build --start <YYYY-MM-DD> --end <YYYY-MM-DD>
uv run baibai-engine historical-evaluation calibration-evaluate --out /tmp/calibration-evaluation.yaml
```

旧Selection用の配布更新だけはevaluateへ `--run-purpose empirical_change_evidence --decision-subject estimator_policy --required-asof <YYYY-MM-DD> --required-metric er_level_calibration --context-out <PUBLISHED_YAML>` を加える。目的・全対象月・metricを先に固定し、required-asofは各月分繰り返す。identity・期限・不足と配布差分をGit reviewし、記述context成功をformal evidence成立としない。context-out省略時は配布artifactを更新しない。

PMIは公表翌週、TSE資本政策/JPX廃止は翌月15日以降に既存refresh/buildと必要な公開を行う。全履歴監査を定例化しない。依頼された注文結果は `measure_limit_outcomes` で測り、価格差を逸失利益や指値規律の因果効果としない。

通知webhook rotationはDiscord新規/再生成→secret `DISCORD_WEBHOOK_URL` 更新→通常通知確認。旧webhookはDiscord側で消すまで有効で、過去Screeningの再実行は不要。

<a id="web-serving"></a>
## 保有・運用Webの表示更新

表示はdashboard・outcomes・tasks・latest-proposalの4 view。旧候補・macro・調査一覧・L1公開取得APIを復活させない。coreの日次統合が保存した後にapplicationを取得し、同じローカルmarketと合わせて生成・配信する。application取得・表示失敗で生成モデルやsource封印を止めたり巻き戻したりしない。台帳・taskの明示更新後は既存のpublish.shがapplication条件付き反映後にcloud-materializeを起動する。

手動再生成はmain上のcloud-materializeを実行する。検証済みapplicationを必須とし、marketをpullして固定releaseからhydrateする。market取得・hydrateの失敗はrunの失敗として残すが、未検証marketを読ませず、時価等を未評価にして最新の台帳fact・task・保存済み運用成績・保存済み提案を配信する。赤いrunでもview公開が完了している場合があるため、marketの復旧と公開結果を分けて確認する。旧run・economic storeは不要。新dashboardは`views/portfolio-dashboard.json`へ保存し、旧UIが読む`views/dashboard.json`を上書きしない。公開処理は4 viewをR2から読戻し、生成したJSONとの全bytes一致を同じlease内で確認する。Web切替前にはこの読戻しまで成功したrunを確認する（切替前のWorkerは新しいrouteを読めない）。過去のuploadだけで読戻し未実施なら、mainのcloud-materializeで再生成・公開・照合する。その後UIの対象HEADを確認し、web workflowをmainで明示dispatchする。切替後に認証付きAPIと画面を確認する。PRのCI成功は本番更新ではない。旧object・Worker secretは削除しない。

```bash
uv run python -m baibai_web.materialize --output-dir /tmp/baibai-serving
```

これはローカル生成だけであり公開しない。cloud workflow内だけが `jobs/scripts/r2_transfer.sh upload-portfolio-views /tmp/baibai-serving` で4 viewを公開する。資本・損益・保有の欠測とエラー、タスク、運用成績、提案のready/empty/unavailableを読戻しで照合する。提案0件のempty表示成功と、実際の保存ID・基準日・発行時刻を持つ本文カードの受入は区別する。

<a id="web-authentication"></a>
Worker secretの`VIEW_PASSWORD`はowner用Bearer、`READ_ACCESS_TOKEN`は共有用Bearerまたは`?share=`で、どちらも同じ4 viewを読める独立credentialである。新しい表示もこの既存認証で保護し、追加のpasswordや権限区分を設けない。R2転送credentialとは別で、owner passwordの変更やUI再配備だけでは共有tokenを失効できない。共有の失効は配備先Workerの`READ_ACCESS_TOKEN`自体を更新、または削除／空値で無効化し、旧tokenが拒否され、必要な新tokenとownerの読取が維持されることを確認する。owner側も`VIEW_PASSWORD`自体を更新して旧値の拒否を確認する。実環境の変更は別の明示依頼に従い、秘密値や共有URLを本文・shell引数・logへ残さない。


## 判断直前の多市場補助観測

`market-evidence` はTradingView quote/5分足のprivate原受領で、日次外部評価の充足・価格master・正式提案終値とは別。[定期workflow](../.github/workflows/cloud-tradingview-market-daily.yml)の途中失敗でも原本を保持するが、全件成功/統合済みとは限らない。朝の判断には保存参照か新しい明示取得を使い、旧値を現在値にしない。

定期/明示取得は同じ[有限capture契約](src/baibai_jobs/acquisition/market_context.py)を使う。実tool名・引数・要求/受領時計・SDK応答を受領時に保持し、時計を後から推定しない。

1. `baibai-jobs market-evidence prepare --capture <CAPTURE> ... --directory <NEW_PRIVATE_DIR>` で固定し、`stage --directory <DIR> --stage-id tradingview_market-<ID>` で保存する。
2. 別sessionは返却stage ID/manifest SHAで `download --directory <NEW_DIR> --stage-id <ID> --manifest-sha256 <SHA>`、続いて `read --directory <DIR> --receipt-id <RECEIPT> --sha256 <CAPTURE_SHA>` を使い、再GETせず観測とAssessment入力を得る。未保存local captureに耐久参照はない。統合後も同じreceipt/hashを読む。

生成/L3は固定economic入力の同じ参照・時計・欠測を使う。[読取owner](../engine/src/baibai_engine/machine_analysis/observations/prices/tradingview_market.py)は最新失敗を旧成功価格で埋めず、評価時刻と基準JST日末の両方でcutoffする。前日基準の寄前判断へ当朝の夜間受領を含めるなら、別の明示入力として固定する。

休場・遅延・限月切替を確認し、同一商品/basisの端点を比べる。受領時計は取引時計でなく、連続先物は現物でない。provider changeを日本引け基準、barの`t`を最終約定、update_mode不明をリアルタイム、最後の足を確定値としない。Assessment/Reviewは同じcaptureを読み、判断が変わる新価格は新入力で再Reviewする。旧判断へ遡及挿入せず、原文/private handoffを公開Issueへ転載しない。

<a id="jquants-native-operations"></a>
## J-Quants native L1の共通操作

以下の7sourceは、取得・原本保持とL2再生成を分ける。通常設定は[財務](src/baibai_jobs/financial_cutover.py)・[日足](src/baibai_jobs/price_cutover.py)・[master](src/baibai_jobs/master_cutover.py)・[取引暦](src/baibai_jobs/calendar_cutover.py)の4sourceが既定/Actionsとも `true`。明示 `false` は旧入力の有限検証用で、本番rollbackに使わない。他3sourceの接続状態は各節で確認する。codeの既定値・運用設定・移行/公開成功は別の事実である。

取得CLIは `jquants-<source>-native`。`initialize` は新規の空namespaceだけに一度行い、既存原本の移行失敗やhead消失を迂回しない。`plan` / `acquire` は毎回新しい `--directory` と次の対象期間を渡す。応答不明は `resume` で確定headを確認し、保存済みattemptは同じdirectoryの `commit-work` でidentity・時刻を変えず再開する。failed/held・partial・未試行を正常空にしない。

| source | plan/acquireの期間指定 | 固有の取得条件 |
| --- | --- | --- |
| financial / price | `--start` / `--end` | 日別原本と訂正確認 |
| master / calendar | `--start` / `--end` | 明示訂正だけ `--force`。連続日足のoverlapを流用しない |
| margin-alerts / short-sale-reports | `--asof`、任意の `--start` | 休場でも公表を確認 |
| margin-interest | `--asof`、有限backfillは `--start` / `--end` | 固定calendarが必要。`--force` は明示区間の訂正再取得だけ |

再生成は `rebuild-<source>-native` に `--work-directory` / `--start` / `--end` / `--output` を渡す。新規の観測出力と `input.json` を作り、既存出力を上書きしない。calendarだけは後述の専用引数を使う。保存入力の再生では、generation/Selectionの正確なInputSnapshot JSONを `--saved-input` へ渡し、summaryや最新headで代用しない。保持条件は[保存責務](../docs/architecture.md#store-authority)に従う。

本番切替は以下を一組で行う。移行・全期間取得を通常dailyへ代行させず、合成testやcodeのmergeを本番切替の証拠としない。

1. 実bucketの原本/共有固定入力prefixに必要なread/write権限と期限削除対象外を確認する。設定を読めなければ未確認であり、初回読戻し成功では代用できない。
2. 対象の旧writer・daily・queued jobを止め、旧release ID/SHA256・UTC境界・統合済みを含む全対象stageを固定する。`prepare` の `blocked` を解消し、原本・宣言identity・scopeを確認する。後着stageがあれば境界を固定し直す。
3. 空targetを一度だけinitializeし、同じplanで `apply` → 全packのcold `verify` → 旧投影との `compare` を行う。移すのは対象sourceだけで、mixed stageの他sourceは保つ。旧投影との差とcoverage不足を確認し、意味差を受け入れる場合は明示する。
4. 全writer/consumerのcode・設定を揃え、初回公開の固定input・coverage・native lineageと次hydrateを読戻してから定期起動を再開する。異常時はwriterを止め、確定head・plan・旧releaseから再開先を決める。flagだけを戻して旧writerを並行稼働させない。

共通command例（対象sourceと固定値を置き換える）。`apply` 前の確認・初期化は省略しない。

```bash
uv run baibai-jobs price-native-cutover prepare --directory <PLAN_DIR> \
  --work-directory <NEW_MIRROR> --release <FIXED_OLD_RELEASE> \
  --manifest-sha256 <FIXED_OLD_SHA256> --known-at <UTC_BOUNDARY>
uv run baibai-jobs price-native-cutover apply --directory <PLAN_DIR> --work-directory <APPLY_WORK>
uv run baibai-jobs price-native-cutover verify --directory <PLAN_DIR> --work-directory <COLD_WORK>
```

`<source>-native-cutover compare` も同じ `--directory` と別の `--work-directory` を使う。price / margin-alerts / short-sale-reportsは初回運用日の `--asof`、margin-interestは比較終端の `--asof` と固定 `--calendar-head` を追加する。他3sourceに追加日付はない。全optionは各commandのhelpを参照する。

切替後のmixed stageは、移行した原本の全page fingerprint/bytes・宣言IDを照合して対象sourceだけ再投影から外す。未移行・改変・不足を黙ってskipしない。旧release/stageには同値確認のexact bytesもあるため、自動削除しない。

<a id="margin-alerts-native-l1"></a>
## 日々公表銘柄margin-alert native L1と通常経路

`BAIBAI_MARGIN_ALERTS_NATIVE` は通常 `jquants-market` とdailyの固定reader/公開を一組で選ぶ。既定 `false`、本番切替は未実施。取得日/overlapは[取得owner](../engine/src/baibai_engine/data_collection/providers/jquants/margin_alerts_native.py)、母集団は[信用残](../docs/reference/data-sources.md#信用残のsourceと日付)に従う。旧market releaseを開く前に原本を確定し、L2成否・品質を取得条件にしない。休場も8暦日の公表確認を行うため、追加GETとprovider予算を切替前に確認する。

### 有限移行と本番切替

[共通手順](#jquants-native-operations)で全market stageから対象原本を移し、予定日までの全保持範囲をcompareする。確認後、sourceとdailyのflagを同時に有効化する。切替後の旧maintenance取得は拒否し、明示native取得を使う。

margin-alertと次節のshort-sale-reportは、通常dailyで一つの固定selectionから投影し、同じsealをreader・モデルinput・公開native lineageへ渡す。[前回cache](src/baibai_jobs/integration/native_projection_cache.py)はcompleteかつParquet/SQLiteの全行一致時だけ再利用し、欠損・改変・partialなら固定L1から再生成する。取得側はcacheを使わず、専用cache namespaceや毎日の全履歴copyを増やさない。固定readerでは全履歴0行も正常空だが、現共通manifestは観測行を要求するため公開できない。両sourceの切替は既存非空履歴の有限移行を前提とし、架空行を補わない。

alertsは変更した公表日だけrawから投影し、year/v2内の月別objectを内容共有する。[投影](src/baibai_jobs/integration/margin_alerts_native.py)のPubDate/AppDate・正常空訂正・partial、`inputs.margin_alerts` と公開lineage、次hydrateを確認する。初回sourceでは独立headの増分と旧stageからalertsが外れることも確認する。PubDate/AppDateをshort-saleの開示日/行順へ読み替えず、全銘柄信用残とも別に運用する。

<a id="short-sale-reports-native-l1"></a>
## 空売り残高報告native L1と通常経路

`BAIBAI_SHORT_SALE_REPORTS_NATIVE` はsourceとdailyを一組で選ぶ。既定 `false`、本番切替は未実施。日付/順序は[取得](src/baibai_jobs/acquisition/jquants_short_sale_reports_native.py)・[固定reader](src/baibai_jobs/integration/short_sale_reports_native.py)に従う。旧market/L2を使わず、直近7日前〜当日の8暦日と保持範囲の不足を再確認する。休場でも確認するため、追加GET・provider予算を確認してから切り替える。

全market stageからの移行は[共通手順](#jquants-native-operations)、cacheと非空履歴の公開条件は[前節](#margin-alerts-native-l1)に従う。compareは開示日と行順のidentityを使う。通常source/dailyのflagを同時に有効化し、旧maintenance writerは使わない。flag・本番writerの変更自体は別操作である。

同値確認ではrawを再読せず、開示日別coverage/確認時計・DiscDate/CalcDate・page/行順・重複を保つ。初回dailyと次hydrateでこれらと正常空/局所partial、`inputs.short_sale_reports` と公開lineageを読戻す。source_coverageはpaired machine SQLiteが保持する。margin-alertとflag・日付・source契約を混ぜない。

<a id="margin-interest-native-l1"></a>
## 週次・全銘柄日次信用残の独立L1

明示CLIのみで、通常market writer・旧maintenance・historical readerは未接続。[指定銘柄](#margin-alerts-native-l1)とは別sourceである。取得/固定読取には必要範囲のnative calendarを用意する。通常calendarの初回53日窓だけでは週次230日・日次2026-09-25以降を満たさない。`unknown_calendar_days` / `unknown_incomplete_requests` を無視せず、unknownを休場へ変えない。

- 週次は230日窓・各ISO週の最終取引日・2営業日公表lagで、2026-09-18残高まで。公表日前の空と最終週の空を再確認する。空の充足には実確認JST日より前の後続2営業日の暦が必要で、不足を残す。有限backfillから現在までの全暦は要求しない。最終週の空は公表日以降、終端後の暦なしでも再確認し、rebuild/compareでも未充足とする。
- 全銘柄日次は2026-09-25〜asofの取引日を保ち、通常は後続1営業日を待つ。空は未充足。7日overlapや230日へ狭めず、9月19〜24日をどちらの系列にも作らない。
- [取得計画](../engine/src/baibai_engine/data_collection/providers/jquants/margin_interest_native.py)は固定暦・実確認時刻・raw件数を使う。非空原本のL2 partialを取得条件へ戻さない。旧maintenanceのpartial再取得は別経路のまま。

取得directoryの `plan.json` / `calendar-fixed.json` とattemptを保ち、`commit-work` でも最新暦へ差し替えない。同値確認は確認時計だけを更新し、元HTTP pageの時計を変えない。有限backfillの終端は含む。

```bash
uv run baibai-jobs jquants-margin-interest-native acquire \
  --directory <NEW_WORK> --asof 2026-11-05 --start 2026-09-18 --end 2026-10-30
uv run baibai-jobs rebuild-margin-interest-native --work-directory <NEW_READ_WORK> \
  --calendar-head <NEW_WORK>/calendar-fixed.json --start 2026-09-18 --end 2026-10-30 \
  --output <NEW_OUTPUT>
```

rebuildは週次9列・日次16列（PubDateを含む）とcoverageを持つ有限copy、`input.json` / `result.json` を出す。`--saved-input` では固定margin/calendarだけを読み、最新head・旧releaseを使わない。歴史promptの完全再現ではない。歴史評価は保存価格日の公表時差fallback・PubDate優先・同日引け除外・観測済26週・35日鮮度・split/share basisを保つ。native暦へ置換せず、比較時は価格も固定する。

### 有限移行

[共通手順](#jquants-native-operations)で全source_pages partitionと全market stageを確認する。境界後の受領・filtered request・page/宣言identity欠落・未知の失敗scopeはblocked。`daily_margin_incomplete` は実在する空/partial原本を要求し、正常空へ昇格しない。

compareは同じ旧release、保存 `--calendar-head` と比較終端 `--asof` を使い、UTC境界後のmargin/calendar確認を拒否する。週次/日次の全保持行を別々に照合し、取得範囲・coverage差が残れば切替根拠にしない。通常writer接続と本番実行は別途の指示・検証が必要。

<a id="price-native-l1"></a>
## 日足native L1の取得・固定読取・有限移行

`BAIBAI_PRICE_NATIVE` は取得とconsumerを一組で選ぶ。[取得owner](src/baibai_jobs/acquisition/jquants_price_native.py)が日付範囲・訂正確認を持つ。切替後は旧 `maintain-market bootstrap-cache` / `backfill-history` で取得せず、専用native CLIを使う。過去generationは `inputs.price` を `--saved-input` へ渡す。

正常空L1と、価格必須の公開policyによる0行拒否は別である。native暦で休場と証明できる未試行日だけ公開coverageから除き、raw未取得は残す。旧v1入力へ遡及しない。[価格入力](src/baibai_jobs/integration/price_native.py)のidentity・補正係数・coverage/lineageを初回公開で読戻す。日足L2は月契約の日別objectを再利用するため、全releaseのobject数・manifest容量上限を確認し、旧月object/releaseは削除しない。

### 旧日足原本と混在market stageからの切替

[共通手順](#jquants-native-operations)でmarket stageの全宣言request IDを照合し、日足だけ移す。compareには初回dailyの `--asof` と通常運用と同じcalendar設定を使う。通常取得窓より古い欠測も確認し、休場未試行と取得失敗を分ける。

<a id="financial-native-l1"></a>
## 財務native L1の明示取得・再生成

`BAIBAI_FINANCIAL_NATIVE` は財務writerとdailyを一組で選ぶ。切替後の旧financial stage resume/replay-copy・bootstrap/backfillは使わず、未移行原本を保持して明示回復する。[取得](src/baibai_jobs/acquisition/jquants_financial_native.py)・[固定reader](src/baibai_jobs/integration/financial_native.py)に従う。

再生成は保存JSONの `inputs.generation.inputs.financial`（generation headerは `inputs.financial`）をdescriptor込みのInputSnapshotとして `--saved-input` へ渡す。旧inline/共有固定入力を同じreaderで読み、旧Selectionのhash/本文を書き換えない。cold readerも対応codeへ揃える。保存Operationの通常再開は保存済みモデルを使い、cold再計算とは別である。旧protocolのpendingは新取得を止め、元directory・原本・確定状態から復旧する。大きすぎるrequestも空初期化で通さない。

### 旧財務L1からの有限移行と切替条件

[共通手順](#jquants-native-operations)で全financial stageを移し、旧のみ/新のみ/内容差と日別不足を比較する。財務query・maintenance・historical readerも切替範囲を確認し、参照releaseの財務L2欠落を正常空にしない。

初回dailyが除くのはdownload済みcopyの旧財務table/coverageだけで、L1・旧immutable release・application DBではない。モデルのfinancial identityと、upload copyのdehydrate/次hydrateを読戻す。

<a id="master-native-l1"></a>
## master native L1と取得依存の切替

`BAIBAI_MASTER_NATIVE` はmaster取得・モデル/公開・TradingView・外部評価を一組で選ぶ。masterは疎なsnapshotで、非空受領済み日は通常再GETせず、訂正だけ `--force` を使う。[取得owner](../engine/src/baibai_engine/data_collection/providers/jquants/master_native.py)に従い、日足の連続backfillを流用しない。

保存 `inputs.master` とその暦で再生し、休日basisを最新暦へ差し替えない。旧bootstrapのmaster取得は使わず、`backfill-master` または専用native CLIへ向ける。月末cohort日を作るbackfillだけは既存bars gridを要求する。

TradingView/外部評価も取得前にnative masterを固定し、元bindingで再開する。空/failed/heldを旧masterで埋めない。通常calendarの休場判定より前に取得するため、休日の追加GETを確認する。

### 旧masterの有限移行

[共通手順](#jquants-native-operations)でmarket/TradingView両writerと全stageを固定する。market request IDとTradingView master/receipt IDを照合してmasterだけ移し、全過去取得日/全Codeをcompareする。mixed stageのmasterはnative fingerprint/bytes一致時だけ再投影から外し、未移行ID・原本不足を既知扱いしない。

<a id="calendar-native-l1"></a>
## market_calendar native L1と日別充足

`BAIBAI_CALENDAR_NATIVE` の通常モデル/公開への切替は、旧exporterの依存を残さないため財務・日足・masterも同時に有効化する。明示取得は他source/L2/releaseを必要としない。

range原本と[欠測補修計画](../engine/src/baibai_engine/data_collection/providers/jquants/calendar_native.py)を使い、既取得日の明示訂正だけ `--force` で取得する。日足の強制overlapを流用せず、未知HolDiv・不正日・partialを休場と推測しない。必要区間が欠ければmasterの休日basisを確認する。`commit-work` は保存実受領だけを再確認し、空/partialを成功にしない。

calendarの固定再生は次だけを指定し、最新暦を混ぜない。

```bash
uv run baibai-jobs rebuild-calendar-native --saved-input <SAVED_INPUT_JSON> --output <NEW_OUTPUT>
```

### 旧calendar rangeとdependencyの有限移行

[共通手順](#jquants-native-operations)でmarket/TradingView全stageのpageと、別保存のcalendar dependency JSONを固定manifest/digestと照合する。旧投影compare・全pack読戻し後、mixed stageのcalendarだけを再投影から外し他sourceを保つ。切替後の旧bootstrap calendar writerは使わず、専用native CLIへ向ける。

<a id="edinet-list-native-l1"></a>
### EDINET一覧原本の独立取得・有限切替

`BAIBAI_EDINET_LIST_NATIVE` の通常既定は `true` で、取得・dailyのActionsも明示 `true` に揃える。明示 `false` は旧入力の有限検証用であり、本番writerの並行稼働には使わない。

配置は移行・全原本読戻し・cold/warm比較の成功後とし、停止中のcronは初回native公開・次hydrate・source inputsの読戻しまで再開しない。

`BAIBAI_EDINET_LIST_NATIVE=true`で一覧の通常GET・原本公開をmarket/L2から分離する。取得範囲・日別CAS・実受領UUID/時刻と同値gzipの扱いは[取得owner](src/baibai_jobs/acquisition/edinet_list_native.py)と[保存owner](../engine/src/baibai_engine/data_collection/providers/edinet/list_native.py)を正本とする。取得失敗を正常空へ変えず、旧normalized行から原本・受領を作らない。flagの値だけで移行完了を認定しない。

日次統合はheadを開始時に固定して全受領から一覧/文書を再構築する。不正dayを毎回報告しつつ選択可能な正常投影を保つ。metrics/factsは各plannerの必要範囲で不足を判定する。archiveやL2の失敗で一覧L1の成功を取り消さない。

切替後のmarket releaseは `edinet.list_responses` を複写しない。push用dehydrate/次世代hydrateでは全cache UUID・日付・時刻・gzipが独立原本と一致した場合だけcacheを空にする。offlineでも検証用の明示 `--bucket` が必要で、他datasetの削除条件は変えず、旧release/stage原本は削除しない。

#### 既存原本を持つ環境の切替

1. EDINET一覧の旧通常writerと手動refresh/bootstrap/backfill、metrics/facts取得、日次統合・公開を停止し、実行中をdrainする。先にschema50切替を終える。対象code、旧固定release ID/digest、移行境界UTC時刻を記録し、旧release/stageは保持する。
2. `uv run baibai-jobs edinet-list-cutover prepare --directory <新規plan-dir> --work-directory <work-dir> --release <固定release> --manifest-sha256 <digest> --known-at <UTC時刻>`を実行する。全履歴partitionと`edinet-list`/`edinet-metrics`/`edinet-facts`の全stage（integrated、late、要確認も含む）を固定する。通常resumeのskip条件は使わない。
3. `blocked`と`missing_raw_days`を確認する。normalized JSONしかない日、selected UUID欠落、旧文書identityを再現できない原本不足、再確認proof欠落、同UUIDの異bytes/時刻、固定境界後の受領は切替を止める。今日のGETを失われた過去受領へ置換せず、旧原本を復元できない間は旧modeを維持する。余分な手動SQLite copyにだけ原本がある場合も、先に旧正規経路へ保全してからprepareをやり直す。
4. 同じ固定planに`edinet-list-cutover apply`、続いて`verify`を実行する（両方`--directory`/`--work-directory`を指定）。全input hashを公開前に検証し、実gzip/UUID/時刻を全件読戻す。途中再開は同じplanだけで行う。別の新取得が混ざったtargetへ古い移行を上書きしない。mixed stageのZIP・scope・結果は変更しない。
5. codeと`BAIBAI_EDINET_LIST_NATIVE=true`を取得・日次統合・手動運用へ同時に揃える。旧edinet-list stageを通常統合で再適用せず、原本として残す。旧metrics/facts stageの一覧はheadに完全一致するものだけ既知として扱い、原ZIP/scopeを従来通り受け入れる。新source input発行はedinet-listを作らず、metrics/factsから旧一覧コピーを外す。
6. 独立取得→固定head再構築→market公開→dehydrate/pull/hydrate→再構築を確認する。L2公開失敗でも既に確認済みの一覧L1は残る。切替後の旧`EDINETRefresh.list_documents`入口はHTTP前に拒否するため、一覧bootstrap/backfillは使わない。必要な一覧は`edinet-list-native acquire --start <開始日> --asof <日付> --directory <新規work-dir>`で取得する。既存`maintain-market refresh-edinet-metrics --lookback-days N`はnative headを固定して一覧を再構築し、同じ任意N日（0や540日超を含む）の未保存CSV原本GET→L2抽出を行う。旧一覧writerを呼ばず、一覧未取得日は成功へ補わない。一覧新GETとCSV取得をこの順に明示実行する。保存原本だけの再抽出は従来のextractを使う。flagを戻して旧writerを並行稼働させない。

原本を全く持たない新規環境だけは、明示`edinet-list-native initialize --directory <work-dir>`で空headを作り、その後acquireする。既存環境の移行失敗をinitializeで迂回しない。

#### 公開失敗時の原受領保全

同じwork directoryに`pending.json`があれば、新しいHTTPを始めず拒否する。手元workが残る場合は`edinet-list-native commit-work --directory <同じwork-dir>`でその実受領を再公開する。新UUIDや取得時刻を付け直さない。

通常workflowでbody/head公開が失敗した場合は、pending原本を既存のprivate source handoffへ保存する（`stage_ready=true`）。runを失敗のまま通知し、通常統合でこのstageを成功扱いしない。保存されたstage IDを確認して、`edinet-list-native commit-stage --stage-id <ID> --directory <新規work-dir>`で固定原本の公開・読戻し・integrated markerを完了する。旧形式stageはこの入口で変換せず、上記有限cutoverを使う。既知のhead PUT成功後の応答不明も同じUUIDで冪等に確認する。

runnerの強制kill、local disk故障、private stage保存も失敗した場合まで原本回復を保証しない。既存の期限前打切りとpartial stage保存を使い、通常publication失敗を成功通知へ変えない。新remote journalやreceipt複製層は追加しない。

CSV/XBRL ZIP・facts scopeの保存/inputと一覧L2公開はmarket経路に残る。独立一覧L1だけでEDINET全取得の独立化や過去L3入力の一貫固定が完了したとは扱わない。後続設計は[#1820](https://github.com/koumatsumoto/baibai-loop/issues/1820)で扱う。

#### EDINET ZIP/scope独立ownerの明示検証入口

以下は通常workflow・共有固定input・market公開・既存SQL consumerへ未接続の明示検証用。既存ZIP/scopeの有限移行は未実装で、本番の空head作成で代用しない。旧market/L2の廃棄、全EDINET L2のcold再生成、過去PIT入力の保証にもならない。

`edinet-archives-native`は初回ZIPと取得scopeを `l1/edinet/archives/v1/` へ保持する。原本を持たない隔離環境と、同じbucketの固定可能なnative一覧headを用意し、`--bucket`を明示する。

```bash
uv run baibai-jobs edinet-archives-native initialize --bucket <isolated-bucket> --directory <new-work-dir>
uv run baibai-jobs edinet-archives-native acquire --bucket <isolated-bucket> --directory <work-dir> \
  --group edinet-metrics --asof <date> --lookback-days 540
uv run baibai-jobs edinet-archives-native acquire --bucket <isolated-bucket> --directory <work-dir> \
  --group edinet-facts --asof <date> --initialize-facts
uv run baibai-jobs edinet-archives-native freeze --bucket <isolated-bucket> --directory <read-dir>
uv run baibai-jobs edinet-archives-native commit-work --bucket <isolated-bucket> --directory <same-work-dir>
```

選択・取得予算は[取得owner](src/baibai_jobs/acquisition/edinet_archive_native.py)、scopeの未初期化・ticker限定は[取得範囲](#edinet-factsの取得範囲schema-50)に従う。計画は固定一覧/scopeを使い、旧market・L2成功・全ZIP本文を入力にしない。`input.json`の一覧/archive headを保持し、各ZIPを次のGET前に確定する。後続失敗で保存済み原本を取り消さず、scope有効化を抽出成功としない。

[保存owner](../engine/src/baibai_engine/data_collection/providers/edinet/archive_native.py)は `(doc_id, source_type)` の初回bytes/保存日を保つ。新取得だけ実受領UTCを持ち、旧日付から時計を作らない。`pending.json`があれば新HTTPを始めず、同じworkの `commit-work` で同じbytes/時刻を再確認する。CAS競合も最新headとの和集合として明示再試行する。失ったheadを部分workから再作成しない。work消失やremote failure stageからの回復は未対応。

固定consumerの検証は、保存した一覧/archive headと新規outputだけを渡す。最新headで未知doc・原本欠落/改変を補わず、原本欠落/改変時は途中DBを公開しない。既存outputは上書きしない。

- [SQL consumer用adapter](src/baibai_jobs/integration/edinet_archive_native.py)の `rebuild_fixed_edinet_input(lists, archives, output)` は原ZIP・stored_on・scopeと一覧投影を再構成し、EDINETReader・comparison・holder・TOBへ渡す。全ZIP本文を読むので取得plannerには使わない。L2値・成功履歴はコピーせず、日付列への変換で時刻精度が上がったとは扱わない。
- [metrics比較](src/baibai_jobs/integration/edinet_metric_rebuild.py)の `rebuild_fixed_edinet_metrics(lists, archives, output, asof=..., lookback_days=540)` は選択CSVだけを読み、旧metrics・facts scope・全ZIP複写を入力にしない。`completed`/`partial`だけoutputを作り、全未取得・一覧不足は延期、parse失敗は失敗、宣言済み原本の欠損/改変は例外として区別する。

metrics候補ができても `edinet-list` と `edinet-metrics` を両方確認する。不正一覧の前の正常受領で計算できても一覧失敗は残る。`quarantined_events`のidentityを保ち、旧metricsだけの銘柄対応は推測しない。部分欠損の採用日・匿名操作の隔離はwarm計算と異なり得る。業務値・欠測・文書/source日を比較し、再計算時計を過去時刻へ変えず、候補生成を通常公開への採用としない。
