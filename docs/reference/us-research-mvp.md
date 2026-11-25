# 米国株の固定受領から調査候補を作る

日次・本番・注文へは接続しない。CLIは固定したAlpaca IEXの日次集計とSEC四半期売上を読み、調査の次の一手と見送り理由を標準出力へ返す。初回は同じ経路を合成fixtureで試せる。

```bash
uv run --offline python -m tools.experiments.us_research_mvp
uv run --offline python -m tools.experiments.us_research_mvp --bundle /private/manifest.json
```

`research_candidate` は調査を進められる状態。幅や成長を証明するシグナルではない。`hold_for_evidence` は証拠保留で、保有継続を指さない。どちらも `no_action`。IEX分割調整済みの**観測20日分**だけで平均と乖離を計算する。NY日付で当日受領しかない足は途中集計として除き、後日同じ束を再読しても採用しない。市場の取引暦が揃ったとはみなさず、公式引けや約定可能値、売買代金を作らない。

## 許可済みの実受領を束ねる

CLI自体はネットへ接続しない。許可済みの収集CLI/transportがある場合だけ、収集と再読を分ける。Alpacaは `DailyBarsQuery(("AAPL",), start, end, "-", "split").url(page_token=...)` が作る [v2日足](https://docs.alpaca.markets/us/reference/stockbars) を使用。`1Day`、`iex`、`USD`、`split`、`asof=-`、`limit=10000`、`sort=asc`。評価日前のNY日を使う。native `bars` と明示 `next_page_token` を含む本文を最終ページまで整形せずに保存する。各ページの取得時に実要求URL・最終応答URL・リダイレクト有無・全文受領を記録する。IEXは[要求の `feed`](https://docs.alpaca.markets/us/docs/market-data-faq#whats-the-difference-between-iex-and-sip-data)で指定するもので、日足本文からは判別しない。固定URLへの直接の完全な応答が確認できなければ、価格だけ保留する。記録した応答に `Content-Range` があれば、HTTP 200・全文読了の申告だけで採用しない。記録されていないヘッダーは要求しない。CLIの整形表、ページを合成したJSON、再保存時刻による代用は使わない。

指定CIKのSEC [Submissions](https://www.sec.gov/search-filings/edgar-application-programming-interfaces#data-sec-gov-submissions) の原本から、Alpacaの銘柄ラベルが同じ発行体に明示されているかを照合する。未受領・受領遅れ・不一致なら日足の観測だけ残し、その発行体の財務とは結合しない。申告ラベルだけでは採用しない。この照合は現在の銘柄表記とCIKまでで、取引所・株式クラス・過去の名称変更まで証明しない。最初の実データ受入では、採用した提出書表紙のSection 12(b)で銘柄と市場・株式クラスが対応する行を別担当が照合する。同一企業のGOOG/GOOGL等は別の株式クラスとして扱い、財務は発行体単位のまま読む。証拠が揃わなければ実データ受入は保留する。

SECは指定CIKの [CompanyFacts](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) の原本と、別途固定した提出証拠を用意する。`published_at` はそのaccessionの独立した初回公表時刻が確認できた場合だけ。SECの受付時計や取得時刻では埋めない。初回公表時刻が不明でも、SECの該当提出索引を実際に受領していれば、`published_at` と `publication_ref` をnullのまま置き、任意の `filing.public_index` に正規 `<accession>-index.htm` の原本を指定できる。報告に示す公開確認時刻は、その索引の受領時刻であり初回公表ではない。`public_seen_at` の申告だけは採用に使わない。年次 `10-K` と時点証明が足りないケースは保留し、代替conceptを勝手に合算しない。

採用する売上は、提出書の単独四半期と照合した開始日・終了日を固定する。このCLIが扱う通常四半期は両端を含む89〜98日（連続3暦月、13〜14週を含む）。期間長だけでは会計上の区分を証明しない。同じ10-Q、同じ期末日でも半年・9か月累計や範囲外の変則期は保留し、差分で単独四半期を作らない。「最新」は自動判定せず、報告には採用した期間を表示する。

同じSEC提出書の発行体財務だけを比較する場合は `--compare-sec CIK 会計四半期 前年四半期開始 前年四半期終了 当期期首 前年期首` を付ける。当期の日付はmanifestの期間を使う。会計四半期（Q1・Q2・Q3）と残りの日付は提出書の比較列で確認して指定する。指定四半期と一致し、四半期の開始・終了と期首それぞれの間隔が364〜371日の前年比較だけを対象にし、売上・営業利益は単独四半期、営業CF・有形固定資産の現金取得額は期首来累計のUSDで出す。原本の符号を変えず、欠測は補わない。株価・候補判定・一株価値には接続しない。

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
      "request_url": "<exact-DailyBarsQuery-url>",
      "response_url": "<same-captured-response-url>",
      "complete": true,
      "redirected": false
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
      "response_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
      "complete": true,
      "redirected": false,
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

`submissions` / `companyfacts` / `filing` は不足時にnull。SubmissionsとCompanyFactsは、実際の要求URLと応答URLがどちらも指定CIKの正規URLで、リダイレクトなしに全文を受領した原本だけを使う。取得時の `request_url`・`response_url`・`complete`・`redirected` とcontent系 `headers` を固定し、不明な値は補わない。応答の範囲を確認できない場合は当該銘柄の財務だけ保留する。

`public_index` はCompanyFactsや提出情報とは別の受領。正規HTTPS URLへ、リダイレクトせず `Accept-Encoding: identity` でGETし、全文受領した時だけ `complete: true` とする。実際の応答URL・本文SHA・[索引](https://www.sec.gov/Archives/edgar/data/320193/000032019325000073/0000320193-25-000073-index.htm)のaccessionタイトルと同じ提出の全文リンクを照合する。取得時にあったcontent系 `headers` を保存し、部分応答、圧縮、内容長の矛盾や本文の手書き・転記を原本にしない。入力の取得・銘柄対応・公表証拠は供給元で固定し、このCLIは再認証しない。cutoffより遅い索引・原本受領で、当時の不足を埋めない。

SECが `text/html` で返す索引では、[HTMLで省略可能な外側の終了タグ](https://www.w3.org/TR/html4/struct/global.html#h-7.3)が原本にないことがある。省略形は実応答の `Content-Length` と原本byte数が一致する場合だけ読む。EDGARの区切りを認識した場合は、外側の終了タグの有無にかかわらず、書類一覧の全文リンクと空でない提出者情報があり、`contentDiv` の末尾まで閉じていることを確認する。原本にタグを補わない。XHTMLでは外側の終了タグを省略できない。
