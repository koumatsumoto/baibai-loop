---
ticker: '8929'
name: 青山財産ネットワークス
sector_33: 不動産業
playbook_id: valuation-reversion
playbook_ref:
  ref_path: records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: approved
  posture: act_now
  reason_code: fee_compounder_record_profit_masked_by_intentional_revenue_deferral_52w_low
candidate_ref:
  candidates_ref: records/02-candidates/2026/07/2026-07-01.yaml
  ticker: '8929'
published_at: '2026-07-03T02:30:00+09:00'
recorded_at: '2026-07-03T02:30:00+09:00'
tradable_at: '2026-07-03T09:00:00+09:00'
position_sizing_overlay:
  estimated_real_order_notional_yen: 119000
  guarded_max_notional_yen: 119000
  adv_participation_pct: 0.1082
market_cap_oku: 301
avg_turnover_oku: 1.1
valuation:
  per_forward: 10.85
  per_trailing: 9.8
  pbr: 2.49
  fcf_yield: 0.0255
  net_cash_to_market_cap: 0.2618
  price_to_equity: 2.6008
  equity_ratio: 0.3854
  primary_metric:
  - ev_ebitda
  - per_trailing
thesis_payoff:
  max_entry_price_yen: 1190
  fair_value_yen: 1625
  expected_upside_pct: 36.55
  expected_downside_pct: 15.97
  risk_reward_ratio: 2.29
  expected_yield_pct: 17.1
  invalidation_conditions:
  - 財産コンサルティング売上（利益エンジン・FY2025 +45.8%）の 2 期連続の失速・減少。
  - 税制・規制による不動産小口化（ADVANTAGE CLUB）ビジネスの恒久毀損（期ズレでなく商品性の喪失）。
  - 連続増配の停止（減配）＝ 16 期連続増配・性向 50% 方針の後退。
  - ROE の持続的な 12% 割れ（FY2025 実績 25.7% からの構造劣化）。
  - 在庫「仕入決済時に AC 組成し在庫リスクを発生させない」方針の放棄（BS リスクへの転化）。
  entry_trigger: user_limit_order_1190_expires_20260731
durability_gate:
  net_cash: true
  operating_cf_positive: true
  low_leverage: true
  refinancing_risk: low
  dividend: true
  judgment: high
entry_preflight:
  evaluated_on: '2026-07-03'
  market_relative_return_pct: -10.94
  sector_or_peer_relative_return_pct: 0.0
  exposure_after_order:
    sector_33_pct: 1.19
    playbook_pct: 4.27
  action: proceed
  reason: 'ユーザー決定により 2026-07-03 に 100 株を指値 1,190 円・期限 2026-07-31 で発注（未約定・約定待ち）。20d は
    -7.98% vs benchmark 1321 +2.96% = market relative -10.94pt、gap_from_52w_low 0.0（52 週安値ちょうど）で
    「減収ヘッドラインの額面視による de-rate」を定量確認（sector 指数は Tier1 取得不可のため not_checked=0.0）。
    exposure after order は実資金 1,000 万円基準で ticker 1.19%（cap 6%）・不動産業 1.19%（新規 sector・cap 40%）・
    valuation-reversion 4.27%（cap 35%）で全て上限内。ADV 1.1 億の薄い板のため成行禁止・指値厳守（参加率 0.108%）。
    発注前条件だった BS 一次確認は短信 PDF 直接読解でクリア（ネットキャッシュ +78.9 億 = 現預金 141.0 億 −
    有利子負債 62.1 億）。注文期限 7/31 は FOMC 7/28-29・BOJ 7/30-31 を跨ぐため、円 158 割れ時は約定前でも撤回して
    macro 更新後に再判断する。'
corporate_action_check:
  checked: true
  result: none
  note: 2026-07-01 candidate は split_adjustment_flag=false、直近 bars の adjustment_factor 1.0。分割・併合・TOB なし。
    自己株消却を 2025-08・2026-02 に実施（希薄化と逆方向）。
---

# Research: 2026-07-03 8929 青山財産ネットワークス valuation-reversion

**成分**: Decision lifecycle の **research / investment memo**（[`/docs/components/thesis.md`](/docs/components/thesis.md)）

2026-07 月次積立選定（PR #288・`reports/2026-07-02-monthly-selection.html`）の第 2 候補。ユーザー決定により 2026-07-03 に 100 株・指値 1,190 円・期限 7/31 で発注（約定待ち）。canonical fact source は 2026-07-01 candidate row（`valuation-reversion` hit: ev_ebitda の sector median gap −63.8%・self-range 19.2%ile）、定性 thesis は FY2025/12 決算短信 PDF の直接読解（BS 明細・在庫方針・資本規律）に基づく。

## 1. Thesis

- **何を買うか**: 相続増税 × 高齢化の構造成長市場で、フィー型財産コンサル（FY2025 +45.8%・うちチェスター G 16.5 億）が牽引する ROE 25.7% のコンパウンダー。売上 −8.4% は税制改正対応で不動産小口化商品（ADVANTAGE CLUB）の販売を意図的に期ズレさせた見かけで、純利益は 27.5 億の過去最高。「減収」ヘッドラインの額面視とスタンダード小型からの資金流出で 52 週安値まで de-rate。
- **財務床（BS 一次確認済み）**: ネットキャッシュ +78.9 億（現預金 141.0 億 − 有利子負債 62.1 億 = 時価総額の 26.2%）・自己資本比率 44.4%。会社が短信に「資本コスト約 8% 想定・性向 50%・純資産配当率 11.9%」と資本規律を明記。
- **payoff**: FV 1,625 円（1,500–1,750）= 正常化 EPS 115–125 円 × 13–15 倍。expected_yield 17.1%/年（保守側: FV 下限 1,500 円の 2 年収束 12.3% + 配当 4.87%。中心 1,625 円なら 21.7%）。保守下値 1,000 円で RR 2.29。

## 3. Valuation snapshot

| 指標 | 値 | source |
| --- | ---: | --- |
| PER (trailing / forward) | 9.8 / 10.85 | 2026-07-01 candidate row（EPS TTM 合成修正後） |
| PBR | 2.49（ROE 25.7% が正当化） | candidate row |
| EV/EBITDA sector median gap | −63.8%（self-range 19.2%ile） | candidate evidence_hits |
| net_cash / 時価総額 | +26.2%（BS 一次確認 +78.9 億） | candidate row / 短信 BS |
| FCF yield | 2.55%（在庫積み増し年の見かけ・平準化前提） | candidate row |
| 自己資本比率 | 44.4%（会社開示）/ 38.5%（screening） | 短信 / candidate row |
| 配当利回り（予想） | 4.87%（DPS 58 円・16 期連続増配・性向 52.5%） | 短信 |
| price_change 20d / 60d | −7.98% / −8.26% | candidate row |
| gap_from_52w_low | 0.0（52 週安値ちょうど） | candidate row |

## 4. 一時的割安の原因仮説

- 税制改正対応で ADVANTAGE CLUB の販売を意図的に期ズレ → 売上 −8.4% の「減収」ヘッドラインを市場が額面視（実態は営業 +10%・純益 +13.2% の過去最高）。
- 東証スタンダード小型からの資金流出（AI 集中相場で取り残された cohort）。
- screening の実績 PER 49.5 倍という偽値（単一四半期 EPS の artifact・本 PR で修正済み）が機械層の見かけを悪化させていた可能性。
- いずれも業績毀損ではなく、期ズレ在庫 41.6 億は FY2026 の組成売上の仕込み — ピーク益トラップではない。

## 5. 反対仮説

- **税制の恒久毀損**: FY2025 の期ズレが「一時対応」でなく、小口化スキーム（任意組合）への課税強化が恒久化すれば、不動産取引セグメントの商品性が毀損する（最大テール・invalidation 対象）。
- **金利・地価**: 金利上昇の持続は富裕層の不動産投資意欲と AC の商品魅力を削ぐ。コンサルのフィーだけでは現在の ROE 水準を維持できない可能性。
- **キーパーソン**: 創業社長依存とトップコンサルタントの人的資本が競争力の源泉で、退任・流出は再現性を毀損。
- これらが重なれば「高 ROE の看板が剥げた小型株」として放置される。ただし配当 4.87% を受け取りながら待てる点、ネットキャッシュ 26% の床、資本規律の明文化が value trap 化への防御。

## 6. Catalyst

- **FY2026/12 Q2 決算（2026 年 8 月）**: 財産コンサル売上の伸び持続と AC 組成（在庫 41.6 億の売上化）の進捗。
- **通期での AC 組成売上の戻り**: FY2026 予想（売上 −6.7%）は保守的で、在庫の組成が進めば上振れ。
- 16 期連続増配の継続（FY2026 予想 DPS 58 円）と自己株消却の継続。

## 7. Price reaction

52 週レンジの安値ちょうど（gap_from_52w_low 0.0）。20d −7.98%（benchmark 1321 +2.96% に対し relative −10.94pt）。2/6 の決算発表（減収・最高益）以降、ヘッドラインReactionで売られ続けた形。

## 8. Positioning / liquidity

- 東証スタンダード・ADV 1.1 億 — 板が薄く、**成行禁止・指値厳守**（100 株 = 参加率 0.108%）。
- 不動産業は既存建玉に無く**新規 sector（分散方向の add）**。約定後の不動産業 exposure 1.19%（cap 40%）。
- playbook valuation-reversion は既存 9534 + 4432（308,200 円）に足して 4.27%（cap 35%）。

## Shareholder return

DPS 46→53→58 円（FY2026 予想）= 16 期連続増配・性向 52.5%・利回り 4.87%。純資産配当率 11.9% は自称資本コスト 8% を上回る。自己株消却を 2025-08・2026-02 に実施。純益 27.5 億 vs 配当総額 ~12.7 億でカバー余裕（在庫積み増し年でも配当原資に問題なし）。

## Entry

ユーザー決定により 2026-07-03 に買い 100 株・指値 1,190 円（終値 1,199 のわずか下）・**期限 2026-07-31**・現物で発注。未約定・約定待ち。guarded max notional 119,000 円。preflight は front matter のとおり action=proceed。**円 158 割れ時は約定前でも撤回**。1,150 円以下への下落時は条件付き増額ルール（選定レポート §6）の対象。

## Exit

long-hold 契約: 価格 stop は置かない。全売りは (a) invalidation 発火（下記）または (b) 割高化（FV 1,625 円を大幅超過し、正常化益 18 倍超への再過熱）のみ。含み損時は配当 4.87% を受け取りながら月次 review で監視する。

## Invalidation

- 財産コンサルティング売上（利益エンジン）の 2 期連続の失速・減少。
- 税制・規制による小口化ビジネスの恒久毀損（期ズレでなく商品性の喪失）。
- 連続増配の停止（減配）。
- ROE の持続的な 12% 割れ。
- 在庫「決済時組成」方針の放棄（BS リスクへの転化）。

## Position size

- decision: approved（ユーザー 2026-07-03 発注済み・約定待ち。100 株 × 指値 1,190 円 = 119,000 円）。
- 実資金 concentration: 119,000 / 10,000,000 = 1.19%（ticker cap 6% 内）。
- exposure after order: 不動産業 1.19%（新規 sector）/ valuation-reversion 4.27% — position records の約定実額から再計算済み。
- ADV participation: 119,000 / 1.1 億 = 0.108%（cap 5%）。
- 追加買いは 1,150 円以下の条件付き増額ルールと月次予算の範囲でのみ検討。

参照: [`reports/2026-07-02-monthly-selection.html`](/reports/2026-07-02-monthly-selection.html), PR [#288](https://github.com/koumatsumoto/baibai-loop/pull/288)
