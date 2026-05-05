---
ticker: "9692"
name: "シーイーシー"
playbook: sales-discount-growth
supporting_signals: []
decision: accepted
candidates_ref: records/03-candidates/2026/05/2026-05-01.yaml
outlook_ref: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
  - records/01-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
  - records/01-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai-draft: true
published_at: "2026-05-05T20:05:00+09:00"
tradable_at: "2026-05-07T09:00:00+09:00"
macro_gate: tailwind
overrides: []
external_refs:
  - records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md
position_size_oku: 0.01
avg_turnover_oku: 1.3
adv_participation_pct: 0.7692
market_cap_oku: 678
sector_33: "情報・通信業"
valuation:
  per_forward: null
  per_trailing: 11.64
  pbr: 1.41
  ev_ebitda: 5.3
  p_s: 1.03
  pcfr: 11.6
  ocf_yield: 0.0859
  fcf_yield: null
  net_cash_to_market_cap: 0.3661
  cash_to_market_cap: 0.3715
  price_to_equity: 1.5924
  equity_ratio: 0.6848
  primary_metric: ["p_s"]
---

# Research: 2026-05-05 9692 シーイーシー sales-discount-growth

**成分**: 個別銘柄リサーチ

**Playbook**: sales-discount-growth

## Thesis

9692 シーイーシーは、情報・通信業 tailwind の中で `sales-discount-growth` が hit した。9682 DTS と同じ SIer / IT services 系だが、P/S 1.03、売上 YoY +17.2%、OCF yield 8.6%、net cash / market cap 36.6%、2027/1 期予想配当 80 円という組み合わせで、9682 より「売上成長 + 財務余力 + 株主還元」の厚みがある。2026-05-05 時点では 9682 追加より優先して 100 株を採用する。

## Macro gate

- **判定**: tailwind
- **業種**: 情報・通信業
- **outlook_ref**: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **根拠**: outlook は AI / クラウド / データセンター需要の spillover を理由に情報・通信業を tailwind としている。シーイーシーは SIer / IT service で、高成長 SaaS ほど直接的ではないが、DX / クラウド / セキュリティ / スマートファクトリー投資の継続は追い風。
- **保守側判定**: tailwind。ただし「AI テーマ」ではなく、実績成長と還元で採用する。

### Portfolio macro risk budget

macro は tailwind だが、2026-05-05 時点ではホルムズ海峡リスク、Brent 110 ドル超、米利上げ再織り込み、5/8 米雇用・5/12 米 CPI 前という制約がある。AI 関連ではなく domestic SIer のため、9692 は macro beta を取りに行く銘柄ではない。投資可能資金は 500 万円、当面の tactical cap は 100 万円。9682 200 株注文後でも、9692 100 株を追加した合計は 395,700 円（5/1 終値基準）で、総資金の 7.9%、tactical cap の約 39.6%。初期投入上限 40-45% の範囲内に収まるため、即時候補として許容する。

6310 / 6835 の 1Q を待つ前に全額を使い切る必要はない。9692 は次回 1Q 予定が 6/11 で、決算またぎまで時間があるため、5/7 以降の即時候補としては 6310 / 6835 より優先する。

## Sales / P/S snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| P/S | 1.03 | primary signal |
| P/S sector gap | -54.4% | 9682 より discount が深い |
| 売上高 TTM | 65,882 百万円 | 2026/1 期実績 |
| 売上 YoY | +17.2% | growth intact |
| 営業利益 | 7,338 百万円 | 営業黒字 |
| 営業利益率 | 11.1% | SIer として十分 |
| PER trailing | 11.64 | 9682 より低い |
| PBR | 1.41 | 資産面でも過度に高くない |
| OCF yield | 8.59% | cashflow lane には未 hit だが補助として強い |
| Net cash / market cap | 36.6% | strict lane 未 hit だが財務余力は明確 |

新 screening selection では、9692 は after-outlook の global rank 104/307、sales-discount-growth lane 30/114。候補順位は 9682 より少し上で、単独 signal ではあるが、net cash と配当が補強材料になる。

Source:

- 会社 IR 決算短信一覧: https://www.cec-ltd.co.jp/ir/accounting/
- 会社 IR 決算説明会資料: https://www.cec-ltd.co.jp/ir/guide/
- candidates: records/03-candidates/2026/05/2026-05-01.yaml

## Margin bridge

2026/1 期は売上高 65,882 百万円、営業利益 7,338 百万円で、売上 +17.2%、営業利益 +9.6%。売上の伸びに対して営業利益の伸びはやや劣るため、P/S discount の一部は margin 拡大余地の限定を織り込んでいる可能性がある。

ただし、営業利益率は 11% 台を維持しており、低採算売上の積み上げだけで売上が伸びているとは見ない。2027/1 期会社予想も売上 68,000 百万円、営業利益 7,750 百万円で増収増益を継続する。DTS よりも小型で、売上成長率と net cash の厚みがある点を重視する。

## CFO / loss narrowing

営業黒字のため loss narrowing 条件は不要。候補 YAML では OCF yield 8.59%、CFO YoY +10.6%、OCF TTM 5,825 百万円。cashflow-yield-discount lane には届かないが、sales signal の裏付けとしては十分。

FCF は EDINET capex tag が取得できず unavailable。採用後の確認では、2026/1 期有価証券報告書の投資 CF / 設備投資 / ソフトウェア投資を見て、営業 CF が株主還元を支える水準かを確認する。

## Growth durability

プラス材料:

- 2026/1 期は売上 +17.2%、営業利益 +9.6%、当期利益 +28.8%。
- 2027/1 期会社予想も売上 +3.2%、営業利益 +5.6%、当期利益 +7.7%。
- 会社 IR の事業領域はインテグレーション、コネクティッド、ソリューションで、DX / クラウド / AI / IoT / セキュリティと outlook の tailwind に重なる。
- net cash / market cap 36.6%、equity ratio 68.5% で、短期景気悪化に対する耐性がある。

反対仮説:

- 2026/1 期の売上成長 +17.2% に対し、2027/1 期会社予想は +3.2% へ鈍化する。
- 情報・通信業の P/S median には SaaS / 通信 / 高成長ソフトウェアが混じるため、SIer の P/S discount は恒久的な事業モデル差かもしれない。
- 会社予想の成長率が一桁前半なら、P/S rerating の上限は大きくない。

## Shareholder return

| 項目 | 確認結果 | 判定 |
| --- | --- | --- |
| 配当政策 | 安定配当に配意しつつ、業績動向・財務状況を総合勘案 | positive |
| 2026/1 期配当 | 年間 70 円 | positive |
| 2027/1 期予想配当 | 年間 80 円 | positive |
| 予想配当利回り | 5/1 終値 1,929 円に対して約 4.1% | 下値支え |
| 自己株式 | 2026/1 期には取得・消却の履歴あり。基本方針でも安定配当と自己株取得を総合勘案 | positive |
| 減配リスク | net cash と OCF があり低-中。業績鈍化時は確認 | acceptable |

Source:

- 配当金の推移: https://www.cec-ltd.co.jp/ir/dividend.html
- 自己株式買付状況報告: https://www.cec-ltd.co.jp/ir/own_shares.html
- 自己株式の保有等に関する基本方針: https://www.cec-ltd.co.jp/ir/treasury_stock.html

## Entry

- 2026-05-05 と 2026-05-06 は JPX cash market holiday のため、最短 tradable_at は 2026-05-07 09:00。
- 2026-05-01 終値 1,929 円を基準に 100 株。2,000 円以下なら採用。2,000 円超の gap up は追わず、指値を置く。
- 次回 1Q 予定は株予報 Pro ベースで 2026-06-11。決算またぎ kill switch まで 1 か月以上あるため、5/7 の即時候補として 6310 / 6835 より扱いやすい。
- 9682 を既に 200 株注文済みでも、当面の 100 万円 tactical cap なら追加資金はまず 9692 に回す。9682 200 株 + 9692 100 株で 395,700 円、総資金 500 万円比 7.9%、tactical cap 比 39.6%。

Source:

- JPX market holidays: https://www.jpx.co.jp/english/corporate/about-jpx/calendar/
- 株予報 Pro 決算予定: https://kabuyoho.jp/sp/report?bcode=9692

## Exit

- 利確目安: 2,200-2,300 円。PER 13-14 倍、または P/S 1.15-1.20 程度への小幅 rerating。
- 損切り目安: 1,800 円割れ。2026/1 期決算・増配を織り込んでも下落する場合、成長鈍化が先に評価されている可能性が高い。
- 時間切れ: 40 営業日、または 6/11 1Q 前。決算前に含み益が乏しい場合はまたぎを避ける。

## Invalidation

- 2027/1 期の増収増益計画が弱く、売上成長鈍化を市場が構造悪化と解釈する。
- OCF が運転資本悪化で急減し、配当 80 円の余裕が薄れる。
- SIer peer 比較では P/S 1.03 が割安ではないと判明する。
- 情報・通信業 gate が tailwind から neutral/headwind へ悪化する。
- 2,000 円超まで gap up し、P/S discount と配当利回りの妙味が薄れる。

## Position size

- **decision**: accepted
- **primary signal**: sales-discount-growth
- **signal 数**: 1
- **market cap**: 678 億円
- **avg turnover**: 1.3 億円
- **paper proxy position**: 0.01 億円
- **ADV participation**: 0.7692%
- **実資金想定**: 100 株、5/1 終値基準 192,900 円。9682 200 株注文後でも合計 395,700 円。総資金 500 万円比 7.9%、tactical cap 100 万円比 39.6%。
- **許容上限**: single signal のため paper proxy 最大 1%

### お買い得候補としての結論

9692 は 9682 と同じ sales-discount-growth だが、P/S discount、売上成長、PER、net cash、配当利回りの組み合わせがより厚い。100 万円 tactical cap では、9682 を 200 株維持した上で 9692 を 100 株入れても初期 risk budget の範囲に収まる。即時候補の最優先は 9692 とする。
