# 米国株の固定受領から調査候補を作る

日次・本番・注文へは接続しない。CLIは固定したAlpaca IEXの日次集計とSEC四半期売上を読み、調査の次の一手と見送り理由を標準出力へ返す。初回は同じ経路を合成fixtureで試せる。

```bash
uv run --offline python -m tools.experiments.us_research_mvp
uv run --offline python -m tools.experiments.us_research_mvp --bundle /private/manifest.json
```

`research_candidate` は調査を進められる状態。幅や成長を証明するシグナルではない。`hold_for_evidence` は証拠保留で、保有継続を指さない。どちらも `no_action`。IEX分割調整済みの**観測20日分**だけで平均と乖離を計算する。NY日付で当日受領しかない足は途中集計として除き、後日同じ束を再読しても採用しない。市場の取引暦が揃ったとはみなさず、公式引けや約定可能値、売買代金を作らない。

## 許可済みの実受領を束ねる

CLI自体はネットへ接続しない。許可済みの収集CLI/transportがある場合だけ、収集と再読を分ける。Alpacaは `DailyBarsQuery(("AAPL",), start, end, "-", "split").url(page_token=...)` が作る [v2日足](https://docs.alpaca.markets/us/reference/stockbars) を使用。`1Day`、`iex`、`USD`、`split`、`asof=-`、`limit=10000`、`sort=asc`。評価日前のNY日を使う。native `bars` と明示 `next_page_token` を含む本文を最終ページまで整形せずに保存する。CLIの整形表、ページを合成したJSON、再保存時刻による代用は使わない。

SECは指定CIKの [CompanyFacts](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) の原本と、別途固定した提出証拠を用意する。`published_at` はそのaccessionの独立した初回公表時刻が確認できた場合だけ。SECの受付時計や取得時刻では埋めない。同時点で公開を観測した時刻だけが分かるなら `public_seen_at` は表示用として記録し、`published_at` と `publication_ref` はnull。年次 `10-K` と時点証明が足りないケースは保留し、代替conceptを勝手に合算しない。

取得toolでは許可済みの秘密情報をプロセスにだけ渡し、鍵、Authorizationヘッダー・コマンドログを保存しない。実際に本文まで受領した時刻とHTTP状態を記録し、SHA-256はその原本bytesから作る。送信時刻やこのCLIの実行時刻を受領にしない。必要な権限または接続がない場合、実データで合格したとは扱わない。

### 入力例

1〜3銘柄。`file` はmanifestと同じディレクトリのファイル名。下の値は形状の例で、実受領ではない。

```json
{
  "schema": "us-research-receipts-v1",
  "cutoff": "2026-11-23T11:00:00Z",
  "alpaca": {
    "request_id": "your-fixed-attempt-id",
    "start": "2026-10-15",
    "end": "2026-11-20",
    "pages": [{
      "file": "page1.json",
      "sha256": "<sha256-of-original-body>",
      "status": 200,
      "received_at": "<actual-UTC-receipt>",
      "request_url": "<exact-DailyBarsQuery-url>"
    }]
  },
  "subjects": [{
    "symbol": "AAPL",
    "cik": "0000320193",
    "identity_ref": "<separately-fixed-issuer-reference>",
    "period_start": "<exact-reported-start>",
    "period_end": "<exact-reported-end>",
    "companyfacts": {
      "file": "facts.json",
      "sha256": "<sha256-of-original-body>",
      "status": 200,
      "received_at": "<actual-UTC-receipt>",
      "attempt_id": "<nonzero-UUID>",
      "request_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
      "headers": {"Content-Type": "application/json"}
    },
    "filing": {
      "issuer_cik": "0000320193",
      "accession": "<exact-accession>",
      "form": "10-Q",
      "receipt_id": "<separate-filing-receipt>",
      "accepted_at": "<SEC-acceptance-UTC>",
      "received_at": "<actual-filing-receipt>",
      "published_at": null,
      "publication_ref": null,
      "public_seen_at": "<observed-public-UTC-if-known>"
    }
  }]
}
```

`companyfacts` / `filing` は不足時にnull。SEC `request_url` とcontent系 `headers` は既に固定できたものだけ。`public_seen_at` は省略可。入力束の取得・銘柄対応・公表証拠は供給元で固定し、このCLIは再認証しない。cutoffより遅い受領で、当時の不足を埋めない。
