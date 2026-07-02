---
ticker: '9715'
name: トランス・コスモス
sector_33: サービス業
playbook_id: cashflow-yield-discount
playbook_ref:
  ref_path: records/_playbooks/cashflow-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: approved
  posture: act_now
  reason_code: conservative_guidance_beat_midterm11_capital_return_deepvalue_lowbeta
candidate_ref:
  candidates_ref: records/04-candidates/2026/06/2026-06-30.yaml
  ticker: '9715'
published_at: '2026-07-01T15:30:00+09:00'
recorded_at: '2026-07-01T15:30:00+09:00'
tradable_at: '2026-07-01T15:30:00+09:00'
position_sizing_overlay:
  estimated_real_order_notional_yen: 355700
  adv_participation_pct: 0.1317
market_cap_oku: 1577
avg_turnover_oku: 2.7
valuation:
  per_forward: 9.98
  per_trailing: 10.3
  pbr: 1.05
  ev_ebitda: 3.9
  p_s: 0.4
  pcfr: 7.6
  ocf_yield: 0.1317
  fcf_yield: 0.1034
  net_cash_to_market_cap: 0.4822
  cash_to_market_cap: 0.5004
  price_to_equity: 1.1321
  equity_ratio: 0.6222
  primary_metric:
  - ocf_yield
  - per_trailing
macro_context_ref: records/01-macro-context/2026/06/macro-context-2026-06-30-overshoot-reverted-ath-risk-on.yaml
macro_context_fit:
  context_freshness: current
  fit: not_matched
  decision_effect: proceed
  required_checks:
  - サービス業は 6/30 context の sector_tilts に明示 tilt が無い（neutral）。ただし本銘柄の実体は AI/DX 実装受益（CX/BPO の生成AI導入・NTT Com Digital
    BPO）で、context の情報・通信業 tailwind（AI/DX 受益）の論拠と整合する点を確認する。
  - 指数は高値圏（日経 ATH−3.8%・benchmark 1321 の 20d +5.27%）で、割安は銘柄固有の de-rating（AI-BPO 懸念と成長減速で取り残された低β バリュー）に偏在する。指数の押し目ではない。
  sizing_caution:
  - risk_on_rally_regime_contrarian_entry_waived_by_low_correlation_beta_0_15
  - q1_fy2027_earnings_early_august_event_risk_within_hold_window
thesis_payoff:
  max_entry_price_yen: 3557
  fair_value_yen: 4000
  expected_upside_pct: 12.45
  expected_downside_pct: 8.63
  risk_reward_ratio: 1.44
  invalidation_conditions:
  - 国内BPO事業（売上¥1,200億・利益率7.1%＝高収益エンジン）の売上/マージンが失速する。
  - 新中期計画（26-28）の営業利益 CAGR 11% 軌道が崩れ、通期 op 成長が <5% に鈍化する、または計画が撤回される。
  - 国内CX事業のデジタルコンタクトセンター/席売上が前年割れ（＝生成AIによる人月BPO侵食の顕在化）。
  - 配当性向40%（2026年6月〜）の還元強化が撤回される。
  - macro 悪化（円急伸のキャリー巻戻し / 米テック melt-up 反転）でネットキャッシュ床（時価の0.48）近辺の¥3,050を割り、再審査で thesis 中核が毀損。
  entry_trigger: user_fill_3557_20260701
entry_preflight:
  evaluated_on: '2026-07-01'
  market_relative_return_pct: -11.89
  sector_or_peer_relative_return_pct: 0.0
  macro_freshness: current
  exposure_after_order:
    sector_33_pct: 20.74
    playbook_pct: 37.26
  near_term_catalyst: false
  action: proceed
  reason: '2026-07-01 にユーザー指示で 100 株を 3,557 円で約定（指値 3,560 円に対し dip 約定）。regime は risk_on_rally（benchmark 1321 の 20d
    +5.27%）で、9715 の 20d は −6.62%＝market relative −11.89pt と 大幅劣後（AI-BPO 懸念と成長減速で高値ラリーに取り残された低β バリューの定量裏付け）。通常この局面の
    contrarian entry は指数に構造的に劣後するが、本銘柄は β=0.15（12 候補中最低）＝low_correlation で regime bet を希薄化するため、action=exception
    を exception_basis=[low_correlation] で正当化する。 sector/peer relative はサービス業セクター指数を Tier1 取得できず not_checked（0.0）。tactical
    exposure after order はサービス業 20.74% / cashflow-yield-discount 37.26%（分母 tactical 200 万円）で 50% 上限内。 実資金基準では ticker
    3.56%（cap 8%）・サービス業 4.15%（cap 45%）・playbook 7.45%（cap 35%）で全て内。 9715 はサービス業で、既存の deployed 集中（情報・通信業 41%＝9682/9692/9470/4432）を悪化させず分散する。
    近接 catalyst は Q1 FY2027（8月上旬）だが routine ゆえ near_term_catalyst=false とし、low_correlation で waive。

    '
corporate_action_check:
  checked: true
  result: none
  note: 2026-06-30 candidate は split_adjustment_flag=false、price_change_60d −8.99% と異常値域でなく、直近 daily bars の adjustment_factor
    も 1.0。分割・併合・TOB 等の corporate action は確認されない。
---

# Research: 2026-07-01 9715 トランス・コスモス cashflow-yield-discount

**成分**: Decision lifecycle の **research / investment memo**（[`/docs/components/thesis.md`](/docs/components/thesis.md)）

本メモは 2026-07-01 にユーザー指示で 100 株を 3,557 円で約定した 9715 の execution を documenting する research。screening の canonical fact source は 2026-06-30 candidate row（`cashflow-yield-discount` hit）、定性 thesis の出所は 6/30 選定（PR #282）と 9715 個別の一次 IR 深掘り（会社決算説明資料 4/30・四半期実績・中期事業計画）。

## 1. Thesis

- **Swing / value thesis**: 2026-06-30 candidate で `cashflow-yield-discount` が hit（PER trailing 10.3・forward 9.98、EV/EBITDA 3.9、ocf_yield 13.2%・fcf_yield 10.3%、net_cash/時価 0.48）。52 週安値近辺（gap 0.045）で、AI-BPO 懸念＋成長減速懸念で高値ラリーに取り残された低β バリューの mean-reversion＋緩やかな再レートを狙う。EV/EBIT 4〜5 倍は異例の割安。
- **来季（FY2027/3）の読み**: 会社予想は売上 ¥4,100億(+4.1%)・営業益 ¥168億(**+1.5%**)・経常 ¥178億(−6.2%=FX益剥落)・純益 ¥135億(+3.2%)。会社は決算説明資料で「営業利益は増益を確保。粗利率の改善を進めつつ、**新中期計画に基づく積極投資を織り込み、営業利益率は保守的な水準**」と明言＝**投資先行の慎重ガイド**。**FY2026/3 は期初ガイドを大きく上回った**（営業益 155→165.6 で +6.8%、純益 115→130.8 で **+13.8% beat**）ため、FY27 も上振れ余地。4Q の営業益 YoY −2.0億は「人事制度改定の一時金」の一過性で構造減速ではない（8 四半期連続増収）。
- **中期成長ロードマップ（＝ dated catalyst）**: 新中計 2026-2028 は営業益 **CAGR 11%**（¥166億→約¥225億、+¥59億）、売上 CAGR 6%、営業利益率 +0.6pt、**ROE 10%+ 維持・株主資本コスト超**。2035ビジョン 売上¥8,000億〜1兆・営業利益率 7-10%。
- **Long-hold fallback**: 高い。**ネットキャッシュ約 ¥760億（時価の 0.48）**・自己資本比率 62%・営業CF潤沢（ocf_yield 13.2%）・**配当利回り約 4.0%**（¥140→¥145 予想）で、含み損ロック時も配当と財務健全性が支える。BPO はリカーリング色が強く低循環（β 0.15）。
- **Capital lock / shareholder return（＝ dated catalyst）**: **配当性向を 40% へ引上げ（2026年6月配当から）**、中計期間の株主還元総額 ¥160億、ROE 10%+ 維持を明言。筆頭株主はトランスコスモス財団 18%（支配的）だが、還元強化に舵を切った点は「trapped cash」懸念を部分的に解消。
- **AI long-term impact**: 中（受益寄り、ただし上限あり）。CXスクエア生成AI（応対アシスト/分析支援）、**NTT Com と Digital BPO 5 年 ¥1,000億**提携、エスカレーション 6 割削減の実績。中計は「AIエコノミー上の新サービス」を成長ドライバーに位置づけ、「グローバルCXは AI の登場で規模拡大から差別化へ」と自認。AI は単独の採用・sizing 根拠にはせず、valuation・CF・財務と合わせて判断する。

## 2. Macro context

- **macro_context_ref**: `records/01-macro-context/2026/06/macro-context-2026-06-30-overshoot-reverted-ath-risk-on.yaml`
- **context_freshness**: current（as_of 2026-06-30 / valid_until 2026-07-07、約定 2026-07-01 は window 内）
- **fit**: not_matched（サービス業は 6/30 context の sector_tilts に明示 tilt が無い）。ただし実体は AI/DX 実装受益で、context の情報・通信業 tailwind（AI/DX 受益）論拠と整合。
- **decision_effect**: proceed（高値圏リスクオン回帰の中、割安は銘柄固有の de-rating に偏在。低β・ネットキャッシュ床で下値保護）。
- **required_checks**: 指数は高値圏で押し目ではない点、AI-BPO 侵食が受益に転じるか。
- **sizing_caution**: risk_on_rally での contrarian entry を β 0.15 の low_correlation で waive、Q1 FY27（8月上旬）の event risk。

## Cashflow snapshot

FY2026/3（EDINET doctype 120、期末 2026-03-31）: 営業CF ¥20.76B（OCF yield 13.17%、cfo +19.9% yoy）、現金及び現金同等物 ¥78.90B（現金等 ¥80.47B）、有利子負債 ¥4.44B → **ネットキャッシュ ¥76.03B（時価総額 ¥157.7B の 0.48）**。PCFR 7.6x。営業CF ¥20.76B は営業利益 ¥16.56B を上回り（OCF/営業益 = 1.25）、cfo 成長 +19.9% は営業益 +14.4% を上回る＝キャッシュ創出が利益成長を上回る良質。

## 運転資本確認

人月型 BPO/CX の労働集約サービスで在庫リスクは実質なし。運転資本は主に売掛金・未払費用。OCF/営業益 = 1.25（>1）、cfo_yoy +19.9% > sales_yoy +4.79% ＝ 売上成長を上回るキャッシュ化で運転資本は tailwind 側（回収・稼働率改善）。装置産業でないため運転資本の急拡大による CF 毀損リスクは低い。

## Capex / FCF quality

capex TTM ¥4.45B（対売上 1.1%＝アセットライト）、減価償却 ¥4.15B（capex ≈ D&A ＝ 維持更新水準で成長投資の先行負担は軽い）。**FCF TTM ¥16.31B（FCF yield 10.34%）、FCF/OCF = 78.6%**（アセットライトゆえ高い FCF 転換）。中計は AI・セキュリティ・ガバナンスへの積極投資を織り込むが、capex 自体は軽く、投資は主に販管費（人材・AI）側。**FCF yield 10.34% ≫ 配当利回り 4.0% ＝ 配当は FCF で十分カバー**（イールドチェイスではない）。

## Earnings quality

accruals_to_assets −0.0252（**負の accruals ＝ 現金利益が会計利益を上回る保守的・良質**）、net_share_change_yoy 0.0（希薄化なし）。営業CF ¥20.76B / 純益 ¥13.08B = 1.59（利益のキャッシュ裏付けが強い）。FY26 純益 +15.5% は営業益 +14.4% に為替差益 ¥1.16B が上乗せされた形で、非経常の FX 成分は限定的・営業利益はキャッシュ裏付けあり。来期（FY27）は経常 −6.2%（FX益剥落）だが営業益 +1.5%（保守ガイド）で、コア収益力は維持。

## 3. Valuation snapshot

| 指標 | 値 | source |
| --- | ---: | --- |
| PER (trailing / forward) | 10.3 / 9.98 | 2026-06-30 candidate row |
| PBR | 1.05 | candidate row |
| EV/EBITDA | 3.9 | candidate row |
| P/S | 0.40 | candidate row |
| OCF yield / FCF yield | 13.17% / 10.34% | candidate row |
| net cash / 時価総額 | 0.4822（約 ¥760億） | candidate row |
| 自己資本比率 | 62.2%（会社開示 57.3%＝少数株主除くベース） | candidate row / IR |
| 配当利回り (予想) | 約 4.0%（¥145） | IR |
| price_change 20d / 60d | −6.62% / −8.99% | candidate row |
| gap_from_52w_low | 0.045 | candidate row |

## 4. 一時的割安の原因仮説

- 2026 年の生成AI 起因の BPO/人月モデル懸念（コンタクトセンター自動化が席課金収益を侵食するという構造フィア）による de-rating。
- 高値圏リスクオン回帰（6/26 押し目が 2 営業日で反転）が半導体・メガテック主導で、低β 内需サービスが非参加（9715 の 20d は benchmark 1321 比 −11.89pt）。
- FY27 営業益 +1.5% の慎重ガイド（投資先行）と、純益 −39% の見出し誤読（実際は +15.5% 増益）がスクリーンを脅かした可能性。
- いずれも業績崩壊でなく、緩やかな増勢＋投資先行の慎重ガイド＝ピーク利益トラップではない点が、循環株（自動車部品・遊戯機械等）と決定的に異なる。

## 5. 反対仮説（弱気・AI が BPO を侵食するシナリオ）

- **人月モデルの自動化デフレ**: 生成AI がコンタクトセンターの席数/単価を削減し、効率益が顧客の値下げ要求に流れれば自社売上は伸びない。FY27 営業益 +1.5% はこの圧力（or 保守計画）と整合。「高収益モデルへの転換」はまだ結果でなく賭け。
- **薄マージン**: 国内CX 2.8%・グローバル 1.3% は薄く、中計のマージン改善（+0.6pt、グローバル +4.2pt）は未実証。グローバルは東南アジア大型案件縮小のドラッグ。
- **trapped cash**: 財団 18%・支配的な株主構成で、¥760億のネットキャッシュは 40% 還元でも全ては還元されない。
- これらが重なれば「低成長のまま安い」塩漬け（value trap）化。ただし 4% 配当をもらいながら待てる点、中計 11% 成長・40% 還元という開示された道筋がある点で、循環ピーク株よりは質が高い。

## 6. Catalyst

- **Q1 FY2027/3（2026年8月上旬 開示）**: 国内BPO の増収・利益率 7% 台維持、国内CX のデジタル/席売上の前年比（AI 侵食の有無）、進捗率が中計に整合するか。
- **配当性向 40% 引上げ（2026年6月配当〜）** の実行、中計 KPI（op CAGR 11%・ROE 10%+）の進捗、NTT Com Digital BPO（¥1,000億/5年）の受注・売上化。

## 7. Price reaction

2025 年来のレンジ上限 ¥4,085（52 週高値）→ 6/30 終値 ¥3,595 → 2026-07-01 約定 ¥3,557（指値 ¥3,560 に対し dip 約定）。52 週安値 ¥3,440 近辺（gap 0.045）。アナリスト・コンセンサスは「買い」・平均目標株価 ¥4,000（+約12%）。

## 8. Positioning / liquidity（集中度の honest flag）

- avg_turnover_oku 2.7（流動性母集団内）。adv participation は paper proxy ¥1,693,810 ベースで 0.6273%。
- **分散方向の add（重要）**: 9715 は**サービス業**で、約定前の deployed open notional ¥1,291,400 のうち最大集中である**情報・通信業 41%（9682/9692/9470/4432）を悪化させない**。サービス業は既存 2749（¥59,000）のみで、9715 追加後もサービス業は実資金比 8.29%・tactical 比 20.74%。
- playbook は cashflow-yield-discount（既存 8255/3539/2749＝¥389,500）に足すため、同 playbook は tactical 比 37.26%（実資金比 14.9%、cap 35% 内）。
- policy cap（実資金 1000 万円基準）: ticker 8%＝80 万円に対し 35.57 万円、サービス業 sector 45%＝450 万円に対し 41.47 万円、playbook 35%＝350 万円に対し 74.52 万円で**全て上限内**。tactical 200 万円に対し deployed 合計は約定後 ¥1,647,100（82.4%）で、tactical dry powder は約 ¥35 万に低下（実資金 dry powder は ¥8,352,900＝83.5%）。

## 9. Shareholder return

配当利回り約 4.0%（FY2027 予想 ¥145）、**配当性向を 40% へ引上げ（2026年6月配当〜）**、中計期間 株主還元総額 ¥160億、ROE 10%+ 維持・株主資本コスト超を明言。ネットキャッシュ厚く FCF が配当を十分カバー（fcf_yield 10.3% ＞ 配当利回り 4%）＝イールドチェイスではない。資産ロック中の収益は配当が主な支え。

## 10. Entry

ユーザー指示・確認により、2026-07-01 に 100 株を 3,557 円で約定（指値 3,560 円に対し dip 約定）。実 notional 355,700 円（100 株 × 3,557 円）。実資金 10,000,000 円比 3.56%、tactical budget 2,000,000 円比 17.79%。低β・分散方向の add ゆえフルサイズ（100 株）だが、単元 100 株が最小単位のため 1 単元に留める。

### Entry preflight

| Check | Value | Source / note |
| --- | --- | --- |
| Price window | basis: candidate asof 2026-06-30 / fill: 2026-07-01 | candidate row + user fill |
| Market baseline | benchmark proxy 1321 20d +5.27% | SQLite daily bars |
| Relative return | vs market: −11.89pt / vs sector-peer: 0.0pt (not_checked) | candidate 20d −6.62% − 1321 20d +5.27% |
| Macro freshness | current | window 内（[6-30, 7-07]） |
| Regime | risk_on_rally（1321 20d +5.27%） | 高値圏リスクオン回帰 |
| Exposure after order | サービス業 20.74% / cashflow-yield-discount 37.26%（分母 tactical 200 万円） | 実資金基準では ticker 7.11%・サービス業 8.29%・playbook 14.9% |
| Action | proceed（当時の判定は exception、exception_basis=[low_correlation]） | risk_on_rally の contrarian entry を β 0.15 の low_correlation で waive。regime gate 廃止後の契約では proceed に対応 |

- **約定価格 / guard**: 3,557 円（指値 guard 3,560 円）
- **quantity / board lot**: 100 株 / 100

## 11. Exit

初期 target は 4,000 円（+12.45%、アナリスト・コンセンサス目標＝予想 PER 約 11→約 12.5 への部分リレート、bull では ¥4,400）、stop は 3,250 円（−8.63%、52 週安値 ¥3,440 割れの breakdown ライン）。120 営業日 time stop は 2026-12-30 目安。**位置づけは 6–12 ヶ月の patient value+income（22 日スイングでない）**で、外れて売却を逃した場合は配当 4.0%＋ネットキャッシュ床（¥3,050 ≈ 時価の 0.48 近辺）で長期保有へ切り替える。

## 12. Invalidation

- 国内BPO（高収益エンジン）の売上/マージン失速。
- 中計 op 成長 <5%／CAGR 11% 軌道の崩れ・計画撤回。
- 国内CX のデジタル/席売上が前年割れ（生成AI による人月侵食の顕在化）。
- 配当性向 40% 還元強化の撤回。
- macro 悪化（円急伸のキャリー巻戻し / 米テック melt-up 反転）で ¥3,050 を割る。

## 13. Position size

- decision: approved（ユーザー 2026-07-01 実弾約定済み、100 株 @ 3,557 円）。
- paper proxy: 1,693,810 円（real / 0.21）。
- real order intent: 355,700 円（100 株 × 3,557 円）。
- ADV participation: 1,693,810 / 2.7 億円 × 100 = 0.6273%。
- 実資金 concentration: 355,700 / 10,000,000 = 3.56%（ticker cap 8% 内）。tactical concentration: 355,700 / 2,000,000 = 17.79%。
- 位置づけ: 6–12 ヶ月の patient value+income。追加買いは Q1 FY27（8月上旬）で国内BPO マージンと国内CX 席売上（AI 侵食の有無）を確認後に再判断する。

参照: [`/docs/components/thesis.md`](/docs/components/thesis.md), [`#282`](https://github.com/koumatsumoto/baibai-loop/pull/282)
