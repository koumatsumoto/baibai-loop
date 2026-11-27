# 米国株の固定受領から調査候補を作る

ここでは固定受領と隔離Appを使い、米国株の観測・調査・査読済み判断を試す。調査CLIは固定したAlpaca IEXの日次集計とSECの指定期間の売上を読み、次に調べることと保留理由を標準出力へ返す。同じ経路を合成fixtureで試せる。本番の査読済み判断は[Webの「売買提案」→「米国株」](../../web/README.md#表示の意味)で読む。ここでのCLIは本番配信・日次選抜・注文に接続しない。

リポジトリのルートで[開発環境を準備](../development.md#手元で始める)してから実行する。`--offline` は依存を初回取得しない。

```bash
uv run --offline python -m tools.experiments.us_research_mvp
uv run --offline python -m tools.experiments.us_research_mvp --bundle /private/manifest.json
```

`research_candidate` は調査を進められる状態。幅や成長を証明するシグナルではない。`hold_for_evidence` は証拠保留で、保有継続を指さない。どちらも `no_action`。終了コード0は報告を出せたことだけを示し、証拠の充足や実データ受入を意味しない。引数やmanifest自体が不正なら2。IEX分割調整済みの**観測20日分**だけで平均と乖離を計算する。NY日付で当日受領しかない足は途中集計として除き、後日同じ束を再読しても採用しない。市場の取引暦が揃ったとはみなさず、公式引けや約定可能値、売買代金を作らない。

## USDの調査・比較を査読して保存する（正式提案は発行しない）

取得した材料から人間が書いた一社の分析を、[ローカルの調査レポートCLI](../../tools/experiments/us_research_report.py)で別担当の確認結果と一緒に新規の私有ファイルへ保存する。App、案件完了、提案の配信・注文には接続しない。既存のIEX・SECの読取や再取得はしない。実データが不足していれば判断保留として記録できる。

1. JSON原稿の例は[合成テスト](../../tests/tools/test_us_research_report.py)の `_draft`。原稿に銘柄・上場市場（NasdaqまたはNYSE）・株式クラス・CIK・USD、情報締切と作成時刻を記録する。取引所の暦で対象立会日と終了時刻を照合し、短縮日を通常の引けで補わない。
2. IEX観測帯は受領manifestのSHA、分割調整済み終値・観測件数・日付窓・最小／最大／末尾。採用できない場合は `iex_band: null`。公式終値が必要な比較では、同じ立会の**上場取引所による無調整終値**と一次参照・取得時刻を別に書く。IEXで代用しない。[Alpaca SIP経由](#nasdaqの公式終値を固定受領から読む)は任意。公式終値または他の比較根拠が不足なら `comparison: null` と未確認事項。
3. 財務では原典と実読時刻、会計期間・観測内容を引用。比較が可能な場合だけ、同じ終期で基本／下振れの1株価値・取得権のある分配・仮定を示す。無利息のUSD現金との差を計算し、税・費用・為替は対象外。未確認の残高・執行条件・買付数量は入れない。別担当は出典の本文、銘柄と権利、期間、反対材料と比較前提を確認する。このCLIはリンク先を取得せず、根拠の真偽や別担当の本人性を認証しない。

```bash
uv run --offline python -m tools.experiments.us_research_report draft-digest /private/draft.json
# 別担当が、出力の report_id / draft_sha256 と reviewer_identity / reviewed_at
# （時差付き）/ conclusion: "pass" / notes をJSONに記入してから保存する。
NEW_REPORT=/private/report.md
uv run --offline python -m tools.experiments.us_research_report save \
  /private/draft.json /private/review.json --output /private/reviewed.json &&
(
  umask 077
  set -o noclobber
  uv run --offline python -m tools.experiments.us_research_report render /private/reviewed.json \
    > "$NEW_REPORT"
) &&
uv run --offline python -m tools.experiments.us_research_report check-render \
  /private/reviewed.json "$NEW_REPORT"
```

出力先は作業者の私有ディレクトリ内の新規ファイル。描画に失敗した出力は使わず、`check-render` が成功したファイルだけを使う。原稿を変更したらレビューも取り直す。照合はIDと原稿SHAだけでなく描画本文も比較する。照合成功は私有ファイルの読戻しであり、Appに査読済みの判断が保存・配信された証拠ではない。

## 隔離Appに米国株の見送り・判断保留を保存する

私有レポートの保存とは別に、USD建ての一社の判断を[米国株の保存owner](../../engine/src/baibai_engine/investment_judgment/us_proposal.py)で記録する。判断保留（`defer`）は根拠未確定でも理由と見直し条件を明示する。見送り（`no_action`）は取引所の無調整終値と同じUSD現金との比較、その上で資金を投じない理由がある場合だけ。比較の終期は判断の作成日・保存日（いずれも米東部時間）より後とし、過ぎた比較を新規発行に使わない。保存済み判断の読戻しには現在時刻を使わない。IEXで埋めない。数量、売買条件、口座や円換算は扱わず、日本株の提案・進捗・台帳への書込も行わない。

査読済みの分析を用い、[判断原稿と査読の例](../../tests/tools/test_us_research_report.py)に沿って独立した提案本文の査読を取る。`proposal-digest` が出す提案IDとSHAに、見送りか保留か、理由、見直し条件、元の分析・その査読を結び付ける。分析の査読を提案の査読として使い回さない。査読済み分析の内容はApp内に保存され、読戻しでは分析と提案それぞれが査読に結び付いていることも確認する。元ファイルや再描画は不要で、同IDの差替えや欠損・破損した最新判断から別判断へのfallbackは行わない。

```bash
uv run --offline python -m tools.experiments.us_research_report proposal-digest \
  /private/reviewed.json /private/decision.json
# 別担当が正確な提案IDとSHAを含む decision-review.json を作成した後。
uv run --offline python -m tools.experiments.us_research_report proposal-save \
  /private/reviewed.json /private/decision.json /private/decision-review.json --db "$APP_COPY" &&
uv run --offline python -m tools.experiments.us_research_report proposal-show \
  --db "$APP_COPY" --proposal-id "$PROPOSAL_ID"
```

保存時と同じ提案ID・提案原稿SHA・判断を読戻して照合する。 `--proposal-id` を省略すると公開時刻の新しい米国株判断を1件だけ読む。現行Appに該当行がないときは `null`（終了1）。DB不在、異なるschema・読めない行はエラー（終了2）。両者を混同しない。この入口は隔離Appでの保存検証であり、R2転送・公開Web・broker発注・台帳記帳の完了ではない。

### 隔離Appを用意する

新規の隔離検証は現行コードで `baibai-engine db init --db "$APP_COPY"` を実行し、空のAppを使う。v31への本番切替は完了済み（[#3012](https://github.com/koumatsumoto/baibai-loop/issues/3012)）。通常の調査や表示のために旧DBを取得・移行する必要はない。

## 最新のIEX観測だけを確認する

運用受入では対象市場の公式暦で期待するNY取引日（休場・短縮日を含む）を確認し、実受領のmanifestと一緒に明示する。

```bash
uv run --offline python -m tools.experiments.us_research_mvp --bundle /private/manifest.json --accept-iex-session YYYY-MM-DD
```

各銘柄で、cutoffまでに受領し、かつその足のNY翌日以降に受領した最新観測が指定日と一致する場合だけ終了コード0。有効なmanifestでの受領チェーン不備・観測欠落・日付不一致は1。cutoffを書き換えても、取引日当日に受領した足は採用しない。暦の推定、SEC・調査候補判定、公式引けや売買可否の確認はしない。通常の調査は `--accept-iex-session` を付けずに実行する。

## 許可済みの実受領を束ねる

CLI自体はネットへ接続しない。許可済みの収集CLI/transportがある場合だけ、収集と再読を分ける。Alpacaは `DailyBarsQuery(("AAPL",), start, end, "-", "split").url(page_token=...)` が作る [v2日足](https://docs.alpaca.markets/us/reference/stockbars) を使用。`1Day`、`iex`、`USD`、`split`、`asof=-`、`limit=10000`、`sort=asc`。評価日前のNY日を使う。native `bars` と明示 `next_page_token` を含む本文を最終ページまで整形せずに保存する。各ページの取得時に実要求URL・最終応答URL・リダイレクト有無・全文受領を記録する。IEXは[要求の `feed`](https://docs.alpaca.markets/us/docs/market-data-faq#whats-the-difference-between-iex-and-sip-data)で指定するもので、日足本文からは判別しない。固定URLへの直接の完全な応答が確認できなければ、価格だけ保留する。記録されたヘッダーが部分応答、圧縮、または原本と矛盾する長さ・転送形式を示す場合は、HTTP 200・全文読了の申告だけで採用しない。記録されていないヘッダーは要求しない。CLIの整形表、ページを合成したJSON、再保存時刻による代用は使わない。

指定CIKのSEC [Submissions](https://www.sec.gov/search-filings/edgar-application-programming-interfaces#data-sec-gov-submissions) の原本から、Alpacaの銘柄ラベルが同じ発行体に明示されているかを照合する。未受領・受領遅れ・不一致なら日足の観測だけ残し、その発行体の財務とは結合しない。申告ラベルだけでは採用しない。この照合は現在の銘柄表記とCIKまでで、取引所・株式クラス・過去の名称変更まで証明しない。最初の実データ受入では、採用した提出書表紙のSection 12(b)で銘柄と市場・株式クラスが対応する行を別担当が照合する。同一企業のGOOG/GOOGL等は別の株式クラスとして扱い、財務は発行体単位のまま読む。証拠が揃わなければ実データ受入は保留する。

SECは指定CIKの [CompanyFacts](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) の原本と、別途固定した提出証拠を用意する。`published_at` はそのaccessionの独立した初回公表時刻が確認できた場合だけ。SECの受付時計や取得時刻では埋めない。初回公表時刻が不明でも、SECの該当提出索引を実際に受領していれば、`published_at` と `publication_ref` をnullのまま置き、任意の `filing.public_index` に正規 `<accession>-index.htm` の原本を指定できる。報告に示す公開確認時刻は、その索引の受領時刻であり初回公表ではない。公開を見た時刻の申告は、採用・保留のどちらの根拠にも使わない。時点証明が足りない場合は保留し、代替conceptを勝手に合算しない。

採用する売上は、提出書の対象列と照合した開始日・終了日を固定する。通常四半期は10-Qまたは10-Q/Aの単独四半期で、期間は両端を含む89〜98日（連続3暦月、13〜14週を含む）。明示した10-Kは52週・53週または暦年相当の通期売上として区別し、10-K/Aや変則決算は対象外。期間長だけでは会計上の区分を証明しない。同じ10-Q、同じ期末日でも半年・9か月累計や範囲外の変則期は保留し、差分で単独四半期を作らない。最新の提出は自動で選ばず、報告には採用期間を表示する。

採用できる提出索引があれば、索引の `Period of Report` と指定期末日 `period_end` が一致する場合だけ当期を採用する。同じ提出の前年列を当期には使わない。`--compare-sec` の前年列は従来どおり比較に使う。索引を使わず独立公表証拠を使う場合は索引での照合は行わないため、提出書の対象列との照合は入力作成側で行う。

同じ10-Qまたは10-Q/Aの発行体財務だけを比較する例（会計四半期はQ1・Q2・Q3）。

```bash
uv run --offline python -m tools.experiments.us_research_mvp --bundle /private/manifest.json \
  --compare-sec <CIK> Q3 <PRIOR_Q_START> <PRIOR_Q_END> <CURRENT_FY_START> <PRIOR_FY_START>
```

終了コード0でも、報告に不足・未採用を含み得る。当期の日付はmanifestの期間を使う。会計四半期と残りの日付は提出書の比較列で確認して指定する。指定四半期と一致し、四半期の開始・終了と期首それぞれの間隔が364〜371日の前年比較だけを対象にし、売上・営業利益は単独四半期、営業CF・有形固定資産の現金取得額は期首来累計のUSDで出す。原本の符号を変えず、欠測は補わない。株価・候補判定・一株価値には接続しない。

取得toolでは許可済みの秘密情報をプロセスにだけ渡し、鍵、Authorizationヘッダー・コマンドログを保存しない。実際に本文まで受領した時刻とHTTP状態を記録し、SHA-256はその原本bytesから作る。送信時刻やこのCLIの実行時刻を受領にしない。必要な権限または接続がない場合、実データで合格したとは扱わない。

### 入力例

1〜3銘柄。`file` はmanifestと同じディレクトリのファイル名。下の値は形状の例で、実受領ではない。

`cutoff` と受領・公表等の時刻は、時差を `Z` または `±HH:MM` で指定し、マイクロ秒単位に正確に表せること。小数7桁目以降がすべて0なら読めるが、端数は切り捨てず受け付けない。

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
        "headers": {
          "Content-Type": "text/html",
          "Content-Length": "<actual-Content-Length-from-the-response>"
        }
      }
    }
  }]
}
```

`submissions` / `companyfacts` / `filing` は不足時にnull。SubmissionsとCompanyFactsは、実際の要求URLと応答URLがどちらも指定CIKの正規URLで、リダイレクトなしに全文を受領した原本だけを使う。取得時の `request_url`・`response_url`・`complete`・`redirected` とcontent系 `headers` を固定し、不明な値は補わない。応答の範囲を確認できない場合は当該銘柄の財務だけ保留する。

`public_index` はCompanyFactsや提出情報とは別の受領。正規HTTPS URLへ、リダイレクトせず `Accept-Encoding: identity` でGETし、全文受領した時だけ `complete: true` とする。実際の応答URL・本文SHA・[索引](https://www.sec.gov/Archives/edgar/data/320193/000032019325000073/0000320193-25-000073-index.htm)のaccessionタイトルと同じ提出の全文リンクを照合する。取得時に実応答にあった `Content-Type`、`Content-Length`、`Content-Encoding`、`Content-Range`、`Transfer-Encoding` を `headers` にそのまま保存する。送られていない値は追加せず、`Content-Length` を保存ファイルのサイズやcurlのダウンロード量で補わない。部分応答、圧縮、内容長の矛盾や本文の手書き・転記を原本にしない。入力の取得・銘柄対応・公表証拠は供給元で固定し、このCLIは再認証しない。cutoffより遅い索引・原本受領で、当時の不足を埋めない。

SECが `text/html` で返す索引では、[HTMLで省略可能な外側の終了タグ](https://www.w3.org/TR/html4/struct/global.html#h-7.3)が原本にないことがある。省略形を読むには、実応答の `Content-Type: text/html` と `Content-Length` を両方保存し、その長さと原本のバイト数が一致している必要がある。EDGARの区切りを認識した場合は、外側の終了タグの有無にかかわらず、書類一覧の全文リンクと空でない提出者情報があり、`contentDiv` の末尾まで閉じていることを確認する。原本にタグを補わない。XHTMLでは外側の終了タグを省略できない。

## Nasdaqの公式終値を固定受領から読む

Alpaca SIP経由の終値を使う場合だけ追加する。対象はNasdaq上場（XNAS）で、同一銘柄・株式クラス・NY立会日とその終了時刻を別の証拠で確認済みのもの。[UTPのQ/M](https://www.utpplan.com/DOC/UtpBinaryOutputSpec_3.0.pdf)に当たるNasdaqの無調整公式終値であり、取引所からの直接受領、約定値、統合終値、IEX観測ではない。

1. 許可済みの取得手段で、`NasdaqOfficialCloseQuery.url()` が作る[Alpaca SIPの要求](https://docs.alpaca.markets/us/reference/stockauctionsingle-1)を使う。単一銘柄、同じNY日内で立会終了を含み、それより後までのUTC秒単位区間、`feed=sip`、`currency=USD`、`asof=-`。実行時に確認した `request_url`・`response_url`（両方とも固定URLと一致）、`status: 200`、`complete: true`、`redirected: false`、全文を受領した `received_at`（`Z` または有効な `±HH:MM`、`-00:00` は不可、小数秒6桁まで）と原本バイト列の `sha256` を受領JSONに残す。応答本文は整形せず別の私有ファイルに保存する。HTTP 200やSHAだけから全文受領・リダイレクト有無を推定せず、欠けた記録は後から補わない。鍵・認証ヘッダーは保存しない。
2. 要求区間の終端以降かつ情報締切以前に受領した、`next_page_token: null` が明示された同日・同銘柄の応答を使う。引けイベント全体でQ/Mが一つだけで、その発生時刻が立会終了から要求区間の終端まで（両端を含む）にある場合だけ採用する。[合成入力例](../../tests/tools/test_us_research_report_sip.py)のように、`read_nasdaq_official_close` の結果を `sip_official_close_fields` で原稿の `official_close`（`origin: "alpaca_sip_nasdaq_official_close"`）に写す。発生時刻は原文の精度を保持し、全文受領時刻とは分ける。証拠が不足する場合は公式値・比較を埋めず、未確認事項を残す。
3. 上の手順どおり別担当の分析査読と判断査読を取り、`draft-digest`・`save`・`proposal-digest`・`proposal-save` の**各回**に同じ受領JSONと本文を渡す。CLIはオフラインで原本・範囲・時刻と原稿の値を再照合する。隔離Appへの保存後に `proposal-show` で同じ提案ID・正式原稿SHA・出典を確認する。直接受領や `official_close: null` にはこの引数を付けない。保存済みの `render`・`check-render`・`proposal-show` にも不要。

```bash
uv run --offline python -m tools.experiments.us_research_report draft-digest \
  /private/draft.json --sip-proof /private/sip-receipt.json /private/sip-original.json
```

本番でSIPを使う判断を初めて保存する前に、対応するApp読取・配信処理と認証Webを先に反映する。実際の受入では同じ保存判断のID・正式原稿SHAと「Nasdaqの無調整公式終値（Alpaca SIP経由）」の表示を照合する。隔離検証やこの手順だけでは実データの受入・本番配信を証明しない。
