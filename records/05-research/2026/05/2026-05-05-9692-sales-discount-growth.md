---
ticker: '9692'
name: シーイーシー
playbook_id: sales-discount-growth
playbook_snapshot:
  ref_path: records/_playbooks/sales-discount-growth/2026-05-01T000000+0900.md
  content_sha256: sha256:37358359da7bd9a56b029ac89ec734acc2a427c3b0c9a53df1c6a59afe03d4ad
  effective_from: '2026-05-01T00:00:00+09:00'
policy_snapshot:
  ref_path: records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md
  content_sha256: sha256:4d8b769749184e6733a4a698000a21070f98d681beacf30bb30568c917f6372a
  effective_from: '2026-05-01T00:00:00+09:00'
portfolio_exposure_snapshot_ref:
  ref_path: records/_portfolio-exposure/2026/05/2026-05-05T200000+0900.yaml
  content_sha256: sha256:8a0a662caf0182ce01b632df412bce5c3b74e4d6ac8af8070fb325a915ccc82e
selected_supporting_evidence_refs:
- source: candidate
  evidence_hit_id: candidate-2026-05-01-9692-sales-discount-growth
research_decision:
  outcome: approved
  posture: act_now
  reason_code: payoff_and_policy_pass
candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
  screen_run_id: screening-20260501-79ec46a8
  ticker: '9692'
  candidate_id: candidate-2026-05-01-9692
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai_draft: false
published_at: '2026-05-05T20:05:00+09:00'
recorded_at: '2026-05-05T20:05:00+09:00'
tradable_at: '2026-05-07T09:00:00+09:00'
macro_regime_gate:
  aggregate_status: supportive
  decision_effect: pass
  source_scope: sector
  reducer_id: macro-regime-reducer-v1
  inputs:
  - scope: sector
    key: 情報・通信業
    status: supportive
    source_ref: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
    valid_until: '2026-05-13'
    weight_or_materiality: high
    confidence: high
policy_overrides: []
external_refs:
- ref_path: records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md
  content_sha256: sha256:852861e52b0021222477ccd3409cb6d603ddd36837ae5e63ec7ae98124bfb3c5
candidate_evidence_decisions:
- evidence_hit_id: candidate-2026-05-01-9692-sales-discount-growth
  effective_sizing_eligible: true
  evaluated_at: '2026-05-05T20:05:00+09:00'
  reason_code: source_status_ok
research_evidence_hits:
- evidence_hit_id: research-9692-shareholder-return
  decision_role: catalyst_note
  evidence_polarity: supports
  evidence_family_set:
  - catalyst
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md
    content_sha256: sha256:852861e52b0021222477ccd3409cb6d603ddd36837ae5e63ec7ae98124bfb3c5
  recorded_at: '2026-05-05T20:05:00+09:00'
- evidence_hit_id: research-9692-risk-review
  decision_role: risk_evidence
  evidence_polarity: risk
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md
    content_sha256: sha256:852861e52b0021222477ccd3409cb6d603ddd36837ae5e63ec7ae98124bfb3c5
  recorded_at: '2026-05-05T20:05:00+09:00'
independent_evidence_count: 1
raw_playbook_concurrence_count: 1
sizing_eligible_playbook_concurrence_count: 1
raw_evidence_family_count: 2
sizing_eligible_evidence_family_count: 2
conviction_tier: medium
conviction_tier_path: count_breadth
depth_verification_ref: null
position_sizing_overlay:
  paper_proxy_position_size_oku: 0.01
  paper_proxy_position_size_yen: 1000000
  real_order_intent_yen: 200000
  adv_participation_pct: 0.7692
  sizing_formula_id: policy-v1-paper-to-real-ladder
counterfactual: null
thesis_payoff:
  max_entry_price_yen: 2000
  target_price_yen: 2300
  stop_loss_yen: 1800
  time_horizon_bd: 40
  invalidation_conditions:
  - Growth slowdown overwhelms P/S and dividend support.
  entry_trigger: price_guard_or_revisit
  expected_upside_pct: 15.0
  expected_downside_pct: 11.11
  risk_reward_ratio: 1.35
tracking:
  mode: post_approval
  plus_15bd: null
  plus_30bd: null
market_cap_oku: 678
sector_33: 情報・通信業
avg_turnover_oku: 1.3
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
  primary_metric:
  - p_s
---

# Research: 2026-05-05 9692 シーイーシー sales-discount-growth

**成分**: 個別銘柄リサーチ

**Playbook id**: sales-discount-growth

## Thesis

9692 シーイーシーは、情報・通信業 supportive の中で `sales-discount-growth` が hit した。9682 DTS と同じ SIer / IT services 系だが、P/S 1.03、売上 YoY +17.2%、OCF yield 8.6%、net cash / market cap 36.6%、2027/1 期予想配当 80 円という組み合わせで、9682 より「売上成長 + 財務余力 + 株主還元」の厚みがある。2026-05-05 時点では 9682 追加より優先して 100 株を採用する。

## Macro regime gate

- **判定**: supportive
- **業種**: 情報・通信業
- **outlook_ref**: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **根拠**: outlook は AI / クラウド / データセンター需要の spillover を理由に情報・通信業を supportive としている。シーイーシーは SIer / IT service で、高成長 SaaS ほど直接的ではないが、DX / クラウド / セキュリティ / スマートファクトリー投資の継続は追い風。
- **保守側判定**: supportive。ただし「AI テーマ」ではなく、実績成長と還元で採用する。

### Portfolio macro risk budget

macro は supportive だが、2026-05-05 時点ではホルムズ海峡リスク、Brent 110 ドル超、米利上げ再織り込み、5/8 米雇用・5/12 米 CPI 前という制約がある。AI 関連ではなく domestic SIer のため、9692 は macro beta を取りに行く銘柄ではない。投資可能資金は 500 万円、当面の tactical cap は 100 万円。9682 200 株注文後でも、9692 100 株を追加した合計は 395,700 円（5/1 終値基準）、9682 を 1,050 円上限で見ても最大 402,900 円で、総資金の 7.9-8.1%、tactical cap の約 39.6-40.3%。初期投入上限 40-45% の範囲内に収まるため、即時候補として許容する。

6310 / 6835 の 1Q を待つ前に全額を使い切る必要はない。9692 は次回 1Q 予定が 6/11 で、決算またぎまで時間があるため、5/7 以降の即時候補としては 6310 / 6835 より優先する。

### External deepresearch verification log

| external_ref | 採用 / 修正 / 未採用 | この research での扱い |
| --- | --- | --- |
| records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md | 採用 | 5/7 は全額投入ではなく、9682 200 株 + 9692 100 株までを tactical cap 40-45% 内に抑える判断を採用 |
| 同上 | 採用 | 9692 は 2,000 円以下なら 100 株追加可、次回 1Q まで時間があるため 6310 / 6835 より即時候補として扱う |
| 同上 | 修正 | 6835 の 100 株 toe-hold は、決算またぎ kill switch と衝突するため、この research の即時 slate には含めない |

## Sales / P/S snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| P/S | 1.03 | primary evidence hit |
| P/S sector gap | -54.4% | 9682 より discount が深い |
| 売上高 TTM | 65,882 百万円 | 2026/1 期実績 |
| 売上 YoY | +17.2% | growth intact |
| 営業利益 | 7,338 百万円 | 営業黒字 |
| 営業利益率 | 11.1% | SIer として十分 |
| PER trailing | 11.64 | 9682 より低い |
| PBR | 1.41 | 資産面でも過度に高くない |
| OCF yield | 8.59% | cashflow lane には未 hit だが補助として強い |
| Net cash / market cap | 36.6% | strict lane 未 hit だが財務余力は明確 |

新 screening selection では、9692 は after-outlook の global rank 104/307、sales-discount-growth lane 30/114。候補順位は 9682 より少し上で、単独 evidence hit ではあるが、net cash と配当が補強材料になる。

Source:

- 会社 IR 決算短信一覧: https://www.cec-ltd.co.jp/ir/accounting/
- 会社 IR 決算説明会資料: https://www.cec-ltd.co.jp/ir/guide/
- candidates: records/04-candidates/2026/05/2026-05-01.yaml

## Margin bridge

2026/1 期は売上高 65,882 百万円、営業利益 7,338 百万円で、売上 +17.2%、営業利益 +9.6%。売上の伸びに対して営業利益の伸びはやや劣るため、P/S discount の一部は margin 拡大余地の限定を織り込んでいる可能性がある。

ただし、営業利益率は 11% 台を維持しており、低採算売上の積み上げだけで売上が伸びているとは見ない。2027/1 期会社予想も売上 68,000 百万円、営業利益 7,750 百万円で増収増益を継続する。DTS よりも小型で、売上成長率と net cash の厚みがある点を重視する。

## CFO / loss narrowing

営業黒字のため loss narrowing 条件は不要。候補 YAML では OCF yield 8.59%、CFO YoY +10.6%、OCF TTM 5,825 百万円。cashflow-yield-discount lane には届かないが、sales evidence hit の裏付けとしては十分。

FCF は EDINET capex tag が取得できず unavailable。採用後の確認では、2026/1 期有価証券報告書の投資 CF / 設備投資 / ソフトウェア投資を見て、営業 CF が株主還元を支える水準かを確認する。

## Growth durability

プラス材料:

- 2026/1 期は売上 +17.2%、営業利益 +9.6%、当期利益 +28.8%。
- 2027/1 期会社予想も売上 +3.2%、営業利益 +5.6%、当期利益 +7.7%。
- 会社 IR の事業領域はインテグレーション、コネクティッド、ソリューションで、DX / クラウド / AI / IoT / セキュリティと outlook の supportive に重なる。
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
- 情報・通信業 gate が supportive から neutral/adverse へ悪化する。
- 2,000 円超まで gap up し、P/S discount と配当利回りの妙味が薄れる。

## Position size

- **research_decision**: approved
- **primary evidence hit**: sales-discount-growth
- **evidence hit count**: 1
- **market cap**: 678 億円
- **avg turnover**: 1.3 億円
- **paper proxy position**: 0.01 億円
- **ADV participation**: 0.7692%
- **実資金想定**: 100 株、5/1 終値基準 192,900 円。9682 200 株注文後でも合計 395,700 円、9682 を 1,050 円上限で見ても最大 402,900 円。総資金 500 万円比 7.9-8.1%、tactical cap 100 万円比 39.6-40.3%。
- **許容上限**: single evidence hit のため paper proxy 最大 1%

### お買い得候補としての結論

9692 は 9682 と同じ sales-discount-growth だが、P/S discount、売上成長、PER、net cash、配当利回りの組み合わせがより厚い。100 万円 tactical cap では、9682 を 200 株維持した上で 9692 を 100 株入れても初期 risk budget の範囲に収まる。即時候補の最優先は 9692 とする。
