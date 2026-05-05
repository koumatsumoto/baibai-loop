---
ticker: "3678"
name: "メディアドゥ"
playbook: strict-net-cash-discount
supporting_signals:
  - valuation-reversion
  - cash-rich-asset-discount
  - sales-discount-growth
decision: skipped
candidates_ref: records/03-candidates/2026/05/2026-05-01.yaml
outlook_ref: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
  - records/01-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
  - records/01-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai-draft: true
published_at: "2026-05-05T20:20:00+09:00"
tradable_at: "2026-05-07T09:00:00+09:00"
macro_gate: tailwind
overrides: []
external_refs: []
position_size_oku: 0
hypothetical_position_size_oku: 0.01
avg_turnover_oku: 1.5
adv_participation_pct: 0
market_cap_oku: 186
sector_33: "情報・通信業"
valuation:
  per_forward: null
  per_trailing: 10.22
  pbr: 0.98
  ev_ebitda: 4.2
  p_s: 0.17
  pcfr: 7.6
  ocf_yield: 0.1317
  fcf_yield: 0.0778
  net_cash_to_market_cap: 0.612
  cash_to_market_cap: 0.7524
  price_to_equity: 0.9691
  equity_ratio: 0.3376
  primary_metric: ["net_cash_to_market_cap"]
---

# Research: 2026-05-05 3678 メディアドゥ strict-net-cash-discount

**成分**: 個別銘柄リサーチ

**Playbook**: strict-net-cash-discount

## Thesis

3678 メディアドゥは、2026-05-01 candidates で 4 signal が重なり、strict-net-cash lane 1 位、sales lane 1 位として浮上した。機械的には今回の最上位候補。ただし 2026-03-02 の Seven Seas Entertainment 持分取得と 2026-03-03 の資金借入開示により、screening が使っている net cash 114 億円は 2026-05-05 の投資判断には古い可能性が高い。会社関連ページでは取得価額 8,000 万米ドル、約 124 億円規模と説明されており、screening 上の net cash 114 億円と同程度の大きさ。strict net cash thesis の根幹が最新イベントで崩れているため skipped とする。

## Macro gate

- **判定**: tailwind
- **業種**: 情報・通信業
- **outlook_ref**: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **根拠**: 情報・通信業は outlook で tailwind。電子書籍・出版流通は AI / クラウドの直接恩恵とは距離があるが、デジタルコンテンツ領域として neutral 以上には扱える。
- **保守側判定**: tailwind。ただし skip 理由は macro ではなく balance sheet freshness。

## Net cash snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| Screening net cash | 11,399 百万円 | strict lane hit |
| Net cash / market cap | 61.2% | 機械的には非常に強い |
| Cash / market cap | 75.2% | cash-rich も hit |
| Debt | 3,006 百万円 | screening 時点 |
| PBR | 0.98 | 資産割安 |
| P/S | 0.17 | sales lane 1 位 |
| OCF yield | 13.2% | 補助 |
| FCF yield | 7.8% | 補助 |

問題は、net cash の鮮度。candidates の EDINET source は `S100WV18`、source submit datetime は 2025-10-15。2026-03-02 に Seven Seas Entertainment 持分取得を発表し、2026-03-03 に資金の借入を発表しているため、5/5 時点の cash / debt は screening snapshot から大きく変わった可能性がある。

Source:

- candidates: records/03-candidates/2026/05/2026-05-01.yaml
- Seven Seas Entertainment 持分取得: https://mediado.jp/corporate/15057/
- Seven Seas グループ参画解説: https://mediado.jp/medicome/challenge/15992/
- メディアドゥ ニュース一覧: https://mediado.jp/news/

## Debt quality

strict-net-cash-discount は、有利子負債確認が primary gate。3678 はこの gate を通過できない。理由は、screening 時点の debt 3,006 百万円に対して、Seven Seas 取得と資金借入が後発事象として発生しており、実質 debt と cash out の再計算が必要だから。

取得対価と借入条件を確認し、取得後 pro forma の net cash / market cap が 30-40% 以上残るなら再検討できる。しかし 5/5 に注文するには、screening の 61.2% をそのまま信じるのは危険。

## Cash usability

cash-rich thesis でも同じ問題がある。保有現金が実際に余剰現金か、買収資金・運転資本・借入返済で拘束されるかが未確認。特に小型株の cash-rich signal は、M&A 後に一気に false positive になる。

この銘柄は「新 screening が見つけた候補」ではあるが、「即時のお買い得」ではなく、「後発イベントを取り込めていない候補」と扱う。

## Asset discount

PBR 0.98、P/S 0.17 は魅力的。ただし、電子書籍流通・出版関連は低 P/S が恒久化しやすく、成長率 +6.5% だけでは大きな rerating を期待しにくい。さらに Seven Seas 取得後はのれん・無形資産・借入が増える可能性があり、PBR / equity の質も再確認が必要。

Asset discount は存在するが、strict net cash が再確認できない状態では primary thesis にできない。

## Shareholder return

| 項目 | 確認結果 | 判定 |
| --- | --- | --- |
| 配当 | 会社 IR で確認が必要 | 未確定 |
| 自社株買い | 今回の primary catalyst ではない | neutral |
| DOE or 配当性向 | 未確認 | 未確定 |
| 減配リスク | M&A 後の借入・cash out 次第 | 中 |

shareholder return は、cash-rich thesis を補強するには不十分。まず pro forma の balance sheet が必要。

## Entry

今回は entry しない。再検討条件は以下:

- Seven Seas 取得後の pro forma cash / debt / net cash を決算短信・有報・決算説明資料で確認する。
- 借入条件、返済スケジュール、金利、財務制限条項を確認する。
- 取得後も net cash / market cap が 30-40% 以上残る、または FCF yield が十分高い。
- 買収統合リスクが短期 earnings を壊さない。

## Exit

skipped のため position は持たない。もし将来 accepted に切り替えるなら、利確は PBR 1.1-1.2 倍、損切りは取得後 net cash thesis の崩壊を基準に設計する。

## Invalidation

- Seven Seas 取得後、net cash が大きく低下し、strict-net-cash-discount の条件を満たさない。
- 借入で財務レバレッジが上がり、小型株の balance sheet safety が失われる。
- 買収統合費用、のれん償却・減損、海外事業リスクが earnings を圧迫する。
- P/S 0.17 が出版流通モデルの恒久 discount と判明する。

## Position size

- **decision**: skipped
- **primary signal**: strict-net-cash-discount
- **supporting signals**: valuation-reversion / cash-rich-asset-discount / sales-discount-growth
- **market cap**: 186 億円
- **avg turnover**: 1.5 億円
- **paper proxy position**: 0
- **hypothetical position**: 0.01 億円
- **ADV participation**: 0%
- **採用判定**: 見送り

### お買い得候補としての結論

3678 は機械的には最上位だが、最新の M&A / 借入イベントを反映しないまま買うと、strict net cash signal の false positive になる可能性が高い。これは新 screening の価値を否定するものではなく、むしろ「後発イベント freshness check」を追加すべき重要な発見として Issue #94 に切り出した。9682 / 9692 / 6310 / 6835 を優先し、3678 は pro forma balance sheet 確認後の再候補に回す。
