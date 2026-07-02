---
ticker: '2331'
name: ＡＬＳＯＫ
sector_33: サービス業
playbook_id: cashflow-yield-discount
playbook_ref:
  ref_path: records/_playbooks/cashflow-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: approved
  posture: act_now
  reason_code: recurring_oligopoly_derated_on_special_demand_rolloff_stage2028_capital_return
candidate_ref:
  candidates_ref: records/02-candidates/2026/07/2026-07-01.yaml
  ticker: '2331'
published_at: '2026-07-03T02:30:00+09:00'
recorded_at: '2026-07-03T02:30:00+09:00'
tradable_at: '2026-07-03T09:00:00+09:00'
position_sizing_overlay:
  estimated_real_order_notional_yen: 105000
  guarded_max_notional_yen: 105000
  adv_participation_pct: 0.0077
market_cap_oku: 5370
avg_turnover_oku: 13.7
valuation:
  per_forward: 13.71
  per_trailing: 15.37
  pbr: 1.33
  ocf_yield: 0.1002
  fcf_yield: 0.0273
  net_cash_to_market_cap: -0.0308
  cash_to_market_cap: 0.1244
  price_to_equity: 1.2578
  equity_ratio: 0.6325
  primary_metric:
  - ocf_yield
  - per_forward
macro_context_ref: records/01-macro-context/2026/07/macro-context-2026-07-01-post-war-derisk-narrow-ath-value-rotation.yaml
macro_context_fit:
  context_freshness: current
  fit: not_matched
  decision_effect: proceed
  required_checks:
  - サービス業は 7/1 context の sector_tilts に明示 tilt が無い（neutral）。ただし context の中核読み「円高スナップバック
    に耐える内需・割安・高還元 cohort を買う」に正面から合致する（海外売上 4.7%・円高ほぼ無風・リカーリング CF・還元強化）。
  - 7/2 の NFP +57K 後、米国は AI 集中銘柄からバリューへのローテーション（Dow ATH・Nasdaq100 -1.8%）が進行しており、
    本銘柄はローテーションの受け皿側にある。
  sizing_caution:
  - order_window_straddles_fomc_0728_boj_0730_within_expiry
  - q1_fy2027_earnings_early_august_after_expiry
thesis_payoff:
  max_entry_price_yen: 1050
  fair_value_yen: 1300
  expected_upside_pct: 23.81
  expected_downside_pct: 9.52
  risk_reward_ratio: 2.5
  expected_yield_pct: 14.4
  invalidation_conditions:
  - 機械警備売上（ストック中核 1,841 億）の YoY マイナス転落（価格改定 +7.0% の浸透が逆転＝価格決定力の喪失）。
  - NDC 統合失敗（のれん減損 or 営業予想の 10% 超下方修正）。
  - 減配、または配当性向 40-50% ガイドラインの撤回。
  - 自己資本比率 50% 割れ（M&A 規律の喪失）。
  - 警備セグメント営業利益率が 8% 台へ低下（人件費インフレ > 価格転嫁の構造化）。
  entry_trigger: user_limit_order_1050_expires_20260731
durability_gate:
  net_cash: false
  operating_cf_positive: true
  low_leverage: true
  refinancing_risk: low
  dividend: true
  judgment: high
entry_preflight:
  evaluated_on: '2026-07-03'
  market_relative_return_pct: -5.01
  sector_or_peer_relative_return_pct: 0.0
  macro_freshness: current
  exposure_after_order:
    sector_33_pct: 5.2
    playbook_pct: 8.5
  near_term_catalyst: false
  action: proceed
  reason: 'ユーザー決定により 2026-07-03 に 100 株を指値 1,050 円・期限 2026-07-31 で発注（未約定・約定待ち）。20d は
    -2.05% vs benchmark 1321 +2.96% = market relative -5.01pt（sell-the-news の劣後を定量確認。sector 指数は
    Tier1 取得不可のため not_checked=0.0）。exposure after order は実資金 1,000 万円基準で ticker 1.05%（cap 6%）・
    サービス業 5.20%（cap 40%）・cashflow-yield-discount 8.50%（cap 35%）で全て上限内。ADV 参加率 0.0077%。
    直近の binary event（7/2 NFP +57K）は通過済みで、米国はバリューローテーション（thesis 追い風）。注文期限 7/31 は
    FOMC 7/28-29・BOJ 7/30-31 を跨ぐため、円 158 割れ（context の撤回トリガー）発生時は約定前でも注文を撤回して
    macro 更新後に再判断する。near_term_catalyst は Q1 決算が 8 月上旬（期限後）のため false。'
corporate_action_check:
  checked: true
  result: none
  note: 2026-07-01 candidate は split_adjustment_flag=false、直近 bars の adjustment_factor 1.0。NDC TOB（6/30 成立・
    7/6 決済）は自社が買い手であり本銘柄の株式に corporate action は無い。自社株買い 100 億（期限 2026-09-30）が進行中。
---

# Research: 2026-07-03 2331 ＡＬＳＯＫ cashflow-yield-discount

**成分**: Decision lifecycle の **research / investment memo**（[`/docs/components/thesis.md`](/docs/components/thesis.md)）

2026-07 月次積立選定（PR #288・`reports/2026-07-02-monthly-selection.html`）の最良リスクリワード銘柄。ユーザー決定により 2026-07-03 に 100 株・指値 1,050 円・期限 7/31 で発注（約定待ち）。canonical fact source は 2026-07-01 candidate row（`cashflow-yield-discount` hit・ocf_yield 10.02%）、定性 thesis は STAGE 2028 一次 PDF・業務別売上（会社 IR）・NDC TOB 開示の深掘りに基づく。

## 1. Thesis

- **何を買うか**: 国内 2 位の総合警備の寡占リカーリング（機械警備 1,841 億の月額課金・retention >95% 目標）が、万博・世界陸上特需の剥落織り込みと AI 集中相場のディフェンシブ売りで −16% de-rate。FY2027/3 会社予想は営業 557 億（+18.7%）・EPS 76.75 円と全項目増益で、業績に崩れはない。
- **成長の源泉**: 業務別 5 年トレンドで FM/防災 +36%（682→930 億）・介護 +33%・海外が牽引し、警備コアは価格改定（+7.0% 浸透）で防衛。人手不足を「売る側」（警備ロボ REBORG・AI カメラ・省人化）かつ「コスト削減側」（BPR）の両輪。NDC 買収（TOB 3,730 円・プレミアム 19.9%・P/E 16.4 倍）は成長セグメント防災への純増。
- **中計 STAGE 2028（一次 PDF 確認）**: FY2029 営業 650–720 億・経常率 ~10%・ROE ~10%・性向 40–50%・成長投資 900–1,000 億。**前中計 GD2025 は未達（経常 65 億目標 vs 実績 49.9 億）** — 中計は割引いて読む。割引後（FY2029 営業 ~600 億・EPS ~85 円）でも現値はその ~12.4 倍。
- **payoff**: FV 1,300 円（1,250–1,350）= EPS 76.75 × 16–18 倍 ≒ DDM（DPS 33 円・g4.5%・r7% → ~1,320 円）。expected_yield 14.4%/年 = FV 2 年収束の値上がり 11.3% + 配当 3.14%。保守下値 950 円（52 週安値 1,003 円のさらに −5.3% 下）で RR 2.5。

## 2. Macro context

- **macro_context_ref**: `records/01-macro-context/2026/07/macro-context-2026-07-01-post-war-derisk-narrow-ath-value-rotation.yaml`
- **context_freshness**: current（as_of 2026-07-01 / valid_until 2026-07-08、発注 2026-07-03 は window 内）
- **fit**: not_matched（サービス業は sector_tilts に明示 tilt なし）。ただし context の中核読み「円高スナップバックに耐える内需・割安・高還元 cohort を買う」に正面から合致（海外売上 4.7%・円高ほぼ無風・リカーリング CF・還元強化）。
- **decision_effect**: proceed。7/2 NFP +57K 通過後の米国はバリューローテーション（Dow ATH・Nasdaq100 −1.8%）で、本銘柄群は受け皿側。
- **sizing_caution**: 注文期限 7/31 は FOMC 7/28-29・BOJ 7/30-31 を跨ぐ（円 158 割れで撤回ルール）。Q1 決算は 8 月上旬で期限後。

## Cashflow snapshot

FY2026/3: 営業 CF 538 億（OCF yield 10.02%・cfo_yoy +26.1%）、現金 668 億（cash/時価 12.4%）。**ネット資金ポジションは基準で符号が変わる（honest flag）**: screening EDINET 基準は net_cash/時価 −3.08%（軽度ネットデット）、IRBANK 基準の検算は現預金 ~778 億 − 有利子負債 ~531 億 = +247 億。乖離は警備輸送業特有の預り現金・預り金等の範囲差で、durability_gate.net_cash は機械検証可能な EDINET 基準を尊重して false とした。いずれの基準でも営業 CF 538 億・自己資本比率 63%（screening）/ 56.8%（会社開示）の床は厚い。

## 運転資本確認

月額課金の警備サービスで在庫リスクは実質なし。警備輸送（731 億）は輸送用預り現金が BS の現金・負債を両建てで膨らませる業種特性があり、net cash の機械値が実体より厳しく出る（上記 honest flag と同根）。回収サイトは安定し、運転資本起因の CF 毀損リスクは低い。

## Capex / FCF quality

FCF yield 2.73% と OCF yield 10.02% の差が示すとおり、機械警備機器・拠点・M&A への投資が重い装置型サービス（NDC 買収 836 億の 51% も投資側）。維持・成長投資後でも配当 168 億 + 自社株買い 100 億（総還元 268 億）は営業 CF 538 億で 2 倍カバー。STAGE 2028 の cash allocation（成長投資 900–1,000 億 + 配当 550 億+α / 3 年）とも整合。

## Earnings quality

営業 CF 538 億 / 純益 333 億 = 1.61 倍（利益のキャッシュ裏付けが強い）。希薄化なし（自社株買いで逆方向）。FY2026 の増益に含まれる特需（万博・世界陸上 = 常駐 +12.6%）は FY2027 ガイダンスが剥落を織り込み済みで、経常率 5 期（8.3→8.9→7.7→7.9→7.8→8.4%）はレンジ内 — ピーク益外挿ではない。

## 3. Valuation snapshot

| 指標 | 値 | source |
| --- | ---: | --- |
| PER (trailing / forward) | 15.37 / 13.71 | 2026-07-01 candidate row |
| PBR | 1.33 | candidate row |
| OCF yield / FCF yield | 10.02% / 2.73% | candidate row |
| cash / 時価総額 | 12.44% | candidate row |
| net_cash / 時価総額 | −3.08%（EDINET 基準・honest flag 参照） | candidate row |
| 自己資本比率 | 63.3%（screening）/ 56.8%（会社開示） | candidate row / IR |
| 配当利回り（予想） | 3.14%（DPS 33 円） | IR |
| price_change 20d / 60d | −2.05% / −16.0% | candidate row |
| gap_from_52w_low | 0.0488 | candidate row |

## Shareholder return

DPS 23.7→25.8→29.2→33.0 円（FY2027/3 予想）と連続増配、性向 42.6% → STAGE 2028 で 40–50% ガイドライン明文化。自社株買い 100 億/900 万株（期限 2026-09-30・進行中）。FY2027/3 総還元 ≈268 億 / 純益 373 億 = **総還元性向 ~72%**。営業 CF 538 億が総還元を 2 倍カバーし、イールドチェイスではない。

## Entry

ユーザー決定により 2026-07-03 に買い 100 株・指値 1,050 円（終値 1,052.5 のわずか下・約定確率優先）・**期限 2026-07-31**・現物で発注。未約定・約定待ち。guarded max notional 105,000 円。preflight は front matter のとおり action=proceed（market relative −5.01pt = sell-the-news の劣後を確認、全キャップ内、NFP 通過済み）。**円 158 割れ時は約定前でも撤回**。

## Exit

long-hold 契約: 価格 stop は置かない。全売りは (a) invalidation 発火（下記）または (b) 割高化（FV 1,300 円を大幅超過し、正常化益 18–20 倍超への再過熱）のみ。含み損時は配当 3.14% を受け取りながら invalidation を月次 review で監視する。

## Invalidation

- 機械警備売上（ストック中核）の YoY マイナス転落（価格決定力の喪失）。
- NDC 統合失敗（のれん減損 or 営業予想の 10% 超下方修正）。
- 減配、または性向 40–50% ガイドラインの撤回。
- 自己資本比率 50% 割れ（M&A 規律の喪失）。
- 警備セグメント営業利益率が 8% 台へ低下（人件費インフレ > 価格転嫁の構造化）。

## Position size

- decision: approved（ユーザー 2026-07-03 発注済み・約定待ち。100 株 × 指値 1,050 円 = 105,000 円）。
- 実資金 concentration: 105,000 / 10,000,000 = 1.05%（ticker cap 6% 内）。
- exposure after order: サービス業 5.20%（cap 40%）/ cashflow-yield-discount 8.50%（cap 35%）— position records の約定実額から再計算済み。
- ADV participation: 105,000 / 13.7 億 = 0.0077%（cap 5%）。
- 追加買いは条件付き増額ルール（選定レポート §6）と月次予算の範囲でのみ検討。

参照: [`reports/2026-07-02-monthly-selection.html`](/reports/2026-07-02-monthly-selection.html), PR [#288](https://github.com/koumatsumoto/baibai-loop/pull/288)
