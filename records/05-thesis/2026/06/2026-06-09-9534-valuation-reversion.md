---
ticker: '9534'
name: 北海道瓦斯
playbook_id: valuation-reversion
playbook_ref:
  ref_path: records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: approved
  posture: act_now
  reason_code: discretionary_panel_value_reversion_starter_against_screen_signal
candidate_ref:
  candidates_ref: records/04-candidates/2026/06/2026-06-08.yaml
  ticker: '9534'
published_at: '2026-06-09T09:00:00+09:00'
recorded_at: '2026-06-09T09:00:00+09:00'
tradable_at: '2026-06-09T09:00:00+09:00'
position_sizing_overlay:
  estimated_real_order_notional_yen: 74200
  adv_participation_pct: 0.0495
market_cap_oku: 665
sector_33: 電気・ガス業
avg_turnover_oku: 1.5
valuation:
  per_forward: null
  per_trailing: 5.74
  pbr: 0.68
  ev_ebitda: 7.6
  p_s: 0.38
  pcfr: 2.5
  ocf_yield: 0.4063
  fcf_yield: 0.0976
  net_cash_to_market_cap: -0.629
  cash_to_market_cap: 0.1473
  price_to_equity: 0.6671
  equity_ratio: 0.5031
  primary_metric:
  - pbr
  - per_trailing
macro_context_ref: records/01-macro-context/2026/06/macro-context-2026-06-08-post-crash.yaml
macro_context_fit:
  context_freshness: current
  fit: headwind
  decision_effect: caution
  required_checks:
  - 電気・ガス業は 2026-06-08 macro context で headwind（BOJ 利上げ＝bond-proxy 逆風 + 原油高＝燃料コストの二重逆風、「net cash の厚い個別のみ」）。9534
    は net debt（−418 億 / 時価比 −62.9%）でこの但し書きに反するため、利上げ・原料費感応を継続確認する。
  - screening の evidence_hit は valuation-reversion / sector_rotation_short_sell（=セクター相対が弱い「売り」候補シグナル、15.2%ile）であり、買いの
    screening 根拠ではない。買い thesis は
  - 都市ガスの原料費調整（pricing power）が燃料コスト上昇を時間差で転嫁できるか、tariff lag を確認する。
  sizing_caution:
  - macro_headwind_utility_net_debt
  - screen_signal_was_short_not_long
  - discretionary_panel_thesis_starter_only
thesis_payoff:
  max_entry_price_yen: 742
  fair_value_yen: 850
  expected_upside_pct: 14.56
  expected_downside_pct: 9.7
  risk_reward_ratio: 1.5
  invalidation_conditions:
  - BOJ 利上げで net debt 9534 の支払利息・bond-proxy ディスカウントが拡大する。
  - 原油高（Brent $96 近辺）が原料費調整の lag を超えてマージンを圧迫する。
  - PBR 0.68 / PER 5.74 の割安が valuation trap（成長性・還元の欠如）で mean-reversion しない。
  entry_trigger: issue_203_limit_fill
entry_preflight:
  evaluated_on: '2026-06-09'
  market_relative_return_pct: 3.06
  sector_or_peer_relative_return_pct: -0.53
  macro_freshness: current
  exposure_after_order:
    sector_33_pct: 3.71
    playbook_pct: 3.71
  near_term_catalyst: false
  action: starter
  reason: '2026-06-09 約定の 100 株 starter。市場相対は 6/8 暴落日（日経 -3.85%）に対し candidate の price_change_1d −0.79% で +3.06pt、4
    週のセクター相対は ticker_return_4w −8.2% vs sector_return_4w −7.67% で −0.53pt とほぼ中立。macro context は current だが 電気・ガス業は
    headwind（利上げ + 原油高）で、9534 は net debt のため macro の「net cash の厚い個別のみ」但し書きに 反する。さらに screening の evidence_hit は sector_rotation_short_sell（売りシグナル）で買い根拠では
    ない。これらの caveat を踏まえ proceed ではなく starter に限定する。tactical exposure は sector / playbook とも 3.71% で 50% 上限内。

    '
corporate_action_check:
  checked: true
  result: none
  note: 2026-06-08 candidate は split_adjustment_flag=false。20d/60d 下落 window 内に corporate action は確認されない。
---

# Research: 2026-06-09 9534 北海道瓦斯 valuation-reversion

**成分**: Decision lifecycle の **research / investment memo**（[`/docs/components/thesis.md`](/docs/components/thesis.md)）

本メモは 2026-06-08 暴落後プラン（#201 訂正版）に基づき 2026-06-09 に発注し約定した 9534 の execution を documenting する事後 research である。**重要な前提として、9534 の screening evidence_hit は `valuation-reversion / sector_rotation_short_sell`（=セクター相対が弱い「売り」候補シグナル）であり、買いの screening 根拠ではない。** 買い thesis は #201 専門家パネルの discretionary 判断であり、本メモはその判断と data 上の矛盾点を併記して honest に記録する。

## Thesis

- **Swing thesis（discretionary）**: #201 パネルは 9534 を「都市ガスの原料費調整＝pricing power、内需ディフェンシブ、PBR 0.68 / PER 5.74 の割安」として新規採用した。20d -8.2% / 60d -13.8% の調整に対し、低 PBR・低 PER の mean-reversion を 100 株 starter で拾う、というもの。
- **Data 上の矛盾（must read）**: (i) screening は 9534 を **short 候補（sector_rotation_short_sell, 15.2%ile）** として出しており long の根拠ではない。(ii) 9534 は **net debt −418 億（時価比 −62.9%、有利子負債 512 億）** で、#201 が 3222 USMH を除外した理由（純有利子負債＝BOJ 利上げ直撃）と同型。(iii) 2026-06-08 macro context は電気・ガス業を **headwind**（利上げ bond-proxy 逆風 + 原油高燃料コスト）とし「net cash の厚い個別のみ」と限定しているが、9534 はこれに反する。#201 の「利上げ耐性」評価は macro context・balance sheet と整合しない。
- **Long-hold fallback**: 限定的。自己資本比率 50.3%、OCF yield は高い（candidate 40.6%）が net debt で利上げ感応が高く、bond-proxy ディスカウント拡大が長期保有の足枷になりうる。原料費調整による pricing power が下支えだが lag があり、原油高局面ではマージンが先に圧迫される。
- **Capital lock / shareholder return**: PBR 0.68・PER 5.74 の割安と配当が下支え。2026-06-13 IR 確認で年間配当 23 円・連結配当性向 30% 目標（増配）を確認（§Shareholder return）。net debt 下でも payout 30% で還元継続。
- **AI long-term impact**: 低い。判断には使わない。

## Macro context

- **macro_context_ref**: `records/01-macro-context/2026/06/macro-context-2026-06-08-post-crash.yaml`
- **context_freshness**: current（as_of 2026-06-08 / valid_until 2026-06-15、発注 2026-06-09 は window 内）
- **fit**: headwind（sector_tilt `utilities-rate-and-oil-headwind`：BOJ 利上げ bond-proxy 逆風 + 原油高燃料コストの二重逆風、net cash の厚い個別のみ）
- **decision_effect**: caution（headwind かつ net debt のため、approved でも sizing を絞り proceed にしない）
- **required_checks**: 利上げ・原料費感応、tariff lag、screening の short シグナルとの矛盾。
- **sizing_caution**: macro headwind × net debt、screen は short、discretionary thesis のため starter。

## Valuation snapshot

| 指標 | 値 | source |
| --- | ---: | --- |
| PER (trailing) | 5.74 | 2026-06-08 candidate row |
| PBR | 0.68 | 2026-06-08 candidate row |
| P/S | 0.38 | 2026-06-08 candidate row |
| OCF yield | 40.63% | 2026-06-08 candidate row |
| FCF yield | 9.76% | 2026-06-08 candidate row |
| Net cash / market cap | −62.9%（net debt） | 2026-06-08 candidate row |
| 自己資本比率 | 50.3% | 2026-06-08 candidate row |

2026-06-08 candidate row（EDINET source doc S100X2AY、期間 2025-04-01〜2026-03-31）を canonical fact source とする。OCF TTM 27,028 百万円、FCF TTM 6,489 百万円、capex TTM 8,192 百万円、net cash −41,839 百万円（有利子負債 51,224 百万円）。OCF yield は高いが、capex 重く net debt である点に注意。

## 一時的割安の原因仮説

低 PBR・低 PER は、(a) 利上げ・原油高という電気・ガス業 sector headwind の織り込み、(b) net debt と capex 負担、(c) 6/8 rotation での連れ安、の複合。#201 は (c) を overshoot と見て mean-reversion を狙うが、(a)(b) は構造・macro 要因で一過性とは言い切れない。したがって本件は「明確な一時的割安」ではなく、caveat 付きの低 sizing starter とする。

## 反対仮説

- **screening は売りシグナル**: evidence_hit は sector_rotation_short_sell。本来の screening 推奨は long ではない。買いは screening を override した discretionary 判断。
- **net debt × 利上げ**: −418 億の net debt は BOJ 利上げ観測下で逆風。#201 自身が同型の USMH を除外した基準と矛盾する。2026-06-13 IR 確認でも会社は有利子負債削減を優先方針として明示しており、net debt が実在する財務制約であることが裏付けられた。
- **macro headwind**: 電気・ガス業は macro context で headwind、「net cash の厚い個別のみ」の限定に 9534 は反する。
- **valuation trap リスク**: 公益の低 PBR は成長性・還元の乏しさを反映した恒常的割安の可能性があり、mean-reversion しない場合がある。

## Catalyst

明確な near-term catalyst は置かない（candidate の next_earnings_date は null）。margin of safety は低 PBR・低 PER と原料費調整による pricing power。

**2026-06-13 IR 一次確認**: 原料費調整制度は実在し、2026年3月期中間は同制度による販売単価上昇でガス売上が伸び、売上 +4.9%（713.67 億円）、経常利益 +40.3%（68.63 億円）、自己資本比率 49.4% と業績は改善した。pricing power は lag があるものの実際に効いており、低 PER 5.74 が単なる valuation trap ではなく業績改善を伴う割安である可能性を補強する。ただし net debt と利上げ感応の caveat（下記）は解消しない。

Source:

- https://www.nikkei.com/nkd/company/kessan/?scode=9534

## Price reaction

candidate row: price_change_1d −0.79%（6/8 暴落日に market 比では逆行耐性）、5d +2.5%、20d -8.2%、60d -13.8%、gap_from_52w_low +37.6%、sector_relative_strength_percentile 15.2%ile（弱い）。evidence_hit metrics: ticker_return_4w −8.2% / sector_return_4w −7.67%。

## Positioning / liquidity

avg_turnover_oku 1.5 億円、board lot 100 株。paper proxy 100 万円での ADV participation 0.67%、liquidity cap（5% ADV）内。100 株 742 円 = 実 notional 74,200 円は流動性・分散とも問題ない。

## Shareholder return

PBR 0.68・PER 5.74 の割安と配当が下支え。**2026-06-13 IR 一次確認**: 年間配当は中間 11.5 円 + 期末 11.5 円 = 23 円（株式分割考慮後ベースで増配）、連結配当性向は 30% を目標水準とする。net debt を抱えつつも payout 30% 目標で還元を継続しており、配当は含み損ロック時の下支えとして機能する。ただし還元の主軸は配当であり、net debt のため自己株買い余地は限定的とみる。

Source:

- https://irbank.net/E04511/dividend
- https://www.nikkei.com/nkd/company/kessan/?scode=9534

## Entry

ユーザー確認により、Issue #203 の注文は 100 株、742.0 円で全量約定した。100 株の実 notional は 74,200 円で、実資金 5,000,000 円比 1.48%、tactical budget 2,000,000 円比 3.71%。macro headwind × net debt × screen short のため、discretionary 100 株 starter に限定する。

### Entry preflight

| Check | Value | Source / note |
| --- | --- | --- |
| Price window | basis: candidate asof 2026-06-08 / entry check: 2026-06-09 | candidate row |
| Market baseline | Nikkei 6/8 -3.85% | macro context（Tier 1 日経） |
| Sector / peer baseline | sector_return_4w −7.67% | candidate evidence_hit metrics |
| Relative return | vs market: +3.06pt / vs sector-peer: −0.53pt | price_change_1d −0.79% − (−3.85%) / ticker_return_4w −8.2% − (−7.67%) |
| Macro freshness | current | window 内。ただし sector は headwind |
| Exposure review | after-order sector: 3.71% / playbook: 3.71% | 分母 tactical_real_budget_yen 200 万円 |
| Action | starter | macro headwind × net debt × screen は short のため、discretionary 100 株 starter に限定 |

- **max entry price**: 742 円
- **guard price**: 742 円
- **quantity / board lot**: 100 株 / 100
- **not submitted 条件**: 742 円超では追わない。

Issue:

- https://github.com/koumatsumoto/baibai-loop/issues/203

## Exit

初期 target は 850 円（+14.56%）、stop は 670 円（−9.70%）。40 営業日 time stop は 2026-08-04 目安。BOJ 利上げで net debt の支払利息・bond-proxy ディスカウントが拡大する、原油高が原料費調整 lag を超えてマージンを圧迫する、低 PBR が valuation trap で mean-reversion しない、のいずれかが確認された場合は価格にかかわらず review する。

## Invalidation

- BOJ 利上げで net debt の支払利息・bond-proxy ディスカウントが拡大する。
- 原油高が原料費調整 lag を超えてマージンを圧迫する。
- 低 PBR・低 PER が valuation trap で mean-reversion しない。

## Position size

- decision: approved（人間パネル #201 承認・2026-06-09 実弾約定済み。screening は short シグナルで buy 根拠ではない点を明示）。
- paper proxy: 1,000,000 円。
- estimated real order notional: 74,200 円（100 株 × 742 円）。
- ADV participation: 1,000,000 / 1.5 億円 × 100 = 0.67%。
- 実資金 concentration: 74,200 / 5,000,000 = 1.48%。tactical concentration: 74,200 / 2,000,000 = 3.71%。
- binding cap: paper→real 21% scaled cap（210,000 円）内。caveat（macro headwind・net debt・screen short）が解消されない限り追加買いしない。

参照: [`/docs/components/thesis.md`](/docs/components/thesis.md), [`#201`](https://github.com/koumatsumoto/baibai-loop/issues/201), [`#203`](https://github.com/koumatsumoto/baibai-loop/issues/203)
