# Owner MCP

保存済みのL1/L2/L3とapplication記録を読み取り専用で提供する所有者用stdio adapterです。用途別toolと`data_catalog / data_list / data_get`による共通入口を持ちます。

ChatGPT Webの通常ChatからSecure MCP Tunnelを経由して呼びます。Work / Agent mode /
Desktopのlocalhost bridgeには依存しません。production packageや定期batchからは起動しません。

## 公開tool

server名は`baibai-loop-owner`です。公開toolは次のとおりです。

- `l1_resolve_current`
- `l1_describe_dataset`
- `l1_query`
- `triage_resolve`
- `triage_get_input`
- `triage_get_judgment`
- `portfolio_get_exclusions`
- `screening_get_review_set`
- `data_catalog`
- `data_list`
- `data_get`

## 準備と起動

Ubuntu Linux、repositoryのPython環境、[OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
に対応するChatGPTのDeveloper modeとcustom appが必要です。
Tunnelは同じPlatform organizationとChatGPT workspaceに関連付け、Runtime API keyには
Tunnels Read / Useを付与します。管理用keyをruntimeへ渡しません。
料金・Platform課金条件は利用アカウントの管理画面でも確認してください。
公開資料に価格がないことは無料の保証ではありません。新たな有料契約が必要なら契約前に確認します。

repository rootで依存を用意します。

```bash
uv sync --frozen --group mcp
```

L1 gatewayを利用する場合に必要なsecretは既存の`READ_ACCESS_TOKEN`だけです。
所有者のローカルsecret管理から環境変数として渡してください。このmoduleは`.env`を自動読込しません。
値を引数、URL、ログへ埋め込まず、secretを含む設定をGitへ追加しません。
repository rootを作業directoryにし、次のmoduleをstdio commandとして起動します。

```bash
.venv/bin/python -m tools.owner_mcp
```

`stdout`はMCP専用です。tokenなしでも起動し、catalogとlocal storeを読めます。
L1 gatewayの認証不足・不通はL1 toolを呼んだときにだけエラーになります。
Tunnel clientは[公式配布](https://github.com/openai/tunnel-client)のchecksumを確認して導入します。
実測対象versionは`0.0.14`です。clientの`run --help`と
[公式手順](https://github.com/openai/tunnel-client/blob/master/docs/end-user-guide.md)を参照し、
上記commandをchannel `main`に設定します。非公開のwrapperを使う場合も、MCPにはREAD tokenだけ、
Tunnel clientにはTunnel Runtime keyだけを渡す環境に分けます。通常shell全体の環境をコピーしません。
Tunnel ID・organization・workspace・API keyは個人のlocal設定に保持します。

ChatGPT側でTunnelのcustom appを接続します。公開toolは[一覧](#公開tool)を参照してください。
すべて`readOnlyHint=true`、`destructiveHint=false`、`openWorldHint=false`です。
PCとTunnel clientが動作している間だけ利用できます。

常駐化、Windowsログイン後の自動起動、切断時の復旧は
[Tunnelの常駐・WSL自動起動](./OPERATIONS.md)を参照してください。

### Tool定義の更新

tool名・schema・説明を変更したら、稼働中serverの変更に加えてChatGPT側のmetadataを更新します。
[公式のRefresh手順](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)
に従い、developer modeの接続詳細で次を行います。

1. Tunnel clientとMCP本体を起動した状態で、ChatGPTの対象接続を開く。
2. 接続詳細の **Refresh** を実行する。ブラウザのページ再読込とは別の操作である。
3. 接続が返すtool名・schemaが更新したserver登録と一致することを確認する。
4. 新しい通常Chatで更新済みの接続を選び、受入テストを実行する。

古いtool名や`Unknown tool`が出る場合は、稼働serverと接続metadataを照合する。ブラウザの再読込だけをmetadata更新とみなさず、tool名・schemaを確認してから受入へ進む。

Refreshを利用できないdeveloper接続では、同じTunnelを選んだ新しいdeveloper接続を作成し、
そのtool scanで更新したtool名・schemaを確認します。TunnelやAPI key自体を作り直す必要はありません。
公開済みpluginはmetadata snapshotを使うため、公式手順に従って再scan・新versionの提出・公開が必要です。

## 保存dataの読み方

1. `data_catalog()`で全resourceを確認する。`layer`（L1/L2/L3）と`domain`はAND条件。
2. `data_catalog(resource_id="macro.observation")`などで実request schema、sort、time basisを確認する。
3. `data_list(resource_id, filters, limit)`で列挙し、itemの`selector`を`data_get`へ渡す。
4. getの`resource_ref`を保存する。同じresourceへの再getでpayloadが変われば`REFERENCE_MISMATCH`になる。

`selector`と`resource_ref`は排他です。latest対応resourceは`{"latest":true}`を明示します。
ledger/ER artifactだけはsingletonなので空selectorを使います。refは過去状態の復元機能ではありません。
返却する公開payloadのhashを比較し、変動するmetaや取得時刻はhashへ含めません。
credentialを含むsource URL・provider errorは既存のredactionを通してからhash化します。

```json
{"resource_id":"macro.reading","selector":{"as_of":"2026-09-18"}}
```

```json
{"resource_id":"macro.observation","filters":{"series_id":"jp.10y","from":"2025-09-01","to":"2026-09-18","mode":"effective"},"limit":200}
```

例の日付は実際の評価日に置き換えてください。readingは現在のstore履歴と既存rulesから再計算したL2です。
過去L3を自動で混ぜません。保存Macro Contextは`macro.context`から別に取得します。

利用可能なresource・selector・filter・sort・time basisは`data_catalog`の実応答を参照する。macro Readingの再計算と保存Contextの取得、ledgerの保存価格と現在quoteは別のresourceである。

一覧はkeyset pageで返す。`next_cursor`の継続条件は元の呼出しと一致させる。itemを途中で切らず、完全なitemまたはget結果を上限内で返せない場合は`RESULT_TOO_LARGE`になる。limitの範囲は入力schema、wire上限は実装contractを参照する。

日付範囲は両端inclusiveです。timestampの期間条件はJST日、date列はその日付です。
macro observationのeffectiveは`as_of >= to`、省略時は`to`をcutoffとします。
publication-quality vintageだけをcutoffへclampします。vintagesは退役系列を含むraw保存行で、`as_of`は不可です。
calibrationでは最初の`meta.snapshot_token`を後続のfilters/selectorへ渡すとcohort→panel→forwardを固定できます。
`macro.reading`は各pageの`meta.rules_revision`に、そのcallで再計算したrulesの版を返します。
一般mutable collectionはcall単位の整合であり、複数page全体のhistorical snapshotではありません。

入力modelの不備は`INVALID_ARGUMENT`にfieldと短い理由を添えます。入力値・未知field名は返しません。
不在・未初期化・exact IDなしは`SOURCE_UNAVAILABLE`、正常storeの空一覧は成功です。
`research.thesis_review`の`thesis_id` filterは親Thesisの存在だけを確認し、親不在は`SOURCE_UNAVAILABLE`、
親が存在してReviewがない場合は空一覧を返します。
旧publicationは識別可能な旧schemaだけ`validation=stored_only`とし、現行schemaの破損は`CONTRACT_MISMATCH`です。
選択したpublicationの保存列と本文の識別情報が矛盾する場合も`CONTRACT_MISMATCH`です。
現在のThesis/CAA適格性やledgerの時価評価は原本閲覧時には実行しません。
新series・tickerにtool追加は不要です。MCPから取得更新・publish・任意file/SQL読取・broker操作はできません。

## L1の読み方

1. `l1_resolve_current()`を一度呼び、返された`release_ref`を保持する。
2. `l1_describe_dataset(release_ref, dataset)`で列・型・PK・日付列・partition・coverageを確認する。
3. 同じ`release_ref`で`l1_query`を呼び、宣言したsourceだけをSQLで分析する。

`release_ref`は`release_id`と`manifest_sha256`の組です。後続toolはcurrentを読み直しません。
objectが取得不能なら分析を停止し、別世代で補完しません。新しい世代を使う場合は新しい分析として
currentを解決します。世代の一致はpoint-in-timeの投資可能情報を保証しません。
`coverage_status`、`coverage_start`、`data_as_of`と要求期間を照合し、欠落や鮮度を分析とともに説明します。

query例（`release_ref`はresolveの返り値をそのまま使います）:

```json
{
  "release_ref": {"release_id": "<resolved id>", "manifest_sha256": "<resolved digest>"},
  "sources": [{
    "dataset": "jquants.daily_bars",
    "alias": "prices",
    "from": "2026-08-01",
    "to": "2026-08-31",
    "columns": ["ticker", "traded_at", "close"]
  }],
  "sql": "SELECT traded_at, close, avg(close) OVER (ORDER BY traded_at ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS mean5 FROM prices WHERE ticker = $ticker ORDER BY traded_at",
  "parameters": {"ticker": "1301"},
  "max_rows": 1000
}
```

sourceで宣言したdataset・期間・columnだけをSQLへ渡す。日付範囲は両端を含み、空期間は空tableとして集計できる。alias・個数・文字数などの形式は公開toolの入力schemaを参照する。

SQLは単一SELECT / WITHです。CTE、JOIN、集約、windowを利用できます。
`parameters`はnamed scalar（文字列・整数・有限小数・真偽値・null）のobjectで、SQLへ文字列展開しません。
DDL/DML、複文、設定変更、拡張導入、ファイルやURLの読取は拒否します。
SQLからの外部I/Oは認めず、指定した保存dataだけを分析する。実行資源は既存の上限で制限する。

## L1の上限と結果

数値上限は[L1 contract](../l1_mcp/contract.py)の`LIMITS`が所有する。同時実行の競合は`BUSY`、行数・wire容量を超える結果は`RESULT_TOO_LARGE`であり、部分結果を正常な完了として返さない。

結果はstructured contentの`schema`、`rows`、`row_count`、固定reference、source情報、
`transfer`、`execution`に入ります。NULLはJSON null、整数は整数、decimalは文字列、
date/timeはISO文字列、binaryはbase64です。NaN/Infinityやnested型等は拒否するのでSQLで変換します。

`transfer`にはcall単位とprocess累計のGET数・download bytes、`execution`には子process実行秒数・
peak resident memory bytesが入ります。`elapsed_seconds`は取得を含むcall全体です。
結果wire bytesはMCP resultをUTF-8 JSONにしたサイズです。Tunnel trafficやChatGPT利用量は別です。

manifestとobjectはprocess専用の一時directoryだけへcacheします。immutable cacheを再利用しても
既存L1 readerでdigest・byte数・schema・行数を再検証します。終了時に自身のdirectoryを削除します。
強制killで残った場合は停止済みprocessに対応するdirectoryだけを削除し、他processのcacheやstoreを触りません。
store、application DB、R2への書込やprovider fetchは行いません。

## L1のエラーと受入確認

公開エラーはcodeと定型messageだけです。credential・local path・上流例外本文は返しません。
`INVALID_ARGUMENT`は入力、`RELEASE_UNAVAILABLE`は固定世代の欠落、`CONTRACT_MISMATCH` /
`INTEGRITY_ERROR`は読取契約・データ不整合です。転送超過は`TRANSFER_BUDGET_EXCEEDED`、
入力容量・memory超過は`QUERY_LIMIT_EXCEEDED`、時間超過は`QUERY_TIMEOUT`、
SQL制約違反は`QUERY_REJECTED`、型の問題は`UNSUPPORTED_RESULT_TYPE`、
認証・通信問題は`UPSTREAM_UNAVAILABLE`です。

受入時はChatGPT Webの通常Chatで3 toolsを順に呼び、実L1の期間集計とJOIN / windowを確認します。
同じqueryをcold / warmで実行し、GET・download・時間・peak memory・result wire bytesが上限内で、
warm時にimmutable objectのGETが増えないことを確認します。
最後にTunnel clientを停止してtoolが利用不能になることを確認します。
接続・料金・実データの確認結果はIssue / PRへ記録します。


## Canonical Triageと時点除外

L1実装は内部subsystemの`tools/l1_mcp`を再利用します。Triageはrepository rootの
`stores/application/baibai.sqlite`、sourceは`stores/screening/runs.sqlite`を既存owner経由で
read-onlyに読みます。application DBはlocal canonicalです。cloud copyやserving JSONで代用しません。
store同期は[ops-maintenance](../../.agents/skills/ops-maintenance/SKILL.md)の別操作です。
MCPからのauto-pull、provider fetch、書込、任意SQL、filesystem読取はありません。

### Triage前のReview Set

`screening_get_review_set`は保存済みcanonical Review Setを1回で固定して返します。
`review_set_id` / `public_run_id` / `as_of` / `not_before`は最大1つです。
IDはexact、`as_of`はその日の最新publication（dailyと同じ）、`not_before`は指定日以降の
最初の対象日の最新publication、無指定は最新対象日の最新publicationです。
同じpublic run IDに複数revisionがあれば`AMBIGUOUS_SELECTION`となり、Review Set IDが必要です。
不在は`SOURCE_UNAVAILABLE`で、別runへ補完しません。

`review_set_ref`、`as_of`、`run_at`、`created_at`、rules / method identityとともに
`input_basis=frozen_review_set`、`candidate_count`、全`candidates`を返します。
候補はproduction ModelInputと同じprojection・順序で、保存済みanalysisとnominationsだけを読みます。
current L1やSecurity Analysisから再計算せず、Triageの有無や判断に依存しません。
返却された`review_set_id`で再取得し、新旧候補の比較は呼出し側で行います。
`triage_resolve`はcomplete Triageだけを返す契約を維持します。

### 固定して別Chatへ渡す

1. `triage_resolve(not_before="2026-09-14")`を呼びます。指定日以降の`as_of`、`published_at`、
   `research_triage_id`昇順で最初の完全なTriageを返します。
2. responseの`triage_ref`全体を保存し、`as_of`、`run_at`、Review Set、run、Macro Context、
   rules / Candidate Discovery method identityを確認します。
3. `triage_get_input(triage_ref=...)`で全候補を含む`model_input`を取得します。
   別Chatでも同じrefを渡します。再resolveは新しい比較の開始として扱います。
4. 元判断を読む段階で`triage_get_judgment(triage_ref=...)`を呼びます。
   full canonical `ResearchTriage`を`research_triage`に返し、candidate snapshotも保持します。

selectorは0〜1個です。`research_triage_id`はexact ID、`as_of`はexact日付
（完全なTriageが複数なら失敗）、無指定はlatestの完全なTriageです。
欠損sourceを別runやrevisionで補いません。

`triage_ref`は`research_triage_id`、`research_triage_sha256`、`model_input_sha256`の3 fieldです。
inputはbatchの共通pure builderを使い、exact Review Setとcanonical Triageがbindするexact Macro Context
から再構築します。Macro Contextがnullなら、後発contextがあっても`missing`のままです。

`input_basis`は`reconstructed_from_production_contract`です。instruction / policyはMCP実行時の
production contractであり、過去processのhistorical exact promptを証明するものではありません。
保存済みprivate `input.json`がある場合だけ、別の受入作業で当時payloadとの一致を確認できます。

resolve後にpolicyやsourceが変わりhashが一致しなくなった場合、input取得は失敗します。
judgment取得はcanonical judgment hashだけを検証し、input policyの変更では拒否しません。
inputにはcanonical判断のdecision、priority、rationale、research_question、key_riskを混ぜません。

### 時点保有・予約除外

`portfolio_get_exclusions(at="2026-09-14T23:59:59.999999+09:00")`のようにtimezone付き時刻を渡します。
比較に使うcutoffは呼出し側で計算します。toolはledger eventを指定時刻までreplayし、
`held_tickers`、`reserved_tickers`、sorted unionの`exclude_tickers`を返します。
数量、金額、価格、注文IDは返しません。

`ledger_as_of`より未来の時刻はcoverage不足として失敗します。期限切れ予約の報告が未解決なら、
解除済みとも有効予約とも決めつけません。未登録・破損ledgerを空集合に変換しません。
`exclusion_sha256`は`at / held_tickers / reserved_tickers`のcanonical JSONから計算します。

### Owner toolsの上限とエラー

Owner toolsも完全な結果を返し、wire上限を超える場合は`RESULT_TOO_LARGE`とする。途中でtruncateしない。

| code | 意味 |
| --- | --- |
| `INVALID_ARGUMENT` | selector・reference・日付・時刻が不正 |
| `SOURCE_UNAVAILABLE` | canonical Triage、source run / Review Set、bound Macro Contextが利用不能 |
| `AMBIGUOUS_SELECTION` | exact日に複数の完全なTriage、またはpublic run IDに複数revisionが存在 |
| `REFERENCE_MISMATCH` | 固定referenceと再読payloadのhashが不一致 |
| `CONTRACT_MISMATCH` | schema・domain・source bindingが不整合、またはledgerが破損 |
| `PORTFOLIO_UNAVAILABLE` | canonical ledgerが未登録 |
| `PORTFOLIO_COVERAGE_INSUFFICIENT` | 指定時刻までledgerのcoverageがない |
| `PORTFOLIO_UNRESOLVED` | 指定時刻に期限切れ予約の報告が未解決 |
| `RESULT_TOO_LARGE` | 完全な結果を上限内で返せない |

内部exception本文・credential・local pathは公開エラーに含めません。
