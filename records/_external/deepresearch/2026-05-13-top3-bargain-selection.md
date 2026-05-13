# 2026-05-13 お買い得銘柄 3 銘柄選定 deepresearch

retrieved_at: 2026-05-13T08:50:00+09:00
status: ok

## Scope

ユーザー依頼は「最新コード、現時点の情報、今までのルールを使い、3 銘柄まで絞って PR で確認できるようにする」。本メモは、機械 selection の上位をそのまま採用せず、一次情報、決算イベント、長期保有耐性、短期チャンス、スクリーニング偏重を重ねて確認した記録。

## Inputs

- branch: `research/select-bargains-20260513`
- code commit: `6976992`
- candidates: `records/04-candidates/2026/05/2026-05-08.yaml`
- outlook: `records/03-outlook/2026/05/outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml`
- current daily brief: `records/02-brief/2026/05/2026-05-13-world-daily-us-cpi-boj-opinions.yaml`
- select command: `uv run baibai-loop-screening select --asof 2026-05-08 --candidates records/04-candidates/2026/05/2026-05-08.yaml --outlook records/03-outlook/2026/05/outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml --top 50`

## Data Availability Check

`baibai-loop-screening verify-cache-coverage --asof 2026-05-13` は、J-Quants daily bars / financial summaries / earnings calendar / market calendar / EDINET metrics / JPX regulation の coverage 不足を返した。`bootstrap-cache --asof 2026-05-13` は応答が返らず停止したため、現在の canonical candidates は 2026-05-08 を使った。これは今回の制約であり、現時点の IR / macro は一次情報で手動補完した。

## Mechanical Selection

2026-05-08 candidates は 369 件。outlook で `supportive` / `neutral` の業種に絞ると 304 件。`select` の research queue は lane 分散で次を返した。

| order | ticker | name | selection_lane | primary screen evidence |
| ---: | --- | --- | --- | --- |
| 1 | 3668 | コロプラ | strict-net-cash-discount | net cash / market cap 99.0%、cash / market cap 85.8%、EV/EBITDA 0.4 |
| 2 | 6835 | アライドテレシスHD | fcf-yield-discount | FCF yield 22.0%、OCF yield 23.9%、net cash / market cap 37.9% |
| 3 | 3632 | グリーホールディングス | strict-net-cash-discount | cash / market cap 111.1%、net cash / market cap 52.7% |
| 4 | 6310 | 井関農機 | cashflow-yield-discount | OCF yield 56.5%、P/S 0.22、PBR 0.55 |
| 5 | 9470 | 学研HD | sales-discount-growth | P/S 0.22、sales YoY +6.0%、forward PER 10.48 |

この段階では、3668 / 3632 が balance-sheet cheap として強く、6835 / 6310 / 9470 が cash flow / sales discount として続く。ただし、この queue は「安い状態が続く銘柄」に寄り、直近 1 週間の急落や決算後の過剰反応を十分に拾えない。そこで Yahoo Finance の東証値下がり率（日次 2026-05-12、週次 2026-05-08）を別レーンとして確認し、既存 candidates と突合した。

## Fast-Moving / Oversold Lane Check

ユーザー指摘どおり、現行 screen は保有資産、net cash、OCF、低 P/S のような slow-moving variable に寄りすぎる。直近の価格変化を別レーンとして見ると、2026-05-08 週次値下がり率では 8255 アクシアル リテイリングが -10.04% で、かつ 2026-05-08 candidates にも cashflow-yield hit として存在した。

Yahoo Finance:

- 東証週次値下がり率 `term=weekly`: https://finance.yahoo.co.jp/stocks/ranking/down?market=tokyoAll&term=weekly
- 8255 quote: https://finance.yahoo.co.jp/quote/8255.T

8255 の候補 snapshot:

- OCF yield 18.1%、FCF yield 5.7%、PBR 0.93、P/S 0.32、net cash / market cap 23.9%、自己資本比率 66.1%。
- 2026-05-12 の Yahoo Finance 参考指標では前日終値 1,001 円、時価総額 936 億円、PER 11.08、PBR 0.93、配当利回り 2.90%、年初来安値 995 円。

これは「ただ安い」ではなく、決算発表後に売られたが、公式決算で business quality と shareholder return が確認できる候補。よって、既存保有確認に近い 9470 より、今回の 3 枠に入れる価値が高い。

## Current Macro Check

- BLS April 2026 CPI: all items +0.6% MoM SA / +3.8% YoY NSA、core +0.4% MoM / +2.8% YoY、energy +3.8% MoM / +17.9% YoY。
- 日銀 2026-04-27/28 主な意見: 中東情勢、原油価格、物価上振れ、利上げ継続または現状維持を巡る複数意見を公表。次回以降の利上げ判断が十分あり得るとの意見もある。
- 読み替え: 金利・油価・イベントリスクが高い局面なので、短期の「割安」は business deterioration とイベント直前リスクで割り引く。長期保有できない銘柄を「安い」だけで採らない。

## Company IR / Event Check

### 3668 コロプラ

公式 IR:

- Q2 tanshin: https://ssl4.eir-parts.net/doc/3668/tdnet/2799795/00.pdf
- Q2 material: https://ssl4.eir-parts.net/doc/3668/ir_material_for_fiscal_ym7/202716/00.pdf
- IR announcement JSON: https://ssl4.eir-parts.net/V4Public/eir/3668/ja/announcement/announcement_25.js

確認事実:

- FY2026 Q2 cumulative sales 10,088 百万円、YoY -28.2%。
- Operating profit 533 百万円、YoY -62.3%。
- Operating cash flow は -82 百万円。
- Cash and equivalents は 43,300 百万円で FY-end から 2,348 百万円減。
- Equity ratio 92.5%。

判定: net cash は極めて厚いが、営業面は悪化。機械 screen が拾った「安さ」は、事業回復ではなく cash pile に偏っている。今だけのチャンスというより、現時点では value trap 反証が強い。最終 3 から除外。

### 3632 グリーホールディングス

公式 IR:

- Q2 tanshin: https://ssl4.eir-parts.net/doc/3632/tdnet/2750870/00.pdf
- Q2 material: https://ssl4.eir-parts.net/doc/3632/ir_material_for_fiscal_ym12/197654/00.pdf
- IR announcement JSON: https://ssl4.eir-parts.net/V4Public/eir/3632/ja/announcement/announcement_24.js

確認事実:

- FY2026 Q2 cumulative sales 25,472 百万円、YoY -10.7%。
- Operating profit 1,468 百万円、YoY -30.6%。
- Game segment sales -19.3%、investment segment sales -35.8%。
- REALITY / DX は増収だが、全社の sales / OP decline を相殺しきれていない。
- 公式 calendar は FY2026 Q3 決算発表を 2026-05-13 としている。

判定: cash / net cash は強いが、当日決算イベントと主要収益低下が同時にある。短期 alpha は Q3 の内容次第で、現時点で 3 枠に入れる根拠は弱い。最終 3 から除外。

### 6835 アライドテレシスHD

公式 IR:

- IR information: https://ir.at-global.com/information
- stock information: https://ir.at-global.com/stock

確認事実:

- 会社公式お知らせで 2026-05-15 15:30 に FY2026 Q1 決算発表予定。
- 2026-05-08 candidates では FCF yield 22.0%、OCF yield 23.9%、net cash / market cap 37.9%、PER 9.76、EV/EBITDA 3.3。
- 100 株単元の想定 notional は 3 万円弱で、イベント通過後の starter として実資金制約に合う。

判定: 最終 3 に採用。ただし 5/15 Q1 前に新規注文はしない。Q1 で FCF / OCF / net cash thesis が壊れなければ、最優先の post-event starter。

### 6310 井関農機

公式 IR:

- IR calendar: https://www.iseki.co.jp/ir/support/calendar/

確認事実:

- 会社公式 IR カレンダーで 2026-05-15 に FY2026 Q1 決算発表と決算説明会予定。
- 2026-05-08 candidates では OCF yield 56.5%、P/S 0.22、PBR 0.55、sales YoY +10.3%。
- 一方で net cash / market cap は -118.8%。net debt が大きく、金利・在庫・運転資本に弱い。

判定: 最終 3 に採用。ただし 1 単元が 17 万円台で、Q1 前のイベントまたぎは risk / return が悪い。Q1 で運転資本の巻き戻りがなければ、最も upside の大きい再評価候補。

### 8255 アクシアル リテイリング

公式 IR:

- FY2026 result page: https://www.axial-r.com/2026/05/07/5737
- FY2026 tanshin: https://ctr.axial-r.com/wp-content/uploads/2026/05/07113156/r2603jp.pdf
- Progressive dividend policy: https://www.axial-r.com/2026/05/07/5735
- Long-term shareholder benefit: https://www.axial-r.com/2026/05/07/5733
- Yahoo quote: https://finance.yahoo.co.jp/quote/8255.T

確認事実:

- FY2026 sales 295,536 百万円、YoY +4.8%。
- Operating profit 12,185 百万円、YoY +1.0%。Ordinary profit 12,799 百万円、YoY +0.7%。Sales / OP / ordinary profit は連結会計年度として過去最高。
- Operating cash flow 16,939 百万円、前期 11,815 百万円から増加。
- Equity ratio 66.1%、cash and equivalents 30,436 百万円。
- FY2027 company plan は sales +1.5%、OP -4.0%、ordinary -6.2%、parent net income -9.1%。
- 2026-05-07 に累進配当導入を公表。FY2027 以降 5 年間、前期水準維持または増配を原則。
- 2026-05-07 に長期保有株主優待制度の新設を公表。

判定: 最終 3 に採用し、順位 1 に上げる。理由は、今回の screen 偏重を補正する「直近急落 + 公式決算確認済み + long-hold fallback」枠だから。営業利益の伸びは強くないが、PBR 1 倍割れ、net cash、累進配当、長期保有優待が下値を支える。1030 円以下なら 300 株 starter。

### 9470 学研HD

公式 IR:

- IR calendar: https://www.gakken.co.jp/ja/ir/calendar.html
- prior official snapshot: `records/_external/gakken/2026-05-08-fy2026-q1-official-ir.md`

確認事実:

- 会社公式 IR カレンダーで 2026-05-15 に FY2026 Q2 決算発表、2026-05-21 に Q2 説明会予定。
- FY2026 Q1 公式 IR では売上 +6.0%、EBITDA +38.9%、営業利益 +85.7%、経常利益 +120.2%。
- 親会社株主帰属利益は -50.4%。前期特別利益の反動と投資有価証券評価損が理由。
- 2026-05-08 candidates では P/S 0.22、sector gap -90.2%、forward PER 10.48。

判定: 既存 hold の review 対象として残すが、今回の新規 3 枠からは外す。理由は、すでに 2026-05-08 に approved memo があり、情報・通信業の sector concentration warning も出ているため。追加買いは Q2 後。

## Final Top 3

| rank | ticker | name | current posture | reason |
| ---: | --- | --- | --- | --- |
| 1 | 8255 | アクシアル リテイリング | 1030 円以下で 300 株 starter | 直近急落、PBR 1 倍割れ、OCF yield 18.1%、net cash、累進配当、長期保有優待。今回の「速い変化」補正枠 |
| 2 | 6835 | アライドテレシスHD | Q1 後に starter | FCF / OCF / net cash が同時成立し、1 単元が小さい。事業悪化確認前の cash pile 銘柄より質が高い |
| 3 | 6310 | 井関農機 | Q1 後に starter | OCF yield と P/S/PBR の upside は最大。ただし net debt と Q1 直前で即時買いは避ける |

## Not Selected

- 3668: cash は厚いが、最新 Q2 の sales -28.2%、OP -62.3%、OCF negative を重く見て除外。
- 3632: cash は厚いが、Q3 が当日イベントで Q2 sales / OP decline。除外。
- 3964: FCF / OCF は良いが、5/12 research で半期 CF data gap と Q2 待ちを既に記録。今回の 3 枠には入れない。
- 9470: 既存 hold として Q2 後に review。新規 3 枠では、直近急落レーンの 8255 を優先。
- 5410 / 5423: 鉄鋼の PBR / cash は安いが、sales decline / cyclicality / oil and rate environment を考えると、今回の「長期保有しても大丈夫か」条件に対して優先度を下げる。
- 6619 / 6753: valuation-reversion 単独寄りで、今回の cash quality / business durability 条件を満たす確認が弱い。

## Screening Process Findings

今回の選定で、スクリーニングが同じ型に寄る構造的な原因を確認した。

1. Candidate universe が高安定。2026-05-01 candidates 366 件、2026-05-08 candidates 369 件で、ticker overlap は 325 件。週次の price / financial update だけでは候補集合がほぼ変わらない。
2. Lane hit が cashflow / sales / valuation に集中。2026-05-08 は cashflow-yield 202 件、sales-discount 134 件、valuation 80 件。FCF は 13 件、strict net cash は 21 件。似た financial cheapness が繰り返し出る。
3. `select` は `research_selection_lane_order` の固定順で、各 lane のトップを 1 銘柄ずつ拾う。2026-05-01 selection は 3632 / 6835 / 6932 / 6310 / 9470、2026-05-08 は 3668 / 6835 / 3632 / 6310 / 9470。5 枠中 4 枠が再登場した。
4. `select` は research outcome を覚えていない。deferred / event pending の 6835 / 6310 / 3632 が次回も通常候補として再登場する。
5. event risk は selection 前に十分に効かない。`next_earnings_date` が null の候補でも、公式 IR では 5/13 / 5/15 決算が確認された。
6. macro gate は業種 status だけで、個別事業との directness を見ない。情報・通信業 supportive が GREE / Colopl / Gakken に同じように適用されるが、AI/cloud 直接 exposure は異なる。
7. 既存 lane は「安い」をよく拾うが、「長期保有して耐えられる」「今だけの catalyst がある」「高品質を妥当価格で買う」「株主還元で下値が固い」などの違う勝ち筋を別 bucket として持っていない。
8. 直近 1 日 / 1 週の急落を独立した discovery source として扱っていない。8255 は candidates 内には存在したが、mechanical selection では top queue に入らず、別途 Yahoo の値下がり率を見て初めて上位化した。これは現行 process が quick dislocation を取り逃がし得る直接証拠。

このため、改善 issue では単に lane を増やすのではなく、selection queue に novelty / diversity / event-state / long-hold fallback / current catalyst の概念を入れるべき。
