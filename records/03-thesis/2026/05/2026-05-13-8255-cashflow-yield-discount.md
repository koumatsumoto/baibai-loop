---
ticker: '8255'
name: アクシアル　リテイリング
playbook_id: cashflow-yield-discount
playbook_ref:
  ref_path: records/_playbooks/cashflow-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: approved
  posture: act_now
  reason_code: post_earnings_oversold_defensive_cashflow
candidate_ref:
  candidates_ref: records/02-candidates/2026/05/2026-05-08.yaml
  ticker: '8255'
published_at: '2026-05-13T09:15:00+09:00'
recorded_at: '2026-05-13T09:15:00+09:00'
tradable_at: '2026-05-13T09:00:00+09:00'
position_sizing_overlay:
  estimated_real_order_notional_yen: 309000
  adv_participation_pct: 0.1818
thesis_payoff:
  max_entry_price_yen: 1030
  fair_value_yen: 1180
  invalidation_conditions:
  - FY2027 guidance weakness proves structural rather than conservative.
  - Same-store traffic or gross margin continues to deteriorate after the app stamp removal effect.
  - Dividend / benefit catalyst fails to stabilize long-term holder demand.
  entry_trigger: post_earnings_oversold_entry
  expected_upside_pct: 14.56
  expected_downside_pct: 8.74
  risk_reward_ratio: 1.67
market_cap_oku: 938
sector_33: 小売業
avg_turnover_oku: 1.7
valuation:
  per_forward: 11.08
  per_trailing: 10.1
  pbr: 0.93
  ev_ebitda: 8.1
  p_s: 0.32
  pcfr: 5.5
  ocf_yield: 0.1805
  fcf_yield: 0.0574
  net_cash_to_market_cap: 0.2385
  cash_to_market_cap: 0.3244
  price_to_equity: 0.9855
  equity_ratio: 0.6606
  primary_metric:
  - ocf_yield
  - post_earnings_oversold
  - shareholder_return
macro_context_ref: records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml
macro_context_fit:
  context_freshness: current
  fit: not_matched
  decision_effect: proceed
  required_checks: []
corporate_action_check:
  checked: true
  result: none
  note: Checked during research review.
---

# Research: 2026-05-13 8255 アクシアル　リテイリング cashflow-yield-discount

## Thesis

8255 は今回の 3 銘柄に「直近急落後のリバーサル候補」として採用する。2026-05-08 candidates では OCF yield 18.1%、PBR 0.93、P/S 0.32、net cash / market cap 23.9%、自己資本比率 66.1% が同時に成立していた。さらに、Yahoo Finance の週次値下がり率ランキングでは 2026-05-08 時点で週次 -10.04%、年初来安値 995 円をつけており、従来の slow-moving cash / asset cheapness ではなく、決算後の売られすぎを拾う枠として意味がある。

採用理由は、売られた直後に公式決算の business quality と shareholder-return evidence が残っていること。FY2026 は売上、営業利益、経常利益が連結会計年度として過去最高で、営業 CF は 16,939 百万円へ改善した。一方で FY2027 会社計画は営業利益 -4.0%、親会社株主帰属利益 -9.1% で、市場が嫌った可能性がある。したがって thesis は「減益計画が構造悪化ではなく保守的 / 競争対応費用込みなら、PBR 1 倍割れと累進配当導入で下値が固い」。

Long-hold fallback は 3 銘柄内で最も強い。食品スーパーは高成長ではないが、需要が景気循環に相対的に鈍く、同社は net cash、66.1% の自己資本比率、累進配当、長期保有優待新設を持つ。含み損になっても、決算後の仮説が崩れない限り保有継続の説明がつく。

AI long-term impact は低い。直接の AI 成長銘柄ではないが、物流・店舗オペレーション・在庫管理の効率化余地はある。今回の primary thesis には使わない。

## Macro context

- 判定: not_matched / proceed。
- Sector: 小売業。
- Source: `records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml`。
- 注意: CPI と燃料価格は粗利と販管費に効くため、macro は支援材料ではない。ただし内需食品スーパーで外需ショックへの直接感応度は低く、保有耐性は高い。

## Oversold check

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| Yahoo 週次下落率 | -10.04% | post-earnings selloff |
| 2026-05-08 price | 1,003 円 | year-low 近辺 |
| 2026-05-12 previous close | 1,001 円 | selloff 後も戻り弱い |
| 年初来安値 | 995 円 | 2026-05-08 |

従来 selection が 4w / 60d と financial cheapness を重視するのに対し、この候補は「決算後 1 週間で売られたが、公式決算と株主還元が反証になっている」点を採用理由にする。これは今回ユーザーが指摘した「素早い変化に対応できていない」問題への暫定手当でもある。

## Cashflow snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| OCF TTM | 16,939 百万円 | strong |
| OCF yield | 18.1% | strong |
| FCF yield | 5.7% | positive |
| Net cash / market cap | 23.9% | balance-sheet support |
| Equity ratio | 66.1% | strong |
| PBR | 0.93 | below book |
| PER forward | 11.08 | reasonable after guidance cut |

Cashflow-yield lane は exact。FCF yield は OCF ほど強くないが、維持投資後も positive。net cash と high equity ratio があるため、6310 のような debt risk は小さい。

## 運転資本確認

FY2026 の OCF は 16,939 百万円で、前期 11,815 百万円から改善した。小売業の cash conversion は在庫、買掛金、ポイント / 優待、設備投資 timing で動くため、OCF yield 18.1% をそのまま永続化しない。次の review では、棚卸資産、仕入債務、キャッシュレス手数料、物流費、人件費が FY2027 にどれだけ粗利を圧迫するかを見る。

approved を維持する条件:

- 既存店客数の下振れがアプリ来店スタンプ廃止による一時要因に収まる。
- 低価格対応で粗利率が継続的に削られない。
- 在庫積み増しと支払条件変化で OCF が急減しない。
- 競合出店・改装の増加に対し、独自商品と店舗運営で売上総利益を守れる。

## Capex / FCF quality

Candidate row では capex source が `purchase_of_fixed_assets`、FCF yield は 5.7%。OCF yield 18.1% と比べると、店舗投資・物流投資後の free cash flow は中程度に落ちる。したがって primary thesis は FCF 高利回りではなく、安定 OCF、net cash、PBR 1 倍割れ、株主還元の組み合わせ。

FY2027 に出店・改装・物流投資が重くなり FCF が薄くなるなら、PBR 1 倍割れでも target multiple を上げない。反対に、投資負担を吸収しながら OCF が維持されるなら、1030 円以下はリバーサル余地がある。

## Earnings quality

FY2026 は sales +4.8%、OP +1.0%、ordinary +0.7%、parent net income -2.3%。売上成長に対して利益成長は薄く、低価格競争、人件費、物流費、キャッシュレス手数料が利益率を抑えている。営業利益と経常利益が過去最高である一方、FY2027 は OP -4.0% 計画なので、質の高い成長株としてではなく、安定内需の selloff reversal として扱う。

Earnings quality の反証は、減益計画が保守的ではなく、競争激化で構造的に margin が下がること。ここが確認されたら、累進配当があっても position を閉じる。

## Latest IR check

公式 FY2026 決算短信では、売上高 295,536 百万円（YoY +4.8%）、営業利益 12,185 百万円（+1.0%）、経常利益 12,799 百万円（+0.7%）、親会社株主帰属利益 8,803 百万円（-2.3%）。売上、営業利益、経常利益は連結会計年度として過去最高。営業 CF は 16,939 百万円で、前期 11,815 百万円から増加した。

FY2027 会社計画は売上 300,000 百万円（+1.5%）、営業利益 11,700 百万円（-4.0%）、経常利益 12,000 百万円（-6.2%）、親会社株主帰属利益 8,000 百万円（-9.1%）。ここが売られた主因になり得る。採用は、過去最高決算 + conservative guidance selloff のリバーサルであり、growth rerating ではない。

Source:

- https://www.axial-r.com/2026/05/07/5737
- https://ctr.axial-r.com/wp-content/uploads/2026/05/07113156/r2603jp.pdf

## Business durability

事業は食品スーパー「原信」「ナルス」「フレッセイ」。FY2026 は競合出店・改装が多い中で、低価格対応と独自商品強化、コスト・コントロールで営業利益を維持した。客数はアプリの来店スタンプ廃止で一時的に下振れしたと会社が説明しているため、FY2027 の弱い計画が一時要因なのか、競争激化による構造的 margin decline なのかを追う。

長期保有に耐える条件は、既存店客数の戻り、粗利率の下げ止まり、物流・人件費増を売上総利益と販管費統制で吸収できること。

## Shareholder return

2026-05-07 に累進配当の導入を公表。FY2027 以降 5 年間、連結 EPS の概ね 30% 程度を目安とし、記念配当等を除く前期水準に対して維持または増配を原則とする。会社予想配当は 29 円、Yahoo Finance の 2026-05-12 参考指標では配当利回り 2.90%。

同日に長期保有株主優待制度も新設。1 年以上、3 年以上の継続保有株主向けに、株主優待券選択時の追加優遇を設ける。優待は primary thesis ではないが、食品スーパーの long-hold demand を作る補助材料になる。

Source:

- https://www.axial-r.com/2026/05/07/5735
- https://www.axial-r.com/2026/05/07/5733
- https://finance.yahoo.co.jp/quote/8255.T

## Entry

1030 円以下なら 300 株 starter。2026-05-12 前日終値 1001 円、単元約 100,100 円なので、300 株でも tactical budget に対する負担は約 30% に収まる。機械 screen では top 5 ではないが、直近急落・決算確認済み・長期保有耐性の組み合わせを優先して、今回の 1 位にする。

## Exit

初期 target は 1,180 円。PBR 1.1 倍弱、年初来安値からの小幅戻りを想定する。stop は 940 円。40 営業日以内に 1,100 円台へ戻らず、月次 / 既存店 / 粗利の悪化が続く場合は撤退。

## Invalidation

- FY2027 減益計画が保守的ではなく構造的競争悪化と確認される。
- 既存店客数がアプリ要因を超えて弱い。
- 粗利率低下、物流費、人件費、キャッシュレス手数料が販管費統制を上回る。
- OCF / FCF が FY2026 から大きく悪化する。
- 累進配当・長期保有優待が株価下支えにならず、PBR 1 倍割れが常態化する。

## Position size

- decision: approved。
- paper proxy: 1,500,000 円。
- 実資金 starter: 300 株、最大 309,000 円。
- policy ADV participation は paper proxy 1,500,000 円を avg turnover 1.7 億円へ当てた 0.8824%。流動性上の問題は小さい。
