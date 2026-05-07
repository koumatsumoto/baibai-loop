---
ticker: '6835'
name: アライドテレシスホールディングス
playbook_id: fcf-yield-discount
playbook_snapshot:
  ref_path: records/_playbooks/fcf-yield-discount/2026-05-01T000000+0900.md
  content_sha256: sha256:f3d5fbf92cfa4b4663b59837b6ed819031df8a5b153e636877c815355154fbc6
  effective_from: '2026-05-01T00:00:00+09:00'
policy_snapshot:
  ref_path: records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md
  content_sha256: sha256:4d8b769749184e6733a4a698000a21070f98d681beacf30bb30568c917f6372a
  effective_from: '2026-05-01T00:00:00+09:00'
policy_applicability: active
calendars_snapshot:
  business_days:
    ref_path: records/_calendars/business-days/2026-05.yaml
    content_sha256: sha256:cd3ddf5dcb6b4de0595547c033d9be68920272be45bf7a358f42465caa17c273
  events:
    ref_path: records/_calendars/events/2026-05.yaml
    content_sha256: sha256:583cf61a16375dfedb766659911daa435b9b8f9d40cd1e26721d3b734ffb0ea6
  corporate_actions:
    ref_path: records/_calendars/corporate-actions/2026-05.yaml
    content_sha256: sha256:5b1f2487807f590acdc5784ac2e84a851aa7264a03c1ffc3f84c5ebfbdcd761c
portfolio_exposure_snapshot_ref:
  ref_path: records/_portfolio-exposure/2026/05/2026-05-05T203000+0900.yaml
  content_sha256: sha256:fe16419b85c5a1c29dff609beb06643e87cb6a208ea2581ffebd31e481bc428a
selected_supporting_evidence_refs:
- source: candidate
  evidence_hit_id: candidate-2026-05-01-6835-fcf-yield-discount
- source: candidate
  evidence_hit_id: candidate-2026-05-01-6835-cashflow-yield-discount
research_decision:
  outcome: deferred
  posture: wait_for_event
  deferral_reason: event_pending
  revisit:
    trigger: earnings_release
    revisit_after: '2026-05-15'
    expires_at: '2026-06-30'
    blocking_conditions:
    - event risk must clear
candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
  screen_run_id: screening-20260501-b2e37953
  ticker: '6835'
  candidate_id: candidate-2026-05-01-6835
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai_draft: true
published_at: '2026-05-05T20:15:00+09:00'
recorded_at: '2026-05-05T20:15:00+09:00'
tradable_at: '2026-05-18T09:00:00+09:00'
macro_regime_gate:
  aggregate_status: supportive
  decision_effect: pass
  source_scope: sector
  reducer_id: macro-regime-reducer-v1
  inputs:
  - scope: sector
    key: 電気機器
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
- evidence_hit_id: candidate-2026-05-01-6835-fcf-yield-discount
  effective_sizing_eligible: true
  evaluated_at: '2026-05-05T20:15:00+09:00'
  reason_code: source_status_ok
- evidence_hit_id: candidate-2026-05-01-6835-cashflow-yield-discount
  effective_sizing_eligible: true
  evaluated_at: '2026-05-05T20:15:00+09:00'
  reason_code: source_status_ok
research_evidence_hits:
- evidence_hit_id: research-6835-risk-review
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
  recorded_at: '2026-05-05T20:15:00+09:00'
independent_evidence_count: 2
raw_playbook_concurrence_count: 2
sizing_eligible_playbook_concurrence_count: 2
raw_evidence_family_count: 1
sizing_eligible_evidence_family_count: 1
conviction_tier: medium
conviction_tier_path: count_breadth
depth_verification_ref: null
position_sizing_overlay:
  paper_proxy_position_size_oku: 0.0
  paper_proxy_position_size_yen: 0
  real_order_intent_yen: 0
  adv_participation_pct: 0.0
  sizing_formula_id: policy-v1-paper-to-real-ladder
counterfactual:
  if_approved:
    hypothetical_paper_proxy_position_size_oku: 0.01
    hypothetical_paper_proxy_position_size_yen: 1000000
thesis_payoff:
  max_entry_price_yen: 270
  target_price_yen: 340
  stop_loss_yen: 230
  time_horizon_bd: 40
  invalidation_conditions:
  - Q1 invalidates FCF or cash-flow durability.
  entry_trigger: price_guard_or_revisit
  expected_upside_pct: 25.93
  expected_downside_pct: 14.81
  risk_reward_ratio: 1.75
tracking:
  mode: re_examination
  plus_15bd: null
  plus_30bd: null
market_cap_oku: 275
sector_33: 電気機器
avg_turnover_oku: 2.2
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
  primary_metric:
  - fcf_yield
  - ocf_yield
---

# Research: 2026-05-05 6835 アライドテレシスホールディングス fcf-yield-discount

**成分**: 個別銘柄リサーチ

**Playbook id**: fcf-yield-discount

## Thesis

6835 アライドテレシスホールディングスは、2026-05-01 candidates で `fcf-yield-discount` と `cashflow-yield-discount` が同時 hit した。FCF yield 22.6%、OCF yield 24.5%、PER 9.5、EV/EBITDA 3.2、net cash / market cap 38.9% で、今回の「お買い得を拾う」目的にかなり合う。機械的には FCF lane 1/11 で、9682 より割安軸は明確。ただし 2026-05-15 15:30 に 1Q 決算予定があるため、5/5 時点の結論は deferred。1Q 通過後に FCF / OCF thesis が崩れなければ追加候補とする。

## Macro regime gate

- **判定**: supportive
- **業種**: 電気機器
- **outlook_ref**: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **根拠**: outlook は AI / HPC / データセンター向け部品需要を背景に電気機器を supportive としている。アライドテレシスはネットワーク機器・ソリューション企業で、データセンター部品というより企業・公共向けネットワーク投資に近い。
- **保守側判定**: supportive。ただし採用理由は macro ではなく、FCF / OCF / net cash の同時成立。

### Portfolio macro risk budget

6835 は決算直前のため 1Q 後に回す。1 単元が 26,200 円と小さく、総資金 500 万円では 0.52%、当面の 100 万円 tactical cap でも 2.62% で、Q1 が悪かった場合の損失寄与は限定的。一方で、現行 system rule は決算またぎ entry を原則避ける。FCF lane 1 位・cashflow lane 10 位・net cash 38.9% という evidence hit は強いが、research_decision: deferred のまま event risk を取りに行くと kill switch の意味が薄れる。

したがって、macro risk budget 上も 5/7 の先行買いはしない。5/15 1Q 通過後に FCF / OCF / net cash thesis が残れば、100 株から approved へ切り替える。決算後に gap up する取り逃しリスクはあるが、今回の PR では「deferred 銘柄を override して決算またぎする」運用変更までは行わない。

### External deepresearch verification log

| external_ref | 採用 / 修正 / 未採用 | この research での扱い |
| --- | --- | --- |
| records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md | 採用 | 6835 は FCF lane 1 位・cashflow lane 10 位で、1Q 後の優先候補として残す |
| 同上 | 未採用 | deepresearch 初版の「100 株 toe-hold optional」は、research_decision: deferred と決算またぎ kill switch に抵触するため採用しない |

## FCF snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| EDINET CFO | 6,747 百万円 | primary cash source |
| Capex | 526 百万円 | `purchase_of_fixed_assets` |
| FCF | 6,221 百万円 | CFO - capex |
| FCF yield | 22.6% | FCF lane 1 位 |
| OCF yield | 24.5% | supporting evidence hit |
| CFO YoY | +17.5% | 悪化なし |
| PER trailing | 9.51 | 補助 |
| EV/EBITDA | 3.2 | 補助 |
| Net cash / market cap | 38.9% | 財務余力 |

新 screening selection では、6835 は after-outlook global rank 41/307、fcf-yield-discount lane 1/11、cashflow-yield-discount lane 10/148。価格が 262 円で 100 株 26,200 円と小さく、残余資金を無理に使わず starter position を作れる点も実資金運用に合う。

Source:

- 会社 IR: https://ir.at-global.com/
- 決算発表予定: https://ir.at-global.com/information
- 株式情報: https://ir.at-global.com/stock
- candidates: records/04-candidates/2026/05/2026-05-01.yaml

## Capex quality

EDINET metric の capex source は `purchase_of_fixed_assets`。CFO 6,747 百万円に対して capex 526 百万円と軽く、FCF が大きく残る。ネットワーク機器会社として、過大な設備投資を必要としない構造ならこの FCF は強い。

反対側では、capex が一時的に低いだけなら FCF yield は過大に見える。2026/12 期 1Q で、研究開発・サービス化投資・海外拠点再編に伴う cash out が増えないか確認する。

## Working capital quality

OCF yield 24.5%、CFO YoY +17.5% は強い。ただしネットワーク機器は在庫・売掛金・前受/保守契約の変動で CFO がぶれやすい。特に、製品販売からソリューション/サービス比率を高める過程では、契約負債や保守収入の timing が cash flow に影響する。

approved 条件:

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

- 2026-05-15 15:30 に 2026/12 期 1Q 決算発表予定。system default では 5/7 に買わず、決算後に判断する。
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
- 電気機器 gate が adverse に悪化する。

## Position size

- **decision**: deferred
- **primary evidence hit**: fcf-yield-discount
- **supporting evidence hit**: cashflow-yield-discount
- **market cap**: 275 億円
- **avg turnover**: 2.2 億円
- **paper proxy position**: 0.01 億円
- **ADV participation**: 0.4545%
- **実資金想定**: 1Q 通過後 100 株から。5/1 終値基準 26,200 円。
- **許容上限**: 複数 evidence hit だが小型・決算直前のため初期 1%

### お買い得候補としての結論

6835 は、FCF lane 1 位で、今回の新 screening の価値が最も分かりやすい候補。9682 よりも「現金創出力に対して安い」根拠は強い。ただし決算直前のため、5/15 1Q 後まで買わない。ここで kill switch を守ることで、FCF/OCF が一過性だった場合の false positive を避ける。
