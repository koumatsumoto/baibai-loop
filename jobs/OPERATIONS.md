# 運用手順

repository rootのBashで、依頼された対象だけを操作する。[architecture](../docs/architecture.md#store-authority)は正本と責務、[development](../docs/development.md)は環境、CLIのhelpはoptionを扱う。本書は開始・到達点・復旧を扱う。

| 目的 | 実行・確認する箇所 |
| --- | --- |
| 初めて実行する | [権限・秘密・対象storeの確認](#actions-write-boundary)。環境準備は[開発手順](../docs/development.md)。 |
| 日次の進行や失敗を見る | [日次の起動・到達点](#daily) → 最後に成功したphaseと同じrun/attemptで確認する。 |
| 取得・再抽出をやり直す | [経済](#economic-acquisition)／[記事](#article-acquisition)／[JPX PDF](#jpx-pdf)／[外部評価](#external-assessment-acquisition)／[個社TradingViewの履歴確認](#tradingview-expectations)／[J-Quants](#jquants-native-operations)／[EDINET](#edinet-list-native-l1) |
| 公開済みの内容を読む | [固定release](#fixed-release-read)。release IDとmanifest SHAを固定する。 |
| 反映が一部だけ成功した | [machine公開の結果不明](#machine公開の結果不明)／[seal・次input](#machine公開後の失敗再開)／[経済単独の再開](#経済単独公開と失敗再開)。日次を丸ごと再送しない。 |
| 日本株の新規提案を本番データで検証する | [本番データで新規の売買提案を受け入れる](#production-proposal-acceptance)。日次成功・保存済み提案の表示とは別。 |
| 提案・台帳を保存する | [application DBの更新](#application-db-を反映する)。[ローカルcheckpointの確認](#inspect-local-application-db)とは異なる。 |
| Webだけを更新する | [表示の生成・原本との照合・配備](#web-serving)。表示失敗でcoreを巻き戻さない。 |

<a id="actions-write-boundary"></a>
## 実行前の確認

通常の取得・公開は既存Actions、application更新は認可環境の作業copyと正規CLIを使う。開発の依頼は本番更新・追加課金（runner・storage・APIを含む）・権限拡大・broker操作・未報告取引入力の許可ではない。コード撤去に伴って配備済みWorkerや原本を消さない。

非公開の`baibai-stores`の秘密・endpoint・原文・DB・原稿はGit・Issue・公開log/artifactに載せない。writer secretは取得/公開step、`DISCORD_WEBHOOK_URL`は通知stepだけに渡す。ローカルCLI・setupが`.env`を自動で読むとは限らない。必要な場合だけ、認可済みの値を[設定例](../.env.sample)に従ってBash用に引用し、repository rootで読み込む。`set -x`は使わない。

```bash
set -a
source .env
set +a
```

`r2_transfer.sh` 自身が `.env` を読むのはR2の認証3項目のいずれかが不足するときだけ。この補完では指定済みのR2設定を優先し、未指定のアカウント・鍵・バケット等だけを同ファイルから使う。認証3項目が揃っている環境で隔離バケットを使う場合は、上記のように先に読み込むか、実行環境に対象バケットを明示する。

- **結果不明:** 固定plan・receipt・ID/hash・原bytes・実受領時計を保ち、現objectと照合する。正常空・partial・未試行・失敗を区別し、新UUID・日付変更・marker手修正・再送で埋めない。
- **拒否・障害:** 認証・403・429を別経路・hydrateの別GET・権限追加で回避しない。取得期限にはexport/stageの時間を残す。429で続行できないquota groupはそのpassを止める。`Retry-After`は最早時刻で自動再実行の指示ではない。R2の存在判定は明示404だけを不在とする。`HeadObject failed`や不明なstatusは停止理由であり、応答本文・stderr・tracebackの公開やkey hashからの匿名性の推定をしない。
- **世代不一致:** schema・origin・CAS・必要なleaseが合わなければ止める。旧release・受領stage・移送証拠を保ち、旧copyは通常writerで移行しない。日次を全期間再取得・旧データ整理に使わない。不可逆な整理には対象keyと失う再開能力を固定した別承認が必要。

<a id="daily"></a>
<a id="日次実行と結果確認"></a>
## 日次と到達点

[共通日次](../.github/workflows/cloud-daily-batch-current.yml)は22:43 JSTの独立した統合枠。各[取得枠](#sources)の完了を待たず、遅着は次の統合に回る。手動実行前に、対象の`main`と[日足](#price-native-l1)の受領を確認する。 切替後の確認・復旧は受入済みの`main`を指定して新規dispatchする。GitHubの「Re-run jobs」は元runのSHAで動くため、同じ現行workflow名でも旧SHAを再実行しない。ローカルも旧checkoutから操作しない。`asof`はモデルの基準日で、価格日でも全sourceの観測日でもない。

```bash
gh workflow run cloud-daily-batch-current.yml --ref main -f "asof=${ASOF:?基準日をYYYY-MM-DDで指定}"
gh run list --workflow cloud-daily-batch-current.yml --branch main --event workflow_dispatch --limit 5
gh run watch "${RUN_ID:?対象の日次run IDを指定}" --exit-status --interval 60
```

[対象日の決め方](src/baibai_jobs/schedule.py): 手動の省略値は元run作成日のJST日付、定期は元run作成時点の直近cron枠。queue/rerunで時計を変えない。寄前・場中に完了した日次を使う場合は前日を明示する（前営業日に丸めて週末を落とさない）。監視timeoutだけで重複dispatchしない。

| 同じrun・attemptで確認する到達点 | 成功してもまだ保証しないこと |
| --- | --- |
| lease内で現行ポインタから同じMarket/Runsを一度取得し、SQLiteとLakeのorigin・EDINET指標契約を照合 | 完全hydrateや全データの受入ではない。不一致なら新規machineも経済L1も公開しない。 |
| 経済L1の受領を公開・読戻し・sealし、次の経済取得inputを発行 | 他のmarket入力、L2、Web。後段が失敗しても経済L1を戻さない。 |
| 到着済みmarket受領を固定し、取得済みMarketをhydrate。固定原本の投影とEDINET抽出を適用 | 全providerの充足。個別の失敗/延期は残し、他sourceを進める。 |
| [モデル生成](src/baibai_jobs/integration/models.py)で候補外も含むGenerationとSelectionを保存し、対応するLakeとMarket/Runsを一組で公開 | 局所の保存成功だけでは公開にならない。正本はポインタ本文の読戻しまで確認し、[machine公開の結果不明](#machine公開の結果不明)は停止する。 |
| 一組の公開後、同じreleaseを読戻して市場の受領をseal | 市場の受領確定は正本の選択と別。[seal失敗](#machine公開後の失敗再開)は表示や経済inputの再発行で埋めない。 |
| core成功後の[Web生成・転送](#web-serving)、lease解放、通知 | 受領・提案・取引の成否。表示はseal成功を条件にせず、表示・通知の失敗だけで正本を再公開しない。 |

定期・手動とも、公開後のRunsには今回のGenerationとSelectionだけを残す。過去の提案・保存済みOperationはAppから読む。未保存の旧機械結果は、該当世代を含む隔離copyが手元にある場合だけ[保持copyからの復元](#保持copyから欠落generationを戻す)で照合できる。旧IDを現行値で補わない。

EDINET revisionを明示受入する場合だけ、隔離比較済みの[受入条件](src/baibai_jobs/integration/edinet_metric_acceptance.py)を`edinet_metrics_acceptance`へ渡す。同じ明示asofを指定し、固定list/archive・revision・対象sampleの原本/保存結果のdigestを生成前に照合する。partial/延期・入力交代・差異は停止し、後付け期待値で通さない。先行する経済L1・planの保持は取り消されず、公開後の実読戻し・L3の受入は別。

失敗時は正本のIDと最後に完了した段階・lease解放を確認する。通知や`done`行だけで公開を判定しない。公開受入の診断は`release_acceptance`のreason codeと登録dataset名、未分類は型までで、原本や識別子は出さない。候補0は投資機会なしではない。[不足理由](../docs/reference/screening-runtime.md#入力不足と品質)を参照する。過去の機械結果をIDで照合する場合は、その世代を含む隔離Runsを明示する。

[watchdog](../.github/workflows/cloud-batch-watchdog.yml)は履歴を読み、実行・cancel・再dispatchはしない。対象日の判断は[判定実装](src/baibai_jobs/observability/watchdog.py)が持つ。

| 通知 | 確認すること |
| --- | --- |
| `MISSING` | 成功/進行中を確認できない。検索は各workflowの最新100runまでで、過去日まで届かなければ履歴不足として通知しない。削除済みの状態は再現しない。 |
| `DELAYED` | 対象日の成功がなく、初回dailyがqueueで210分超。再実行の待機・実行中の遅延・原因は推定しない。定期監視なので閾値到達時の即時通知ではない。 |

```bash
gh workflow run cloud-batch-watchdog.yml --ref main -f "check_date=${ASOF:?対象日をYYYY-MM-DDで指定}"
```

<a id="fixed-release-read"></a>
## 固定releaseを読む

「今公開されているもの」と「過去の特定release」を分ける。現在の`release_id`と`manifest_sha256`を調べるには次を使う。`resolve`が検証するのはmanifestまでで、data objectの読戻しではない。DuckDBのquery profileや作業用一時directoryは不要。

```bash
uv run baibai-jobs snapshots resolve --mirror stores --bucket baibai-stores --format json
```

現在のmarket・runs・経済の作業copyを一括で用意するのは `jobs/scripts/pull.sh`。Machineは同じ現行ポインタに固定した両DBとLakeを検証し、Marketをhydrateした後で両作業DBを置き換える。Machineの検証・hydrateが失敗した場合は作業DBを置換しない。経済の取得はMachineの準備に成功してから行う。経済の取得で止まった場合、Market/Runsは反映済み。同じ組で続けるなら、案内された `jobs/scripts/r2_transfer.sh pull-indicators` だけを再実行する。applicationは`pull-app`で取得する。歴史の固定入力を現在のcopyへ付け替えない。

<a id="local-release-hydrate"></a>
### 完全local mirrorからのhydrate

固定したreleaseの値を調べるには、[`snapshots hydrate`](src/baibai_jobs/publication/snapshot_write_cli.py)へそのIDと同じmanifestのSHA-256を**両方**渡す。
固定指定なしの全体hydrate（日次を含む）と転送用dehydrateは、照合済みのrelease manifestでEDINET metricsの現行契約を要求する。固定releaseの調査、EDINET metricsを展開しないportfolio用hydrate、個別の移行手順は別経路。

```bash
uv run baibai-jobs snapshots hydrate \
  --mirror "${MIRROR:?完全なlocal mirrorを指定}" --store "${MARKET_COPY:?検証用の作業DBを指定}" \
  --release "${RELEASE_ID:?固定するrelease IDを指定}" --manifest-sha256 "${MANIFEST_SHA256:?そのmanifestのSHA-256を指定}"
```

完全mirrorならbucket不要。必要なobjectをR2から読む場合は `--bucket baibai-stores` を加える。退役した旧releaseをR2だけから全量再現できるとは限らない。作業DBに旧EDINET原受領cacheが残る場合も[照合](#edinet-list-native-l1)にbucketが必要。hydrateはlake所管tableを置換するため、対象契約に合う既存のmarket作業DBと一時snapshot容量が必要。空ファイルや唯一の未公開copyに実行しない。store-local取得coverageは作らない。対象契約が古ければ元commitのreaderを使い、未収録・欠損・破損を別release/providerで補わない。Parquetは[reader](../engine/src/baibai_engine/storage/snapshots/reader.py)の検証済みpathで読み、globで混ぜない。

ParquetのSQLite由来をコードでは `sqlite_snapshot` と呼ぶ。公開済みParquetの `baibai.source_kind` に保存した `legacy_sqlite_import` は、既存objectのSHA・key・固定releaseの読取を変えないためそのまま使う。物理タグの変更は既存releaseの読取を含む別の契約移行とし、通常publishで既存objectを書き換えない。

<a id="production-proposal-acceptance"></a>
## 本番データで新規の売買提案を受け入れる

[日次](#daily)は材料を作り、[表示生成](#web-serving)は既存提案を表示する。どちらの成功も新規提案の受入ではない。正規の入口は [Investment Judgment](../.agents/skills/investment-judgment/SKILL.md)。既存コマンドを本番入力が揃う認可済み作業環境で実行する。入力が揃っていればDBの再取得や別環境への転送は不要。

手動日次に `-f jp_input_probe_ticker=7203`（例）を加えると、同じrunnerでその日次が生成したGeneration・Selectionの1銘柄と、その期待価格日の未調整終値を確認する。追加入力DB・再転送は不要。dispatch入力は秘密ではないため公開テスト銘柄だけを指定する。通常の日次と独立した結果はActionsのJob summaryで確認する。これだけでは調査・正式提案・本番公開を証明しない。

1. 検証CLIのcommitと日次runのcommitを別々に記録し、日次の同一run/attempt、[現行ポインタ](#machine-publication)のETag・Lake release ID・manifest SHA・Machine UUIDとMarket/Runsの各SHAを固定する。同じポインタに合うMarketのorigin、RunsのGenerationとSelectionを確認する。現行が進んで元の組を取得できない場合は、元runの再現とはみなさない。Appは別正本なので、[取得元・取得版・時刻を固定した作業copy](#application-db-を反映する)を使う。保存先は隔離Appのみ。必要な入力を同じ出所で読めない場合は、別日や最新のデータで補わず、そこで止める。
2. 隔離Appを指定して `operation --db <APP_DB> --format json show --status active` を先に確認する。同じ案件は[保存したOperation IDで再開](../.agents/skills/investment-judgment/SKILL.md#完了と再開)し、別案件なら新規の `prepare` は止める。受入のためだけに別案件を閉じず、本番Appも変更しない。
   新規案件は `selection-context` にそのGeneration・Selection・研究基準日・情報締切を明示し、`selection-context` と `prepare` には同じ `--runs-path` を渡して[選定と調査・評価の保存](../.agents/skills/investment-judgment/SKILL.md#開始と固定対象)へ進む。正式終値と一次資料は別に照合し、終値の価格日と資料の公表時刻を記録する。選定・prepare・評価保存・Operation操作にも同じ隔離Appを `--db` で明示する。価値と価格の差、市場・同業との比較、欠測、判断を変える条件を確認し、評価を独立査読する。買いを件数目標にしない。
3. [提案の手順](../.agents/skills/investment-judgment/SKILL.md#trade-proposal)に沿って `trade-proposal-context` → `trade-proposal-check` → 提案本文の独立査読 → `trade-proposal-check` で `valid=true` と `review_binding=match` を確認 → `trade-proposal-publish` → [`operation complete`](../.agents/skills/investment-judgment/SKILL.md#完了と再開)（同じ提案ID、結果、次の手を指定）。3つの提案コマンドには同じ `--db` と `--sqlite-path` を明示する。正式保存された同じ提案IDを読戻す。ここでのpublishは隔離Appへの保存であり、R2や実配信への公開・発注ではない。
4. 隔離checkoutでローカル表示を生成し、同じAppと提案IDで照合する。複数ケースは各提案がそのAppのlatestのうちに照合する。同じIDで保存・Operation完了・表示を確認できても、売買や資金配分の判断の有無までは分からない。
   発行した提案の `research_scope.outcomes[].status` と `items[].action` を銘柄コードで照合する。買い・売り・保有継続・根拠のある見送り（`no_action`）と、査読後も判断を保留した対象（`reviewed` / `defer`）、調査未完の対象（`unfinished` / `defer`）を分けて報告する。表示側の未完理由と参照は `items[].unfinished` を確認する。調査未完は評価済みに数えず、発行した提案の全項目が保留なら「経路は確認済み、売買判断は未確認」とする。対象0件で提案なしのOperation完了は、正式提案の受入には数えない。
   受入で見つけた問題は、根拠と再現条件を残して修正後に同じ入力で再確認する。実配信まで確認する場合だけ [Webの照合](#web-serving) を別に行う。

作業は検証するcommitの隔離checkoutで行い、そのルートを `WORK_ROOT` とする。DBだけのディレクトリは使えない（[ルートの判定](../engine/src/baibai_engine/storage/paths.py)は `pyproject.toml` と `engine/src/baibai_engine/` を必要とする）。固定した入力の作業コピーは、そのcheckoutの `stores/application/baibai.sqlite`、`stores/market/market.sqlite`、`stores/screening/runs.sqlite` に置く。執筆ファイルと表示出力は `.cache/` 配下に置く。これらはGit管理外で、本番正本への書戻しは行わない。

縮小転送されたMarket作業コピーに日足・立会暦・銘柄masterが未展開なら、提案の準備・表示の前に、コピーのoriginと同じ固定releaseからこの3種だけを展開する。別releaseや別の取得元で欠損を補わない。

```bash
uv run baibai-jobs snapshots hydrate-portfolio \
  --mirror .cache/proposal-acceptance/lake --store stores/market/market.sqlite \
  --release "${RELEASE_ID:?同じMarketのrelease IDを指定}" \
  --manifest-sha256 "${MANIFEST_SHA256:?同じrelease manifestのSHA-256を指定}"
```

`WORK_ROOT` で実行する。取得済みのmirrorを使うならその場所を指定し、認可済みR2から不足分を読む場合に限り `--bucket baibai-stores` を追加する。

```bash
(
  set -eu
  umask 077
  cd "${WORK_ROOT:?検証用の隔離checkoutを指定}"
  PRIVATE_OUTPUT=".cache/proposal-acceptance/${PROPOSAL_ID:?照合する提案IDを指定}"
  uv run python -m baibai_web.materialize --root . --output-dir "$PRIVATE_OUTPUT"
  uv run python -m tools.diagnostics.check_served_proposal \
    --db stores/application/baibai.sqlite --latest-json "$PRIVATE_OUTPUT/views/latest-proposal.json" \
    --history-json "$PRIVATE_OUTPUT/views/proposal-history.json" \
    --proposal-id "$PROPOSAL_ID" --require-completed
)
```

<a id="machine-publication"></a>
## 共通公開とmachineの復旧

Machineの正本は `lake/pointers/market/current.json` が指す一件のLake releaseと一組のMarket/Runs。日次の `model-integration` だけがその組を更新する。固定Lakeのimmutable graphと、SQLiteの整合・origin・Generation/Selectionを照合した両DBを、再利用しない `machine/payloads/<UUID>/` に準備する。転送前の両DBのSHA-256とMD5、転送応答、HEADのETag・長さを照合する。lease内で取得時のポインタETagを条件に一回だけ切り替え、**ポインタ本文**を読み戻してから、そこに記録した前組UUIDの転送ファイル二件だけを除く。通常の日次writerは両DBを再GETしない。一回切替の受入は別に両DBの実バイトを確認する。固定 `.bak` は作らず、旧Lake・原本は触らない。

Runsの新しい公開には、そのreleaseのGenerationとSelectionだけを入れる。直前のRunsは財務比較のseedとして読み、旧DBをcloneして新しい組へ持ち越さない。準備済みOperation、提案・報告・実績はAppに残る。古い世代を指したままAppに未保存の `selection.json` は、現行へ切り替わったら現行の入力で選び直す。`model-integration` の `--runs` には他のSQLite利用者を閉じた専用の作業copyを指定する。公開成功後に手元の置換だけ失敗した場合は `local_runs_refreshed=false` となるため、そのcopyを使う前に `pull-machine` で更新する。

`jobs/scripts/r2_transfer.sh pull-machine` は同じポインタから両DBを検証し、Marketをhydrateしてから作業copyに反映する。既存のDBを置き換えるため他のローカル作業を閉じてから使う。`--daily-preflight` はleaseと現行契約を確認して取得までを行う。経済L1の後、`hydrate-market-for-daily` はMarket内の同じoriginから展開し、currentを読み直さない。`pull-portfolio-market` は表示に必要なMarketだけを取得する。正本の切替と衝突して取得できなければ、別世代の片方を混ぜず現行から取得し直す。現行の探索には取得結果の `generation_id` と `selection_id` を既存CLIの `--generation-id`・`--screening-selection-id` へ渡す。ID省略時も、指定した基準日・情報締切の範囲外の結果は選ばない。

`push-indicators` と `upload-portfolio-views` には `PUBLICATION_LEASE_HANDLE` が必要。App更新とsource固有CASのnative L1はこのleaseの対象外。途中でownerが失効・交代したら次writeを止める。leaseのverifyは延長ではない。

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

関数は処理の失敗後もreleaseを試す。解放失敗ではhandleを保持する。busyならowner/expiry/runを確認し、hard cancel後も元期限までは待つ。Actionsのqueueだけではlocal writerを排除できない。不正leaseは自動修復せず、全active/pending writerがいないと確認した明示復旧時だけexact keyを扱う。prefix削除や世代の書換えで古いwriterを通さない。

原本に基づく修正は隔離copyで先に比較し、承認済みの共通日次へ同梱する。MarketやRunsだけの上書きは行わない。経済だけの承認済み変更は `with_publication_lease jobs/scripts/r2_transfer.sh push-indicators` を使い、L2利用のための再pushは不要。

### machine公開の結果不明

- 候補と実際のポインタが一致すると確認できるまで、公開成功とはみなさずseal/input/表示を進めない。通信拒否で別経路を使わず、応答不明のPUTを再送しない。旧ポインタが見えても、応答を失ったPUTがまだ到着し得る間は候補を削除しない。
- 現行を確認し、失敗したrunnerの作業copyを正本へ戻さずfresh pullで前進する。旧Machineの除去失敗は、確認済みの公開・seal・表示の成否と分ける。次の日次は同じ現行ポインタが記録した旧2キーの不在を確認するまで、新しいLake・Machineをstageしない。切替前に停止した未公開候補はこの前世代UUIDには含まれない。応答不明の送信が落ち着くまでは、404だけを削除の根拠にしない。Lake graphや原本の一掃は行わない。

日次の通常writerはMarket・ポインタを単一PUTし、上限超のRunsのみ固有キーへmultipartする。条件付き完成と応答不明時の停止、未完partの期限、実転送・独立読戻しは別のmultipart受入。模擬S3や縮小fixtureを実R2の証拠にはしない。

単一ポインタへの一回限りの移行は[完了済み](https://github.com/koumatsumoto/baibai-loop/pull/2963#issuecomment-7183844510)。旧版のwriterや移行手順を日次復旧に使わない。

### 保持copyから欠落Generationを戻す

現在のrunsを古いcopyで丸ごと戻すと、後発Generation/Selectionを失う。検証済み・静止中のcurrentと旧sourceのSHAを固定し、必要なGeneration IDを明示して、新しい隔離copyへ欠落分だけ追記する。同IDのheader・revision・圧縮payload・Selectionが異なれば全体を停止する。再計算・再圧縮・時点更新はせず、current側の追加政策や他tableも残す。

```bash
uv run baibai-jobs restore-model-history \
  --source <VERIFIED_OLD_RUNS_COPY> --source-sha256 <OLD_SHA256> \
  --current <VERIFIED_CURRENT_RUNS_COPY> --current-sha256 <CURRENT_SHA256> \
  --output <NEW_PRIVATE_RUNS_COPY> \
  --generation-id <EXACT_GENERATION_ID>
```

複数IDは`--generation-id`を繰り返す。既存outputやWAL/journal付き入力は使わない。この成功は隔離copyの準備だけで、L1/共有入力/L3の復元や本番反映を意味しない。元copyは保持し、通常readerで対象と現行の保存値/IDを照合する。履歴の照合はその隔離copyを使う。通常の本番日次は過去のGeneration/Selectionを持ち越さない。

### machine公開後の失敗再開

- machine成功/seal失敗: 元の固定planと完全な `applied.json` を私有の作業copyに保持している場合に限り、元の公開release・market/runsの組と照合してsealから再開する。通常のActionsは両ファイルをrunnerの一時領域にだけ保存し、公開ログでは読戻し計画を省く。runner終了後に元の証拠がなければ、同一attemptのsealは復元できない。既存のstageとmarkerを確認し、通常探索できる未seal分だけを新しい固定planで前向きに適用する。対象を特定できなければ止める。ログや現行値から旧計画を作らず、providerへ再GETしない。

JPXのsealはapplyで固定したbundle原本と、決算予定の全体/規制の対象日を公開releaseから読戻す。未公開applyや異なる本文/投影では成功markerを発行しない。旧applied.jsonに読戻し計画がなければ、元stageを保ってfresh正本へ同じplanを再applyし、通常の生成・公開・読戻しへ進む。現在の値から旧計画を補わない。

### 経済単独公開と失敗再開

fresh正本＋固定handoffの受入→条件付きPUT/全bytes読戻し→seal→経済の全group inputの順。byte不変時は同ETag/leaseを確認し、初回検証済みの正本copyを再利用する。seal/input直前にも同ETag/leaseを確認し、読戻し経済入力からモデルを作る。取得グループの計画body/形式が同じなら、正本ETagや統合日だけが変わっても既発行stageをそのまま読む。そのETag・日付は発行時の記録で、次回取得の日付は取得ジョブが指定する。対象groupの本文・形式が変わった場合や未発行・欠損の組は発行する。L2失敗でL1を再PUT/rollbackしない。

取得用の経済scoped inputは、現pointerのmanifestとそこから実際に参照するSQLiteだけを使う。新pointerに直前manifestの正確なID/hashを1件だけ記録し、その新しい固定stageの全文を読戻してから、共通leaseのまま旧物件を退役する。新旧が同じ物理bodyなら消さない。異なるときは旧bodyの不在確認を先に行い、旧manifestは最後に消す。切り離した物件だけの一時的な通信失敗や不在未確認は `inputs_retirement_pending` に記録し、確認済みの正本利用と他groupの更新は続ける。該当groupだけ、次の世代へ進む前に同じ債務を解消する。切り離した物件へのGET/HEAD/DELETEは各一回だけ送る。そこでの403/429や分類できない応答、lease/現pointer/現役読戻しの異常はそのまま停止する。新旧に関係ないraw/別groupを探索・退役しない。pointer切替前に中断した未公開stageは別で、自動探索は行わない。

有効化前に、稼働・待機中の旧checkout（v2 leaseを使える旧dailyを含む）とlocal手動処理を排出し、以後旧版からの経済公開・元SHAのActions再実行を行わない。v2 lease自体は旧v2対応publisherを止めない。取得側も#2965以前の送受信中のcopyを排出する。実行主体のR2 DeleteObject権限を別途確認する。新readerは選択済み旧stageの明示不在と実際のpointer前進に限り一度だけ最新へ移れる。固定stageの読戻しは別の版にすり替えない。

- 公開/readback失敗: 後段を止め、実bytesを照合する。未確認ではsealせず空DB/旧値へfallbackしない。
- PUT後/seal前: fresh runnerで未sealの同じ不変stageを再適用する。同sourceの後続watermarkを先に進めない。
- seal後/input途中: 次cycleのfresh copy・新attemptで全groupを照合し、未完・差分だけを発行する。旧local出力を上書きしない。
- A→同値A→Bの一括公開後の中断: 本文省略の中間stageがneeds_explicit_recoveryになり得る。元stage/hash・中断runのreadback・現正本/次inputを照合し、元時計/markerを変更しない。最新B/inputから前進できるが、新取得成功を旧stage採用の証明にしない。

FRED確認state自体の欠落は[通常recheck-source](#経済原本)へ。他sourceへ同じ確認方式を広げない。

## application DB を反映する

空の作業領域へpullし、DBとstores/.r2-generations/baibai.sqlite.etagを同じ組で使う。版は取得した本文と同じ応答から取り、配置前に現正本と異なれば中止する。同じ作業領域に対する `pull-app` が重なった場合、後から来た取得はDB・取得世代を変えずに停止する。別の作業領域やR2側のwriterを止めるロックではない。更新済みの組があれば再pullしない。

```bash
jobs/scripts/r2_transfer.sh pull-app
# 正規CLIでこのDBを更新した後
jobs/scripts/publish.sh
```

[`publish.sh`](scripts/publish.sh)は引数なしで、台帳preflight → 取得ETagを条件とする本体PUT → PUT応答のETagを記録 → Web生成の依頼、の順に進む。時価不足と台帳破損は別。DBとETagは組で扱い、未確認世代を現HEADから補わない。通常のApp反映ではR2に日付別backupを作成せず、過去の退避物の整理もしない。

成功は正本から対象Proposal/評価等のIDで読戻す。競合したら未公開差分を保ち、fresh正本へ正規serviceで再適用する。非0・応答不明では元世代と対象IDを確認し、盲再送しない。PUT済みで表示runがない時だけ`cloud-materialize`を再依頼する。旧schemaや未公開の事実のあるDBは原copyを保ち、正本へ丸ごと戻さない。

<a id="inspect-local-application-db"></a>
### ローカルの application DB を確認する

出所と時点を確認できるローカルcheckpointは、反映用のDBや取得ETagと分け、現行storeを置かない隔離checkoutのルートで読み取りだけ行う。確認するcheckpointのパスを `LOCAL_CHECKPOINT` に指定する。

```bash
test -f "${LOCAL_CHECKPOINT:?隔離したローカルcheckpointを指定}" || exit 1
uv run baibai-engine db info --db "$LOCAL_CHECKPOINT"
uv run baibai-engine portfolio ledger --db "$LOCAL_CHECKPOINT"
```

schemaが合わない・台帳を読めない場合に初期化や変換はしない。台帳表示は現在の正本や当時の時価を証明しない。旧checkpointで反映用Appを置換せず、現行の取得ETagを流用して再公開しない。訂正が必要なら、最新の正本に対して正規の手順で該当する事実だけを反映する。

<a id="sources"></a>
## source取得と再開

取得はproviderごとに独立し、共通日次で全sourceを待ち合わせない。運用上違うのは **次にどの正本を見るか** である。時刻は起動枠で、完了時刻ではない。

| 取得の入口 | 日次・判断への受け渡し |
| --- | --- |
| [市場取得](../.github/workflows/cloud-market-sources.yml) | [J-Quants](#jquants-native-operations)と[EDINET](#edinet-list-native-l1)はsource固有のnative L1を先に確定。JPXは[原本packet](#jpx-pdf)をstageし、統合で投影する。 |
| [経済取得](../.github/workflows/cloud-economic-sources-current.yml) | group別に部分受領をstageし、共通日次の先頭で[経済正本を別途公開](#経済単独公開と失敗再開)する。 |
| [記事の必要時調査と停止時の取扱い](#article-acquisition) | 新規の定期・手動一括取得は行わない。日次の新規統合も行わず、固定済みの出典は別経路で読む。 |
| [数値の外部評価](../.github/workflows/cloud-external-assessment.yml) | 固定planで確認済みの集合（ready）を通常統合で採用する。 |
| [多市場TradingView](../.github/workflows/cloud-tradingview-market-daily.yml) | [市場判断用の補助原本](#判断直前の多市場補助観測)。個社外部評価や正式終値の代わりにしない。 |

取得時には全market/applicationをhydrateしない。同source/groupのqueueと共有quotaを守り、L2不足だけで再GETしない。[経済のsource input](#経済単独公開と失敗再開)は経済統合から発行し、元bindingで再開する。計画材料が欠けたときは空seedや全DB取得で代用しない。形式変更時は対応readerを先行させ、非対応writerをdrainする。

破損handoffは当該sourceの失敗として他を進める。経済等の正本やR2全体の障害では止まる。`needs_explicit_recovery`は元ID/hash/時計/markerを保った限定回復とし、正常nullや遅着を一律全期間取得で埋めない。

<a id="economic-acquisition"></a>
### 経済原本

[workflow](../.github/workflows/cloud-economic-sources-current.yml)は休場日もgroup別に取得し、部分受領をprivate stageへ保存する。限定再実行は所属groupとsource IDを指定する。

経済L1の正本は `indicators.sqlite` 一件。日次統合は読んだ正本のETagを条件に更新し、実際の読戻しを確認してからhandoffを確定する。正本更新後にhandoff確定や次回入力の発行が止まった場合、古いDBには戻さず次回の通常統合を現行の正本と残っているhandoffから続ける。手動の `push-indicators` も固定 `.bak` を作らない。 手動の `pull-indicators` と `push-indicators` のマージ用取得は正本のETagを1回のGETに指定し、同じ応答のETagと受信バイト数が一致しなければローカル置換やマージを行わない。

取得summaryの`failed`はHTTP試行件数ではない。`blocked_by_source_id`は先行sourceが止めた未試行分であり、自身のHTTP statusを持たない。status・例外型だけで拒否原因を断定せず、旧summaryの欠落診断や受付時計を推測しない。

```bash
uv run baibai-jobs acquire-economic groups
gh workflow run cloud-economic-sources-current.yml --ref main -f group=fred_csv -f source_id=fred-payems
uv run baibai-jobs acquire-economic refresh <SOURCE_ID> --all-history --end <YYYY-MM-DD>
```

H.15はBoardの自動取得用パッケージを1回読み、必要な観測日だけ保存する。空・HTML・拒否を別経路で埋めない。現行パッケージの終了予定（2027-03-31）までに後継の出所・単位・観測日・受領時刻を確認する。

取得窓/空seed/確認はsource別[policy](../engine/src/baibai_engine/data_collection/acquisition/policy.py)に従う。空seedを欠落の自動初期化にせず、一律の短期窓へ狭めない。FRED全CSV・BOJ四半期APIは期間を狭めても通信が減らず、返却された窓外期間も保持する。未返却を撤回/全履歴確認とせず、history_recovery_requiredは別の有限期間/all-historyで回復する。同じ無期間応答を回復用に重ねて処理しない。

FRED成功時のL1は従来のnative行とし、同値は原本を複写せず実確認時計を進め、本文変更・公表時計と分ける。完全応答の期間集合/hashを保ち、headerだけ/短い行を成功としない。GET後のdecode/CSV解析失敗でnative行を作れない場合だけ、requestsのresponse entity bodyを既存economic_source_documentsへ保全し、runは失敗のままにする。HTTP header/圧縮wireや成功応答全文のarchiveではなく、旧原文/時計も復元しない。確認欠落/遅着で通常pushを拒否されたときだけ、fresh正本を基点に実GETする。

```bash
with_publication_lease jobs/scripts/r2_transfer.sh push-indicators --recheck-source <FRED_SOURCE_ID>
```

失敗時は両copyを保つ。このrecheckを他sourceへ広げず、現値/完全応答/実確認/保持原本から過去の採用順を作らない。他のrecord sourceの確認は取得range内だけの証拠で、source全体の置換や明示nullを証明しない。旧rawの実時計を使い、捨てた時計をrun終了時刻で補わない。

同値確認欠落は[local診断](../tools/diagnostics/inspect_economic_confirmation_gaps.py)で調べる。閉じたcurrent-schema canonicalと、統合済みを含む明示stageのhandoff.json/receipts.sqlite/取得済みmarkerを私有copyへ固定する。認可済み環境で有限取得し、canonical SHA/ETag・manifest/receipt・markerの元identity/時計/statusを照合する。無制限prefix走査・旧schema移行・失敗copy上書きをせず、marker未供給だけで未統合と決めない。

```bash
uv run python -m tools.diagnostics.inspect_economic_confirmation_gaps \
  --canonical <LOCAL_CANONICAL> --canonical-sha256 <FIXED_SHA256> \
  --stage <STAGE_ID> <MANIFEST_SHA256> <LOCAL_PACKET_DIRECTORY> \
  --source-id nikkei-pbr --start <OBSERVED_DATE> --end <OBSERVED_DATE> \
  --received-since <RECEIVED_AT_WITH_ZONE> --received-until <CUTOFF_WITH_ZONE>
```

stage指定は必要分を繰り返す。終了0は固定copyの診断完了だけで、全stage網羅・remote headの現在性・保存済みL2への影響や回復を証明しない。canonicalのfetched_atは確認時計がなければ既知の下限にすぎない。欠落を空応答へ変えず、同時計衝突は要確認とする。

回復時だけcanonical確認時計以降〜固定cutoffのstage欠落/後続受領を人が調べる。原本が揃い一意なら再GETせず既存accept_rechecks/merge/verifyで別copyを準備する。不足時の限定再取得は現在の実確認であり、過去時計の復元や衝突解消の証明ではない。実公開は対応code、lease/CAS・全bytes読戻し・input再発行を別に確認し、元stage/marker・保存L2/L3を変えない。

current-onlyは実取得日を使い、他handoffの日付は動かさない。松井の過去asofを現在pageで埋めず、optionsの古い訂正は明示期間で取得する。

<a id="pmi-public-articles"></a>
PMIはS&P公式PDFの日本/米国製造業headline・サービスBusiness Activity Index。ISM/Output/Composite、旧値/snippet、拒否後fallbackで代用しない。原文/抽出・速報/確報・対象月・公表/配信時計を分け、完全PITとしない。

<a id="指標観測とregistryの修正"></a>
原本訂正は再取得/明示再構築、系列/式はL2定義、取得条件はData Collectionで変更する。式変更だけで再取得せず、local DELETEをcloud mergeへ流さない。

```bash
uv run baibai-jobs acquire-economic list
uv run baibai-engine market-context reading --asof <YYYY-MM-DD>
uv run baibai-jobs validate-market-context-stores
uv run python -m tools.pmi_manifest --dry-run
uv run python -m tools.pmi_manifest
```

validateはL2算式/L3履歴の証明でない。原本更新時だけ経済pushする。PMIは公表翌週にdry-run→manifest更新→対象月refresh→公表値照合→commit。URL訂正/同URL差替えと抽出式変更を分け、式だけなら保存PDFを再読する。403で通知が省略されてもUNAVAILABLE/Actions失敗は残り、未公表と推定しない。

<a id="article-acquisition"></a>
### 記事

TSRデータインサイト・日銀新着の定期収集と、その他の媒体を含む手動一括取得は終了した。[必要時の企業調査](../docs/reference/stock-assessment.md#recent-news-research)で発表元の一覧から原文を読み、判断に使った出典を残す。[経済原本](#economic-acquisition)（日銀の経済数値を含む）と[数値の外部評価](#external-assessment-acquisition)は同じ入口で継続する。

日次は圧縮済みの現行Marketから始め、旧releaseの記事本文・観測・抽出・投影を復元も再発行もしない。[旧記事workflow](../.github/workflows/cloud-article-sources-current.yml)は無効のままにし、旧runを再実行しない。この日次は記事の旧版を移行・削除するジョブではない。

最新提案の表示はAppの保存済み本文と根拠から行い、旧Lake releaseを読まない。旧提案を再表示・再調査するための物理データは保持の対象にしない。記事を含まない新しい現行が確認できた後、現行処理や未完の受領に不要な旧記事オブジェクトを退役させる。過去記事の原本照合や、旧releaseの全量hydrateは保証しない。

公表済み提案は保存した根拠で読み、Lakeに記録した出典は指定の固定releaseで同じID・hashを照合する。stageだけが分かる場合は、元のstage ID・manifest SHA・receipt ID・receipt SHAを指定し、原本を私有ディレクトリにコピーする。304の原本は同じ内容の別受領ではなく、その304が指定する元200。

```bash
uv run baibai-jobs article-evidence --source <SOURCE> --stage-id <STAGE_ID> --manifest-sha256 <HASH> \
  --receipt <RECEIPT_ID> <RECEIPT_SHA256> --directory <PRIVATE_OUTPUT>
```

固定stageの読取は、Lakeへの所属や抽出成功の証明ではない。原本が照合できない場合は出典を未確認とし、現在のWeb本文で置き換えない。

### 先行切替の旧writerフェンス (#2913 / #2829)

日次・経済取得の前に、[旧writer確認](../.github/workflows/cloud-writer-retirement.yml)が停止した記事・経済・日次workflowの明示無効とrunの終端を確かめる。対象のID・パスと例外のrun・SHAは[診断実装](../tools/diagnostics/check_article_cutoff.py)が持ち、workflowは `--report-v2-condition` で判定する。この確認だけでは現在の記事取得停止や入力の充足を証明しない。

未終了runの例外は、既知の旧日次が同じqueued状態でjobが0件と確認でき、現行writerがv2 leaseを検証した場合だけ。それ以外の未終了runや例外の状態変化、一覧不明なら止める。例外が残る間はv2の記録を削除・旧版化しない。

### JPX PDF

JPX決算予定/規制は `uv run python -m baibai_jobs.integration.source_handoff acquire --source <SOURCE> --asof <DATE> --directory <PRIVATE_DIRECTORY>` で原本だけ取得する。SOURCEは `jpx-earnings` または `jpx-regulations`。部分受領も同じモジュールの `stage --source <SOURCE> --stage-id <UNIQUE_ID> --directory <PRIVATE_DIRECTORY>` で保存し、L2は通常の統合で作る。

失敗seal済みのJPX stageを限定回復する場合は、原本とmarkerをread-onlyで照合してからexact source・stage ID・manifest hashを指定する。複数件はoptionを繰り返す。

```bash
uv run python -m baibai_jobs.integration.sources plan --directory <PRIVATE_RECOVERY_DIRECTORY> \
  --recover-jpx-stage <SOURCE> <EXACT_STAGE_ID> <EXACT_MANIFEST_SHA256>
```

他のpending stageや外部評価readyは混ぜず、元ID/hash/受領時計を保つ。fresh canonicalと同じpublication leaseでapply→model-integration→readback→sealへ進める。成功は同stageのrecovered-<hash>.jsonに記録し、元の失敗markerを削除/上書きしない。通常plan/resumeは失敗stageを自動で再選択しない。

明示PDFは既知URLで取得済みの原本だけを`financial-pdf-source --receipts <JSON> --directory <NEW_PRIVATE_DIRECTORY>`へ渡し、daily自動取得へ加えない。HTTPなしで実UTC/bytes/hash等を検査するため、mtime/公表日で時計を作らない。生成後はintegration.source_handoff stageへ--source jpx-financial-pdfs（完了転換は--collection capital_actions/--source jpx-capital-actions）を渡す。不対応も原本を保ち、株数/自己株を推定しない。選択/訂正basisは[観測reference](../docs/reference/model-observations.md#reported-comparisons)へ。

EDINETの取得・facts・再抽出は[独立L1の手順](#edinet-list-native-l1)へ。

<a id="external-assessment-acquisition"></a>
### 外部評価

[数値の取得枠](../.github/workflows/cloud-external-assessment.yml)はMonexと株マップ。個社TradingViewは[必要時のL3調査](#tradingview-expectations)だけに使う。楽天・みんかぶの数値は[廃止判断](https://github.com/koumatsumoto/baibai-loop/issues/1820#issuecomment-6856119813)により新規取得・新しい評価への旧値採用をしない。旧原本や保持済みの隔離copyは読めるが、旧planから未試行pairを取得し直さない。ニュースやトウシル[記事](#article-acquisition)の扱いとは別。

通常TLS/proxyで有限銘柄を直列取得し、試行別private captureをcurrent-schemaの隔離marketへimportする。`legacy_stage_unsupported` は原応答を含まない正規化済みstageの拒否で、原本captureを指定し直す。parse失敗でも原本は保ち、L2を部分追記しない。再投入0件を確認する。

```bash
uv run baibai-jobs external-assessment collect --source monex --tickers 7203 4436 --output <NEW_PRIVATE_CAPTURE>
uv run baibai-jobs external-assessment import --input <PRIVATE_CAPTURE> --db <LOCAL_MARKET_COPY>
gh workflow run cloud-external-assessment.yml --ref main -f source=monex -f batch_id=<LABEL> -f master_asof=<YYYY-MM-DD>
```

日次は固定master/calendar/parserの元bindingで再開する。依存不足を全market再取得で迂回しない。同sourceを重ねずqueue後cycleを確認し、plan再作成/hash手修正をせず期限内未試行pairだけを進める。保存失敗pairの再取得は別labelとする。HTTP 401/403はそのsourceの当該passを止め、次の通常slotでは未試行pairだけを進める。

[外部評価summary](src/baibai_jobs/acquisition/external_assessments/cohorts.py)のstate_counts/reason_countsと取得ログのfailed_http_status_countsは保存状態の集計。failed_http_status_countsは非200で失敗したpairだけの内訳で、正常欠測やHTTP 200の解析失敗を含まない。provider_requestsは今回の呼出し、http_requests/response_content_bytesは再開前を含む[cohortの記録](src/baibai_jobs/acquisition/external_assessments/daily.py)であり、本文の新規保存数とは分けて読む。

新原本はretainedにのみ保存する。旧参照でも同じretained原本が検証できれば再開できる。原本がなければ停止し、旧rawからの複写や現在の再取得で埋めない。

lease外でacquire→inspect→ready --cumulative、共通統合で固定集合を公開/readbackしてackする。通常の探索は、同じplanにreadyが一つだけでその本文SHAへ正式ackがあるとき、採用済みstageを再監査しない。別のreadyがあれば固定集合の包含を確認し、後着小集合で後退させない。確定分で明示終了するときだけworkflowの-f operation=stage_confirmed_partialを使う。401/403/429/未完を自動closeしない。pointer後terminal失敗は現状態を読み、未sealならfresh hydrateから再適用する。再開は確定済みstageだけを使い、stage確定前の試行を別の監査台帳に保存しない。中断時の未確定試行・期限切れ原本の復元は保証しない。history_digestsは保存後baselineで、更新前の全履歴保全を証明しない。

保存原本の再解析が必要なときは、現行の隔離copyへ対象attemptだけを `external-assessment rebuild-raw` で再解析する。元の受領日、対象L2の差分、直前/直後cutoffの値を確かめて[共通公開](#machine-publication)へ進む。HTTPを再試行せず、未公開の旧copyは本番へ戻さない。

<a id="tradingview-expectations"></a>
### 個社TradingView

取得は**L3で選定済みの個社に、判断を変え得る問いがある場合だけ**。利用を認められたTradingViewの個社予想を参照し、対象期間・通貨・観測時点の比較可能性が確認できない値は、方向や執行条件の根拠にしない。データが得られなくてもL3を止めず、一次資料の確認を続ける。全銘柄の定期・手動一括収集や旧巡回の途中からの再開、日次の個社入力発行、受領の新規stage・通常統合・sealは行わない。[多市場の取得](#判断直前の多市場補助観測)は別。

通常planはhandoffの宣言sourceで旧個社を除外する。stage名だけでJPXを除外しない。保存済みstage、元のreceiptと判定、過去のGeneration・提案は書き換えず、成功markerを付けない。

切替前に旧版の個社取得と共通日次の稼働・待機を確認し、新規起動せず使われなくなってから切り替える。旧個社と他sourceが混ざった `plan.json` / `applied.json` を、新版で引き継いで部分sealしない。残る非TradingViewの受領を確認し、必要なら現行の固定計画からやり直す。

履歴の確認には、元の `result.json` とWAL反映済みの同じ `receipts.sqlite` を使う。必要なら既知のstageを新しい私有作業領域に `source_handoff download --source tradingview --stage-id <STAGE_ID>` で取得できる。 `result.json` の `snapshot_date` と `receipt_ids` を指定した下記の診断は読取専用で、評価列の欠落と正常な欠測を分ける。

```bash
uv run python -m baibai_jobs.integration.tradingview_receipts \
  --sqlite <LOCAL_RECEIPTS_SQLITE> --asof <CYCLE_DATE> --diagnose-receipt <ID>
```

既存receiptの投影再現が必要な場合に限り、固定参照を使う `source_handoff replay-copy --source tradingview --database <LOCAL_MARKET_COPY>` または `tradingview-source rebuild` を隔離コピーに対して使う。現在のmasterで過去観測を書き換えず、コピーは本番へ戻さない。

### 判断直前の多市場補助観測

[market-evidence](src/baibai_jobs/acquisition/market_context.py)はquote/5分足のprivate原受領で、日次充足・価格master・正式提案終値とは別。prepare→stageで保存する。別sessionではdownloadに`--stage-id`・`--manifest-sha256`、取得した作業copyのreadに`--receipt-id`・`--sha256`を渡し、同じAssessment入力を使う。同じ引数名に異なるIDやSHAを重ねる指定は受け付けない。未保存captureに耐久参照を付けない。

日次market-dailyはjob上限15分、capture上限5分（TERM後30秒でKILL）。capture timeoutは失敗のまま、完了分を既存partial handoffへ渡してcooldownを保存する。setupを含むjob期限や外部cancelでは後段も停止し得る。

固定economic入力の参照/時計/欠測と、評価時刻/基準JST日末の両cutoffを守る。最新失敗を旧成功で埋めず、前日基準へ当朝夜間受領を加えるなら別入力にする。休場/遅延/限月/basisを揃え、受領時計を取引時計、bar tを最終約定、update_mode不明をリアルタイム、末尾足を確定値にしない。価格で判断を変えるなら新入力/再Reviewへ進める。

<a id="jquants-native-operations"></a>
## J-Quants native L1

取得はnative L1へ保存し、L2生成/公開と分ける。入口は`jquants-<source>-native`のinitialize/plan/acquire/resume/commit-work。初期化は原本のない新しい空namespaceだけで一度行い、head消失/移行失敗を空initializeで迂回しない。新plan/acquireは新directory、結果不明はresume、保存attemptは同directoryのcommit-workを使う。各source optionはhelpを参照する。

| source | 明示取得範囲 |
| --- | --- |
| financial / price / master / calendar | --start / --end。master/calendarの--forceは明示訂正だけ。 |
| margin-alerts / short-sale-reports | --asof、任意--start。休場も公表確認。 |
| margin-interest | --asof、有限backfillは--start / --end。固定calendarが必要。 |

`rebuild-<source>-native`は--work-directory / --start / --end / --outputから新しいL2とinput.jsonを作る（calendarは下の専用引数）。保存入力からの再計算はGenerationのinputsにあるdescriptor付きexact InputSnapshotを--saved-inputへ渡す。head/summaryに付け替えず、新outputを使う。欠損/改変/partialのL2や不適格な[投影cache](src/baibai_jobs/integration/native_projection_cache.py)は同じ固定L1から再生成し、原本を再GETしない。J-Quantsの受領はnative L1で完結する。旧定期SQL handoffの予約形式は[通常の探索](src/baibai_jobs/acquisition/receipts.py)で読まず、J-Quants SQLを含む旧planも再生しない。取得済み全原本を照合した意味でも、旧stageを削除する指示でもない。

信用・空売りの保存入力に投影のcoverageがあり、revisionが未記録または現行と異なる場合は、現行コードで再計算せず止める。当時の公開releaseを読むか、記録された生成commitで固定入力から再計算する。取得範囲だけを固定した原本選択は、過去の投影と同じ結果を保証しない。

<a id="price-native-l1"></a>
**日足。** 正常空L1と価格0行の公開拒否は別。native暦で休場を証明できる未試行日だけを公開coverageから除き、raw不足は残す。各日を固定L1から再投影する。同じ作業SQLiteは他のwriterと共有しない。

日足の冷再構成は `input.json` のある新出力だけを完了として扱う。中断でマーカーのない出力が残った場合は別の新規出力先を使う。保存入力に固定暦がないときは未試行日を休場扱いしない。

`--saved-input` を渡さない日足CLIは、開始時に現在のL1 headを一度固定する。`--work-directory` に置いた旧 `head.json` や過去のattemptは入力にならない。元の失敗attemptに結び付く保存InputSnapshotがなければ、元attemptの再現としてはそこで止める。現行headを診断する場合は別の新しい出力先を使い、同じ日付でも過去の選択に付け替えない。

旧releaseに公開された日足を調べる場合は、native L1からの再計算ではなく[固定releaseのhydrate](#local-release-hydrate)を使う。
日足の保存入力で投影版が未記録または実行コードと異なる場合、`--saved-input` は旧digestに異なる投影を結び付けず停止する。再構築する場合は公開時のproducer commitを使う。原本の選択や公開済みreleaseの読取は変わらない。

<a id="financial-native-l1"></a>
**財務。** [`rebuild-financial-native --saved-input`](src/baibai_jobs/integration/financial_native.py)は、同じ投影版の固定native入力から財務の派生表を再構築する。版が未記録・不一致なら停止し、当時の実行commitを確認できる場合に限ってその版で再構築する。日次が採用した財務は、そのGenerationの `inputs["financial"]` に固定したnative入力で確認する。旧Parquet `jquants.fin_summaries` は現行Lakeの必須データではない。過去のreleaseに何があるかは[固定manifest](#fixed-release-read)で確認する。`--saved-input` なしの診断は起動時の固定原本を現行版で投影する。いずれも保存原本・Generation・Selectionは置き換えない。

定期財務取得は平日18:13 JSTの `financial-edinet-facts` 枠。Standard等の[公式更新目安](https://jpx-jquants.com/ja/spec/data-update)は18:00速報・翌00:30確報で、取得・統合への到着は保証されない。下の手動取得も同じ[通常計画](src/baibai_jobs/integration/source_handoff.py)を使い、`asof` の7日前から当日までの8暦日と、取得範囲内の欠落・失敗・保留日を再照会する。特定の一日だけを指定するコマンドではない。早期更新が必要な場合だけ実行する。

```bash
gh workflow run cloud-market-sources.yml --ref main -f source=jquants-financial -f "asof=${ASOF:?基準日をYYYY-MM-DDで指定}"
gh run list --workflow cloud-market-sources.yml --branch main --event workflow_dispatch --limit 5
gh run watch "${SOURCE_RUN_ID:?対象の財務run IDを指定}" --exit-status --interval 60
```

[J-Quantsの財務訂正](https://jpx-jquants.com/ja/spec/fin-summary)には通常、新しい開示番号（`DiscNo`）が付くが、2026-11-16以降は同じ番号の値がフラグなしで上書きされる場合がある。過去日の訂正は差分cursorに現れず、通常計画も窓外の取得済み正常日までは再確認しない。調査対象の数値に影響する訂正を把握したときだけ、元の開示日（`DiscDate`）を開始・終了の両方に指定して取り直す。認可された環境で他のJ-Quants取得と重ねない。

```bash
uv run baibai-jobs jquants-financial-native acquire --directory "${NEW_WORK:?新しい作業先を指定}" \
  --start "${DISC_DATE:?元の開示日をYYYY-MM-DDで指定}" --end "$DISC_DATE"
```

新しい受領の日付・取得結果を確認し、必要な[日次統合](#daily)へ進む。再取得は今返る値の確認で、訂正前の値や訂正が反映された時刻は復元できない。日次の再実行だけでは財務を再取得しない。

財務summaryの[API枠](https://jpx-jquants.com/ja/spec/rate-limits)はプラン共通60要求/分で通常plan枠とは別。現行のJ-Quants共通queueはこれより保守的な実装上の直列化であり、時刻変更では外さない。`financial-edinet-facts`は枠名で、土日は従来どおりEDINET factsだけを取得する。

<a id="master-native-l1"></a>
<a id="calendar-native-l1"></a>
**master/calendar。** 日次の外部評価はcalendarを先に固定し、休場なら直前営業日のbasis masterから銘柄集合を固定する。unknown/partialを休場へ変えず、空/failed/held masterを旧値で補わない。保存packetの元master/calendarはhead削除/訂正後も保持参照から再生し、新しいheadへ再固定しない。

masterは明示した日付を取得し、過去L2を使わない。`jquants-master-native acquire --asof`は指定日の間を埋めず、未来日を拒否する。calendarの明示取得は他source/L2/release不要で、必要範囲を指定する。空/partialを成功へ変えない。

日付不正で利用できないcalendar原本も残す。新しい価格の休場証拠は対象日・日別内容SHA・区分を採用原本と照合し、旧固定selectionは当時の判定で再生する。日付契約の切替時は、既存headを固定して有効な採用日をすべて原本と照合する。価格の休場候補だけでなく、取引日を使う信用・銘柄マスター等の経路も同じ暦を参照するため。旧取得処理が残っていないこととhead同一性を確認し、不一致があれば該当期間だけ新しい受領として明示訂正する。通常dailyで全期間は再取得しない。

新しく固定する暦の投影は、採用rangeの同じ読取で対象日・内容SHA・区分・受領時計を照合する。不一致は元の受領を保ち、休場ではなく不明とする。日次の価格証明・信用残の対象日判定は同じ検証結果を使い、信用残は週内の金曜まで必要な場合だけ参照する。外部評価の新規計画も固定する範囲を同じ規則で検証し、現在headを訂正しない。版記録のない既存の固定暦は明示再生で当時の採用値を保つ。

```bash
uv run baibai-jobs jquants-master-native acquire --asof <DATE> --asof <DATE>
uv run baibai-jobs rebuild-calendar-native --saved-input <SAVED_INPUT_JSON> --output <NEW_OUTPUT>
```

この明示日指定は成功時だけ一時workを整理する。失敗/未充足時に表示されるworkの日付directoryを保持し、受領済みattemptの公開結果を解決するには`jquants-master-native commit-work --directory <WORK>/<DATE>`を使う。元受領の時計/UUIDを変えず、公開結果が不明なまま再取得しない。machine自体が失われた場合のlocal work保持までは保証しない。

<a id="margin-alerts-native-l1"></a>
<a id="short-sale-reports-native-l1"></a>
**日々公表銘柄/空売り報告。** 取得窓と保持範囲の不足はplanで確認する。固定selection/sealを公開lineageまで渡し、正常空の全履歴0行と観測行を要求する公開policyを分ける。架空行で公開条件を通さない。

<a id="margin-interest-native-l1"></a>
**週次/全銘柄日次信用残。** 取得・公表確認に必要なnative暦を先に保持する。planの暦不足を確認し、unknown_calendar_days/unknown_incomplete_requestsを休場にしない。週次は2026-09-18残高まで、日次は2026-09-25からを扱い、9月19〜24日の系列を作らない。

週次の空充足には実確認JST日より前の後続2営業日、日次には後続1営業日の暦を使い、日次空は不足のままにする。最終週は公表日後に再確認し、未充足を残す。有限backfillへ今日までの全暦を要求しない。

```bash
uv run baibai-jobs jquants-margin-interest-native acquire \
  --directory <NEW_WORK> --asof <DATE> --start <DATE> --end <DATE>
uv run baibai-jobs rebuild-margin-interest-native --work-directory <NEW_READ_WORK> \
  --calendar-head <NEW_WORK>/calendar-fixed.json --start <DATE> --end <DATE> --output <NEW_OUTPUT>
```

plan・固定calendar・実受領時計を保つ。`--saved-input`はそのmargin/calendarからの再計算で、過去の判断・PITの完全な復元ではない。

<a id="edinet-list-native-l1"></a>
## EDINETの独立L1

一覧→ZIP/scope→L2を分け、通常source/手動refresh/TOBはnativeへ原本を先に確定する。通常のnative archive取得はmarket DBを読まず、固定一覧原本とsource scopeからZIP候補を決める。明示の `maintain-market refresh-edinet-metrics` は固定一覧をmarket SQLiteへ投影し、その一覧行からCSV ZIP候補を選ぶ。どちらも抽出済みmetricsに依存しない。共通統合・モデル・publisherはplan/applied.jsonの同じ固定一覧/archive pairを使い、欠落を現在headや旧stageで埋めない。旧固定release readerと明示cold再構築は残す。

head欠落を空initializeで埋めない。原本のない隔離環境だけで新規取得を始める。以下は一覧とarchiveを同じ隔離bucketに作る例で、一覧CLIだけは環境変数でbucketを指定する。

```bash
R2_STORES_BUCKET=<ISOLATED_BUCKET> uv run baibai-jobs edinet-list-native initialize --directory <NEW_LIST_INIT_WORK>
R2_STORES_BUCKET=<ISOLATED_BUCKET> uv run baibai-jobs edinet-list-native acquire \
  --start <DATE> --asof <DATE> --directory <NEW_LIST_WORK>
uv run baibai-jobs edinet-archives-native initialize --bucket <ISOLATED_BUCKET> --directory <NEW_ARCHIVE_INIT_WORK>
uv run baibai-jobs edinet-archives-native acquire --bucket <ISOLATED_BUCKET> --directory <NEW_METRICS_WORK> \
  --group edinet-metrics --asof <DATE> --lookback-days 540
uv run baibai-jobs edinet-archives-native acquire --bucket <ISOLATED_BUCKET> --directory <NEW_FACTS_WORK> \
  --group edinet-facts --asof <DATE> --initialize-facts
uv run baibai-jobs edinet-archives-native freeze --bucket <ISOLATED_BUCKET> --directory <READ_DIR>
```

手動source=allは一覧/stage成功後にarchiveへ進む。一覧失敗時はall内archiveを止め、有効一覧を確認できる場合だけ同asofのarchiveを再実行する。metricsは取得窓の一覧に未受領日があっても、固定一覧で判明した許可CSVを先に保持する。未受領日はlist_unavailable_days、失敗受領日はlist_failed_daysへ残し、取得全体は失敗のままにする。明示refresh-edinet-metricsも既知CSVを先に保存する。要求窓内に未受領日または最新一覧の失敗日が残る場合は、先着の有効一覧があっても既存metrics/coverageを変えず失敗終了する。窓外の失敗は停止理由にしない。L2の完全window条件を緩めず、一覧回復後は保存済みZIPを再利用する。保持docID/typeを再GETせず、L2成否/revisionを取得条件にしない。年次訂正の未保存分は同じ年次取得で扱う。CSV/XBRLともHTTP 401/403と、HTTP 200本文のStatusCode 401で当該passを止め、保存済みの部分原本を保ち、次の通常取得は未保存docID/typeだけを進める。

大量保有報告（書類種別350/360）のZIPは通常のモデル・提案の入力ではないため、一括取得しない。保存済み原本は変更せず、必要な案件だけ原典を個別に確認する。

一覧/archive未到着はdeferred_inputsで適格保存値を使い、到着後に再抽出する。解析失敗/原本破損を延期で隠さず、不正dayは正常投影を保ちながらsource-check失敗を残す。延期だけの継続とpartial/日別成功を分ける。誤orphanは現年度CSVを確保して次統合で回復し、全期間GETを代行しない。

<a id="edinet-research-facts"></a>
### EDINET factsの取得と読取

固定releaseから[facts reader](../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_facts/)を使い、文書/ticker/対象JST日とusable_documentを確認する。日末cutoffはintraday PITでなく、未選択は書類不存在を意味しない。segment/debtはdocID・期・連結・通貨を揃え、抽出行だけで全債務/資金余命を確定しない。

facts scopeはL1取得範囲。L2品質でglobal開始日/docID集合を狭めず、failed/未提供/交代のfamilyも出所と共に保つ。初回annual取得は明示initialize成功後だけ有効化する。途中失敗は同じrefreshで再開し、ticker限定で他社/globalを置換しない。offline extractのinitializeは将来GETを有効にしない。

```bash
uv run baibai-jobs maintain-market refresh-edinet-facts --asof <YYYY-MM-DD> --initialize --sqlite-path <STAGED_MARKET_SQLITE>
```

手動refreshのSQLには年次scopeの保存済みXBRLを残すので、再抽出だけなら同じSQLでextract-edinet-factsを使う。大量保有報告のXBRLや財務指標CSVを新規に複写しない。全原本が必要なら[明示cold再構築](#zipscopeの通常取得と固定consumer)を使う。stored_onは初回保存UTC日で公表時刻でない。未対応0行/failedと鮮度を分ける。

<a id="edinet-metrics-revision"></a>
metric revisionは抽出/選択の意味変更だけで更新し、配置/import/retryやfacts revisionと分ける。隔離copyで保存CSVを再抽出・比較後、同じleaseでfresh pull/hydrate→extract-edinet-metrics→共通公開→固定readback。期待外差/失敗は止め、変更後hydrateや旧copyの本番復元はしない。

### 公開失敗の再開

一覧/ZIPとも保存attemptは同workのcommit-workで公開/readbackする。pendingがあれば新HTTPを止める。workflowがprivate failure stageを残した場合は元stage IDで回復する。

```bash
uv run baibai-jobs edinet-list-native commit-stage --stage-id <ID> --directory <NEW_WORK>
uv run baibai-jobs edinet-archives-native commit-stage --bucket <BUCKET> --directory <NEW_WORK> --stage-id <ID>
```

同UUID・実UTC・初回bytes/stored_on/scopeを保持し、CASは最新headとのunionで明示再試行する。部分workで失ったheadを再作成せず、旧stageは保全して調査する。killやlocal/private両保存失敗まで回復を保証しない。

<a id="zipscopeの通常取得と固定consumer"></a>
### 固定consumerと旧copy

[SQL adapter](src/baibai_jobs/integration/edinet_archive_native.py)のrebuild_fixed_edinet_inputは保存pairから全ZIP/stored_on/scopeを再構成するため、plannerには使わない。必要CSVだけの比較は次を使う。候補の完成/partialと延期/parse失敗を分け、生成を通常公開への採用としない。

```bash
uv run baibai-jobs rebuild-edinet-metrics --saved-input <INPUT.json> --output <NEW.sqlite> --asof <DATE> --lookback-days <N>
```

手動TOB退出値の再計算には `maintain-market build-tender-offer-exits --saved-input <INPUT.json> --asof <DATE> --sqlite-path <MARKET.sqlite>` を使う。EDINETは保存pairの一覧と必要なtype=5だけを読み、別の一覧・SQL ZIP・最新headで補わない。JPX上場廃止一覧は指定のSQLiteから読むため、当時の結果を再計算するには同じJPX投影も用意する。EDINET原本の欠損・破損・decode失敗や、JPX一覧が未投影なら停止し、既存の退出値を保つ。投影済み一覧に対象が0件なら正常に0件へ置換する。`--asof`は提出日と上場廃止日のcutoffで、受領時点の再現ではない。`--saved-input`なしの旧SQL読取と明示refreshは別経路として残す。

手動TOB退出値と東証の資本政策一覧は任意の補助資料で、現行提案の入力ではない。[提案に使う監理・整理指定とEDINET書類参照](../docs/reference/trade-proposal.md#listing-and-price-basis)は別経路。

旧SQL原本を含む作業copyは固定pairのkey・内容・初回保存日・scopeが照合できた時だけ原本cacheを除く。範囲外のZIP/scopeやnativeより早いSQL受領日は捨てずに停止する。全ZIPの再取得は不要。旧一覧cacheのあるhydrateには`--bucket`が必要で、初回の全体確認・必要なcompactの容量を確保する。[日次](#daily)は `sources plan` でEDINETの一覧とarchiveの組を固定し、同じ `sources apply` の `applied.json` を同じ基準日の `model-integration --integration-report` に渡す。固定一覧を投影し、raw SQLの複製・Parquetの再出力はしない。個別の比較から別の公開経路は起動しない。

匿名の取消・訂正で過去の発行体が分からない場合、旧tickerや採用順を現在値から復元しない。単独の解析で元の同定が必要なら、同じ固定pairの一覧を[一覧の投影](src/baibai_jobs/integration/edinet_list_native.py)で明示materializeする。既存の隔離・出所・revision・時計・factsの0行/失敗理由を保ち、cold再構築をwarm履歴/PITの完全再現としない。

<a id="保持とgc"></a>
## ローカルmirrorの整理

GCはlocal lake mirrorだけ。grace後の到達不能prefixをplanし、apply時の再plan hash一致で削除する。native L1/共有固定inputは別寿命で、R2は削除しない。

rootはlocal currentだけで、保存run/Researchが参照する旧releaseを自動保護しない。必要な旧releaseを保持するmirrorではapplyしない。またlockはGC同士だけを直列化し、公開・hydrate等は共有しない。同じmirrorの読取・生成・公開を止めた状態でplan/applyし、再planの後も静止を保つ。R2のcurrentやpublication leaseをlocal mirrorの排他保証に読み替えない。

```bash
uv run baibai-jobs snapshots inventory --root stores
uv run baibai-jobs snapshots gc --mirror stores
uv run baibai-jobs snapshots gc --mirror stores --apply --plan-hash <HASH>
```

問題releaseは正しいstoreから前向き発行し、immutable Bucket Lockとmutable pointer/stagingを混同しない。

## 定期maintenance

TSE資本政策/JPX廃止は翌月15日以降に既存refresh/buildと必要な公開を行う。全履歴監査を定例化しない。依頼された注文結果だけmeasure_limit_outcomesで測り、価格差を逸失利益や指値規律の因果効果としない。

通知webhookのrotationは新規/再生成→DISCORD_WEBHOOK_URL更新→通常通知確認。旧webhookはDiscord側で消すまで有効。実credential変更は別の依頼範囲に従い、過去Screeningを再実行しない。

<a id="web-serving"></a>
## 保有・運用Webの表示更新

[表示生成](../web/backend/src/baibai_web/materialize.py)の入口は二つある。[日次](#daily)はMachine公開成功後にその作業Marketを使う。表示だけを更新する場合は `gh workflow run cloud-materialize.yml --ref main`。単独更新ではlease内でAppを取得し、現行ポインタのMarketから価格・calendar・masterだけをhydrateする。Runsは取得しない。Marketが失敗した場合はrunに失敗を残し、未検証の時価を使わずAppに保存された取引・成績・提案だけを表示する。過去の日次runを指定して再公開する入口ではない。

```bash
uv run python -m baibai_web.materialize --output-dir /tmp/baibai-serving
```

これはlocal生成だけ。公開はworkflow内の`jobs/scripts/r2_transfer.sh upload-portfolio-views /tmp/baibai-serving`を使い、全viewの全bytes読戻しを同lease内で確認する。

**実配信と正本の照合:** 私有の空の領域へ`pull-app`し、固定Appと取得ETagを組で保つ。認証済み本番の`/api/proposal/latest`と`/api/proposal/history`の成功JSONを私有ファイルに取得する。Appと2表示objectの各ETagが採取前後で同じで、App取得世代・Workerのbucket/routeが対象と対応することを確かめる。Workerは応答にETagを含めない。元App世代からの生成まで受け入れるには、同じ取得ETagのmaterializer runが当該objectを公開・読戻した証拠も必要。なければ「本文一致、生成元世代は未確認」とする。下記は表示生成に使った版で実行し、稼働CLI・Worker/UIの版とは別に記録する。本文・銘柄・ETag・閲覧秘密を公開logに出さない。

```bash
uv run python -m tools.diagnostics.check_served_proposal \
  --db "$APP_COPY" --latest-json "$AUTH_LATEST_JSON" --history-json "$AUTH_HISTORY_JSON"
```

診断は訂正を反映した正式latest全項目、履歴の形式・ID集合・生成時刻、Caseの対象/結果/Selectionを照合する。他の履歴本文の全項目比較ではない。`--proposal-id`で対象を固定できる。`--require-completed`はCase完了参照まで同じ提案であることを求める場合だけ使う（後日の訂正で異なり得る）。終了0は配備・実画面・新規提案発行や売買の証明ではない。

必要な表示データを公開・読戻しした成功main runを確認後、[web workflow](../.github/workflows/web.yml)をmainで明示dispatchする。push/PRのweb runは検証だけで、Worker/UIを配備しない。

```bash
gh workflow run web.yml --ref main
gh run list --workflow web.yml --branch main --event workflow_dispatch --limit 5
gh run watch <WEB_RUN_ID> --exit-status --interval 60
```

対象SHAの`Deploy Worker and UI assets`が実行・成功したことを確認する。待機中にmainのWeb入力が変わると配備はskipされるため、run成功だけで配備済みとしない。認証APIは対話terminalで`bash web/edge/scripts/verify-deployment.sh`を実行し、閲覧パスワードはpromptへ入力する。`WORKER_BASE_URL`で確認先を変える場合はHTTPS originだけを指定し、userinfo・path（末尾slashを含む）・query・fragment・空白を含めない。画面のDashboard・提案emptyまたは保存ID/基準日/発行時刻付き本文の表示受入は別に確認する。旧object/secretは削除しない。

<a id="web-authentication"></a>
VIEW_PASSWORDをBearerとして使う。閲覧画面は空白なしの半角英数字・記号（印字可能ASCII）だけを受け付け、入力の前後空白を除くため、secretもこの範囲で用意する。実Workerのsecret更新は別途認可された操作として行い、秘密を本文・shell引数・logへ残さず、R2 credentialと混ぜない。
