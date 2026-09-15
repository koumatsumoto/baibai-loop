# Local L1 MCP

Researchで既存L1の観測値をChatGPTから読むための、所有者用stdio adapterです。
ChatGPT Webの通常ChatからSecure MCP Tunnelを経由して呼びます。Work / Agent mode /
Desktopのlocalhost bridgeには依存しません。production packageや定期batchからは起動しません。

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

MCP親processに必要なsecretは既存gatewayの`READ_ACCESS_TOKEN`だけです。
所有者のローカルsecret管理から環境変数として渡してください。このmoduleは`.env`を自動読込しません。
値を引数、URL、ログへ埋め込まず、secretを含む設定をGitへ追加しません。
repository rootを作業directoryにし、次のmoduleをstdio commandとして起動します。

```bash
.venv/bin/python -m tools.l1_mcp
```

`stdout`はMCP専用です。missing tokenは`stderr`に変数名だけを表示して終了します。
Tunnel clientは[公式配布](https://github.com/openai/tunnel-client)のchecksumを確認して導入します。
実測対象versionは`0.0.14`です。clientの`run --help`と
[公式手順](https://github.com/openai/tunnel-client/blob/master/docs/end-user-guide.md)を参照し、
上記commandをchannel `main`に設定します。非公開のwrapperを使う場合も、MCPにはREAD tokenだけ、
Tunnel clientにはTunnel Runtime keyだけを渡す環境に分けます。通常shell全体の環境をコピーしません。
Tunnel ID・organization・workspace・API keyは個人のlocal設定に保持します。

ChatGPT側でTunnelのcustom appを接続します。公開するtoolは次節の3つだけです。
すべて`readOnlyHint=true`、`destructiveHint=false`、`openWorldHint=false`です。
PCとTunnel clientが動作している間だけ利用できます。

### Tool定義の更新

tool名・schema・説明を変更したら、稼働中serverの変更に加えてChatGPT側のmetadataを更新します。
[公式のRefresh手順](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)
に従い、developer modeの接続詳細で次を行います。

1. Tunnel clientとMCP本体を起動した状態で、ChatGPTの対象接続を開く。
2. 接続詳細の **Refresh** を実行する。ブラウザのページ再読込とは別の操作である。
3. tool一覧が`l1_resolve_current`・`l1_describe_dataset`・`l1_query`になったことを確認する。
4. 新しい通常Chatで更新済みの接続を選び、受入テストを実行する。

旧fixtureの`g0_echo`だけが見えて実行時に`Unknown tool: g0_echo`となる場合は、
本体切替後も古いtool定義を参照している可能性があります。上の一覧が変わるまでSQLの受入へ進みません。
Refreshを利用できないdeveloper接続では、同じTunnelを選んだ新しいdeveloper接続を作成し、
そのtool scanで3 toolsを確認します。TunnelやAPI key自体を作り直す必要はありません。
公開済みpluginはmetadata snapshotを使うため、公式手順に従って再scan・新versionの提出・公開が必要です。

## 読み方

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

sourceは1〜6個、aliasは小文字英字で始まる英小文字・数字・underscoreの32文字以内で重複不可です。
`from` / `to`は必須のISO日付で両端を含みます。存在するpartitionのうち期間と交差するものだけを読み、
SQL実行前に行の期間・列を限定します。列省略時はdatasetの全列を使います。
空期間は空tableとして扱い、結果ゼロ件やaggregateの結果を返します。

SQLは単一SELECT / WITHです。CTE、JOIN、集約、windowを利用できます。
`parameters`はnamed scalar（文字列・整数・有限小数・真偽値・null）のobjectで、SQLへ文字列展開しません。
DDL/DML、複文、設定変更、拡張導入、ファイルやURLの読取は拒否します。
SQLごとにsecretを渡さない新しい子processを起動し、trusted loaderがtableを作った後に
DuckDBのexternal accessを無効化して設定をlockします。timeout時はterminate / kill後に回収します。

## 上限と結果

数値の正本は[contract.py](./contract.py)の`LIMITS`です。
同時実行は1件で、競合は`BUSY`です。

| 対象 | 上限 |
| --- | --- |
| queryで選択する圧縮Parquetの重複なし合計 | 256 MiB（cache hitも含む） |
| MCP process累計gateway GET / download | 1,000回 / 2 GiB |
| SQL / parametersのUTF-8 JSON | 各16 KiB |
| 子processの起動・load・SQL・結果化 | 30秒 |
| DuckDB memory / 子process仮想memory | 512 MiB / 2 GiB |
| max_rows | 既定1,000、最大2,000 |
| MCP result全体 | 256 KiB |

行数・bytes超過では部分結果を返さず`RESULT_TOO_LARGE`になります。SQLで集約するか期間を縮めます。
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

## エラーと受入確認

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
