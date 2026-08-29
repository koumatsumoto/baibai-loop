# Runtime stores

実行時の永続stateを、authorityと再構築可否が分かるpathへ置きます。SQLite本体とlake objectはGit管理しません。

| store | 正本 | 書き込み主体 | 復旧・cloud反映 |
| --- | --- | --- | --- |
| `application/baibai.sqlite` | local。cloudはreplica | engine application service | `baibai-engine db backup`で`backups/`へ保存。自動再構築は禁止。`batch/scripts/publish.sh`で公開 |
| `market/market.sqlite` | lake所有17 tableはR2 L1 releaseのruntime copy。残る2 tableはSQLiteが正本 | providerとcontrolled merge | 17 tableはfixed releaseからhydrate。残る2 tableはproviderから再取得可能。push時は17 tableを空にする |
| R2 `lake/l1/` | lake所有17 datasetのL1正本 | lake publisher | immutable content object、manifest、CAS pointerでpublish |
| `lake/` | disposable local mirror・staging・object cache | lake build | R2 manifestから再取得。authorityにしない |
| `macro/macro.sqlite` | cloud rolling window + local full history | macro indicator serviceとcontrolled merge | providerから再取得可能。no-loss merge後だけpush |
| `screening/runs.sqlite` | cloud only | daily batch screening service | runから再生成可能。localからpushしない |
| `screening/calibration/current.sqlite` | typed panel / diagnostics / forwardからなるrebuildable L2 current snapshot | engine calibration command | market/ledger evidenceからtemp buildし、検証後にatomic replace |

## 入口と安全境界

store操作には`baibai-engine`のdomain CLIと[`batch/scripts`](../batch/scripts)を使います。`lake inventory`と`lake validate`は書き込みません。`lake resolve`はpointerを1回だけ解決し、`lake hydrate`は固定releaseから17 tableを満たします。

書き込み主体は上表のownerに限定します。application DBの作成とmigrationはengineのapplication serviceだけが行い、読み取り専用経路は自動初期化しません。cloud copyで上書きする、旧`data/`と現行pathや二つの正本writerを併存させる、repository root以外からstoreを生成する操作は禁止です。起動rootが不正な場合は、storeを作らず停止します。一時cacheは`.cache/`へ置き、storeにしません。

schema変更は対応codeをmainへ入れてからcloudへ反映します。cutover時は専用のone-shot toolで新しい出力storeを作り、`integrity_check`、foreign key、required table、正本row/head/ledger identityを確認してから置換します。runtime migrationや旧layoutの自動検出は行いません。lake contractは[market lake](../docs/reference/market-lake.md)を正本とします。

## 配置と検証

production ruleは[method](../method/README.md)、historical evidenceは[reports](../reports/README.md)が所有します。全体構造は[architecture](../docs/architecture.md)、application eventは[portfolio ledger](../docs/reference/portfolio-ledger.md)、screening storeは[screening runtime](../docs/reference/screening-runtime.md)を参照します。schema/write invariantは[tests/engine](../tests/engine)、transfer/mergeは[tests/batch](../tests/batch)、pathは[tests/contracts](../tests/contracts)で検証します。
