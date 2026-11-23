# 米国株の固定受領から調査候補を作る

日次・本番・注文へは接続しない。CLIは固定したAlpaca IEXの日次集計とSEC四半期売上を読み、調査の次の一手と見送り理由を標準出力へ返す。初回は同じ経路を合成fixtureで試せる。

```bash
uv run --offline python -m tools.experiments.us_research_mvp
uv run --offline python -m tools.experiments.us_research_mvp --bundle /private/manifest.json
```

`research_candidate` は調査を進められる状態。幅や成長を証明するシグナルではない。`hold_for_evidence` は証拠保留で、保有継続を指さない。どちらも `no_action`。IEX分割調整済みの**観測20日分**だけで平均と乖離を計算する。NY日付で当日受領しかない足は途中集計として除き、後日同じ束を再読しても採用しない。市場の取引暦が揃ったとはみなさず、公式引けや約定可能値、売買代金を作らない。

## 許可済みの実受領を束ねる

CLI自体はネットへ接続しない。許可済みの収集CLI/transportがある場合だけ、収集と再読を分ける。Alpacaは `DailyBarsQuery(("AAPL",), start, end, "-", "split").url(page_token=...)` が作る [v2日足](https://docs.alpaca.markets/us/reference/stockbars) を使用。`1Day`、`iex`、`USD`、`split`、`asof=-`、`limit=10000`、`sort=asc`。評価日前のNY日を使う。native `bars` と明示 `next_page_token` を含む本文を最終ページまで整形せずに保存する。CLIの整形表、ページを合成したJSON、再保存時刻による代用は使わない。

指定CIKのSEC [Submissions](https://www.sec.gov/search-filings/edgar-application-programming-interfaces#data-sec-gov-submissions) の原本から、Alpacaの銘柄ラベルが同じ発行体に明示されているかを照合する。未受領・受領遅れ・不一致なら日足の観測だけ残し、その発行体の財務とは結合しない。申告ラベルだけでは採用しない。この照合は現在の銘柄表記とCIKまでで、取引所・株式クラス・過去の名称変更まで証明しない。最初の実データ受入では、採用した提出書表紙のSection 12(b)で銘柄と市場・株式クラスが対応する行を別担当が照合する。同一企業のGOOG/GOOGL等は別の株式クラスとして扱い、財務は発行体単位のまま読む。証拠が揃わなければ実データ受入は保留する。

SECは指定CIKの [CompanyFacts](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) の原本と、別途固定した提出証拠を用意する。`published_at` はそのaccessionの独立した初回公表時刻が確認できた場合だけ。SECの受付時計や取得時刻では埋めない。初回公表時刻が不明でも、SECの該当提出索引を実際に受領していれば、`published_at` と `publication_ref` をnullのまま置き、任意の `filing.public_index` に正規 `<accession>-index.htm` の原本を指定できる。報告に示す公開確認時刻は、その索引の受領時刻であり初回公表ではない。`public_seen_at` の申告だけは採用に使わない。年次 `10-K` と時点証明が足りないケースは保留し、代替conceptを勝手に合算しない。

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
    "submissions": {
      "file": "submissions.json",
      "sha256": "<sha256-of-original-submissions-body>",
      "status": 200,
      "received_at": "<actual-submissions-receipt-UTC>",
      "attempt_id": "<nonzero-UUID>",
      "request_url": "https://data.sec.gov/submissions/CIK0000320193.json",
      "response_url": "https://data.sec.gov/submissions/CIK0000320193.json",
      "complete": true,
      "redirected": false,
      "headers": {"Content-Type": "application/json"}
    },
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
      "public_index": {
        "file": "filing-index.htm",
        "sha256": "<sha256-of-original-index-body>",
        "status": 200,
        "received_at": "<actual-index-receipt-UTC>",
        "request_url": "<exact-SEC-Archives-index-url>",
        "response_url": "<same-captured-response-url>",
        "complete": true,
        "redirected": false,
        "headers": {"Content-Type": "text/html"}
      }
    }
  }]
}
```

`submissions` / `companyfacts` / `filing` は不足時にnull。Submissionsは正規URLへの単一GETを実際に完了した原本だけを使う。CompanyFactsの `request_url` とcontent系 `headers` は既に固定できたものだけ。

`public_index` はCompanyFactsや提出情報とは別の受領。正規HTTPS URLへ、リダイレクトせず `Accept-Encoding: identity` でGETし、全文受領した時だけ `complete: true` とする。実際の応答URL・本文SHA・[索引](https://www.sec.gov/Archives/edgar/data/320193/000032019325000073/0000320193-25-000073-index.htm)のaccessionタイトルと同じ提出の全文リンクを照合する。取得時にあったcontent系 `headers` を保存し、部分応答、圧縮、内容長の矛盾や本文の手書き・転記を原本にしない。入力の取得・銘柄対応・公表証拠は供給元で固定し、このCLIは再認証しない。cutoffより遅い索引・原本受領で、当時の不足を埋めない。
