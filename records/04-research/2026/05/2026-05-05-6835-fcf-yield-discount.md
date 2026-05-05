---
ticker: "6835"
name: "アライドテレシスホールディングス"
playbook: fcf-yield-discount
supporting_signals:
  - cashflow-yield-discount
decision: pending
candidates_ref: records/03-candidates/2026/05/2026-05-01.yaml
outlook_ref: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
  - records/01-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
  - records/01-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai-draft: true
published_at: "2026-05-05T20:15:00+09:00"
tradable_at: "2026-05-18T09:00:00+09:00"
macro_gate: tailwind
overrides: []
external_refs: []
position_size_oku: 0.01
avg_turnover_oku: 2.2
adv_participation_pct: 0.4545
market_cap_oku: 275
sector_33: "電気機器"
valuation:
  per_forward: null
  per_trailing: 9.51
  pbr: 1.29
  ev_ebitda: 3.2
  p_s: 0.55
  pcfr: 4.1
  ocf_yield: 0.2452
  fcf_yield: 0.2261
  net_cash_to_market_cap: 0.3888
  cash_to_market_cap: 0.6189
  price_to_equity: 1.2897
  equity_ratio: 0.4378
  primary_metric: ["fcf_yield", "ocf_yield"]
---

# Research: 2026-05-05 6835 アライドテレシスホールディングス fcf-yield-discount

**成分**: 個別銘柄リサーチ

**Playbook**: fcf-yield-discount

## Thesis

6835 アライドテレシスホールディングスは、2026-05-01 candidates で `fcf-yield-discount` と `cashflow-yield-discount` が同時 hit した。FCF yield 22.6%、OCF yield 24.5%、PER 9.5、EV/EBITDA 3.2、net cash / market cap 38.9% で、今回の「お買い得を拾う」目的にかなり合う。機械的には FCF lane 1/11 で、9682 より割安軸は明確。ただし 2026-05-15 15:30 に 1Q 決算予定があるため、5/5 時点の結論は pending。1Q 通過後に FCF / OCF thesis が崩れなければ追加候補とする。

## Macro gate

- **判定**: tailwind
- **業種**: 電気機器
- **outlook_ref**: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **根拠**: outlook は AI / HPC / データセンター向け部品需要を背景に電気機器を tailwind としている。アライドテレシスはネットワーク機器・ソリューション企業で、データセンター部品というより企業・公共向けネットワーク投資に近い。
- **保守側判定**: tailwind。ただし採用理由は macro ではなく、FCF / OCF / net cash の同時成立。

## FCF snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| EDINET CFO | 6,747 百万円 | primary cash source |
| Capex | 526 百万円 | `purchase_of_fixed_assets` |
| FCF | 6,221 百万円 | CFO - capex |
| FCF yield | 22.6% | FCF lane 1 位 |
| OCF yield | 24.5% | supporting signal |
| CFO YoY | +17.5% | 悪化なし |
| PER trailing | 9.51 | 補助 |
| EV/EBITDA | 3.2 | 補助 |
| Net cash / market cap | 38.9% | 財務余力 |

新 screening selection では、6835 は after-outlook global rank 41/307、fcf-yield-discount lane 1/11、cashflow-yield-discount lane 10/148。価格が 262 円で 100 株 26,200 円と小さく、残余資金を無理に使わず starter position を作れる点も実資金運用に合う。

Source:

- 会社 IR: https://ir.at-global.com/
- 決算発表予定: https://ir.at-global.com/information
- 株式情報: https://ir.at-global.com/stock
- candidates: records/03-candidates/2026/05/2026-05-01.yaml

## Capex quality

EDINET metric の capex source は `purchase_of_fixed_assets`。CFO 6,747 百万円に対して capex 526 百万円と軽く、FCF が大きく残る。ネットワーク機器会社として、過大な設備投資を必要としない構造ならこの FCF は強い。

反対側では、capex が一時的に低いだけなら FCF yield は過大に見える。2026/12 期 1Q で、研究開発・サービス化投資・海外拠点再編に伴う cash out が増えないか確認する。

## Working capital quality

OCF yield 24.5%、CFO YoY +17.5% は強い。ただしネットワーク機器は在庫・売掛金・前受/保守契約の変動で CFO がぶれやすい。特に、製品販売からソリューション/サービス比率を高める過程では、契約負債や保守収入の timing が cash flow に影響する。

accepted 条件:

- 2026-05-15 1Q で、営業 CF が大幅に悪化しない。
- 棚卸資産が急増せず、在庫圧縮だけで 2025/12 期 CFO が膨らんだわけではない。
- 米州事業譲渡や事業再編の cash impact が、FCF thesis を壊さない。

## Earnings quality

候補 YAML では売上 TTM 49,950 百万円、営業利益 4,228 百万円、売上 YoY +3.1%。成長率は高くないが、営業黒字で、PER 9.51、EV/EBITDA 3.2、FCF yield 22.6% が valuation を支える。

構造的に評価されない理由は、成長率の低さ、小型スタンダード市場、海外事業の変動、ネットワーク機器の競争環境と考えられる。したがって、採用は「高成長 rerating」ではなく、「低 multiple + 高 FCF + net cash」の回復狙いに限定する。

## Shareholder return

| 項目 | 確認結果 | 判定 |
| --- | --- | --- |
| 配当方針 | 財務体質と業績を勘案し安定配当を基本方針 | positive |
| 配当基準日 | 期末 12/31、中間 6/30 | 確認済み |
| 自社株買い | 2026/3-4 に自己株式取得関連開示あり | catalyst 補助 |
| 株主優待 | 継続保有期間に応じたデジタルギフト | 小口保有の補助 |
| 減配リスク | FCF / net cash が維持されれば低-中 | 1Q で確認 |

Source:

- 株主還元・配当金: https://ir.at-global.com/stock03
- 株式情報: https://ir.at-global.com/stock
- IR お知らせ: https://ir.at-global.com/information

## Entry

- 2026-05-15 15:30 に 2026/12 期 1Q 決算発表予定。5/7 に買うと決算またぎになるため、5/5 時点では買わない。
- 1Q 通過後、FCF / OCF / net cash thesis が維持されれば 100 株から。5/1 終値 262 円基準で 26,200 円。
- 275 円以下なら starter position。決算後に 300 円超まで gap up した場合は、FCF yield を再計算してから判断する。
- 残余資金が大きく、1Q の cash quality が強ければ 200-300 株まで増やせるが、最初は数量より thesis 確認を優先する。

## Exit

- 利確目安: 310-330 円。EV/EBITDA 4 倍台、または FCF yield 15% 台への小幅 rerating。
- 損切り目安: 240 円割れ。FCF / OCF thesis が残らず下落する場合は撤退。
- 時間切れ: 1Q 通過後 40 営業日。低成長銘柄なので、cash thesis が見えないまま長く持たない。

## Invalidation

- 2026/12 期 1Q で営業 CF / FCF が急減し、2025/12 期の FCF が一過性だったと確認される。
- capex が一時的に低かっただけで、通常投資を戻すと FCF が薄くなる。
- net cash が事業再編・投資・株主還元で急減する。
- 売上成長が止まり、低 multiple が構造的な低成長 discount と判明する。
- 電気機器 gate が headwind に悪化する。

## Position size

- **decision**: pending
- **primary signal**: fcf-yield-discount
- **supporting signal**: cashflow-yield-discount
- **market cap**: 275 億円
- **avg turnover**: 2.2 億円
- **paper proxy position**: 0.01 億円
- **ADV participation**: 0.4545%
- **実資金想定**: 1Q 通過後 100 株から、5/1 終値基準 26,200 円
- **許容上限**: 複数 signal だが小型・決算直前のため初期 1%

### お買い得候補としての結論

6835 は、FCF lane 1 位で、今回の新 screening の価値が最も分かりやすい候補。9682 よりも「現金創出力に対して安い」根拠は強い。ただし決算直前のため、2026-05-05 に注文する銘柄ではない。5/15 1Q 通過後、cash quality が維持されれば少額 starter を置く。
