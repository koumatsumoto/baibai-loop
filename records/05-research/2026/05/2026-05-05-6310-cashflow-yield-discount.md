---
ticker: '6310'
name: 井関農機
playbook_id: cashflow-yield-discount
playbook_snapshot:
  ref_path: records/_playbooks/cashflow-yield-discount/2026-05-01T000000+0900.md
  content_sha256: sha256:d5f498eacc64dd045922d944d6417e21657f7e8f8a4e641162f3cefafc544248
  effective_from: '2026-05-01T00:00:00+09:00'
policy_snapshot:
  ref_path: records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md
  content_sha256: sha256:4d8b769749184e6733a4a698000a21070f98d681beacf30bb30568c917f6372a
  effective_from: '2026-05-01T00:00:00+09:00'
portfolio_exposure_snapshot_ref:
  ref_path: records/_portfolio-exposure/2026/05/2026-05-05T203000+0900.yaml
  content_sha256: sha256:50232ed102544c69f9279cf89370ddbdf9cd95ae34f3e0ecbea0089a91d89690
selected_supporting_evidence_refs:
- source: candidate
  evidence_hit_id: candidate-2026-05-01-6310-cashflow-yield-discount
- source: candidate
  evidence_hit_id: candidate-2026-05-01-6310-sales-discount-growth
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
  screen_run_id: screening-20260501-79ec46a8
  ticker: '6310'
  candidate_id: candidate-2026-05-01-6310
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai_draft: true
published_at: '2026-05-05T20:10:00+09:00'
recorded_at: '2026-05-05T20:10:00+09:00'
tradable_at: '2026-05-18T09:00:00+09:00'
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
- evidence_hit_id: candidate-2026-05-01-6310-cashflow-yield-discount
  effective_sizing_eligible: true
  evaluated_at: '2026-05-05T20:10:00+09:00'
  reason_code: source_status_ok
- evidence_hit_id: candidate-2026-05-01-6310-sales-discount-growth
  effective_sizing_eligible: true
  evaluated_at: '2026-05-05T20:10:00+09:00'
  reason_code: source_status_ok
research_evidence_hits:
- evidence_hit_id: research-6310-risk-review
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
  recorded_at: '2026-05-05T20:10:00+09:00'
independent_evidence_count: 2
raw_playbook_concurrence_count: 2
sizing_eligible_playbook_concurrence_count: 2
raw_evidence_family_count: 2
sizing_eligible_evidence_family_count: 2
conviction_tier: medium
conviction_tier_path: count_breadth
depth_verification_ref: null
position_sizing_overlay:
  paper_proxy_position_size_oku: 0.0
  paper_proxy_position_size_yen: 0
  real_order_intent_yen: null
  adv_participation_pct: 0.0
  sizing_formula_id: policy-v1-paper-to-real-ladder
counterfactual:
  if_approved:
    hypothetical_paper_proxy_position_size_oku: 0.01
    hypothetical_paper_proxy_position_size_yen: 1000000
thesis_payoff:
  max_entry_price_yen: 1750
  target_price_yen: 2100
  stop_loss_yen: 1550
  time_horizon_bd: 40
  invalidation_conditions:
  - Post-1Q operating cash-flow thesis deteriorates.
  entry_trigger: price_guard_or_revisit
  expected_upside_pct: 20.0
  expected_downside_pct: 12.9
  risk_reward_ratio: 1.55
tracking:
  mode: re_examination
  plus_15bd: null
  plus_30bd: null
market_cap_oku: 397
sector_33: 機械
avg_turnover_oku: 2.0
valuation:
  per_forward: null
  per_trailing: 14.16
  pbr: 0.53
  ev_ebitda: 9.4
  p_s: 0.21
  pcfr: 1.7
  ocf_yield: 0.5912
  fcf_yield: null
  net_cash_to_market_cap: -1.2422
  cash_to_market_cap: 0.3237
  price_to_equity: 0.5058
  equity_ratio: 0.3744
  primary_metric:
  - ocf_yield
  - p_s
---

# Research: 2026-05-05 6310 井関農機 cashflow-yield-discount

**成分**: 個別銘柄リサーチ

**Playbook id**: cashflow-yield-discount

## Thesis

6310 井関農機は、2026-05-01 candidates で `cashflow-yield-discount` と `sales-discount-growth` が同時 hit した。OCF yield 59.1%、P/S 0.21、PBR 0.53、売上 YoY +10.3% で、今回の「お買い得を拾う」目的に最も合う上位候補の一つ。ただし 2026-05-15 に 1Q 決算発表予定があり、2026-05-05 時点で新規注文は決算またぎ kill switch に近い。decision は deferred とし、1Q 通過後に thesis が崩れなければ 100 株を採用する。

## Macro regime gate

- **判定**: supportive
- **業種**: 機械
- **outlook_ref**: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **根拠**: outlook は機械を supportive。AI / 半導体製造装置色が強い gate だが、日銀短観の製造業設備投資や生産用機械の底堅さは機械セクター全体の下支えになる。
- **保守側判定**: supportive。ただし井関農機は農業機械で、半導体装置の直接恩恵は薄い。採用理由は macro より個別の CF / 低 PBR / 低 P/S。

### Portfolio macro risk budget

6310 は機械 sector supportive だが、半導体製造装置ではなく農業機械で、macro supportive の直接度は低い。さらに 2026-05-05 時点ではホルムズ・油価・米 CPI 前の macro risk が高く、1Q 決算も 2026-05-15 に迫る。OCF lane 1 位という upside は大きいが、1 単元 172,600 円で、総資金 500 万円では 3.45% でも、当面の 100 万円 tactical cap では 17.3% のイベントリスクになる。

したがって、5/7 に先行買いを入れる risk / return は 6835 より劣る。買うなら 1Q 通過後に 100 株。5/15 までに market が上がって取り逃すリスクはあるが、OCF の一過性反証を避ける価値の方が大きい。

### External deepresearch verification log

| external_ref | 採用 / 修正 / 未採用 | この research での扱い |
| --- | --- | --- |
| records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md | 採用 | 6310 は OCF lane 1 位だが、5/15 1Q 直前かつ 1 単元 172,600 円の event risk が重いため 5/7 には買わない |
| 同上 | 採用 | 5/8 米雇用、5/12 CPI、5/15 個別決算を通過してから tactical cap を 60-70% へ上げる、という段階投入を採用 |

## Cashflow snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| OCF TTM | 23,456 百万円 | primary cash source |
| OCF yield | 59.1% | cashflow-yield-discount lane 1 位 |
| CFO YoY | +165.8% | 悪化なし |
| P/S | 0.21 | sales-discount-growth も hit |
| PBR | 0.53 | 資産面の補助 |
| PCFR | 1.7 | CF 対比で安い |
| Net cash / market cap | -124.2% | net debt、大きな反対仮説 |
| 有利子負債 | 62,172 百万円 | debt quality 確認必須 |

新 screening selection では、6310 は after-outlook global rank 52/307、cashflow-yield-discount lane 1/148、sales-discount-growth lane 3/114。機械的には 9682 より明確に強い。

Source:

- 井関農機 IR 最新資料: https://www.iseki.co.jp/ir/
- IR カレンダー: https://www.iseki.co.jp/ir/support/calendar/
- candidates: records/04-candidates/2026/05/2026-05-01.yaml

## 運転資本確認

OCF yield 59.1% は非常に強いが、農業機械は棚卸資産・売掛債権・販売金融・季節性で CFO が大きく動きやすい。2025/12 期の強い OCF が、在庫圧縮や売掛金回収の一時的 cash-in だけなら、翌期に巻き戻る。

approved 条件:

- 2026-05-15 1Q で、営業 CF の急反転や在庫再積み上がりがない。
- 国内外の農機需要、販売金融、為替の説明が 2025/12 期から大きく悪化していない。
- プロジェクト Z など構造改革の進捗が、単なる短期コスト削減ではなく収益性改善につながっている。

## Capex / FCF quality

EDINET では capex tag が取得できず、FCF yield は unavailable。したがって本銘柄は FCF lane ではなく OCF lane として扱う。設備投資が後ずれしているだけなら、OCF の強さは維持されない。

1Q 後に確認する項目:

- 2025/12 期の投資 CF / 設備投資 / 減価償却の水準。
- 2026/12 期に大型投資や構造改革費用が予定されていないか。
- CFO 23,456 百万円に対して、維持投資後も equity value を支える FCF が残るか。

## Earnings quality

候補 YAML では売上 TTM 185,770 百万円、営業利益 4,225 百万円、売上 YoY +10.3%。P/S 0.21 と PBR 0.53 はかなり安い。ただし営業利益率は 2% 台で、農機需要・原材料・為替・物流費の変動で利益が薄くなりやすい。

2026/12 期の会社計画や 1Q 進捗が弱い場合、低 P/S / 低 PBR は構造的低収益の反映にすぎない。採用は、低 valuation に加え、営業 CF が巻き戻らないことを確認してからにする。

## Shareholder return

| 項目 | 確認結果 | 判定 |
| --- | --- | --- |
| 配当 | 2026/12 期予想 45 円 | positive |
| 予想配当利回り | 5/1 終値 1,726 円に対して約 2.6% | 補助 |
| 配当性向 | 2025/12 期 32.8% 程度 | 維持余地あり |
| 自社株買い | この research 時点の確認 source では明確な新規 catalyst なし | neutral |
| 減配リスク | net debt と低営業利益率があるため中 | 要監視 |

配当は下値支えにはなるが、primary thesis ではない。OCF が維持されなければ、増配期待ではなく財務負担が意識される。

Source:

- 井関農機 IR: https://www.iseki.co.jp/ir/
- 株予報 Pro 決算・配当情報: https://kabuyoho.jp/sp/report?bcode=6310

## Entry

- 2026-05-15 に 2026/12 期 1Q 決算発表予定。5/7 に買うと決算まで 6 営業日程度で、OCF thesis の反証がすぐ来るため見送る。100 万円までリスク許容しても、1 単元 172,600 円は「少額の先行オプション」とは言いにくい。
- 1Q 通過後、営業 CF / 在庫 / 受注 / 会社計画が崩れていなければ 100 株。5/1 終値 1,726 円基準で 172,600 円。
- 1,800 円以下なら採用余地あり。1Q 後に 1,900 円超まで gap up した場合は追わず、OCF yield の妙味を再計算する。

## Exit

- 利確目安: 1,950-2,050 円。PBR 0.6 倍台への小幅 rerating、または OCF yield の過度な割安修正。
- 損切り目安: 1,580 円割れ。1Q 通過後に CF thesis が残らず下落する場合は撤退。
- 時間切れ: 1Q 通過後 40 営業日。農機は決算で CF の質が判定しやすいため、期待だけで長く持たない。

## Invalidation

- 2026/12 期 1Q で営業 CF が大きく悪化し、2025/12 期の OCF が一過性だったと確認される。
- 棚卸資産の再積み上がり、販売金融、売掛金回収遅延で運転資本が悪化する。
- 有利子負債の重さが意識され、金利上昇や需要悪化で財務余力が削られる。
- 会社計画が弱く、P/S 0.21 / PBR 0.53 が低収益構造の反映と判明する。
- 機械 sector gate が adverse に悪化する。

## Position size

- **decision**: deferred
- **primary evidence hit**: cashflow-yield-discount
- **supporting evidence hit**: sales-discount-growth
- **market cap**: 397 億円
- **avg turnover**: 2.0 億円
- **paper proxy position**: 0.01 億円
- **ADV participation**: 0.5%
- **実資金想定**: 1Q 通過後 100 株、5/1 終値基準 172,600 円
- **許容上限**: 複数 evidence hit だが net debt と決算直前のため初期 1%

### お買い得候補としての結論

6310 は、数値上は今回の候補群で最も強い。cashflow lane 1 位かつ sales lane 3 位で、9682 より screening 上の説得力は明確に高い。ただし 5/15 決算直前で、OCF の一過性リスクも大きく、1 単元も大きい。2026-05-05 時点では「今すぐ買う」ではなく、「1Q 通過後に最優先で買う候補」として残す。
