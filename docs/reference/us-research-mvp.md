# 米国株の固定受領から調査候補を作る

ここでは固定受領と隔離Appを使い、米国株の観測・調査・査読済み判断を試す。調査CLIは固定したAlpaca IEXの日次集計とSECの指定期間の売上を読み、次に調べることと保留理由を標準出力へ返す。同じ経路を合成fixtureで試せる。本番の査読済み判断は[Webの「売買提案」→「米国株」](../../web/README.md#表示の意味)で読む。ここでのCLIは本番配信・日次選抜・注文に接続しない。

リポジトリのルートで[開発環境を準備](../development.md#手元で始める)してから実行する。`--offline` は依存を初回取得しない。

```bash
uv run --offline python -m tools.experiments.us_research_mvp
uv run --offline python -m tools.experiments.us_research_mvp --bundle /private/manifest.json
```

`research_candidate` は調査を進められる状態。幅や成長を証明するシグナルではない。`hold_for_evidence` は証拠保留で、保有継続を指さない。どちらにも出る `売買: no_action` は「この観測CLIは売買判断を出さない」という固定表示で、査読済みの「見送り」ではない。正式な判断は[別の手順](#隔離appに米国株の見送り判断保留を保存する)で記録し、比較に必要な根拠が欠ければ `defer` とする。終了コード0は報告を出せたことだけを示し、証拠の充足や実データ受入を意味しない。引数やmanifest自体が不正なら2。IEX分割調整済みの**観測20日分**だけで平均と乖離を計算する。NY日付で当日受領しかない足は途中集計として除き、後日同じ束を再読しても採用しない。市場の取引暦が揃ったとはみなさず、公式引けや約定可能値、売買代金を作らない。

## USDの調査・比較を査読して保存する（正式提案は発行しない）

取得した材料から人間が書いた一社の分析を、[ローカルの調査レポートCLI](../../tools/experiments/us_research_report.py)で別担当の確認結果と一緒に新規の私有ファイルへ保存する。App、案件完了、提案の配信・注文には接続しない。既存のIEX・SECの読取や再取得はしない。実データが不足していれば判断保留として記録できる。

1. JSON原稿の例は[合成テスト](../../tests/tools/test_us_research_report.py)の `_draft`。原稿に銘柄・上場市場（NasdaqまたはNYSE）・株式クラス・CIK・USD、情報締切と作成時刻を記録する。取引所の暦で対象立会日と終了時刻を照合し、短縮日を通常の引けで補わない。
2. 価格の基準は[Alpaca SIPの無調整日足終値](#alpaca-sipの日足終値で比較する)とし、公式終値を必須にしない。公式値を使う場合は無調整の上場取引所値を選んで出典を別に示す。IEX観測帯は任意の分割調整済み参考値で、比較価格に代用しない。価格か他の比較根拠が不足なら `comparison: null` と未確認事項。
3. 財務では原典と実読時刻、会計期間・観測内容を引用。比較が可能な場合だけ、同じ終期で基本／下振れの1株価値・取得権のある分配・仮定を示す。無利息のUSD現金との差を計算し、税・費用・為替は対象外。未確認の残高・執行条件・買付数量は入れない。別担当は出典の本文、銘柄と権利、期間、反対材料と比較前提を確認する。このCLIはリンク先を取得せず、根拠の真偽や別担当の本人性を認証しない。

新規の `draft-digest` と `save` には、同じ非公開の現行銘柄ファイルと、認可済みの受領を固定したNY暦を渡す。新規対象は対象日が合うUS区分の **上場中の銘柄ID × 楽天普通株対象 × 買付制限なし** であることを確認する。これは注文受付の確認ではない。通常・短縮立会を区別し、休場の繰り越しは暦が当日まで連続して確認できる場合だけ。立会前の新規調査で前回の終値を使う場合も、同一銘柄ID・上場市場がその価格日に成立している必要がある。原稿の情報締切を後から前倒しして古い現行値を採用しない。Digestには検証した銘柄IDと対象立会が含まれ、同じティッカーでも別銘柄への変化には新規査読が必要。米国株のSEC発行体や種類の照合は別の根拠で行う。

`--calendar-proof` は作業用の私有JSON。認可済みのAlpaca Paper Trading Calendarから実受領した `start`・`end`（NY日付）、`request_url`・`response_url`、`status`、全文受領の `received_at`（時差付き）、`complete`・`redirected`、UTF-8の応答全文をそのまま表す文字列 `body` とその `sha256` を指定する。正規の `date_type=TRADING` の完全な受領だけを既存の暦処理で照合する。このCLIは認証情報を得ず、通信もせず、暦の別保管を作らない。SIPデータの認可だけから同APIの認可を推定しない。どちらかがない場合、新規原稿の承認は止める。保存済み原稿の `render` / `check-render` の意味は変えない。

以下は[固定受領の日足](#alpaca-sipの日足終値で比較する)を使う例。日足を原稿に記録しない保留や取引所からの直接受領では `--daily-close-proof` とファイル名を外す。SIP経由で取引所公式値を使う場合は[別の原本と引数](#nasdaqの公式終値を固定受領から読む)を使う。

```bash
uv run --offline python -m tools.experiments.us_research_report draft-digest \
  /private/draft.json --daily-close-proof /private/sip-bars-capture.json \
  --current-listing-master "$CURRENT_LISTING_MASTER" --calendar-proof "$US_CALENDAR_PROOF"
# 別担当が、出力の report_id / draft_sha256 と reviewer_identity / reviewed_at
# （時差付き）/ conclusion: "pass" / notes をJSONに記入してから保存する。
NEW_REPORT=/private/report.md
uv run --offline python -m tools.experiments.us_research_report save \
  /private/draft.json /private/review.json \
  --daily-close-proof /private/sip-bars-capture.json --output /private/reviewed.json \
  --current-listing-master "$CURRENT_LISTING_MASTER" --calendar-proof "$US_CALENDAR_PROOF" &&
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

私有レポートの保存とは別に、USD建ての一社の判断を[米国株の保存owner](../../engine/src/baibai_engine/investment_judgment/us_proposal.py)で記録する。判断保留（`defer`）は根拠未確定でも理由と見直し条件を明示する。見送り（`no_action`）は採用した無調整価格と同額のUSD現金を比べ、その上で資金を投じない理由がある場合だけ。比較の終期は判断の作成日・保存日（いずれも米東部時間）より後とし、過ぎた比較を新規発行に使わない。保存済み判断の読戻しには現在時刻を使わない。IEXで埋めない。数量、売買条件、口座や円換算は扱わず、日本株の提案・進捗・台帳への書込も行わない。

現在の判断として見送りを新規発行するときは、比較の基準価格が対象取引所の直近の完了立会のものか、分析の締切後に判断を変える公表・権利変更がないかを査読で照合する。発行時刻だけでは、価格や根拠の鮮度は確認できない。重要な根拠が足りなければ `defer` とし、会社IRなどの確認先と再検討の条件を `revisit_when` に記す。

新規の `proposal-digest` と `proposal-save` の各回に、現行正本から取り直した作業用銘柄ファイルと、その時点まで確認できる暦の受領を渡す。どちらの判断も、査読済み分析の銘柄ID、楽天普通株対象、買付制限、上場市場、対象立会と直近の完了立会を現在時刻で再照合する。保存ではAppの書込ロック取得後に再照合する。同じ立会・同じ銘柄の再取得は査読を変えない。対象外・銘柄変更・立会切替・証拠不足、または銘柄IDを結び付けていない旧分析では新規判断を止める。CLIは非通信で、渡されなかった新しい変更は検知しない。既存判断の読戻しにはこれらの入力を要求しない。

査読済みの分析を用い、[判断原稿と査読の例](../../tests/tools/test_us_research_report.py)に沿って独立した提案本文の査読を取る。`proposal-digest` が出す提案IDとSHAに、見送りか保留か、理由、見直し条件、元の分析・その査読を結び付ける。分析の査読を提案の査読として使い回さない。査読済み分析の内容はApp内に保存され、読戻しでは分析と提案それぞれが査読に結び付いていることも確認する。元ファイルや再描画は不要で、同IDの差替えや欠損・破損した最新判断から別判断へのfallbackは行わない。

```bash
uv run --offline python -m tools.experiments.us_research_report proposal-digest \
  /private/reviewed.json /private/decision.json --daily-close-proof /private/sip-bars-capture.json \
  --current-listing-master "$CURRENT_LISTING_MASTER" --calendar-proof "$US_CALENDAR_PROOF"
# 別担当が正確な提案IDとSHAを含む decision-review.json を作成した後、現行の入力を再取得。
uv run --offline python -m tools.experiments.us_research_report proposal-save \
  /private/reviewed.json /private/decision.json /private/decision-review.json \
  --daily-close-proof /private/sip-bars-capture.json --db "$APP_COPY" \
  --current-listing-master "$CURRENT_LISTING_MASTER" --calendar-proof "$US_CALENDAR_PROOF" &&
uv run --offline python -m tools.experiments.us_research_report proposal-show \
  --db "$APP_COPY" --proposal-id "$PROPOSAL_ID"
```

判断原稿の任意の `model_perspectives` に、日本株と同じ[七観点](stock-assessment.md)ごとの**提案作者の判断**を記録できる。各行の `model`、`assessment`、`summary` と `sources` は提案査読の対象であり、認証Webへ配信する。Webの履歴に載せる要旨は480文字、代表出典は各観点4件までとし、私有の調査原稿とは分ける。出典は公開してよい資料名・該当箇所（`label`、180文字まで）と、情報締切までにその資料を実際に読んだ時差付き時刻（`observed_at`）を指定する。URL・認証情報・私有調査メモを書かない。支持・相反・逆風・中立には根拠が必要。確認したが不足した場合と対象外の場合は、理由を残し、確認できた出典だけを書く。未記録の観点は未記録のままにし、IEX・財務資料の存在や日本株の観測から賛否を作らない。例は[査読から配信までのテスト](../../tests/tools/test_us_research_report.py)。

保存時と同じ提案ID・提案原稿SHA・判断を読戻して照合する。 `--proposal-id` を省略すると公開時刻の新しい米国株判断を1件だけ読む。現行Appに該当行がないときは `null`（終了1）。DB不在、異なるschema・読めない行はエラー（終了2）。両者を混同しない。この入口は隔離Appでの保存検証であり、R2転送・公開Web・broker発注・台帳記帳の完了ではない。

### 隔離Appを用意する

新規の隔離検証は現行コードで `baibai-engine db init --db "$APP_COPY"` を実行し、空のAppを使う。v31への本番切替は完了済み（[#3012](https://github.com/koumatsumoto/baibai-loop/issues/3012)）。通常の調査や表示のために旧DBを取得・移行する必要はない。

<a id="us-iex-live-acceptance"></a>
## Alpaca Paperの実接続を確認する

1. [GitHub ActionsのリポジトリSecrets](https://github.com/koumatsumoto/baibai-loop/settings/secrets/actions) に、Alpacaの同じPaper口座の鍵を `APCA_API_KEY_ID` と `APCA_API_SECRET_KEY` として登録する。Variables、実行入力、チャット、コミットには入れない。Live鍵やURLの設定は不要。
2. [IEX公式暦](https://www.iex.io/resources/trading/trading-hours-holidays) で対象立会日を確認し、米東部の翌日以降に `main` の [us-iex-acceptance](../../.github/workflows/us-iex-acceptance.yml) を手動で実行する。

```bash
gh workflow run us-iex-acceptance.yml --repo koumatsumoto/baibai-loop --ref main \
  -f expected_date=YYYY-MM-DD
```

取得は [Alpaca Market Dataの日足](https://docs.alpaca.markets/us/reference/stockbars) のIEXに固定し、口座・注文用のAPIは呼ばない。AAPL・GOOG・GOOGL・NVDAの4銘柄すべてで指定日の観測がそろえば成功。鍵がない、認証できない、4銘柄がそろわない場合は失敗として扱い、SIPや別日の足で埋めない。短縮日も米東部の日付判定を変えない。

この確認ジョブは日本株の日次に接続せず、R2・App・Web配信を変更しない。鍵・価格・原本はログやArtifactsに残さない。成功は指定日のIEX観測を確認したことだけを示す。公式終値、価格の保存・再配布の許諾、SEC連携や売買提案の受入とは別で、下の固定受領用manifestも作らない。

## 最新のIEX観測だけを確認する

運用受入では対象市場の公式暦で期待するNY取引日（休場・短縮日を含む）を確認し、実受領のmanifestと一緒に明示する。

```bash
uv run --offline python -m tools.experiments.us_research_mvp --bundle /private/manifest.json --accept-iex-session YYYY-MM-DD
```

各銘柄で、cutoffまでに受領し、かつその足のNY翌日以降に受領した最新観測が指定日と一致する場合だけ終了コード0。有効なmanifestでの受領チェーン不備・観測欠落・日付不一致は1。cutoffを書き換えても、取引日当日に受領した足は採用しない。暦の推定、SEC・調査候補判定、公式引けや売買可否の確認はしない。通常の調査は `--accept-iex-session` を付けずに実行する。

## 許可済みの実受領を束ねる

固定受領を読む調査CLI自体はネットへ接続しない。許可済みの収集CLI/transportがある場合だけ、収集と再読を分ける。Alpacaは `DailyBarsQuery(("AAPL",), start, end, "-", "split").url(page_token=...)` が作る [v2日足](https://docs.alpaca.markets/us/reference/stockbars) を使用。`1Day`、`iex`、`USD`、`split`、`asof=-`、`limit=10000`、`sort=asc`。評価日前のNY日を使う。native `bars` と明示 `next_page_token` を含む本文を最終ページまで整形せずに保存する。各ページの取得時に実要求URL・最終応答URL・リダイレクト有無・全文受領を記録する。IEXは[要求の `feed`](https://docs.alpaca.markets/us/docs/market-data-faq#whats-the-difference-between-iex-and-sip-data)で指定するもので、日足本文からは判別しない。固定URLへの直接の完全な応答が確認できなければ、価格だけ保留する。記録されたヘッダーが部分応答、圧縮、または原本と矛盾する長さ・転送形式を示す場合は、HTTP 200・全文読了の申告だけで採用しない。記録されていないヘッダーは要求しない。CLIの整形表、ページを合成したJSON、再保存時刻による代用は使わない。

指定CIKのSEC [Submissions](https://www.sec.gov/search-filings/edgar-application-programming-interfaces#data-sec-gov-submissions) の原本から、Alpacaの銘柄ラベルが同じ発行体に明示されているかを照合する。未受領・受領遅れ・不一致なら日足の観測だけ残し、その発行体の財務とは結合しない。申告ラベルだけでは採用しない。この照合は現在の銘柄表記とCIKまでで、取引所・株式クラス・過去の名称変更まで証明しない。最初の実データ受入では、採用した提出書表紙のSection 12(b)で銘柄と市場・株式クラスが対応する行を別担当が照合する。同一企業のGOOG/GOOGL等は別の株式クラスとして扱い、財務は発行体単位のまま読む。証拠が揃わなければ実データ受入は保留する。

SECは指定CIKの [CompanyFacts](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) の原本と、別途固定した提出証拠を用意する。`published_at` はそのaccessionの独立した初回公表時刻が確認できた場合だけ。SECの受付時計や取得時刻では埋めない。初回公表時刻が不明でも、SECの該当提出索引を実際に受領していれば、`published_at` と `publication_ref` をnullのまま置き、任意の `filing.public_index` に正規 `<accession>-index.htm` の原本を指定できる。報告に示す公開確認時刻は、その索引の受領時刻であり初回公表ではない。公開を見た時刻の申告は、採用・保留のどちらの根拠にも使わない。時点証明が足りない場合は保留し、代替conceptを勝手に合算しない。

採用する売上は、提出書の対象列と照合した開始日・終了日を固定する。通常四半期は10-Qまたは10-Q/Aの単独四半期で、期間は両端を含む89〜98日（連続3暦月、13〜14週を含む）。明示した10-Kは52週・53週または暦年相当の通期売上として区別し、10-K/Aや変則決算は対象外。期間長だけでは会計上の区分を証明しない。同じ10-Q、同じ期末日でも半年・9か月累計や範囲外の変則期は保留し、差分で単独四半期を作らない。最新の提出は自動で選ばず、報告には採用期間を表示する。

採用できる提出索引があれば、索引の `Period of Report` と指定期末日 `period_end` が一致する場合だけ当期を採用する。同じ提出の前年列を当期には使わない。`--compare-sec` の前年列は従来どおり比較に使う。売上単体・四半期比較で独立公表証拠だけを使う場合は、提出書の対象列との照合は入力作成側で行う。

同じ10-Qまたは10-Q/Aの発行体財務だけを比較する例（会計四半期はQ1・Q2・Q3）。

```bash
uv run --offline python -m tools.experiments.us_research_mvp --bundle /private/manifest.json \
  --compare-sec <CIK> Q3 <PRIOR_Q_START> <PRIOR_Q_END> <CURRENT_FY_START> <PRIOR_FY_START>
```

終了コード0でも、報告に不足・未採用を含み得る。当期の日付はmanifestの期間を使う。会計四半期と残りの日付は提出書の比較列で確認して指定する。指定四半期と一致し、四半期の開始・終了と期首それぞれの間隔が364〜371日の前年比較だけを対象にし、売上・営業利益は単独四半期、営業CF・有形固定資産の現金取得額は期首来累計のUSDで出す。原本の符号を変えず、欠測は補わない。前年同期差は同じ科目定義の金額だけを比較し、前年比は前年値が正の場合だけ表示する。差額が正確に表せない場合は原額と未算出理由を残す。設備取得は原額のみ。株価・候補判定・一株価値には接続しない。

通常の10-Kでは、同じ提出書の当期と直前の**連続する通期**を比較できる。10-Kには過去3年度の数値が載り得るため、通期比較は実受領した提出索引の `Period of Report` と当期末の一致を必要とする。公表時刻だけでは当期を確定しない。通期の日数が異なる52週／53週でも報告額のまま比較し、年率換算、差引の第4四半期、FCFや成長モデルの判定は出さない。

```bash
uv run --offline python -m tools.experiments.us_research_mvp --bundle /private/manifest.json \
  --compare-sec <CIK> FY <PRIOR_FY_START> <PRIOR_FY_END>
```

取得toolでは許可済みの秘密情報をプロセスにだけ渡し、鍵、Authorizationヘッダー・コマンドログを保存しない。実際に本文まで受領した時刻とHTTP状態を記録し、SHA-256はその原本bytesから作る。送信時刻やこのCLIの実行時刻を受領にしない。必要な権限または接続がない場合、実データで合格したとは扱わない。

### 入力例

通常の調査は1〜3銘柄。`--accept-iex-session` は、最大4銘柄を一括指定した受領も、元の銘柄・順序と全ページを保ったまま確認できる。`file` はmanifestと同じディレクトリのファイル名。下の値は形状の例で、実受領ではない。

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

## Alpaca SIPの日足終値で比較する

通常の比較価格はAlpaca SIPの **無調整の日足終値（USD/株）**。引け後に受領した時点の値であり、後日の訂正がないことや取引所の公式終値・約定値を示さない。NYSE・Nasdaqどちらにも使えるが、日足は上場市場・株式クラスの証明ではない。別に確定した銘柄マスターと短縮日を含む立会暦を使い、元の銘柄ラベル・対象NY立会日・引けの後の実受領を照合する。出来高がゼロなら価格を採用しない。引け後に受け取った対象日足が使えれば、NY翌日まで待つIEX診断の制約は使わない。

許可された用途で取得した `DailyBarsQuery(..., "-", "raw", feed="sip")` の完全な受領を使う。[固定受領](#許可済みの実受領を束ねる)のAlpaca部分（`request_id`、`start`、`end`、`pages`）と要求順の `symbols` を一つのJSONにし、元の全文をJSONと同じディレクトリに置く。`sip_daily_close_fields` で対象の観測を原稿の `daily_close` へ写す。比較に使う価格は一種類。日足の場合は `official_close`、IEX観測を使わない場合は `iex_band` を省略できる。詳細は[取得から保存・読戻しの合成例](../../tests/tools/test_us_research_report_daily_close.py)。

上記の新規作成・保存の各回に、同じ固定受領JSONを `--daily-close-proof` で渡す。保存後の読戻しに原本は不要。IEX、最終約定、分割調整後、別日・別株式クラス、取引所の公式値に読み替えない。日足を取得できなければ価格を埋めず、保留理由と見直し条件を記す。第三者への配信はデータ利用条件の確認が別に必要で、認証Webであることだけでは許可にならない。

### 現行対象の日足を作業用に取得する

既に許可されたSIP Market Dataの `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` と、別に確認したPaper Calendarの `APCA_PAPER_API_KEY_ID` / `APCA_PAPER_API_SECRET_KEY` を実行環境へ渡す。新たに照会するのは固定した現行銘柄マスターの対象だけ。 `--asof` を指定する場合は暦で確認した最新の引け済みNY立会日を指定し、異なれば取得しない。

```bash
uv run python -m baibai_jobs.integration.source_handoff acquire \
  --source alpaca-us-daily --current-listing-master "$CURRENT_LISTING_MASTER" \
  --directory "$RUNNER_TEMP/us-daily-prices"
```

同じ run の私有作業ディレクトリに、`result.json`、`calendar.json` と必要な応答原本・`batch-*/capture.json` を出す。欠測・未確認を区別し、全対象に適格な足が揃わなければ終了コードは非0。受領が完了した一部の原本は対応する既存の `--daily-close-proof` で使える。この引渡しから初回原稿を作成・保存するときは、同じ `result.json`・元の価格原本・取得に使った銘柄マスターを組で渡す。この入口はSQL staging・外部への配信・定期起動・L2や提案の発行をしない。

## Nasdaqの公式終値を固定受領から読む

Alpaca SIP経由でNasdaqの公式値を比較価格に採用する場合だけ追加する。対象はNasdaq上場（XNAS）で、同一銘柄・株式クラス・NY立会日とその終了時刻を別の証拠で確認済みのもの。[UTPのQ/M](https://www.utpplan.com/DOC/UtpBinaryOutputSpec-4.1b.pdf)に当たるNasdaqの無調整公式終値であり、取引所からの直接受領、約定値、統合終値、IEX観測ではない。

**SIPを取得してよいかは、価格の検証より先に確認する。** Alpacaの[Paper Only口座の説明](https://docs.alpaca.markets/us/docs/paper-trading)では、受領・利用できるのはIEXのデータだけ。[一般の市場データFAQ](https://docs.alpaca.markets/us/docs/market-data-faq)が説明する「履歴の終了時刻が15分以上前」という条件は、その口座にSIPの利用を許すものではない。利用する口座・用途でSIPの許諾が確認できない場合はSIPを取得せず、IEX日足を調査用観測としてだけ使う。公式値は別途確認できた場合だけ記録する。公式値がなくても、適格なSIP日足を使う比較では不要。

[銘柄別のオークションAPI](https://docs.alpaca.markets/us/reference/stockauctionsingle-1)はSIP専用。通信成功と、その価格を第三者に表示できるかは別である。[Alpacaの再配布方針](https://alpaca.markets/support/redistribute-alpaca-api)に従い、配信先・用途に対応する許諾が確認できない限り、取得した価格や原本を第三者に配信しない。認証付きWebであっても、認証がその許諾の代わりにはならない。

1. 許可済みの取得手段で、`NasdaqOfficialCloseQuery.url()` が作る[Alpaca SIPの要求](https://docs.alpaca.markets/us/reference/stockauctionsingle-1)を使う。単一銘柄、同じNY日内で立会終了を含み、それより後までのUTC秒単位区間、`feed=sip`、`currency=USD`、`asof=-`。実行時に確認した `request_url`・`response_url`（両方とも固定URLと一致）、`status: 200`、`complete: true`、`redirected: false`、全文を受領した `received_at`（`Z` または有効な `±HH:MM`、`-00:00` は不可、小数秒6桁まで）と原本バイト列の `sha256` を受領JSONに残す。応答本文は整形せず別の私有ファイルに保存する。HTTP 200やSHAだけから全文受領・リダイレクト有無を推定せず、欠けた記録は後から補わない。鍵・認証ヘッダーは保存しない。
2. 要求区間の終端以降かつ情報締切以前に受領した、`next_page_token: null` が明示された同日・同銘柄の応答を使う。引けイベント全体でQ/Mが一つだけで、その発生時刻が立会終了から要求区間の終端まで（両端を含む）にある場合だけ採用する。[合成入力例](../../tests/tools/test_us_research_report_sip.py)のように、`read_nasdaq_official_close` の結果を `sip_official_close_fields` で原稿の `official_close`（`origin: "alpaca_sip_nasdaq_official_close"`）に写す。発生時刻は原文の精度を保持し、全文受領時刻とは分ける。証拠が不足する場合は公式値を埋めない。適格な日足に基づく比較とは分ける。
3. 上の手順どおり別担当の分析査読と判断査読を取り、`draft-digest`・`save`・`proposal-digest`・`proposal-save` の**各回**に同じ受領JSONと本文を渡す。CLIはオフラインで原本・範囲・時刻と原稿の値を再照合する。隔離Appへの保存後に `proposal-show` で同じ提案ID・正式原稿SHA・出典を確認する。直接受領や `official_close: null` にはこの引数を付けない。保存済みの `render`・`check-render`・`proposal-show` にも不要。

```bash
uv run --offline python -m tools.experiments.us_research_report draft-digest \
  /private/draft.json --sip-proof /private/sip-receipt.json /private/sip-original.json \
  --current-listing-master "$CURRENT_LISTING_MASTER" --calendar-proof "$US_CALENDAR_PROOF"
```

本番でNasdaqのSIP経由公式終値を使う判断を初めて保存する前に、対応するApp読取・配信処理と認証Webを先に反映する。実際の受入では同じ保存判断のID・正式原稿SHAと「Nasdaqの無調整公式終値（Alpaca SIP経由）」の表示を照合する。隔離検証やこの手順だけでは実データの受入・本番配信を証明しない。
