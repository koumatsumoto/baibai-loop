---
ticker: '3964'
name: オークネット
playbook_id: cashflow-yield-discount
playbook_ref:
  ref_path: records/_playbooks/cashflow-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: deferred
  posture: wait_for_event
  deferral_reason: data_gap
  revisit:
    trigger: half_year_cash_flow_statement
    revisit_after: '2026-08-07'
    expires_at: '2026-08-31'
    blocking_conditions:
    - Q2 で営業 CF / working capital を確認し、2025年12月期の高い CFO が一過性ではないことを確認する。
    - システム償却費・広告宣伝費増加後も、営業利益率が構造的に悪化していないことを確認する。
candidate_ref:
  candidates_ref: records/02-candidates/2026/05/2026-05-08.yaml
  ticker: '3964'
published_at: '2026-05-12T20:42:11+09:00'
recorded_at: '2026-05-12T20:42:11+09:00'
tradable_at: '2026-08-10T09:00:00+09:00'
position_sizing_overlay:
  estimated_real_order_notional_yen: 0
  adv_participation_pct: 0
thesis_payoff:
  invalidation_conditions:
  - Q2 で営業 CF が弱く、2025年12月期 CFO の一過性が示唆される。
  - 取扱高の伸びに対して営業利益率がさらに悪化し、システム償却費・広告宣伝費を吸収できない。
  - 通期予想上方修正後に再下方修正または進捗鈍化が出る。
  entry_trigger: q2_cash_flow_confirmation
market_cap_oku: 585
sector_33: 情報・通信業
avg_turnover_oku: 1.8
valuation:
  per_forward: null
  per_trailing: 9.39
  pbr: 2.1
  ev_ebitda: null
  p_s: 0.91
  pcfr: 4.6
  ocf_yield: 0.2177
  fcf_yield: 0.1887
  net_cash_to_market_cap: null
  cash_to_market_cap: 0.3946
  price_to_equity: 2.1991
  equity_ratio: 0.5243
  primary_metric:
  - ocf_yield
  - fcf_yield
  - p_s
corporate_action_check:
  checked: true
  result: none
  note: Checked during research review.
---

# Research: 2026-05-12 3964 オークネット cashflow-yield-discount

## Thesis

3964 オークネットは、2026-05-08 screening で `cashflow-yield-discount`、`fcf-yield-discount`、`sales-discount-growth` が同時 hit した。候補時点の TTM OCF yield は 21.8%、FCF yield は 18.9%、P/S は 0.91、売上 YoY は +14.7% で、降格した cash-rich game names の代替候補として見る理由は残る。

Q1 公式 IR では、売上高 +13.8%、営業利益 +4.6%、通期営業利益予想の上方修正、年間配当予想の上方修正が確認できた。したがって `reject` ではなく継続 research とする。一方、Q1 では四半期連結キャッシュ・フロー計算書が作成されておらず、2025年12月期の強い CFO / FCF が再現可能かは確認できない。decision は `deferred / wait_for_event` とし、Q2 の半期 CF で営業 CF と working capital を確認する。

## Cashflow snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| OCF TTM | 12,745 百万円 | candidate primary signal |
| OCF yield | 21.8% | cashflow-yield-discount hit |
| FCF TTM | 11,046 百万円 | fcf-yield-discount hit |
| FCF yield | 18.9% | strong |
| CFO YoY | +162.6% | 2025年12月期 source |
| Cash / market cap | 39.5% | 補助 |
| P/S | 0.91 | sales-discount-growth hit |

Source: `records/02-candidates/2026/05/2026-05-08.yaml` and 2026-05-12 Aucnet official IR。

Q1 は売上・利益・guidance では positive だが、営業 CF は開示されない。現金及び預金は 2025年12月末 23,104 百万円から 2026年3月末 21,225 百万円へ減少しており、オークション貸勘定、オークション借勘定、未払法人税等が動いている。これだけでは悪化とは判断しないが、2025年12月期の CFO が一過性かどうかを確定する evidence にはならない。

## 運転資本確認

オークネットはオークション貸勘定 / 借勘定が大きく、取扱高の伸びと working capital のタイミングで CFO が振れやすい。Q1 はオークション貸勘定が 2,942 百万円増加、オークション借勘定が 2,767 百万円増加しており、貸借両建ての動きが大きい。

Q2 で確認する条件:

- 半期営業 CF が 2025年12月期の高い CFO と整合する。
- 取扱高増加がオークション貸勘定の増加だけでなく、現金回収と利益に結びつく。
- 税金支払や季節性を除いて、運転資本が構造的に悪化していない。

## Capex / FCF quality

候補時点の FCF TTM は 11,046 百万円、FCF yield は 18.9%。ただし Q1 では CF 計算書がないため、capex と operating cash conversion の更新確認ができない。Q1 の減価償却費は 195 百万円、のれん償却額は 30 百万円で、システム償却費の増加が利益成長を抑えた一因として会社側が説明している。

FCF thesis は Q2 の半期 CF で、営業 CF、投資 CF、無形固定資産投資、システム償却後の利益率を確認するまで approved にしない。

## Earnings quality

Q1 の売上高は 18,189 百万円、営業利益は 3,226 百万円。前年同期比では売上 +13.8%、営業利益 +4.6% で、売上の伸びに対して利益の伸びは鈍い。会社側は、期初時点ではシステム償却費や広告宣伝費の増加により減益予想だったが、両主力セグメントが想定を上回ったため増収増益になったと説明している。

事業 KPI はおおむね positive。デジタルプロダクツは取扱高 +22.3%、流通台数 +10.3%。ファッションリセールは BtoB 出品点数と成約点数が減ったが、平均成約単価上昇で BtoB 取扱高 +12.9%。モビリティ＆エネルギーは取扱高 +20.6%、総成約/落札台数 +9.4%、検査台数 +13.0%。このため、auction volume / margin durability は継続確認に値するが、margin 拡大を確認したとは言わない。

## Shareholder return

2026年12月期の通期予想は、売上高 72,000 百万円、営業利益 11,500 百万円、親会社株主帰属当期純利益 7,500 百万円へ上方修正された。年間配当予想も、株式分割考慮後で 40 円から 42 円へ上方修正された。会社は連結配当性向 50%以上を目標としている。

株主還元は positive catalyst だが、この thesis の主因は CF yield と P/S discount である。配当上方修正だけで approved にしない。

## Entry

現時点では entry しない。Q1 後の判断は `継続 research` だが、cashflow-yield-discount thesis の中核である営業 CF が Q1 で更新されないため、Q2 の半期 CF を待つ。次の確認日は、公式 IR カレンダーと Q1 説明資料の「8月上旬 / 昨年 8月7日」を踏まえて 2026-08-07 暫定とする。

## Exit

未保有のため exit 条件は未設定。Q2 後に approved へ進める場合は、max entry、target、stop loss、time stop をその時点の価格と流動性で計算する。

## Invalidation

- Q2 で営業 CF が弱く、2025年12月期の CFO +162.6% が一過性だった可能性が高まる。
- 取扱高の増加に対して営業利益率が低下し、システム償却費・広告宣伝費を吸収できない。
- デジタルプロダクツ、ファッションリセール、モビリティ＆エネルギーの取扱高または成約台数が鈍化し、P/S discount が構造的な低 margin の反映だと判断される。
- 通期予想上方修正後に、会社計画の再下方修正や進捗鈍化が出る。

## Position size

- **decision**: deferred / wait_for_event。
- **paper proxy size**: 0 円。
- **real order intent**: 0 円。
- **binding cap**: data gap。Q1 で営業 CF が開示されていない。
