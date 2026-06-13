---
ticker: '2749'
name: ＪＰホールディングス
playbook_id: cashflow-yield-discount
playbook_ref:
  ref_path: records/_playbooks/cashflow-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
research_decision:
  outcome: approved
  posture: act_now
  reason_code: defensive_childcare_cashflow_starter_post_crash
candidate_ref:
  candidates_ref: records/04-candidates/2026/06/2026-06-08.yaml
  ticker: '2749'
published_at: '2026-06-09T09:00:00+09:00'
recorded_at: '2026-06-09T09:00:00+09:00'
tradable_at: '2026-06-09T09:00:00+09:00'
position_sizing_overlay:
  paper_proxy_position_size_yen: 1000000
  real_order_intent_yen: 59000
  adv_participation_pct: 0.8333
market_cap_oku: 524
sector_33: サービス業
avg_turnover_oku: 1.2
valuation:
  per_forward: null
  per_trailing: 11.92
  pbr: 2.23
  ev_ebitda: 12.7
  p_s: 1.21
  pcfr: 8.4
  ocf_yield: 0.1195
  fcf_yield: 0.0498
  net_cash_to_market_cap: 0.2621
  cash_to_market_cap: 0.4313
  price_to_equity: 2.2867
  equity_ratio: 0.6003
  primary_metric:
  - ocf_yield
  - fcf_yield
macro_context_ref: records/01-macro-context/2026/06/macro-context-2026-06-08-post-crash.yaml
macro_context_fit:
  context_freshness: current
  fit: not_matched
  decision_effect: proceed
  required_checks:
  - サービス業（保育）は 2026-06-08 macro context の sector_tilts に明示されず not_matched。internal-demand
    かつ net cash の value として「金利・半導体非感応の domestic net-cash value の overshoot」テーマに乗るかを個別確認する。
  - 出生数の構造減（2024 に初の 70 万人割れ）が保育稼働率・補助金前提を中期で崩さないか。
  sizing_caution:
  - discretionary_panel_thesis_starter_only
thesis_payoff:
  max_entry_price_yen: 590
  target_price_yen: 680
  stop_loss_yen: 530
  expected_upside_pct: 15.25
  expected_downside_pct: 10.17
  risk_reward_ratio: 1.5
  time_horizon_bd: 40
  invalidation_conditions:
  - 出生数の構造減で保育稼働率・補助金前提が崩れ、営業 CF と増益基調が反転する。
  - cashflow-yield-discount の前提（OCF yield 11.95%、net cash）が次回決算で剥落する。
  - macro context 更新後に internal-demand defensive の前提が悪化する。
  entry_trigger: issue_203_limit_fill
entry_preflight:
  evaluated_on: '2026-06-09'
  market_relative_return_pct: 4.36
  sector_or_peer_relative_return_pct: 0.0
  macro_freshness: current
  tactical_exposure_after_order:
    sector_33_pct: 2.95
    playbook_pct: 19.48
  near_term_catalyst: false
  action: starter
  reason: >
    2026-06-09 約定の 100 株 starter。市場相対は 6/8 暴落日（日経 -3.85%）に対し candidate の
    price_change_1d +0.51% で +4.36pt と逆行耐性を示した。サービス業セクター指数の同期間リターンは
    Tier1 で取得できず peer relative は not_checked（0.0）扱いだが、sector_relative_strength_percentile
    81.8%ile で相対的に強い。macro context は current。tactical exposure は sector 2.95% /
    playbook（cashflow-yield-discount 既存 3539+8255 含む）19.5% で 50% 上限内。discretionary な
    #201 パネル thesis のため proceed ではなく starter に留める。
corporate_action_check:
  checked: true
  result: none
  note: 2026-06-08 candidate は split_adjustment_flag=false。20d/60d 下落 window 内に corporate action は確認されない。
tracking:
  mode: post_approval
  plus_15bd: null
  plus_30bd: null
---

# Research: 2026-06-09 2749 ＪＰホールディングス cashflow-yield-discount

**成分**: Decision lifecycle の **research / investment memo**（[`/docs/components/research.md`](/docs/components/research.md)）

本メモは 2026-06-08 暴落後プラン（#201 訂正版）に基づき 2026-06-09 に発注し約定した 2749 の execution を documenting する事後 research であり、screening の canonical fact source は 2026-06-08 candidate row、定性 thesis の出所は #201 専門家パネルとする。

## Thesis

- **Swing thesis**: 2026-06-08 candidates で `cashflow-yield-discount` が hit（OCF yield 11.95%、CFO YoY +49.1%、net cash / market cap 26.2%、自己資本比率 60.0%）。保育・学童は景気非感応の internal demand で、6/8 のような金利・半導体起因の sector rotation 暴落に対し price_change_1d +0.51% と逆行耐性を示した。20d -11.6% / 60d -19.0% の調整に対し、cashflow と net cash が下値を支える starter として 100 株拾う。
- **Long-hold fallback**: 中程度。net cash（+137 億）、自己資本比率 60.0%、保育の需要 defensive 性が支え。ただし保育は出生数の構造減（2024 に初の 70 万人割れ）という長期逆風があり、補助金・稼働率前提が崩れる場合は長期保有へ逃がさない。
- **Capital lock / shareholder return**: 配当はあるが PBR 2.23・P/S 1.21 と balance sheet 倍率は割安ではなく、cashflow yield と net cash が主な下支え。含み損ロック時の安心材料は配当より財務健全性に依存する。配当の正確な水準・方針は会社 IR で要確認（本メモでは未検証）。
- **AI long-term impact**: 低い。保育オペレーションの効率化余地はあるが、採用根拠・sizing 根拠には使わない。

## Macro context

- **macro_context_ref**: `records/01-macro-context/2026/06/macro-context-2026-06-08-post-crash.yaml`
- **context_freshness**: current（as_of 2026-06-08 / valid_until 2026-06-15、発注 2026-06-09 は window 内）
- **fit**: not_matched（サービス業は sector_tilts に明示されない）
- **decision_effect**: proceed（暴落は半導体・bond-proxy 集中で、internal-demand net-cash value はむしろ macro の最良 risk-reward テーマ側）
- **required_checks**: 出生数構造減が中期の稼働率・補助金前提を崩さないか。
- **sizing_caution**: discretionary パネル thesis のため starter のみ。

## Cashflow snapshot

| 指標 | 値 | source |
| --- | ---: | --- |
| OCF yield | 11.95% | 2026-06-08 candidate row |
| FCF yield | 4.98% | 2026-06-08 candidate row |
| CFO YoY | +49.1% | 2026-06-08 candidate row |
| Net cash / market cap | 26.2% | 2026-06-08 candidate row |
| 自己資本比率 | 60.0% | 2026-06-08 candidate row |
| PER (trailing) / PBR / P/S | 11.92 / 2.23 / 1.21 | 2026-06-08 candidate row |

2026-06-08 candidate row（EDINET source doc S100X161、期間 2025-04-01〜2026-03-31）を valuation の canonical fact source とする。OCF TTM 6,268 百万円、FCF TTM 2,610 百万円、net cash 13,744 百万円。OCF yield は cashflow-yield-discount の primary metric で、CFO YoY +49.1% と方向も整合する。

## 運転資本確認

保育・人材サービスは食品小売ほど在庫・仕入債務の振れが大きくないが、補助金・委託費の入金 timing と人件費の支払 timing で四半期の営業 CF は変動する。candidate の OCF TTM 6,268 百万円・CFO YoY +49.1% がそのまま永続すると見ず、次回決算で稼働率・人件費・補助金入金の動きを確認する。EDINET ベースの OCF（2,761 百万円）と J-Quants ベース（6,268 百万円）に差があるため、source 差も次回 review で突き合わせる。

## Capex / FCF quality

candidate の capex TTM は 151 百万円と軽く、FCF TTM 2,610 百万円 / FCF yield 4.98%。保育は出店・改修が capex の中心だが装置産業ではないため、FCF は安定的にプラスを維持しやすい。ただし FCF yield 4.98% は OCF yield 11.95% より低く、減価償却・運転資本・補助金 timing の影響を受ける点に留意する。

## Earnings quality

candidate row の sales_yoy +5.3%、operating_profit 6,533 百万円、CFO YoY +49.1%。#201 は「JPHD は近年 過去最高益・net cash・高 ROE で良好、#201 旧版の『減益予想』は陳腐化した前期ガイダンス」と整理した。ただし最新の通期ガイダンス・進捗は会社 IR の一次情報で未検証であり、次回決算で増益基調と利益の質（特別損益依存でないか）を確認する。

## Shareholder return

配当はあるが、正確な配当額・配当方針・優待は会社 IR で要確認（本メモでは未検証）。cashflow yield と net cash を下支えの主軸とし、配当だけを理由に追加買いしない。

## Entry

ユーザー確認により、Issue #203 の注文は 100 株、590.0 円で全量約定した。100 株の実 notional は 59,000 円で、実資金 5,000,000 円比 1.18%、tactical budget 2,000,000 円比 2.95%。discretionary な #201 パネル thesis のため初回 100 株 starter に限定する。

### Entry preflight

| Check | Value | Source / note |
| --- | --- | --- |
| Price window | basis: candidate asof 2026-06-08 / entry check: 2026-06-09 | candidate row |
| Market baseline | Nikkei 6/8 -3.85% | macro context（Tier 1 日経） |
| Sector / peer baseline | not_checked（サービス業セクター指数を Tier1 取得できず） | sector_relative_strength_percentile 81.8%ile を定性参照 |
| Relative return | vs market: +4.36pt / vs sector-peer: 0.0pt (not_checked) | price_change_1d +0.51% − (−3.85%) |
| Macro freshness | current | window 内 |
| Exposure review | after-order sector: 2.95% / playbook: 19.5% | 分母 tactical_real_budget_yen 200 万円。playbook は既存 3539+8255 を含む cashflow-yield-discount 合計 |
| Action | starter | discretionary パネル thesis のため 100 株 starter に限定 |

- **max entry price**: 590 円
- **guard price**: 590 円
- **quantity / board lot**: 100 株 / 100
- **not submitted 条件**: 590 円超では追わない。

Issue:

- https://github.com/koumatsumoto/baibai-loop/issues/203

## Exit

初期 target は 680 円（+15.25%）、stop は 530 円（−10.17%）。40 営業日 time stop は 2026-08-04 目安。出生数構造減・補助金前提の毀損や cashflow yield 剥落が確認された場合は価格にかかわらず review する。

## Invalidation

- 出生数の構造減で保育稼働率・補助金前提が崩れ、営業 CF・増益基調が反転する。
- OCF yield・net cash の cashflow thesis が次回決算で剥落する。
- macro context 更新後に internal-demand defensive の前提が悪化する。

## Position size

- decision: approved（人間パネル #201 承認・2026-06-09 実弾約定済み）。
- paper proxy: 1,000,000 円。
- estimated real order notional: 59,000 円（100 株 × 590 円）。
- ADV participation: 1,000,000 / 1.2 億円 × 100 = 0.83%。
- 実資金 concentration: 59,000 / 5,000,000 = 1.18%。tactical concentration: 59,000 / 2,000,000 = 2.95%。
- binding cap: paper→real 21% scaled cap（210,000 円）内。追加買いは出生動態・補助金前提と次回決算の確認後に再判断する。

参照: [`/docs/components/research.md`](/docs/components/research.md), [`#201`](https://github.com/koumatsumoto/baibai-loop/issues/201), [`#203`](https://github.com/koumatsumoto/baibai-loop/issues/203)
