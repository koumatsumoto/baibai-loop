# batch — production orchestration と cloud store 運用

本書はmachine処理、store転送、公開と復旧の手順を所有する。domain処理はengine、read modelはwebが担う。以下のcommandはrepository rootのBashから実行し、終了状態と次の操作は各節に従う。CLIの全引数は該当`--help`を参照する。

## 目的から探す

| 作業 | 手順 |
| --- | --- |
| 分析前に保存情報を揃える | [クラウド正本の取得](#クラウド正本をローカルへ取得する) |
| localで更新したmachine情報を反映する | [クラウド更新](#ローカルからクラウドを更新する) |
| 判断・確認済み事実を表示へ反映する | [application反映](#application-db-を反映する) |
| 経済原本・L2系列を直す | [原本と系列定義の修正](#指標観測とregistryの修正) |
| EDINET metricsの抽出意味を変更する | [意味revision更新](#edinet-metrics-revision) |
| EDINET原典のResearch factsを揃える | [EDINET Research facts](#edinet-research-facts) |
| remote拡張なしで固定releaseを読む | [完全mirrorからのhydrate](#local-release-hydrate) |
| applicationの過去snapshotへ戻す | [復元](#application-db-を復元する) |
| machineの途中公開・世代競合から復旧する | [部分push](#部分-push-からの復旧)・[R2安全境界](#r2-transferの安全境界) |
| 公開brokerページの観測を取得・再開する | [限定取得](#external-assessment-acquisition)・[日次cohort](#日次取得と固定コホートの再開) |
| TradingViewを取得・認証復旧する | [観測と運用](#tradingview-expectations)・[障害診断](#tradingview-expectationsの障害診断) |
| 画面code・共有readを設定する | [Worker配信](#workerをdeployする)・[共有read](#shared-read-setup) |
| 初回の構成を確認する | [Cloudflare / GitHub Actions構成](#cloudflare--github-actions-構成) |
| 過去の切替と現在の修正を区別する | [保存名切替後の運用](#store-identity-recovery)・[schema修正](#schema-cutoverを修正するとき) |


<a id="actions-write-boundary"></a>

## 開発環境と本番書込の実行境界

3系統の分担と開発用接続は[Python foundation](../docs/reference/python-foundation.md#cloud-development-boundary)に従う。
以下のlocal commandは対応OS・依存・認可が揃った作業環境で使う通常の運用経路であり、物理的なユーザーPCだけに限定しない。
データ状態の正本と作業コピーは[architecture](../docs/architecture.md#store-authority)に従う。
`publish.sh`はapplicationの作業コピーを条件付きでクラウドへ反映し、Actionsのmaterializeを起動する。
machine / lake発行、初回seed、独立TradingView取得・公開も既存のlocal経路を維持する。

外部データの更新・公開は、依頼範囲に含まれる既存Actionsを優先する。各節の前提・main ref・入力・対象storeを確認し、run IDを固定して完了と公開物を検証する。
Research判断・提案・task等のapplication更新は、対象内容と本番保存先・実行環境が承認されていれば、既存engineで取得済み作業コピーを更新し、[application反映](#application-db-を反映する)へ進める。taskだけの特例や、対応workflowが無いことを理由としたローカルPC必須条件にしない。
操作は正規writer/serviceを使い、別環境への移行で検証・backup・取得世代の照合を省かない。実行依存や接続が不足する場合はその状態を報告し、権限変更・独自SQL・無条件upload・重複dispatchで回避しない。承認済み本番操作も、注文実行や未報告の台帳更新を含まない。
私的な保有・資金を含む原稿やDBを環境間で渡す場合は、データの範囲・非公開の保存先・目的を確認する。退避の許可を本番更新の許可と読み替えず、Git・PR・Issue・公開artifactへ載せない。
文書変更・PR作成だけを理由に本番workflow、OAuth smoke、R2 acceptanceを起動しない。

writer用R2 credential、GitHub Secret writer token、deploy tokenは下表の実行主体に配置し、
通常のクラウド開発へ渡さない。providerキーによる認可済み読取はPython foundationの接続表に従う。
承認済みapplication操作に使う既存runtime credentialも、通常開発用の常設接続や他環境へ流用しない。
値をInstall script・Start skill・image snapshot、Git、PR、Issue、log、artifactへ埋め込まず、設定の有無だけを確認する。
文書の変更は既存credentialやruntime権限を変更しない。

## 長時間処理の監視

対象run IDを固定して監視する。hard timeoutはworkflow定義を正本とする。

```bash
gh run watch <RUN_ID> --exit-status --interval 60
```

外部監視がtimeoutでも処理が終了したとは限らないため、対象の状態と現在stepを確認し、activeな処理を重複起動しない。

## Cloudflare / GitHub Actions 構成

`baibai-stores`はL1・machine store・application DBの正本、`baibai-serving`は表示用read modelを保持する。両bucketのPublic Development URLとcustom domainを無効にする。

| bucket | object | owner |
| --- | --- | --- |
| `baibai-stores` | `market.sqlite`（store-local data + `lake_store_origin` metadata） | `cloud-daily-batch` + ローカル`push-market`（cloud copyのmerge後だけupload） |
| `baibai-stores` | `lake/`（`l1_source`原本と`l2_processed`派生datasetの正本） | `cloud-daily-batch`・`cloud-tradingview-snapshot`・`cloud-external-assessment`の公開job + ローカル`r2_transfer.sh publish-lake` |
| `baibai-stores` | `runs.sqlite` | `cloud-daily-batch` + 明示的なローカルdailyの`push-machine` |
| `baibai-stores` | `indicators.sqlite` | `cloud-daily-batch`（rolling窓）+ ローカル`push-indicators`（全履歴。cloud copyのmerge後だけupload） |
| `baibai-stores` | `baibai.sqlite` | 取得世代への条件付き更新（実行環境の分担は前節） |
| `baibai-serving` | `views/*.json` | GitHub Actions materialize |
| `baibai-serving` | `history/candidate-views/<asof>.json` | 日次batch、R2 lifecycleで31日後に削除 |

`baibai-serving`のR2 lifecycle ruleは`history/candidate-views/`だけに設定する。bucket全体へ設定すると`views/meta.json`まで期限で消え、欠落を検知できない。候補membershipの長期履歴は保持しない。

`cloud-materialize`・`cloud-daily-batch`・`cloud-tradingview-snapshot`と、`cloud-external-assessment`のpublish jobはGitHubの`cloud-publish`を共有する。外部評価のplan/acquireは別の取得queueで動く。GitHubとローカルのpublication競合は[ローカル発行手順](#ローカルからクラウドを更新する)のleaseで止める。servingへのwriteはGitHub Actionsだけが行う。application更新の取得世代・競合処理は[application反映](#application-db-を反映する)に従う。

資格情報はprincipalごとに分ける。

| principal | 設定 | scope |
| --- | --- | --- |
| GitHub Actions | variable `R2_ACCOUNT_ID`、secrets `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / provider 3本 | 必要なtransfer/provider stepだけ、stores + serving read-write |
| GitHub Actions（通知） | secret `DISCORD_WEBHOOK_URL` | daily・TradingView・External Assessmentの通知 stepとwatchdogの警報 stepのみ（job envに出さない） |
| GitHub Actions（Worker deploy） | variable `R2_ACCOUNT_ID`、secret `CLOUDFLARE_API_TOKEN` | 対象accountの`Workers Scripts Write`、`web`のdeploy stepのみ |
| ローカルPCの`.env` | `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | stores read-write。通常のクラウド開発へ持ち込まない |
| Wrangler OAuth | `wrangler login` | bucket初期設定、Worker secretの手動設定 |
| Worker secret（閲覧認証） | `VIEW_PASSWORD` / `READ_ACCESS_TOKEN` | Worker runtimeだけ、ownerと共有を分離 |
| Worker secret（market lake接続） | `L1_R2_BASE_URL` / `L1_R2_ACCESS_KEY_ID` / `L1_R2_SECRET_ACCESS_KEY` | `baibai-stores`だけのObject Read only、publisher credentialと分離 |

R2 S3 endpointは`https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`からscriptが組み立てる。credential、password、endpointの実値をGit、issue、logへ書かない。

servingは各workflowのsource store取得・materializeが成功した後だけ発行する。dailyではstore pushの成功も必要である。前提工程が失敗した場合は表示更新を止め、`views/meta.json`を最後に発行する。

provider secretは`JQUANTS_API_KEY` / `ESTAT_APP_ID` / `EDINET_API_KEY`。JPX規制情報の公開URLは
[JPX source設定](../engine/src/baibai_engine/market/jpx_sources.py)の固定値を使い、環境変数では指定しない。
`bootstrap-cache`は`universe.required_jpx_flags`の必要sourceを取得する。日次で取得・検証に失敗した場合は
新しいScreening runを発行せず、独立した指標更新と全views exportへ進みexit 3で報告する。
store検査・exportが成功した場合の後続公開と、致命的失敗による公開停止は[日次機械工程](#日次機械工程--baibai-batch-daily)に従う。


## 日常運用

### クラウド正本をローカルへ取得する

**前提**: research（market-context判断を含む）の分析前に、`cloud-daily-batch`の実run状態を確認する。queued / in_progressなら通常の分析用pullを開始せず、完了後に実行する。固定時刻で安全性を判断しない。

```bash
gh run list --workflow cloud-daily-batch.yml --limit 5
```

TradingViewは受領receiptのL1と正規化snapshotのL2を独立更新するため、完了済みreleaseをcurrent machine bundleへhydrateしてよい。`cloud-materialize`はmachine store / L1を書き換えないので待機条件にしない。ローカルの`publish-lake` / `push-machine` / `push-market` / `push-indicators`とも意図的に重ねず、発行終了後にfreshにpullする。

**実行**:

```bash
batch/scripts/pull.sh
```

**成功確認**: `pull-machine`はreceipt / ETagでmarket / runs / indicatorsを同じ確認済みbundleとして取得し、`hydrate-market`は開始時のcurrent market releaseを固定する。途中でpointerが動けばvisible storeを置換せず停止する。ただしmachine bundleとmarket release pointerを同一transactionでは固定しない。receipt / generation / pointerの競合や検査失敗後のlocal stateは分析に使わず、原因を確認してfreshにpullし直す。`baibai.sqlite`はbulk pullしない。applicationの取得は[application反映](#application-db-を反映する)の`pull-app`前提に従う。

### Screening入力を検証・補修する

**前提**: screening入力を実際に検証・補修する場合だけ実行する。Market Context Assessmentの作成や、
保存済み入力を読むResearchの前提にはしない。対象日を指定し、個別銘柄の確認が必要なら対象tickerを指定する。

**実行**:

```bash
uv run baibai-engine screening verify-cache-coverage --asof YYYY-MM-DD
# 個別銘柄の入力を確認する場合
uv run baibai-engine screening ticker-profile --ticker TICKER
```

**成功確認**: `verify-cache-coverage`が対象日の必要入力を満たし、個別確認では
`ticker-profile`が対象tickerを返すことを確認する。

**停止と復旧**: 取得範囲の欠落は同じas-ofの`screening bootstrap-cache`で補完し、commit済みのchunkを再利用する。財務は未取得範囲と末尾の訂正確認窓を取得する。提供元recordに元から無い数値や、L2で計算不能な値を取得失敗と混同しない。数値が一定割合以上埋まるまで過去の開示日を自動再取得する処理は持たない。元recordの訂正が必要なら対象期間を明示して修復し、raw保存からL2を再計算する。通常の欠測を理由に広いcoverageを無効化したり、全DBを組み戻したりしない。

### ローカルからクラウドを更新する

EDINETの抽出意味revisionを更新する場合は、[再抽出・反映手順](#edinet-metrics-revision)に従う。それ以外は変更codeをmainへ反映し、対象storeと変更内容を確認する。marketはlake所有tableを編集する前に現行releaseへhydrateする。変更後の再hydrateはlocal変更を上書きするため行わない。

GitHubの`cloud-publish` queueはGitHub内の順序だけを守る。GitHubとローカルの共通発行にはR2の`coordination/publication-lease.json`を使う。TTLは固定120分。`publish-lake` / `push-machine` / `push-market` / `push-indicators`をローカルから行う際は、長い取得・準備を済ませてから、production read / merge / publishを次の同一lease内で実行する。`r2_transfer.sh`の低レベルcommandはleaseを暗黙取得しない。`publish-lake`は`PUBLICATION_LEASE_HANDLE`で既存handleを受け取り、export開始前とpointer切替直前に有効性を確認する。`push-app`、`publish.sh`、初回`seed-all`は対象外。

`seed-all` は各 key の不存在を条件に新規作成し、既存 key を上書きしない。4 key は一括transactionではないため、途中失敗後は到達点を確認し、初回seedを既存データの復旧・置換に使わない。

```bash
with_publication_lease() (
  handle="$(mktemp "${TMPDIR:-/tmp}/baibai-publication-lease.XXXXXX.json")"
  rm -f "$handle"
  uv run python -m baibai_batch.storage.publication_lease acquire --purpose operator --handle "$handle" || return $?
  if PUBLICATION_LEASE_HANDLE="$handle" "$@"; then result=0; else result=$?; fi
  uv run python -m baibai_batch.storage.publication_lease release --handle "$handle" || return $?
  return "$result"
)
```

失敗時もreleaseを試みるため、上の関数内では`set -e`を使わない。release失敗時はhandleを残して原因を確認する。busyならowner・purpose・expiryと該当run/processの生存を確認し、blind retryしない。expired leaseのみ次のacquireが条件付きで引き継ぐ。L1とmachine storeのCAS / receiptも整合性を検査し、servingはGitHub Actionsだけが書く。applicationの公開は取得世代への条件付きPUTで競合を止める。

変更した対象に合う経路だけを実行する。lake所有tableの変更:

```bash
with_publication_lease bash -c 'batch/scripts/r2_transfer.sh publish-lake && batch/scripts/r2_transfer.sh push-market'
```

marketのstore-local tableだけの変更:

```bash
with_publication_lease batch/scripts/r2_transfer.sh push-market
```

指標だけの変更:

```bash
with_publication_lease batch/scripts/r2_transfer.sh push-indicators
```

直前に`pull.sh`した3 storeでlocal daily全体を実行した場合は、上の単独pushではなく同じbundleを反映する。

```bash
with_publication_lease bash -c 'batch/scripts/r2_transfer.sh publish-lake && batch/scripts/r2_transfer.sh push-machine'
```

単独pushはcloudの全行を包含したmerge、bundle pushはpull時点の全ETag一致とCAS uploadの成功を確認する。選んだpushが成功し、表示更新も必要な場合だけ実行する。

```bash
gh workflow run cloud-materialize.yml --ref main
```

対象runの完了を[監視手順](#長時間処理の監視)で確認する。merge・origin・schema・CASの失敗では公開へ進まず、[R2安全境界](#r2-transferの安全境界)に従って原因とcloud copyを確認する。local dailyの3 storeを個別pullや無条件uploadで組み替えない。

leaseがmalformedなら自動修復しない。GitHubの`cloud-publish` writerにrunning/pendingがなく、ローカル発行processも動いていないことを確認し、固定key`coordination/publication-lease.json`だけをローカルへ取得して原因を調べる。人間がactive writerなしと確認した場合に限り、そのexact key 1個だけをR2から削除する。次のacquireが`If-None-Match: *`で再作成する。通常運用でprefix削除や手書きJSON上書きはしない。

<a id="local-release-hydrate"></a>

### remote拡張を使わず固定releaseをhydrateする

Researchの正規readerに必要なのはhydrate済みmarket storeであり、選択銘柄のJSON exportや部分Parquet cacheで代用しない。httpfsを使えない運用環境では、対象releaseのmanifestとその列挙objectを認可済み読取で完全なローカルmirrorへ揃え、既存のローカルhydrateを使える。任意pathへの拡張配置だけでremote接続の準備完了とはしない。

事前にmanifestの全対象量と展開・作業snapshot用の空き容量を確認する。既存cacheはhash照合後に再利用し、pointerを取り直して対象releaseを黙って替えない。具体的なrelease IDとmanifest hashを読取結果から固定し、次を作業コピーに実行する。

```bash
uv run baibai-engine lake hydrate \
  --mirror <COMPLETE_LOCAL_MIRROR> --store <LOCAL_MARKET_COPY> \
  --release <RELEASE_ID> --manifest-sha256 <MANIFEST_SHA256>
```

この経路では`--bucket`を付けない。schema・hash・行数・世代の通常検証を保ち、不足object・破損・容量不足では停止する。部分成功を入力準備完了とせず、固定runと必要なindicator/application storeの時点・世代も確認する。hydrateは作業コピーの準備であって、R2正本・servingの更新ではない。

### application DB を反映する

**前提**: 空の作業領域へ`batch/scripts/r2_transfer.sh pull-app`でクラウド正本を取得し、取得したDBと`stores/.r2-generations/baibai.sqlite.etag`を一組として保持する（世代directoryを変更した場合も同じ組を使う）。取得済み作業コピーで判断・確認済み事実・運用状態を更新し、schema cutoverを含むcodeはmainへ入れる。
既存fileへのpullは未公開変更を守るため停止する。コピー更新が必要ならwriterを止め、`uv run baibai-engine db backup`でWAL込みのsnapshotを検証して退避した後、空の作業領域へ取得し直す。DBだけを別世代へ差し替えてsidecarを流用しない。

**実行**:

```bash
batch/scripts/publish.sh
```

backupは実行時のJST日付で選ばれる。日付を跨いだ場合も当日keyを確認し、古いdated backupの削除を行わない依頼では`R2_APP_BACKUP_PRUNE=skip batch/scripts/publish.sh`を使う。この指定は世代照合・backup作成・条件付きPUTを変えず、古いbackupの整理だけを省く。当日keyが既にあれば通常処理は置換するため、別の非公開保管先への保全とその後の置換が許可されている場合だけ、元ETagを固定した取得・内容hash・復旧先を確認して進む。保存場所そのものを変えない条件なら保全だけで置換してよいとは扱わず停止する。保全対象がDB全体なら、その内容と保存先・目的の承認も確認する。

引数なしでだけ実行する。`-h` / `--help`は外部書き込みを行わずusageを表示し、その他の引数はupload前に拒否する。
引数なしでは最初に`position ledger`を実行し、ledgerの整合性を確認する。保有価格の欠損・staleや権利単位の未確認は時価未評価として表示し、uploadを止めない。価格の読み取りは[`portfolio-ledger.md`](../docs/reference/portfolio-ledger.md#market-price-and-tax)を正本とし、配信のための台帳への価格転記は要求しない。

このscriptは取得ETagの一致を確認し、backupのsource条件とPUTの`If-Match`で競合を止めて`baibai.sqlite`のconsistent snapshotだけをstoresへ送り、`cloud-materialize`をdispatchする。servingへの直接writeは行わない。PUT応答のETagを次の更新基準へ保存する。未知世代ではuploadせず停止し、現在のHEADだけで取得世代を補わない。

**成功確認**: 作業コピーの正規保存、R2正本の更新、serving生成・公開、実際の表示を別々に確認する。正本を再取得し、対象のID/hash/本文と許可した差分、対象外の旧判断・ledger・予約等の不変を照合する。dispatchされた`cloud-materialize`のrun ID・headと公開stepの成功を確認し、表示のas-ofと対象judgmentのIDを読む。CLIの成功やActionsの起動だけを画面反映完了としない。
Screeningの候補は表示中のrun/候補に束縛され、保存済みInvestment Proposalは別の提案記録として読む。
公開済みProposalの反映は、そのimmutable IDの提案詳細と参照先Stock Assessment・Assessment Reviewで確認する。
後発runによる最新候補の変化だけで既存Operationの選定・固定入力を付け替えない。
後発分析からResearchを進める場合は、完了済みmachine bundleを取得してResearchの通常手順で扱う。

**停止と復旧**: 世代競合では未公開変更を退避し、最新正本を取得して必要な変更をapplication serviceで再適用する。自動mergeや無条件再uploadはしない。PUT成功後の世代記録・backup整理・dispatchの失敗は部分成功として確認し、未反映と決めつけて旧DBを再送しない。ledger preflightが失敗した場合はstoreをuploadせず停止する。exportはstoreのschemaがcodeと一致しない間、viewを1件も書かずexit 1で停止する。
schemaを一致させてから再実行する。application更新の表示には、そのsnapshot uploadとmaterializeの成功が必要である。cloud dailyによる独立した機械view更新とは区別する。

application DBはcurrent schemaだけを開き、materializeは取得済みコピーをread-onlyで読む。schemaを上げるPRは、
source versionを限定したtemporary one-shot toolとexact commandを用意し、main merge後のattended cutover・検証・
cloud反映が終わるまで保持する。完了証拠を残してから同じdeliveryのcleanupで削除する。
完了済みのResearch domain切替は[切替記録](#research-domain-cutover)を参照する。
対象外のsource versionでは手書きSQLや汎用移行へ進まず、schema変更issueで専用toolと検証を用意する。

application24→25は #1541、market47・経済source12のfresh L1切替は #1544/#1546 で受入済み。一回限りの準備・公開・machine切替toolと専用testは撤去した。当時の実装は[固定commit](https://github.com/koumatsumoto/baibai-loop/tree/c2a854bc6eb9f54941cdd2486ccb37bec1d65a78/tools/cutover)、fresh切替後の公開は[materialize成功](https://github.com/koumatsumoto/baibai-loop/actions/runs/39245024546)、その後の認証済み表示受入は[完了記録 #1548](https://github.com/koumatsumoto/baibai-loop/issues/1548)を参照する。旧sourceや旧pointerを現行正本へ再適用しない。これは今回のIFRS意味revision再計算を完了済みとする記録ではなく、現行の[意味再抽出手順](#edinet-metrics-revision)を別に行う。

<a id="research-domain-cutover"></a>

### Research domain の切替記録

application22→24・run5→6は2026-10-15に切替を完了した。[統合PR #1539](https://github.com/koumatsumoto/baibai-loop/pull/1539)、
[materialize](https://github.com/koumatsumoto/baibai-loop/actions/runs/38660989351)、
[Web配信](https://github.com/koumatsumoto/baibai-loop/actions/runs/38661977456)を確認し、定期writerを再開済み。
原本・バックアップを保持し、台帳の再生・保持データ・参照の一致と公開読戻しを確認した。私的な移行証拠は公開repositoryへ保存しない。

Research domain移行専用toolと対応する旧schema fixtureは撤去した。切替時の実装・手順は[検証済みcommit](https://github.com/koumatsumoto/baibai-loop/tree/5ac69d04c1fb99cbdc80e3f43673e18068971307/tools/cutover)を参照する。
旧データの調査が必要でも、現行runtimeで移行を再実行したり、旧DBを現行正本へ上書きしたりしない。
退避材料は新しいResearchの参考資料であり、新Assessment・Review・Proposalとして自動公開しない。通常の開始・再開はResearch skillに従う。

### application DB を復元する

application DBは判断とledgerを保持するため再生成しない。通常のコピー更新は前節の`pull-app`を使う。この節は、明示的に選んだ過去snapshotへクラウド正本を戻す復旧である。
全writerを停止し、未公開の作業コピーをconsistent snapshotで退避する。次に空の作業領域へ`pull-app`し、復旧で置き換える現在のクラウド世代とDBを確保してから以下を同じshell sessionで実行する。現在の正本が取得・検査できなければ通常更新経路での復旧を止める。key消失は[初回seed](#storeをseedする)の既存key確認を含む別の復旧判断に戻し、無条件PUTへ迂回しない。
復旧候補はR2の日別世代、または出所・時点を照合できるローカルcheckpointから選ぶ。ローカル候補を自動的に正本より新しいとみなさない。

**1. 復元前backup。** 取得した現在の正本のWAL込みconsistent snapshotを作る。backup失敗なら復旧へ進まない。

```bash
restore_backup=
if test -f stores/application/baibai.sqlite; then
  restore_backup="$(
    set -o pipefail
    uv run baibai-engine db backup | \
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
  uv run baibai-engine position ledger
)
```

ledgerを復元対象の時点と照合し、不一致なら手順5へ進む。照合成功後は[application DB反映](#application-db-を反映する)へ進み、反映確認後に判断業務を再開する。公開途中の失敗では検証済みlocalを戻さず、upload・dispatch・materializeの到達点を確認して反映側を復旧する。ledgerを`head`へpipeしない。

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
  uv run baibai-engine position ledger
)
```

途中停止では残存pathと失敗commandを確認し、blockを無条件反復しない。

### 履歴を深くする

#### 指標履歴

source追加後やrolling窓を超える取得断を補う場合に使う。L2系列IDではなくCollection source IDを指定する。source一覧と利用可能範囲を確認し、取得後は必要なL2系列・Readingを別途読む。

```bash
(
  set -e
  uv run baibai-engine collection economic list
  read -r -p '対象日 YYYY-MM-DD: ' asof
  read -r -a source_ids -p '対象source IDを空白区切りで入力: '
  test "${#source_ids[@]}" -gt 0
  uv run baibai-engine collection economic refresh "${source_ids[@]}" --all-history --end "$asof"
  uv run baibai-engine market-context reading --asof "$asof"
)
```

取得成功は原本受領・保存の結果であり、すべてのL2値を計算できた意味ではない。数値・履歴不足を確認し、codeをmainへ反映してから[指標のcloud更新](#ローカルからクラウドを更新する)へ進む。失敗時は完了した原本と未完範囲を確認し、日次batchに全履歴取得を代行させない。

#### 指標観測とregistryの修正

原本のrevisionとL2読取は[Market Context reference](../docs/reference/market-context.md#①-データindicator-series-を引く)に従う。scalar観測の撤回CLI、registry世代台帳、系列退役に伴う原本pruneは持たない。

原本が誤っている場合は、sourceの訂正取得か明示的なstore再構築を行う。localだけのDELETEをcloudとのmergeによる訂正手段にはしない。source取得失敗・原本の欠測・L2計算エラーを区別して原因を確認する。

系列の追加・退役・改名では、`market_context/indicators/registry/`と必要な計算・読取を変更する。外部取得条件を変える場合だけCollection catalog/providerも変更する。既存原本から再計算できる変更に再取得を要求せず、式・単位・欠測・公開結果をテストと保存済み読取で確認する。L2系列の退役だけでsource factsや過去の発行済みContextを削除しない。判断履歴の退役系列warningは確認する。

原本storeの構造・全revisionと、判断履歴は`uv run baibai-batch validate-market-context-stores`で別々に検査する。この検査はL2の値域・数式の正しさを証明しない。原本を更新した場合は読取確認後に`push-indicators`へ進み、式だけの変更なら原本storeのpushは不要である。

optionsの3系列も同じ方針で原本からL2再計算する。取得・再開の契約は[optionsの取得と復旧](../docs/reference/market-context.md#jquants-options-provider)を参照する。

**schema切替と原本の再構築**: 通常のopen・pull・mergeに旧schema互換を追加しない。schema変更時は実行中writerがいない切替窓を確保し、現行codeで別のcandidate storeを準備する。codeとstoreの世代を合わせ、条件付き書込みとbundle receiptの整合を確認して切り替える。

[#1543](https://github.com/koumatsumoto/baibai-loop/issues/1543)の一回限りの全L1再取得・公開は完了している。新たな全面再取得が必要になった場合は、対象sourceと取得可能範囲を改めて確定する。通常の系列追加・式変更のたびに全取得を要求するものではない。既存L1や旧scalarを新規取得原本の代替として混ぜず、取得不能・未確認の範囲は明示する。原本保存、L2再計算、公開読戻しを確認するまで再構築完了とはしない。投資部門別の取得範囲は公表日であり、L2の集計期間と取り違えない。TOPIXの旧終値からO/H/Lを推定しない。

**停止と復旧**: DB拒否・意図しない退役・provider取得失敗・merge/CAS失敗では後続公開へ進まない。取得失敗が既にcommitした原本を取り消すとは扱わない。誤った内容をcloudへ反映した場合は次のpushを止め、[R2 transferの安全境界](#r2-transferの安全境界)の前世代復元へ進む。部分pushによるreceipt不一致だけは[部分pushからの復旧](#部分-push-からの復旧)で扱う。

#### Market履歴

取得成功後、[ローカルからクラウドを更新する](#ローカルからクラウドを更新する)のlake変更またはstore-local変更の片方を実行する。

**成功確認**: `publish-lake` / `push-market`のno-loss検査を確認し、cloud copyをpullした後、`source_coverage`の`ok`窓が指定範囲を連続して覆うことを照合する。

```bash
(
  set -e
  read -r -p '確認する開始日 YYYY-MM-DD: ' history_start
  read -r -p '確認する終了日 YYYY-MM-DD: ' history_end
  batch/scripts/pull.sh
  sqlite3 -header stores/market/market.sqlite \
    "SELECT source, coverage_start, coverage_end, record_count, status
     FROM source_coverage
     WHERE NOT (coverage_end < '${history_start}' OR coverage_start > '${history_end}')
     ORDER BY source, coverage_start, coverage_end;"
)
```

### merge が検査するもの

`push-market` / `push-indicators`は原本identityの衝突やremote世代の競合があれば停止する。cloudとlocalのどちらかを推測で正本にして上書きしない。CAS / generation conflictでは同じ世代をblind retryせず、最新cloud stateからやり直す。local変更後にhydrateして成果を上書きしない。machine bundle途中pushは[部分 push からの復旧](#部分-push-からの復旧)へ進む。内部契約は`baibai_batch.storage.merge_market_store` / `baibai_batch.storage.merge_economic_store`と対応testsを正本とする。

### 手動で lake を publish する

**前提**: schema cutoverなど、定時batchを待たずにローカルstoreからpublishする必要があること、worktreeが
cleanであること、storeの`lake_store_origin`が開始時current pointerと一致することを確認する。

**実行**:

経路は日次と同じ1つで、毎回全partitionを導出し、serving releaseからorigin束縛と履歴の床だけを読む。
[ローカル発行手順](#ローカルからクラウドを更新する)の`with_publication_lease`を同じshellで定義してから実行する。

```bash
(
  set -e
  with_publication_lease batch/scripts/r2_transfer.sh publish-lake
  uv run baibai-engine lake resolve --mirror stores --bucket baibai-stores --format json
)
```

**成功確認**: `resolve`のrelease identityがpublish結果と一致することを確認する。

**停止と復旧**: originが違えばdataset export前に停止する。SQLiteだけを古いbackupへ戻したstoreでは
実行せず、現行releaseからhydrateし直す。

### 部分 push からの復旧

`push-machine`は3 keyを逐次に条件付きPUTし、全て終わってから`machine-manifest.json`（bundle receipt）を
書く。2本目以降が失敗すると、receiptは旧世代のまま残り、bucketは「先行keyだけ新世代」というどのbatchも
書いていない組合せを持つ。この状態は`pull-machine`のreceipt突合が拒否する（安全側——実在しない
cross-sectionをscreeningへ渡さない）。

**停止条件**: 同じコマンドや日次batchを再実行しない。条件付きPUTは失敗したrunがpullした世代に束ねられ、
成功済みkeyのETagは既に変わっている。再実行は`push_pulled_keys`の事前照合で
`changed on R2 after the pull`となり、日次batchもpull stepで止まる。

#### 手順 (i): receiptを再生成する（既定）

**前提**: 失敗runのlogで部分pushとreceipt不一致を確認し、削除対象がbucket直下のexact key
`machine-manifest.json` 1件であることを確認する。このkeyはmutableで、Bucket Lockの対象prefix
（`lake/l1/source/` / `lake/l2/processed/` / `lake/manifests/`）の外にある。

**実行**:

```bash
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  aws s3api delete-object --bucket baibai-stores --key machine-manifest.json \
    --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
)
```

**成功確認**: 次の`pull-machine`が
`no machine bundle receipt … taking the store set unverified`と警告しつつ現行世代を記録し、次の日次batchが
3 keyのpush後に新しいreceiptを書く。以後の`pull-machine`でreceipt一致を確認する。

**危険と復旧**: prefix削除はしない。receiptが無い1周だけcross-sectionの保証が落ち、削除直後に戻す
操作はない。新しい完全pushがreceiptを再生成するまでを一つの復旧作業とする。完全pushが失敗したら、
追加削除や再dispatchをせず、そのrunの失敗をローカルで再現する。

#### 手順 (ii): ローカル成果のpushで復旧する

**前提**: publishすべきローカル変更があり、[ローカルからクラウドを更新する](#ローカルからクラウドを更新する)
のno-loss条件を満たす。`push-market`には、現行releaseからhydrateした後に変更したstoreを使う。変更後に
hydrateして成果を上書きしない。

**実行と成功確認**: `push-market`または`push-indicators`を実行する。mergeとCAS pushの成功後、3 keyのHEADから
receiptが現在世代へ書き直される。次の`pull-machine`でreceipt一致を確認する。

**停止条件**: `push-market` / `push-indicators`はruns storeを変更しない。local dailyのruns成果を反映する場合だけ、
直前のbundle pullからremote generationが変わっていないことを`push-machine`に検査させ、個別uploadは行わない。

### 日次 workflow を手動実行する

**前提**: 取得・再計算・公開が必要な対象を確認して実行する。原因調査だけのために繰り返しdispatchしない。code修正が必要な場合は最新HEADのCI成功とmainへの反映を確認する。`asof`省略時は当日JSTを
market calendarで判定し、非営業日はScreeningだけをskipして指標更新と単一公開を進める。過去日を指定するとScreeningの営業日gateをskipする。

**実行**:

当日実行または過去日実行の片方だけを選ぶ。

```bash
gh workflow run cloud-daily-batch.yml --ref main
```

```bash
read -r -p '対象日 YYYY-MM-DD: ' asof &&
  gh workflow run cloud-daily-batch.yml --ref main -f asof="$asof"
```

dispatch成功後に対象runを確認し、[監視手順](#長時間処理の監視)へ進む。

```bash
gh run list --workflow cloud-daily-batch.yml --limit 10
```

**成功確認**: 対象runの終了通知を受けてconclusionを確認し、Discord通知、store push、serving freshnessを照合する。
対象run IDを固定して[監視手順](#長時間処理の監視)に従う。

**停止と復旧**: exit 1は安全な全viewsを作れないためuploadせず原因を直す。Screening入力不足・取得/計算失敗は指標更新と全views export後のexit 3で示す。exit 3は後続の公開結果を確認し、公開成功なら失敗した対象だけを復旧する。upload失敗・部分pushは対応する復旧節に従い、全工程をblindに再dispatchしない。

scheduled runはcron日を対象に営業日gateを適用する。queue遅延でJST日付が変わっても対象を翌日へ進めない。Screening coverage不足はそのrunを発行せず、指標更新と公開可能な全viewsへ進む。欠測はwatchdogで確認し、修正後の手動dispatchは現行mainから行う。

非営業日は旧Screeningの値・ID・実計算時刻・historyを保持して指標を更新する。exit 3では後続公開の成否を別に確認する。

## Password rotation

**前提**: 新しい32文字CSPRNG値をpassword manager等で生成する。実値をGit、issue、log、引数、shell historyへ
書かない。

**実行**:

```bash
(cd web/edge && npx wrangler secret put VIEW_PASSWORD)
```

**成功確認**: promptへ新しい値を入力し、旧値が401、新値が認証成功になることを確認する。次の401でbrowserの
旧値はlocalStorageから削除され、password入力画面へ戻る。

**停止と復旧**: 値を表示・引数化しない。失敗時はR2やapplication dataを変更せず、secret設定をやり直す。
Workerの再deployは不要である。

## R2 transferの安全境界

- upload前にPython `sqlite3.backup`でsnapshotを作り、WAL未checkpoint行を含めて`quick_check`する。
- `push-machine`は各storeのsnapshotを作成・検査してから、そのstoreを順にuploadする。3 storeは原子的には反映されず、途中失敗は下記の部分push復旧で扱う。3 store一括の`push-machine`はGitHub Actionsと明示的なlocal dailyで使い、pull時の全ETag一致と各PUTの`If-Match`を必須にする。`indicators.sqlite` / `market.sqlite`を単独でローカルから進める場合は`push-indicators` / `push-market`でcloud copyをmergeし、cloud側の行の取り残しを検出したら停止する。
- machine storeのpushは対象の前世代を`<key>.bak`に1世代保存する。復元時はwriterを停止し、対象bucketとexact `<key>.bak`・`<key>`を人間が確認したうえで、`aws s3api copy-object --cli-read-timeout 300`を使う。`aws s3 cp`へ置換しない。
- machine store の`.bak`は1世代のみで次のpushで置き換わるため、24時間保持される保証ではない。`baibai.sqlite`だけは`baibai.sqlite.bak-YYYYMMDD`（JST）で日ごとに1世代を残し、直近14世代を超えた分をpush成功後に削除する。machine storeにも再取得できない観測があるため前世代を残し、判断と確認済み事実を持つapplication storeは日別世代を残す。prune は`baibai.sqlite.bak-`配下をlistし、`baibai.sqlite.bak-YYYYMMDD`に一致するkeyだけを完全一致で削除する（prefix削除はしない）。
- 初回seedは既存のstore keyを1件でも検出したら停止し、再seedによるクラウド正本の上書きを許可しない。
- pullは固定4 key以外を受け付けず、全downloadと`quick_check`完了後に置換する。
- application store (`baibai.sqlite`) のpullは`pull-app`だけが行い、bulk pullは触らない。未公開の変更を失わないよう、`pull-app`はローカルにfileがあれば止める。CIはcheckout直後で`stores/application/`が空なので素通りする。作業コピーの更新・競合・復旧は[application反映](#application-db-を反映する)に従う。
- servingの`views/`は`aws s3 sync --delete`で完全像に合わせる。historyは追記だけで削除しない。
- `views/meta.json`は他のviewとhistoryが全て成功した後に最後にuploadする。
- bucket名は`R2_STORES_BUCKET` / `R2_SERVING_BUCKET`で明示的にoverrideできるが、通常は固定defaultを使う。

machine `.bak`を手動復元する場合は、対応する発行を停止して対象keyを確認してから実行する。

```bash
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  read -r -p '復元するexact machine key: ' store_key
  case "$store_key" in market.sqlite|runs.sqlite|indicators.sqlite) ;; *) exit 1 ;; esac
  aws s3api copy-object --bucket baibai-stores --key "$store_key" \
    --copy-source "baibai-stores/${store_key}.bak" --cli-read-timeout 300 \
    --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
)
```

復元後はfreshにpullし、receipt / generationの整合と`quick_check`を確認する。不一致なら分析と公開を止め、[部分 push からの復旧](#部分-push-からの復旧)へ進む。

### schema cutoverを修正するとき

**停止条件**: store全体を古いsnapshotへ戻さない。storeは毎営業日伸びるため、過去schemaのcopyへの交換は
それ以降の事実を失う。

**実行**: application / market / run / indicator storeはruntime migrationを持たない。schema変更PRが保持しているtemporary
one-shot toolを修正し、cutover前のlocal copyへ再実行してcurrent schemaの別fileを作る。toolがcleanup済みなら、
過去commandを推測せず、修正issueでexact source version・変換・検証を固定したtemporary toolを再構築する。
通常のtransferではdownloadしたstoreのschemaを変更しない。indicatorも現行schema同士をmergeする。

**成功確認と復旧**: sourceと出力のintegrity・foreign key・必須table・保持対象row/headを照合してからmainへ入れ、
同じ作業でstoreを反映する。直前のpush自体が壊れた場合だけ、上の「R2 transferの安全境界」に従って`<key>.bak`の1世代を使う。

### #1520 財務文書の切替履歴

market schema31→32 / financial-v1→v2 は切替済みであり、通常運用で再実行しない。
2026-10-15 のドメイン切替開始時にも本番 market は schema32 であることを確認している。
過去の手順は[切替時の運用文書](https://github.com/koumatsumoto/baibai-loop/blob/d416cbd5d21207427a96068b196356e021e15155/batch/OPERATIONS.md#1520-schema31--financial-v1-の一回限り切替)を履歴として参照する。
旧 source31 や過去の stage を現在の store へ再投入しない。今後の修正は上の「schema cutoverを修正するとき」に従う。

schema の切替確認と #1520 全体の受入完了は別である。財務文書の内容・評価入力の検証は個別に確認する。
旧 one-shot tool は撤去済み。通常の R2 部分 push 復旧の回帰検証は現行 schema のテストで継続する。

## Read modelの生成 — baibai_web.materialize

materializeはapplication / machine storeをread-onlyで読み、servingの完全像を作る。schema / storeがcodeと整合しなければpublishせず停止する。`views/`はcurrent image、`history/candidate-views/`は保持期限付き履歴、`views/meta.json`はfreshnessで最後に発行する。routeとJSON名の対応は[Web](../web/README.md)と`web/contracts/routes.json`、Web testsを正本とする。

手動生成:

```bash
uv run python -m baibai_web.materialize --output-dir <dir> [--batch daily|manual] [--repo-root <path>]
```

失敗時はstore schemaとhydrate状態を確認し、生成物を公開しない。

## 日次機械工程 — baibai-batch daily

GitHubの日次jobは開始時にprocess内の取得期限を設定し、90分のjob上限のうち後半55分を生成・公開・lease解放用に残す。取得期限はsetup/hydrateを含む開始35分後で、2026-10-20の実runでは取得後の生成・公開に約48分かかったことを根拠とする。J-Quants、EDINET、JPXと対応する経済HTTPのread/retryは残時間を確認し、Retry-Afterが収まらなければ短縮せず終了する。初回＋最大5retryを単一GETのtransportで所有し、EDINETのHTTP/body 429も同じ予算を使う。成功済みL1は既存保存経路に残す。期限・retry回数を次runのDB/R2へ持ち越さず、公開・通知・認証にはこのretryを適用しない。これは公開完了時間の保証や全source共通のrate設定ではない。

Screeningのcalendar判定、独立した市場/財務/EDINET/JPX取得、必要入力のcoverage検証、全Stock Analysisと候補文脈のrunの一つのtransaction、独立した指標refresh、全viewsの単一exportへ進む。各取得は既存provider/cacheを再利用し、後半HTTP失敗でも検証してcommitしたL1 chunkを保持する。個別値の欠測を取得済み全観測の無効化へ置き換えず、source復元や全DB組戻しは行わない。Screeningの失敗は指標更新を止めない。

以下は用途別の選択肢であり、続けて全件を実行しない。`.cache/daily-serving`はlocalの生成先である。

通常のlocal実行:

```bash
uv run python -m baibai_batch.jobs.daily --output-dir .cache/daily-serving
```

明示した対象日の再実行:

```bash
read -r -p '対象日 YYYY-MM-DD: ' asof &&
  uv run python -m baibai_batch.jobs.daily --asof "$asof" --output-dir .cache/daily-serving
```

同じ工程のmachine resultとmanifestを取得する場合:

```bash
read -r -p '対象日 YYYY-MM-DD: ' asof &&
  uv run baibai-batch daily --asof "$asof" --output-dir .cache/daily-serving \
    --format json --quiet --manifest-out .cache/daily-manifest.json
```

終了コード:

| exit | ローカル処理の結果 |
| --- | --- |
| 0 | 全views export完了。非営業日はScreeningだけskipし、指標更新・exportを行う |
| 1 | store検査/export等の致命的失敗。今回の結果を公開へ進めない |
| 3 | 全views export完了だが、Screening取得/計算、指標refresh、prune等で失敗。新Screeningの有無はrun_revision_idとscreening_statusで確認 |

クラウド公開はこの後に既存market release、machine store、serving views、history/freshnessの順で一度進む。読み取り専用connectionの`PRAGMA data_version`でmarket storeのcommit変化を確認し、正常に読めるnoticeの明示的`market_updated=false`の時だけ既存market releaseを保持する。比較用connectionや変更前後の読み取りに失敗した場合は`market_updated=null`（不明）とし、未変更とは扱わない。noticeの欠落・読取不能・不正JSON・field不正は詳細を含まないwarningで示し、既存market公開経路へ進む。これは値のbyte比較ではなく、commit済みstore変更の検出である。Screening未更新なら`--skip-history`でhistoryを生成せず、workflowの新しい出力先にはhistoryを含めないため再uploadしない。run pruneも省き、旧値・ID・実計算時刻を保持する。local exportの完了を示すoutput名`export_ready`を、R2公開成功と読み替えない。workflowが失敗しても一部反映が済んでいる場合がある。

機械結果の`screening_status`は`published / failed / skipped_non_business_day`を区別する。Screeningが未更新でも指標・全viewsを更新し、画面生成時刻を旧Screeningの実計算時刻へ上書きしない。store schema/hydration検査と全views exportの失敗はfatalである。詳細なstepとoptionはpublic `--help`と実run logを確認する。

## 日次結果のDiscord通知

`cloud-daily-batch` は run ごとに終端結果を Discord チャンネル `#batch-runs` へ1件通知する。run単位の通知は、watchdog、workflow履歴、serving freshnessと併せて読む。チャンネルは
code が選ばず repository secret `DISCORD_WEBHOOK_URL` が指す webhook で固定する。workflow 末尾の
単一 step（`if: always()`）が、cancel を含むあらゆる終端状態で1回だけ行う。

message は 4 要素からなる。

1. 見出し行 — label・as-of・失敗した step 名（あれば）。`[FAILED] as-of 2026-08-26 — failed step: hydrate`
2. `executor: GitHub Actions|Local` — 実行環境から判定した固定2値。見出しの直後に必ず1行表示する
3. `🆕 新規 候補入り:` / `👋 候補退出:` の 2 行 — それぞれ銘柄コード順・最大5件（超過時は全件数・表示件数・他の件数を明示）・`<ticker> <社名> E[r]±X.X%`。急落当日の候補と、候補一覧から落ちた銘柄を通知だけで拾えるようにするための行である。**新Screeningを発行した run では常に出す** — 0 件の日は `なし`、delta view が読めない日は `計測なし（<理由>）` と書く。行が無いことは「0 件」「計測不能」「通知経路の異常」の3つを同時に意味してしまい、読み手が区別できない。Screening未更新日は旧差分を再通知せず、`Screening更新なし（非営業日/失敗）; 保存済み結果を保持`を出す
4. `run:` — GitHub Actions の run URL。所要時間・step ごとの結果・lake release・error の本文はこの run log にある

`cloud-daily-batch`の終端通知labelは次の4種。

| label | 意味 |
| --- | --- |
| `[OK]` | batch exit 0、upload および lease release まで成功 |
| `[DEGRADED]` | batch exit 3。全views export済みで、見出しに最初の失敗 stepを表示。Screening未更新の場合は保存済み結果の保持を明示 |
| `[FAILED]` | batch の致命的失敗（見出しに batch 内の stage 名）、または lease 解放を含む batch 以外の step の失敗（見出しに step 名） |
| `[CANCELLED]` | job が中断された（`timeout-minutes` 超過・手動 cancel） |

**通知の配送失敗は job を赤にしない**（`continue-on-error: true`）。workflowの失敗は公開の完全完了を示さず、途中までの反映がある場合は各stepの結果を確認する。配送失敗は
notify step の stderr に sanitized な理由（未設定・HTTPS 以外・Discord 以外の host・timeout・HTTP
status）で残り、webhook URL・response body は log に出ない。`#batch-runs` が静かな日は
`gh run list --workflow cloud-daily-batch.yml` で run 自体の有無と結論を見る。

### 欠測の検知（`cloud-batch-watchdog`）

dailyの対象日について成功runがないときだけ`[MISSING]`を通知する。成功対象日は該当attemptの終端stepに残したnoticeの`asof`を読み、run作成日や予定日から推定しない。判定窓は最新attemptの更新時刻を使う。対象runがin-flightなら通知せず、結果の対象日を確認できない場合やAPI読取失敗はwatchdog自身の失敗として止める。両workflowの実run状態を確認し、過去日を確認する場合:

```bash
gh workflow run cloud-batch-watchdog.yml -f check_date=YYYY-MM-DD
```

### webhook rotation

`DISCORD_WEBHOOK_URL` は GitHub Actions の repository secret で、通知 step だけが読む（job env ・
CLI 引数・log には出ない）。secret の実値を Git・issue・log へ書かない。

1. Discord で `#batch-runs` の webhook を作り直す（または既存 webhook の token を再生成する）。
2. `gh secret set DISCORD_WEBHOOK_URL --repo <owner>/<repo>` で新しい URL を登録する。
3. 次の通常runで通知を確認する。即時確認が明示的に必要な場合だけ通知経路の受入を行い、rotationだけで過去日のscreeningを再生成しない。

旧 webhook は Discord 側で削除するまで有効。

<a id="edinet-metrics-revision"></a>

## EDINET metricsの抽出意味revision更新

**対象**: [抽出意味](../docs/architecture.md#edinet-metric-revision)を変更し、`EDINET_METRIC_REVISION`を明示更新する場合。配置・import・transport retryだけの変更では再抽出や保存行のrevision書換えを行わない。Research factsのrevisionとは別であり、過去snapshot全体を書き換えない。

1. 新しい意味revisionと変更の期待差分をレビューし、現行Lakeをhydrateした隔離DBコピーで保存済みCSV ZIPから再抽出する。明示したSQLite source archiveを再利用し、原典不足時だけ対象を限定して取得する。exit 0、保存全列・quality/failure理由・coverage/count・integrityと期待差分を確認してから本番へ進む。
2. daily・TradingView・materialize・外部評価publisherと他operatorが競合せず、次のslot前にreadbackまで完了できる時間帯で既存operator leaseを取得する。busyなら切替へ進まない。
3. 同じlease内で確認済みcodeをmainへ反映し、freshな`pull-machine`と`hydrate-market`から最新as-ofを再確認して比較元をbackupする。準備コピーをそのまま本番へ戻さない。
4. 保存済み文書と原CSV ZIPが全件あるなら、そのas-ofを指定してofflineの`baibai-engine screening extract-edinet-metrics --asof YYYY-MM-DD`を実行する。不足する原本の取得が必要なら`baibai-engine screening refresh-edinet-metrics --asof YYYY-MM-DD`を使う。refreshは文書一覧を明示取得し、隔離・再利用を除く必要な原本だけを取得してから同じ計画で抽出する。exit 0、新意味revision、全列・quality/failure・coverage/count・integrityを確認する。再抽出後にhydrateで成果を上書きしない。
5. 同じlease内で`publish-lake`→`push-market`を実行し、発行したrelease ID/hashを固定してfreshな読戻しを行う。保存全列と意味revision、store-local coverage、integrityの一致を確認してleaseをreleaseする。

**停止・復旧**: 取得失敗、期待外の差分、generation競合、lease失効では後続公開へ進まず、対象を限定して直す。日次batchに意味移行を代行させず、旧値へ新revisionを一括設定して再抽出済みと扱わない。code・保存値・readbackの整合が揃うまで切替完了としない。

<a id="edinet-source-archives"></a>

## EDINET原本の共有保存

`edinet.source_archives`はtype=5 CSV ZIPとtype=1 XBRL ZIPの取得bytesを、`doc_id`・`source_type`別にmarket storeへ保持する。通常のlake公開・hydrate・dehydrateに含まれる。`stored_on`はこのstoreに最初に保持したUTC日付で、開示日・APIで利用可能になった日・元cacheの取得日の代用ではない。ZIPの内容をJSONへ再構成せず、元のencoding・空欄・単位・未選択の項目を保つ。

原本を読む順序は明示したmarket SQLiteの`edinet_source_archives`、必要なAPI取得である。以前の端末のlocal ZIP cacheは暗黙に取り込まない。CSVとXBRLのtransport・保存経路は同じproviderを使う。取得エラーや不開示通知PDFで保存済みZIPを上書きしない。`doc_id + source_type`はarchiveの論理keyであり、同じ書類・取得種別でZIP bytesが永遠に同じと保証するものではない。原本の公開世代・保持は既存lakeの不変objectとretentionに従い、別の版管理機構を追加しない。

通常のschema移行・意味再計算では、明示して保持した原本を再利用できる。2026-10-19の全面fresh baselineは旧L1/L2を混ぜず取得・公開・読戻しを完了した。固定releaseは`20261019T100002Z-54cf954c-d37a5ef3028d`。当時取得できなかった12 datasetの旧公開期間は旧releaseを参照用に保持し、新baselineでは欠測として記録した。範囲と受入証拠は[Issue #1543](https://github.com/koumatsumoto/baibai-loop/issues/1543)と当時の[固定切替実装](https://github.com/koumatsumoto/baibai-loop/blob/c2a854bc6eb9f54941cdd2486ccb37bec1d65a78/tools/cutover/publish_fresh_market_v47.py)を参照する。

この履歴移行の例外を通常発行や別datasetへ持ち越さない。通常の`publish-lake`はrequired dataset、coverage、Parquet/SQLite整合を検査する。新たな再取得・保存契約変更が必要な場合は対象と取得不能範囲を明示し、broker factや判断履歴を黙って初期化しない。完了済みの切替用commandを通常運用として再実行しない。

EDINET文書一覧のHTTP body bytesはJSON解析より前にL1の`edinet.list_responses`（SQLite `edinet_list_responses`）へ受領ごとに保持する。`edinet_document_lists.response_json`は選択した成功応答のnative payloadを記録するL2 projectionであり、不変のraw receipt履歴とは別である。最新応答のprojectionが失敗した日はcurrent coverageをfailedとし、過去の成功projectionを新しい原本受領の証拠や成功coverageに読み替えない。解析できない原応答もL1に残し、以前の正規化済み行から原本を捏造しない。

受入ではCSV/XBRLのbytes一致、別環境の空の作業コピーへのhydrate後の再読、元のcontext/単位/欠測を保った再抽出、lake公開・hydrateの往復、保存量・読込時間を確認する。archive列はPython側のbatchを一行にしてParquetへ書き出し・照合・SQLiteへ戻し、ZIP内容のmin/max統計は作らない。通常のスカラー観測は既存のrow batchを使う。全原本の取得完了や全過去期間の再計算可能性を、行が存在するだけで保証しない。

<a id="edinet-research-facts"></a>

## EDINET Research factの初期化・日次差分

**前提**: current codeと整合するmarket store、既存EDINET document inventory、必要な取得のための`EDINET_API_KEY`を使う。再利用入力は明示したmarket SQLiteの`edinet_source_archives`である。

未初期化storeだけ、ローカルstaging storeを明示して初期抽出する。初期化済みproductionへ毎回実行しない。

```bash
uv run baibai-engine screening refresh-edinet-facts \
  --asof YYYY-MM-DD --initialize --sqlite-path <STAGED_MARKET_SQLITE>
```

成功時は`source_coverage`の`edinet_research_facts / initialized`を記録する。失敗時は同じcommandで再開し、okの書類は再取得・再抽出しない。保存済み原本だけを再計算する場合は`extract-edinet-facts`を使う。`--ticker`を繰り返した限定評価は全体のinitialized markerを作らない。有限parserの未対応は理由付きok、取得・ファイル破損はfailedに分ける。未知の形式を成功率だけのために補完しない。

**成功確認**: summaryのselected/reused/extracted/failedとmissing reasons、代表書類の原典照合、固定releaseのexport/query/hydrateを確認する。現行codeのmain反映、整合するL1発行、store-local coverageのクラウド反映を一組で行う。codeだけを先に日次運用へ入れない。

日次batchはScreening工程の成否と独立して`refresh-edinet-facts --asof`を実行する。初期化前は明示的skip、初期化後は収録済みfamilyと新規年次・訂正を処理する。取得失敗は既存screening発行を止めず、他の後段処理を終えた後のexit codeへ反映する。raw cacheを残してretryできる。抽出契約が変わった場合はResearch fact専用revisionを更新し、既存EDINET metricsのbaselineを無効化しない。

storeのmergeでは同日でも別docIDの成否を混同しない。共有docIDはtargetのfactに対応する抽出claimを保持する。source側にしかないdocIDはtargetの抽出revisionを証明できないため、未確認のretry対象として取り込む。initialized markerは引き継げる。

**失敗時**: failedのdocIDを原典と照合し、認証・rate limit・破損cacheを原因別に解消して再実行する。factが残っていても、最新訂正の失敗を旧版で埋めない。利用側は[書類選択と欠測時の手順](../docs/reference/market-lake.md#edinet-research-query)に従う。

<!-- 歴史reportからの引用anchorを保持する。runtimeの旧名aliasではない。 -->
<a id="external-assessment-acquisition"></a>

## 公開broker観測の限定取得

手動の限定入口は`baibai-engine external-assessment`または`baibai-batch external-assessment`である。取得は公開ページの通常HTTP経路を使い、明示した少数銘柄を直列で読み、前HTTP完了から次開始まで最低2秒を空ける。待機もrequest数・wall-clockの上限に含める。403・429は当該sourceの残りを止め、別sourceの確定した成功は保持する。スケジュール、publication lease、R2更新、UI反映はこの入口に含まれない。

取得先はprivateなローカル原応答captureとし、ログへ値やHTMLを出さない。既存の出力先は通信前に拒否し、保存時の競合も上書きしない。部分成功をimportする前に再取得する場合も必ず別名を選ぶ。例の保存先directoryと現行schemaの隔離market作業コピーをあらかじめ用意し、repositoryや公開Actions artifactへcaptureを置かない。

```bash
uv run baibai-engine external-assessment collect --output /tmp/private-external-assessment/responses.json
uv run baibai-engine external-assessment collect --source kabumap --tickers 130A 8306 --output /tmp/private-external-assessment/quants-responses.json
uv run baibai-engine external-assessment collect --source minkabu --tickers 7203 4436 --output /tmp/private-external-assessment/minkabu-responses.json
uv run baibai-engine external-assessment import --input /tmp/private-external-assessment/responses.json --db /tmp/private-external-assessment/market.sqlite
```

import対象は明示した既存の隔離作業コピーであり、current market schemaに整合させる。CLIは新規DBの初期化も旧schemaの移行も行わない。新形式`equity-web-source-capture-v1`はHTTP 200で受領した原応答の正確なID・時刻・encoding・body bytesと、正常観測に選ばれたattempt IDだけを保持する。以前の正規化観測stageはimport入力として拒否し、旧ファイルは変更しない。importは全原応答を保持した後、選択attemptを保存原本から再解析してL2へ不可変追記する。選択attemptの解析失敗時、原応答は残り得るがL2は部分追記しない。成功時は原応答と観測の新規挿入件数を確認し、再投入では両方0を確認する。partialまたは失敗時はsafe summaryで未試行・HTTP障害・parse障害を正常nullと区別し、captureを保持する。captureはtransportや制御結果の再生を主張しない。schema・破損・保存障害は取得欠測へ置き換えず、コピーの整合を直してから再投入する。

本番導入は下記のcode/store切替を一組にする。後続の独立workflowでは取得をlease外で終え、公開段階で最新クラウド世代をpull/hydrateしてから確定stageを適用する。公開権限、private handoff、料金・所要時間の実測は定期実行前に別途受入する。保持・照会は[market lake](../docs/reference/market-lake.md#external-assessment-query)を参照する。

<a id="market-v48-cutover"></a>

### みんかぶ導入時の market 47 → 48 切替

みんかぶの原本source制約と新しいL2表を追加するため、codeだけの反映では既存schema 47を読めない。
本番切替の前に、固定masterの対象全銘柄について取得・対象外・失敗・未試行を集計し、未解決の取得経路や解析不備がないことを確認する。以下の3銘柄preflightは切替直前の疎通確認であり、全銘柄検証の代わりにはしない。403/429で停止した全件検証を、少数の公開画面やCI成功で完了扱いにしない。
日次や通常readerに暗黙migrationは追加しない。最終HEADのCIと切替toolの検証を終えてから、
`cloud-materialize`の明示入力`upgrade_market_v48=true`を使い、一度だけ担当者が切り替える。
通常のmaterializeではこの入力はfalseであり、machine storeを公開しない。

1. 他のpublicationと競合しない時間帯を選び、対象codeのmain反映とstore反映を一組にする。
   既存`cloud-publish`とpublication leaseを使い、切替中に旧codeのbatchを別環境で動かさない。
2. lease取得前に既知の3銘柄（7203/4436/8306）を通常collectorで取得する。未確認・parse失敗・403/429なら本番切替前に停止する。原応答captureはrunnerのprivate一時領域だけに置き、公開artifactへ出さない。
   workflowはfresh pullした47をread-onlyで開き、`tools/cutover/upgrade_market_v48.py`で別の48へ全行をコピーする。
   旧schemaのtable/index定義、全表の件数と双方向の値一致、foreign keyとintegrityを確認する。
   原本bytes・受領時刻・Lake origin・非Lakeの派生値を保持し、予想表は空で追加する。provider再取得はしない。
3. 現行Lakeを48へhydrateした後、同じ3銘柄のcaptureを正規importし、元の受領時刻のL1原本とL2予想を既存`publish-lake`で追記・公開する。再取得しない。
   続いて既存`push-machine`のbackup・ETag条件・bundle receiptで反映する。
   runs/indicatorsは同じlease内でfresh pullした無変更の内容を同時に戻す。applicationを変更せず、既存Lake原本の履歴も保持する。
   続けて通常のmaterializeを行い、各stepの成否とlease解放を確認する。
4. 反映済み48に対して移行入力を再指定しない。store反映後にmaterializeだけ失敗した場合は、
   次回は`upgrade_market_v48=false`で再実行する。部分machine pushでは既存の部分push復旧手順に従う。
   未確認のbackupへの上書きやuser_versionだけの変更をしない。
5. 切替受入後に一回限りのtool・専用test・workflow入力/stepを撤去し、通常経路に互換分岐を残さない。
   3銘柄の初回実取得・原本/観測の新規件数・固定release公開と、schema切替の各stepを別に確認する。schema切替成功だけを取得済みとは数えない。
   一回限りの取得・Lake公開・machine pushを含むためjob上限は一時的に90分とし、lease TTL120分以内に保つ。撤去時に通常materializeの30分へ戻す。
   本番画面の目視確認は今回の完了条件ではない。

### 日次取得と固定コホートの再開

`cloud-external-assessment.yml`はmain限定で毎日19:13 JSTに日次取得し、同じ入口で手動受入・再開も行う。上の限定collectorと分け、保存済み全普通株の取得をpublication lease外で行う。scheduled runは元のGitHub run作成時刻からcron対象日と`external-assessment-daily-YYYY-MM-DD`のbatch IDを決め、queue遅延や翌日のrerunで対象を別日へ進めない。実行時点が15:30 JSTより前なら取得・公開前にskipする。

初回に最新L1のreleaseを固定し、日次取得ではそのJPXカレンダーで対象営業日のexact-date masterを選ぶ。休日も取得し、連続したカレンダーで直近営業日を確認してその保存済みmasterを使う。手動では従来どおりmaster日付を明示する。calendarと必要なmaster年partition以外をこの計画段階で取得・hydrateせず、新しいmasterをproviderから取得しない。calendarの対象日・連続性、普通株のproduction floor、分類、master日付・release bindingが不足する場合はprovider通信前に停止する。planはmaster日付・release・ticker集合・collector policyに固定され、同じbatch IDの再開では保存済みplanを読み、別universeへ置換しない。受領日時とJST受領日は実通信から保持し、休日や翌日の再開をmaster日付・cron対象日へ付け替えない。

既定・最大50銘柄のcohortを直列のmatrix jobへ分ける。通常の4媒体では50×（Monex 1HTTP＋楽天2HTTP＋株マップ1HTTP＋みんかぶ最大2HTTP）で最大300HTTPとなる。取得は銘柄ごとにplanの媒体順で回し、別hostの待機枠を使う。保存planとstageのpair順は従来の媒体順を保持する。別のHTTP数groupや細粒度matrixは作らない。workflow全体の取得専用concurrencyで別batch ID・再実行も直列化する。全5hostは平均2HTTP/秒・最大4creditのtoken bucketをcohort内の全pair・再試行で共有し、実送信直前にtokenを消費する。任意の1秒窓で4件以下という厳密上限ではない。実際のHTTPは直列1件、process開始時のcreditは0で、再起動によるcredit付与はない。HTTP Sessionはcohort内で再利用し、各pair開始時にcookieを空にする。限定CLIの2秒間隔と通常のTLS/proxy経路は維持する。

pair取得は120秒以内、HTTP timeoutは30秒以内とする。取得jobは4時間のsoft budgetに次pair・保存の15分余裕を残し、4時間30分のprocess timeout、5時間のjob上限で打ち切る。HTTP timeoutの積だけを総所要時間の保証にしない。

### cohort stageの確定と再開

確定したtyped観測と安全な成否は、既存`baibai-stores`の`coordination/external-assessment/<batch_id>/cohort-NNNN.json`へ`external-assessment-cohort-stage-v2`で保存する。成功・正常null・既知失敗・未記録を区別し、実受領UTC時刻を変更しない。通常終了・soft budget・retry上限など制御された停止では、raw bundleの保存を確認してから確定結果をcohort stageとして一度だけ条件付きPUTする。取得前の永続予約、inflight状態、永続429 retry/cooldownは持たない。

planは既存stageを一度走査し、未記録pairを持つcohortだけをmatrixへ渡す。acquireは対象cohortのstageだけを読み、完了済みならprovider通信0・PUT0で返す。部分再開では保存済みの成功・正常null・失敗と元受領時刻を保ち、未記録pairだけを取得する。hard kill前に未commitのGETは再取得されうる。通常cohort最大300HTTPの再取得を受け入れ、HTTP exactly-onceを保証しない。結果が一件も確定しないsoft stopでは空stageを保存しない。

初回PUTは`If-None-Match`、部分再開は読取世代の`If-Match`で更新する。PUT応答不明・ETag欠落・CAS競合では実際のstageを読んで確定済み結果を採用し、新しい受信値で既存結果を上書きしない。保存が確認できない場合は失敗とし、競合stageに未記録pairが残る場合も未完了と報告する。通常の成功PUTではContent-MD5と応答ETagを検証し、取得用storeのPUT直後HEADを省く。保存済みobjectのHEAD→GET→HEADとSHA256/世代検証は維持する。

429 retryはprocess内だけで行う。`Retry-After`の秒数またはHTTP-dateを受信完了UTC基準の期限へ変換し、期限を短縮しない。headerがないか不正なら5・10・20・40・80秒のbackoffに0～1秒のjitterを加える。初回に加えて最大5回のpair再試行を許可し、HTTPライブラリの内部retryは無効のままとする。楽天はouter→公式iframeを再検証するため、初回＋5回で最大12HTTPとなる。待機はpair deadline外だがjobのsoft budgetに含め、待機中は他pairを開始しない。

待機がbudgetへ収まらない、有効な待機値が表現不能、5回使い切った後の429、403では新規通信を止め、最後に確定した失敗と未記録pairをpartial stageへ保存する。失敗は同planで再取得しない。operatorが原因を確認して同batchを再開する場合も未記録pairだけを取得する。失敗pairを改めて取得する必要があれば、重複取得範囲を明示して別batchを計画する。retry専用matrix・再dispatchにまたがる残り回数や待機期限は作らない。

追加取得のcollector bindingはparser・host・pair上限を含むv7である。旧checkpoint形式は新readerで実行時変換せず、未公開の旧batchは旧revisionで確認・完結させるか、レビュー済みの個別切替を行う。新batchへ旧checkpointを読み替えない。今回のcode変更だけで本番の旧stage移行済みとは扱わない。verified terminalは追加取得のbinding確認より先に認識し、元の完全/部分公開結果のままno-opで返す。

### 公開HTML原本のcohort handoff bundle

HTTP 200本文はprivateな標準tar.gz（format v2）へまとめてtyped stageより先に保存する。対象は既存collectorが要求した公開Monex、楽天outer、確認済みIFIS iframe、株マップの銘柄診断、みんかぶの証券アナリスト予想と、404時に対象外を確認する同一銘柄の公開本体ページに限る。SRC/FISCOの企業レポートはこの日次取得へ加えず、Research時の参照とする。HTTP転送圧縮展開後・文字decode前のbytesを捕捉し、manifestにreceipt UUID・attempt UUID・媒体・要求ticker・page/iframeと親receipt・URL・encoding・実受領UTC・HTTP status・本文SHA256/bytes・archive memberを記録する。Cookie・認証header・全response headerは保存しない。旧診断用format v1はv2の原本読取で拒否し、旧本文からcanonical identityを補わない。

受信した本文は文字decode前にcallbackへ渡す。各attemptの最大2本文をpair deadline外でprivateなlocal fileへspoolし、cohort終了時にarchiveを作ってimmutable bundleをuploadする。成功したuploadのkeyと選択attempt IDを含めてからtyped stageを条件付きcommitする。raw append・finalize・upload失敗はstage commitを止める。stage PUTが曖昧な場合は既存の読戻しで実際に保存された競合stageを採用し、上書きしない。commit前のhard killでは未確定GETを再実行し得るため、解析の各命令より前に永続保存済みとは主張しない。完了済みstage/terminalは再取得・raw補完しない。

keyは既存rootの`external-assessment/raw/utc-date/<bundle作成UTC日>/bundles/<batch_id>/cohort-NNNN-<bundle UUID>.tar.gz`とする。manifestの各本文の実受領時刻はbundle作成日へ付け替えない。本文共有用のhash-indexや独自containerは持たず、3媒体と429 retryのsynthetic fixtureで全bytes復元と容量を検証する。200のparse失敗本文も保持するが、typed failureをnormalnullへ変えない。非200・未受領本文のreceiptは作らず、応答なしの試行はattempt IDだけを残す。限定`collect` CLIも同じ原応答callbackからprivate captureを作る。

raw rootは引き続き`external-assessment/raw/`であり、R2標準lifecycleの期限方針はこの変更では触らない。この単一prefixへ`Expiration: {Days: 3}`を設定する方針とし、deploymentでは実設定を確認する。適用時の実保持は通常概ね3〜4日で、厳密な72時間TTLではない（[R2公式仕様](https://developers.cloudflare.com/r2/buckets/object-lifecycles/)）。v2 bundleはstageより先にdurable uploadされる短期handoffであり、stage commitだけで公開済みL1とは扱わない。publisherが原本をmarket SQLiteへ取り込み、固定Lake releaseを読戻して初めて長期L1として確認する。旧rootの移動・削除やlifecycle操作を日次取得へ加えない。

次のinventoryは単一rootをread-only LISTし、UTC当日と前2日の3日付を調査対象、古い日を整理候補としてobject数/bytesを示す。旧形式の同root内本文・receiptと新bundleを日付別に集計し、形式を認識できないkeyは`unclassified`へ分ける。GET・PUT・DELETEやlifecycle設定は行わず、実削除済みを保証しない。

```bash
uv run python -m baibai_batch.storage.external_assessment_raw --as-of 2026-10-12
```

本文再検証では既存runtime認証と`read_raw_bundle(store, bundle_key)`を使い、manifestと復元したcanonical receipt・bytesのsize/SHA256・親子関係を照合する。archiveをfilesystemへ展開せず、本文や再解析値をstdoutへ出さない。必要なサンプルの期限外保持はraw root外の明示したprivate領域へ手動コピーし、専用pin/GC管理は追加しない。

### 公開・成功確認・停止後の運用

code/store切替、レビュー・CI・private handoff・費用/所要時間の受入を確認してからmainのworkflowを実行する。新credential・serverは不要だが、開発テストは本番取得・R2書込・通知の受入証拠ではない。再開は元runのrerun、または保存planと同じmaster日付・batch IDでdispatchする。

```bash
gh workflow run cloud-external-assessment.yml --ref main \
  -f batch_id="$external_assessment_batch_id" -f master_asof="$external_assessment_master_asof"
```

**成功確認**: 固定cohort・pairのaccountingと未記録0を確認し、success・normalnull・parse/HTTP失敗を区別する。全pairの試行完了は全指標の値ありを意味しない。通常公開は全cohort・pairの確定が必要で、403/429を含むbatchは明示的な部分公開を使う。publisherだけ既存`cloud-publish` concurrencyとpublication leaseへ入り、最新cloudをfresh pull/hydrateしてから元受領時刻のstageを適用する。欠落cohort、期限切れ・置換lease、古いhydrate originは公開前に拒否し、pointer切替直前にもleaseを確認する。leaseとpointerの二つのkeyを原子的なfencingとはみなさない。workflowの公開job上限110分は既存lease TTL120分より短い。

公開前に参照されたraw bundleを1件ずつ検証し、同じreceipt IDの原本をmarket SQLiteへ独立transactionで取り込む。選択attemptは保存済み原本を読み直して再解析し、typed stageとの一致を全件確かめてからL2を更新する。後続bundleの不備では、それまでの有効なL1は残るが、L2と公開pointerは変更しない。再試行で同じ原本は追加row 0となり、異なる内容の同一IDは拒否する。HTTP 200のparse失敗は原本だけを残し、非200や応答なしの本文は作らない。

公開後は今回専用のprivate mirrorから固定releaseの変更されたExternal Assessment L1/L2のUTC/JST年月partitionを読み、原本のmetadata・本文bytesとtyped観測の全列、変更月内の既存・追加履歴digestを照合して`readback: verified`を記録する。原本の月全履歴は行順に逐次hashし、全HTMLを一括保持しない。新upload objectはpublisherがremoteから全bytesを読戻してSHAを検証し、その取得fileを固定releaseのSHA照合・履歴検証にも使う。未取得の変更月objectはremoteから取得し、別runやexport元fileを流用しない。無変更partitionはmanifest object identityで確認する。同stageの再投入は追加row 0である。JST日付は元UTCから導出し、日跨ぎを単一日の全銘柄coverageとしない。公開失敗後は次回も最新正本をhydrateし、readback失敗を完了扱いしない。

停止したbatchを確定済み観測だけで閉じるとoperatorが明示的に選ぶ場合は、同じ保存batch ID/master日付に`operation=publish_confirmed_partial`を指定する。この入口は取得jobを実行せず、保存planとtyped観測がなければ拒否する。最新正本のfresh hydrateと同じlease/readback契約で確定typedだけを公開し、欠落cohort・未記録pairを正常nullや確定失敗へ変えず、元stageを保持する。部分terminalは4状態件数・元JST受信日/source別typed件数を残し、全件完了と区別して同batchを閉じる。旧5状態terminalは現行readerでは受け付けない。過去の証拠は元revisionで確認し、現行checkpointへ読み替えない。

完全/部分verified terminalの再実行はstage・DB・lease・L1を読み直さず、`previously_verified`・provider通信0・追加row 0を返す。過去の検証結果であり、今回の新公開や現在世代の再検証ではない。terminalに保存されていない集計値を0で補わない。

終端`notify` jobは既存secret `DISCORD_WEBHOOK_URL`の宛先へrun単位で1件通知する。成功・既知失敗を含む公開・部分公開・以前の完全/部分公開・取得未完了・公開確認失敗・取消を区別し、確認済みの固定pair・typed・既知失敗・normalnull・未記録・追加row件数と実行linkを載せる。matrix失敗時やterminalに保存されていない集計値は不明とし、通知用のR2読取は加えない。銘柄別値・HTML・private batch ID・認証情報・error本文は通知しない。配送失敗はsanitizedな理由をstderrへ残し、batch結果を変更しない。job成功だけで実配送済みとは判断せず、workflow履歴も確認する。

正常な初回cohortではraw bundleとstageをこの順に各1 PUTする。private読取はplanの一度のstage走査、各acquireの自cohort読取、publishの確定stage走査に限定し、各acquireで全cohortを再走査しない。部分再開・競合・失敗では操作数が変わる。summaryの`provider_requests`は今回の実HTTP件数、`http_requests`/`response_content_bytes`は採用stageの累計で、wire bytesは未計測である。`elapsed_seconds`は今回の総時間、`collection_elapsed_seconds`はHTTP・host制限待機・解析、`storage_elapsed_seconds`はprivate読取・local spool・raw/stage保存を示す。429 backoffは総時間に含む。完了stageの取得時間とprovider件数は0で、過去runの時間を補わない。

R2費用確認ではprivate stage/raw、lease、pull/hydrate、L1 export/upload/readbackを分けて実測する。既存TransferReportはpublish層の範囲であり、private handoffやSDK retry、wire bytesを含むrun総計ではない。公開済みのL1原本とL2履歴はraw expirationによって削除しない。未公開のbundleが期限切れになれば、その原本の投入・再構築は保証できない。公開/readback後も固定planと小さなterminalを保持し、同batchを再取得へ戻さない。今回の最適化だけで全runの料金・所要時間を確定したとは扱わない。

<a id="store-identity-recovery"></a>

### 保存名切替後の運用と復旧

External Assessmentのtable/dataset、Market Contextの指標keyとapplication判断tableの切替は[PR #1442](https://github.com/koumatsumoto/baibai-loop/pull/1442)で完了した。変換・公開・読戻しの完了証拠は[Issue #1414](https://github.com/koumatsumoto/baibai-loop/issues/1414)に集約し、一回限りのtoolと初回切替手順は通常運用から除去した。

通常の取得・反映は[クラウド正本の取得](#クラウド正本をローカルへ取得する)、[machine公開](#ローカルからクラウドを更新する)、[application反映](#application-db-を反映する)を使う。取得世代、publication lease、machine receiptと、対象codeのschemaが一致することを確認する。Readingの再計算・vintage選択・known_atの制約は[Market Context reference](../docs/reference/market-context.md#reading)を正本とする。

旧正本keyと固定releaseは歴史的読戻し用に保持する。旧releaseは元commitのruntimeと組み合わせ、現行runtimeへ透過的に読み替えない。旧DB/keyの存在を根拠に現行keyへ上書きせず、[application復元](#application-db-を復元する)または[R2 transferの復旧](#r2-transferの安全境界)で対応するcode・store・世代を確認する。schema不一致の修正は[明示schema切替の修正](#schema-cutoverを修正するとき)に従う。

raw保持方針は[公開HTML原本のcohort handoff bundle](#公開html原本のcohort-handoff-bundle)に従う。通常の公開では`meta.json`をtailで発行し、servingの複数keyを原子的に切り替えたとは扱わない。

<a id="external-assessment-identity-cutover"></a>

### 過去のschema切替

market30→31・application20→21と指標の保存名切替は[PR #1442](https://github.com/koumatsumoto/baibai-loop/pull/1442)で完了した。歴史資料が引用する切替手順は[当時のcommitの記録](https://github.com/koumatsumoto/baibai-loop/blob/0762f1ca2746384e5d6656a7277c960ba8161063/batch/OPERATIONS.md#external-assessment-identity-cutover)を参照し、現行runtimeで再実行する通常手順とはしない。

28→29の切替時点のschemaは29である。既存履歴とstore-local dataを保持したクラウド反映・fresh readbackは確認済みであり、役目を終えた一時移行toolと専用テストは除去した。切替の検証記録は[Issue #1407](https://github.com/koumatsumoto/baibai-loop/issues/1407)、対応codeは[PR #1408](https://github.com/koumatsumoto/baibai-loop/pull/1408)・[PR #1410](https://github.com/koumatsumoto/baibai-loop/pull/1410)を参照する。

通常の取得・公開は既存の入口を使い、旧schemaの再移行をdailyへ代行させない。lease、取得世代への条件付き更新、公開後の確認は通常の発行契約を維持する。

<a id="tradingview-expectations"></a>

## TradingView Analyst Expectations

`cloud-tradingview-snapshot.yml`は平日15:37 / 17:37 / 19:37 / 21:37 / 23:37 JST（UTC `37 6,8,10,12,14 * * 1-5`）、土日09:37 / 13:37 / 17:37 JST（UTC `37 0,4,8 * * 0,6`）に独立して起動する。平日の祝日も既存5 slotを休場日の観測・未取得補完に使う。GitHub queueで実開始が遅れるため、targetは予定時刻ではなくrun開始時のJST当日とする。起動回数やjob成功数をprovider取得成功数として数えない。

手動・定期ともhydrateしたJPX calendarで当日を判定し、営業日は15:30以降、休場日は09:00以降に取得する。09:00前はsetup前に終了する。09:00以降の営業日朝はlease・pull・hydrate・preflightまで進んでcalendarを確認するが、master取得・OAuth/provider接続・保存へ進まず`before_close`となる。朝の手動観測では同日PKを埋めず、引け後の取得を妨げない。

Universeは営業日なら当日master、休場日ならcalendar上の直近営業日の保存済みexact-date masterから選ぶ。選んだ営業日から実観測日までcalendarの日付が連続していることを確認し、間の欠落があれば停止する。休場日の必要masterやcalendarがなければ停止し、より古い保存済みmasterへ暗黙に退避しない。`observation_master_asof`はこの必須基準日との一致確認であり、時間guardを解除しない。同日再開は同じ基準日を使う。`snapshot_date`は実観測日のJST当日、`fetched_at_utc`は実受領完了時刻で、masterの日付へ現在値を保存しない。master基準日を結果・progress・summaryの`master_asof`に記録する。日跨ぎ前の正常batchは部分結果として残し、跨いだresponseは前日へ保存しない。

同日のnullやunresolvedとして保存した銘柄もaccount済みであり、その日は上書き・再取得しない。既保存の完全snapshotを何度も観測する診断ではない。

取得は50銘柄ずつ、応答完了後15秒以上の間隔とする。正常batchは検証後に確定し、新規rowがあれば`partial`でもmarket releaseへ公開する。後続slotは固定market releaseからhydrateした同日rowのticker集合をexact-date masterのUniverseと照合し、未保存銘柄だけを取得する。全銘柄account済みならMCP接続前に終了する。hydrate後にmasterのraw rowsがあれば、`source_coverage`がなくても再取得せずに使う。TV workflowはremote `market.sqlite`や`machine-manifest.json`を更新せず、market releaseだけを永続化する。dailyは独立して実行する。

summaryは営業日判定・master基準日・保存済み/追加/残件・unresolved・指標ごとの正常null・first/last受領時刻・provider障害・公開結果を分けて示す。partialをcooldownで取得skipした場合も保存済みrowの品質を示す。全件account済みでも全指標が利用可能とは限らない。検証・commit済みの追加rowはcooldown記録より先にmarket releaseへ公開し、補助記録の失敗で正常観測の公開を止めない。公開後の補助記録・lease解放の失敗は、公開済み件数と失敗stepを併記して通知する。成功・no-opはDiscord通知せず、partial・障害は既存通知に残す。

休日の同値観測も履歴として保持するが、独立した情報sampleとして数えない。分析時は各行の`fetched_at_utc`が評価のas-of cutoff以下であることを確認し、未来値や無条件のlatest-nonnullで欠測を埋めない。土日3 slotは候補頻度であり、実網羅率・欠測・受領時刻で受け入れを判断する。provider通信が0のno-opでもlease・pull・hydrateのR2 I/Oは発生する。R2 Standardの無料枠は[公式料金](https://developers.cloudflare.com/r2/pricing/)に従い、全bucketの月間保管量・Class A/B消費を確認する。使用量が不明なら無料とは保証せず、追加課金を前提に頻度を増やさない。

### 保存済みmasterを使う当日の手動観測

mainへmanual dispatchし、`observation_master_asof`に保存済みmasterの日付を指定する。営業日は当日、休場日はcalendar上の直近営業日のmasterを指定し、上記の取得時間を守る。J-Quants masterの追加取得は行わず、指定日がcalendarの必須基準日と異なる場合やmasterがない場合はOAuth/provider接続前に停止する。50銘柄ずつ、応答完了後15秒以上の間隔を必須とし、通常collector・normalizer・publication lease・cooldown・25分soft budget・market publisherを使う。

```bash
read -r -p '保存済みmasterの基準日 YYYY-MM-DD: ' master_asof &&
gh workflow run cloud-tradingview-snapshot.yml --ref main \
  -f observation_master_asof="$master_asof" -f interval=15
```

同じ観測日の再開は同じmaster基準日を指定し、fresh pull/hydrate後に同日の保存済み集合を除外する。正常null / unresolvedも保存済みとして上書き・再取得しない。provider失敗・Retry-After・日跨ぎ・soft budgetで止まった場合、新規確定rowがあればpartialを公開し、残件だけを同日中に再開する。summaryの`target_date` / `master_asof` / first・last fetched_at、件数、stop_reason、公開release、lease解放を確認する。partial公開は全Universe取得成功とは数えない。取得中に当日`tradingview_source_receipts`が増えたが新規の正規化済みrowが0件のときも、provider/auth/secret/time_guard/timeout系の取得失敗に限り既存market release publisherで原受領記録を公開する。取得runは失敗のままとし、cooldownとlease解放を続ける。storage/internal障害や中断、受領記録0件ではこの公開を行わない。公開後は固定releaseからhydrateして原受領記録を読戻し、正規化済みrowとは区別する。runner強制停止・timeoutやstorage/internal障害で正常な結果を出せなかった場合は取得成功とみなさず、[障害診断](#tradingview-expectationsの障害診断)に従う。

### 初期設定と認証の復旧

公式MCP SDKで対話認証する。秘密値をGit、L1、workflow artifact、チャットへ出さず、state fileはリポジトリ外に置く。認証URLを開く間はcommandを動かしたままにする。戻り先は`127.0.0.1:8765`であり、接続拒否の場合は待受終了・Windows/WSL間の接続を確認して再実行する。

```bash
(
  set -e
  uv run baibai-engine tradingview authorize \
    --state-file "$HOME/.cache/baibai-loop/tradingview/oauth.json"
  gh secret set TRADINGVIEW_OAUTH_STATE \
    --repo koumatsumoto/baibai-loop \
    --env tradingview-runtime \
    < "$HOME/.cache/baibai-loop/tradingview/oauth.json"
)
```

`TRADINGVIEW_OAUTH_STATE`の正本は`tradingview-runtime` environment secretとする。`TRADINGVIEW_SECRET_WRITER_TOKEN`には、このrepositoryの`Environments: Read and write`権限を持つfine-grained PATをrepository secretとして設定する。通常の`GITHUB_TOKEN`ではSecret更新を代行しない。更新されたOAuth stateは、データ取得より先にenvironment secretへ保存する。書戻し失敗時は取得を止め、対話認証から復旧する。PATの期限切れもこの失敗として扱う。`tradingview-runtime`にはrequired reviewers、wait timer、deployment approvalを設定しない。このenvironmentはrotating stateをjob開始時に読み込むために使う。

runnerでのOAuth smokeはmain refだけに限定する。確認するときは手動CIの`tradingview_oauth_smoke`を指定する。認証更新・Secret保存・2銘柄取得を行い、snapshotは書かない。前run終了後に新しいrunを開始して、更新stateの再利用を確認する。

```bash
gh workflow run ci.yml --ref main -f tradingview_oauth_smoke=true
```

GitHub Secretの初期投入に使ったローカル認証fileは、GitHub runnerが更新した後の最新版ではない。同じstateを複数processで更新したり、古いfileを再投入しない。対話認証による置換も、`cloud-publish`の実行がない時間に行う。

<a id="独立したローカル認証で取得l1公開する"></a>

### 独立したローカル認証で取得・market releaseへ公開する

GitHubとlocalはそれぞれ独立したOAuth stateを使い、共有publication leaseだけでmarket release writerを直列化する。GitHub側のenvironment secretとworkflowは変更しない。local成功はprovider quotaの分離や全市場取得の安定を保証しないため、GitHub失敗直後の自動fallbackやblind retryには使わない。

**前提**: current mainのcleanな専用runtime checkoutを用意する。runnerはそのcheckoutの`stores/market/market.sqlite`をfresh pull・hydrateで置換するため、分析・編集中のcheckoutを使わず、同checkoutのstoreを他processで更新しない。`.env`には既存のstores用R2 credentialと`JQUANTS_API_KEY`を設定し、`uv sync --locked`と、workflowと同じ[DuckDB httpfsの事前配置](../tools/diagnostics/provision_duckdb_httpfs.py)を済ませる。既存transferに必要なAWS CLIも使える状態にする。

初回のみ、GitHubへ投入したfileとは別のpathへ新規に対話認証する。本番Secretや、その元になった古いfileをコピーしない。fileはrepo外に保存し、token rotationも同じfileへatomicに書き戻す。

```bash
uv run baibai-engine tradingview authorize \
  --state-file "$HOME/.cache/baibai-loop/tradingview-local/oauth.json"
```

同じfileを使う取得・smoke・再認証を同時実行しない。GitHubの手動OAuth smokeも、provider requestの重複を避けるためlocal取得中には実行しない。cloud smokeはL1を書かずlocal認証とも独立しているため、既存のGitHub queueを維持しpublication leaseには参加しない。

**実行**: 専用checkoutのrootから、[営業日・休場日の共通取得条件](#tradingview-expectations)に従って実行する。

```bash
uv run baibai-batch tradingview \
  --state-file "$HOME/.cache/baibai-loop/tradingview-local/oauth.json"
```

runnerは実観測日のJST当日を固定し、lease取得 → market pull → current market release hydrate → calendarとexact-date masterのpreflight → 必要な営業日masterと未保存銘柄のTV取得 → 新規rowの`partial`または`saved`をmarket releaseへ公開 → releaseの順に進む。取得時間、休場日の保存済みmaster選択、calendar欠落時の停止、同日再開、batch size・intervalは[共通契約](#tradingview-expectations)を使う。全件保存済みならOAuth接続前に終了し、過去日のbackfillはしない。TV取得は25分のsoft budgetで通常終了を図り、30分のhard timeout、取得前のpullから公開まで全体60分で打ち切る。remote `market.sqlite`・machine bundle・servingは更新しない。

**成功確認**: exit 0、`saved`または`partial`の`rows_added`・`rows / expected_universe`・`remaining`・`stop_reason`、market publish reportのrelease ID/hash、lease解放を確認する。固定releaseの行数・Universe・null・unresolved・`fetched_at_utc`・前日snapshot保持を[固定release参照手順](../docs/reference/market-lake.md#shared-raw-read)で照合する。`partial`は未取得が残るDEGRADEDであり、provider正常や全指標coverageを示さない。`already_saved` / `skipped_historical_asof` / `before_close` / `before_observation`は取得・公開成功と区別したno-opである。同日後続runがfresh hydrateから保存済みrowを再取得しないことも確認する。

**停止・復旧**: busyはexit 3、その他の失敗はexit 1。失敗・SIGINT/SIGTERM・timeoutでは子processを停止してからreleaseする。release失敗時はstderrに示されたhandleを保持し、[既存lease手順](#ローカルからクラウドを更新する)に従う。SIGKILL・host停止ではreleaseできない場合がある。公開失敗後のlocal snapshotだけを根拠に保存済みとせず、次回はfresh pull/hydrateから始める。

認証失敗はlocal fileだけを再認証し、GitHub Secretを変更しない。`secret_persistence`ならfileの書込権限・空き容量を確認し、保存できなかったrotationを古いfileから再試行せず再認証する。継続認証を明示的に確認するときだけ、取得中でないことを確認して次を使う。2銘柄取得とtoken更新を行うがsnapshotは保存しない。次processで同じfileを再利用できることを確認する。

```bash
uv run baibai-engine tradingview smoke \
  --state-file "$HOME/.cache/baibai-loop/tradingview-local/oauth.json"
```

engineの`refresh --state-file`は取得だけの低レベル入口であり、production取得・公開には上のbatch runnerを使う。local file指定時にGitHub認証へfallbackすることはない。

### 保存と再試行

当日の各銘柄で最初に保存した正常応答を維持する。受領したSDK応答は検証前に`tradingview.source_receipts`へ独立commitし、L2は正常batchごとに別transactionで保存する。取得不良batchはL2へ保存せず次の未保存batchを試す。応答不良が3 batch連続した場合と明示的429では新規requestを止める。同一batchの即時retryは行わず、skip後も通常intervalを置く。新規rowが0件なら失敗とし、過去に公開した部分snapshotだけで今回を成功にしない。次slotは公開済みの未保存銘柄から再開する。過去日の穴を現在値で埋めない。

正常応答が要求銘柄を明示的に`missing`と返した場合は、batch内の全件か一部かによらず`unresolved`として保存する。`saved`は全銘柄の応答を記録した意味であり、予測値の充足やproviderの健全性を保証しない。全件`unresolved`でも同日の自動再取得・障害通知は行わない。明示的なHTTP・認証・通信エラーと不正応答は、引き続き未保存として失敗・再開を扱う。

新しい観測は通常のreceipt保持付き取得で保存・公開する。旧`accept-observations`用の正規化済み受入ファイルやLibrary参照はL1再構築の入力にしない。既存ファイルは変更しない。

保存済みL1から1日のL2を再構築する場合は、作業用の既存`market.sqlite`と対象receipt IDを明示してofflineで実行する。認証・provider接続は使わず、他の日付のL2には触れない。実行前に対象receiptが同じ観測日・master・tool・要求列・Universeに属することを確認する。返値の`rows`・`unresolved`・`expected_universe`・`remaining`・`rejected_receipts`を読み、正常receipt 0件や同日銘柄重複ではL2が置換されず、全銘柄が明示的な`missing`の正常receiptは`unresolved`行へ復元されることを確認する。

```bash
uv run baibai-engine tradingview rebuild \
  --sqlite /tmp/market-rebuild.sqlite \
  --asof 2026-10-16 \
  --receipt-id <対象receipt ID>
```

### TradingView Expectationsの障害診断

run summaryの`requests_attempted` / `symbols_requested`は`fetch(chunk)`に渡した論理的なbatch試行数・銘柄数（実HTTP送信数とは限らない）、`rows_added`は検証後に保存した行数であり、HTTP成功数とは区別する。`preflight_saved_rows`と`rows_added` / `remaining`で再開を確認し、`normal_nulls`と`unresolved`を分ける。progressがない取得失敗は件数不明として扱う。MCP transportの`mcp_http_status`とtool応答に明示された`provider_http_status`を混同せず、未観測statusを200と推定しない。

`retry_not_before_utc`はMCP HTTPのRetry-Afterが読めた場合の待機期限だけを表し、元ヘッダーを保存しない。Actionsは既存publication lease内で`baibai-stores/coordination/tradingview-actions-cooldown.json`へUTC期限だけを条件付き保存する。次runは未保存分の取得前に読み、期限内ならprovider非接続、期限切れなら再開する。objectなしは未設定として通常処理、不正object・読取失敗は取得を止める。期限は短縮・削除せず、正常runやログ削除で消さない。新credential・権限・datasetは不要。

待機期限のR2保存失敗はjob失敗として通知する。未保存の期限を次runが復元する保証はないため、記録失敗時もworkflowを停止し、期限を確認してから再開する。強制終了やVM消失で期限記録前にrunnerが失われた場合の永続化保証はないため、この場合はworkflowを停止し、直前応答と待機期限を確認してから再開する。自動引継ぎはこのActions workflowの範囲であり、local・Chatの手動取得も同じ待機指示を守る。cronは最小実開始間隔を保証しない。

TradingView stepのsafe failure line、`TradingView progress:` summaryの順に読む。`chunks_completed / chunks_total`と`chunk_index`で失敗位置を、`provider_elapsed_seconds / elapsed_seconds`と`max_chunk_elapsed_seconds`でprovider待ちとintervalの影響を確認し、`oauth_rotations`で認証更新回数を見る。payload検証失敗では固定の`validation_reason`とcanonicalな`validation_field`を確認する。正常終了時は最終JSONの`rows_added / rows / expected_universe / remaining / stop_reason / unresolved / normal_nulls`とfirst / last fetched_atも確認する。

- `provider_rate_limit`（429）: `is_error=false`でも、`success=false`のerrorが明確なHTTP 429を示す場合はこの分類になる。同runで新規requestを止め、manual scanner callを重ねない。既に正常batchがあれば部分公開を確認し、明示待機期限があればそれを守ったうえで次のscheduled slotまたはprovider quota確認へ進む。
- `auth`: [対話認証](#初期設定と認証の復旧)をやり直す。
- `secret_persistence`: `tradingview-runtime`のEnvironment secretとwriter tokenの権限・期限を確認する。
- `provider_response`: 固定validation reasonを起点に応答契約を調べる。Secret・raw body・symbol一覧をIssueやlogへ転載しない。
- `provider_transport`: 接続・通信の失敗を確認し、provider側の状態と次slotを見て再取得を判断する。
- `time_guard`: target日がJST当日で、JPX calendar上の営業日なら15:30以降、休場日なら09:00以降か確認する。条件を満たさなければ取得せず、過去日を現在値で補完しない。
- `internal`: safe failure lineの`exception_type`と失敗stepを起点に未分類の例外を調べる。`MCPError`では標準RPCの`mcp_error_code`も返すが、HTTP statusとは別である。例外本文・data・tracebackは出さず、原因未確認のまま正常partialへ変更しない。
- `timeout`: 最後のphase / chunkとprovider時間を確認し、原因確認なしにtimeoutを延長しない。
- `storage`: calendar / master / schemaとlocal storeの整合を確認し、修復までpublishしない。


## 初回seedとWorker deploy

### Storeをseedする

**前提**: 初回だけ、ローカル4 storeのconsistent SQLite snapshotをstores bucketへ送る。4 keyの
いずれかが既に存在する場合は、古いローカルcopyによる正本の巻き戻しを防ぐため何も
uploadせず停止する。

**実行**:

```bash
batch/scripts/seed.sh
```

**成功確認**: commandが4 keyをuploadし、0で終了したことを確認する。4 keyすべてが存在するならseed済みなので、
再seedせず通常運用へ移る。seedは取得世代を記録しないため、applicationの通常更新を始める前に
[application反映](#application-db-を反映する)の`pull-app`で正本と世代を取得する。

**停止と復旧**: 1〜3 keyだけが存在する場合は部分seedであり、通常のpull / merge / pushでは復旧できない。
まだ供用前で、他のwriterがなく、対象bucketと次のexact keyだけを人間が確認できた場合に限り、部分seedのkeyを
削除して`seed.sh`をやり直す。供用開始後または状態を確認できない場合は削除せず停止する。prefix削除は禁止する。

```bash
(
  set -e
  set -a; source .env; set +a
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
  endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
  aws s3api delete-object --bucket baibai-stores --key market.sqlite --endpoint-url "$endpoint"
  aws s3api delete-object --bucket baibai-stores --key runs.sqlite --endpoint-url "$endpoint"
  aws s3api delete-object --bucket baibai-stores --key indicators.sqlite --endpoint-url "$endpoint"
  aws s3api delete-object --bucket baibai-stores --key baibai.sqlite --endpoint-url "$endpoint"
  batch/scripts/seed.sh
)
```

### Workerをdeployする

Actions variable `R2_ACCOUNT_ID`と、対象accountの`Workers Scripts Write`に絞ったsecret `CLOUDFLARE_API_TOKEN`を設定する。`web.yml`のPR/pushは品質検査を自動実行し、本番deployはmainの`workflow_dispatch`だけが行う。

Web変更の通常反映はレビュー・merge→必要なread modelの生成・公開・読戻し→Web明示配信の順で行う。read model変更を伴う場合は、旧版のdaily/materializeがqueued・実行中なら完了を待ち、対象版mainで`gh workflow run cloud-materialize.yml --ref main`を実行する。runのhead、全views/history/metaの公開成功と対象JSONの変更fieldを確認してから下のWeb配信へ進む。daily cronは止めず、純粋なUI変更ではmaterializeを必須にしない。field削除・renameでは旧UIと新JSONの一時不整合があり得るため、この順序だけで無停止を保証しない。

```bash
(
  set -e
  gh workflow run web.yml --ref main
  gh run list --workflow web.yml --limit 3
)
```

対象runを特定し、[監視手順](#長時間処理の監視)でdeploy stepの成功と対象画面を確認する。古いWeb treeなら配信をskipするため、run全体のgreenだけを配信成功と数えない。materializeや読戻しが失敗した場合はWebを配信せず、原因を直してから同じ順序で進める。

初回構成だけは先にWorkerを作り、secret設定後に表示を初回生成する。secret未設定のAPIは401となる。password managerで生成した32文字のCSPRNG値を、次のpromptへ入力する。

```bash
(cd web/edge && npx wrangler secret put VIEW_PASSWORD)
```

secret設定の成功後、表示を初回生成する。

```bash
(
  set -e
  gh workflow run cloud-materialize.yml --ref main
  gh run list --workflow cloud-materialize.yml --limit 3
)
```

対象materializeの成功後だけ実行する。

```bash
web/edge/scripts/verify-deployment.sh
```

未認証・誤認証が401、正認証で既存keyが成功し不存在keyが404となることを確認する。passwordはpromptだけで扱い、引数・Git・logへ出さない。TTYなしのexit 2は未検証であり、迂回しない。失敗時はworkflow/検査結果の原因を直し、deployやR2操作を重ねない。

<a id="shared-read-setup"></a>

### 共有readとmarket lake接続の設定

**前提**: mainのweb deployが共有read実装を含むことを確認します。CloudflareのR2 API token管理で、
production `baibai-stores`だけにscopeした **Object Read only** credentialを新規作成します。
publisherのread-write credentialは流用せず、canonical bucketのWorker bindingは追加しません。
Object Read onlyはbucket全体を読めます。外部へ返す範囲をmarket lakeのpointer・manifest・L1/L2 Parquetに限定する境界はWorkerのnamespace allowlistであり、
prefix単位IAMではありません（[Cloudflare公式](https://developers.cloudflare.com/r2/api/tokens/)）。

`READ_ACCESS_TOKEN`はowner passwordとは別に、32 random bytes以上をCSPRNGで生成しbase64url等にします。
実値と共有URLはpassword manager等の非公開経路だけで扱い、Git、Issue、PR、CI log、artifact、shell引数へ
残しません。`wrangler.jsonc`ではobservabilityとLogpushを無効にし、`redact_query_string=true`を設定します。real-time logの`wrangler tail`、dashboard Live Logs、Tail Worker等は、
共有tokenを使うrequest中に起動・接続しません。

**実行**: 以下はrepository rootから実行し、promptへ値を入力します。`L1_R2_BASE_URL`は実bucketのjurisdictionに合う
`https://<account-endpoint>/<bucket>`を指定し、末尾にobject key、query、credentialを入れません。
endpointの実値もGitへ残さないためWorker secretとして設定します。

```bash
(
  set -e
  cd web/edge
  npx wrangler secret put READ_ACCESS_TOKEN
  npx wrangler secret put L1_R2_BASE_URL
  npx wrangler secret put L1_R2_ACCESS_KEY_ID
  npx wrangler secret put L1_R2_SECRET_ACCESS_KEY
)
```

**成功確認**: productionのScript Settingsでobservabilityが未設定またはlogs/tracesとも無効、Logpush無効、
tail consumerなしを確認します。認証値・bindingsは出力せず、設定項目だけを確認します。無効状態が異なる場合は
共有URLの利用を始めず、deployment設定と実設定を一致させます。
owner Bearerと共有Bearer/queryで既存JSONの具体値・更新時点を読み、未認証と旧/誤tokenが
401、重複shareが400になることを確認します。`/?share=...`は通常UIを開き、最初のAPI request前にURLから
shareが消え、owner保存値が維持されることを確認します。共有URLを第三者へ一般公開しません。

market lakeのL1原本・L2派生値は[固定release手順](../docs/reference/market-lake.md#shared-raw-read)でcurrent → release manifest → dataset manifest → 実Parquetへ進む。対象の分析workspaceで取得・decodeし、同じreleaseの対象値をlocal baselineと照合する。HTTP 200や手動uploadだけでは成功としない。取得・decode・照合が失敗した場合は原因を確認して利用を止める。

**停止と復旧**: shared secret未設定・空は共有accessだけを無効化し、market lake接続値の不足はlakeだけ503にします。
上流403/5xx等は安全な502となるので、read-only scopeとendpoint設定を確認します。秘密値や上流error bodyを
logへ出しません。設定失敗時はowner credential、store、daily batchを変更せず設定をやり直します。

**rotationと撤回**: 共有tokenは同じ`(cd web/edge && npx wrangler secret put READ_ACCESS_TOKEN)`で置換し、旧値401・新値成功を確認します。
次の401でfrontendは共有sessionだけを消します。共有撤回は`(cd web/edge && npx wrangler secret delete READ_ACCESS_TOKEN)`です。
market lake credentialは新しいbucket-scoped Object Read only tokenを作り、2つのS3 secretを更新してGETを確認した後に
旧R2 tokenを失効させます。更新途中のlake 502は全設定が揃ってから再確認します。market lakeの共有読取を止める場合は
`(cd web/edge && npx wrangler secret delete L1_R2_ACCESS_KEY_ID)`でgatewayを503にし、Cloudflare側でもその専用tokenを失効させます。
既存owner viewとcanonical dataは維持され、secret変更で再deployは不要です。

