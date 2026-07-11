---
ticker: '3539'
name: ＪＭホールディングス
playbook_id: cashflow-yield-discount
playbook_ref:
  ref_path: records/_playbooks/cashflow-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: approved
  posture: act_now
  reason_code: defensive_retail_cashflow_starter_before_q3
candidate_ref:
  candidates_ref: records/02-candidates/2026/05/2026-05-08.yaml
  ticker: '3539'
published_at: '2026-05-23T20:43:22+09:00'
recorded_at: '2026-05-23T20:43:22+09:00'
tradable_at: '2026-05-25T09:00:00+09:00'
position_sizing_overlay:
  estimated_real_order_notional_yen: 127500
  adv_participation_pct: 0.0708
thesis_payoff:
  max_entry_price_yen: 1275
  fair_value_yen: 1450
  invalidation_conditions:
  - 2026-06-12 予定の Q3 決算で既存店・粗利・営業 CF の thesis が崩れる。
  - 食品小売の低価格志向が粗利率を継続的に削り、営業増益が維持できない。
  - Q3決算で既存店・粗利・営業CFの悪化が確認される。
  entry_trigger: issue_176_limit_fill
  expected_upside_pct: 13.73
  expected_downside_pct: 5.88
  risk_reward_ratio: 2.33
market_cap_oku: 677
sector_33: 小売業
avg_turnover_oku: 1.8
valuation:
  per_forward: 9.67
  per_trailing: 27.0
  pbr: null
  ev_ebitda: 7.4
  p_s: 0.35
  pcfr: 5.1
  ocf_yield: 0.1954
  fcf_yield: 0.0811
  net_cash_to_market_cap: 0.2314
  cash_to_market_cap: 0.2924
  price_to_equity: 1.4281
  equity_ratio: 0.5832
  primary_metric:
  - ocf_yield
  - fcf_yield
  - sales_growth
corporate_action_check:
  checked: true
  result: none
  note: 2026-05-08 candidate は split_adjustment_flag=false。公式 Q2 短信の 2025-11-01 1:2 split は 20d/60d 下落 window 外で、1株指標・配当予想に反映済み。
---

# Research: 2026-05-23 3539 ＪＭホールディングス cashflow-yield-discount

## Thesis

3539 ＪＭホールディングスは、2026-05-08 candidates で `cashflow-yield-discount` と `sales-discount-growth` が同時 hit した。OCF yield 19.5%、FCF yield 8.1%、net cash / market cap 23.1%、自己資本比率 58.3%、P/S 0.35、売上 YoY +9.0% がそろっており、食品スーパー主体の defensive retail として、短期の売られすぎを 100 株だけ拾う。

Swing thesis は、2026-05-08 時点の 20d -14.9%、60d -25.6% の下落に対し、公式 Q2 では売上・営業利益・経常利益が増収増益を維持しているため、構造悪化ではなく決算前警戒と小売セクター内の需給悪化で売られている可能性を取る、というもの。

Long-hold fallback は中程度。スーパーマーケット事業が売上の中心で需要は相対的に defensive、net cash と自己資本比率が支えになる。一方で食品小売は人件費、物流費、電気料金、円安・原材料高による価格転嫁と低価格志向の板挟みを受けるため、粗利と既存店が崩れる場合は長期保有へ逃がさない。

AI long-term impact は低い。店舗オペレーション、在庫、需要予測の効率化余地はあるが、今回の採用根拠とsizing根拠には使わない。

## Cashflow snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| OCF TTM | 13,233 百万円 | strong |
| OCF yield | 19.5% | strong |
| FCF TTM | 5,492 百万円 | positive |
| FCF yield | 8.1% | supportive |
| Net cash / market cap | 23.1% | balance-sheet support |
| Equity ratio | 58.3% | adequate |
| P/S | 0.35 | low sales multiple |

2026-05-08 candidate row を valuation の canonical fact source とする。公式 2026年7月期 Q2 決算短信では、中間期営業 CF 7,874 百万円、投資 CF -834 百万円、現金及び現金同等物の中間期末残高 19,801 百万円を確認した。候補 row の TTM OCF / FCF と方向は整合している。

Source:

- https://contents.xj-storage.jp/xcontents/AS93761/0d65265e/93df/4e16/8293/b364405ff191/140120260313582015.pdf

## 運転資本確認

Q2 の中間連結 CF では、棚卸資産の増加 -1,253 百万円に対し、仕入債務の増加 +3,618 百万円が営業 CF を押し上げている。食品小売では在庫、仕入債務、出店、価格転嫁の timing で cash conversion が大きく動くため、OCF yield 19.5% はそのまま永続化しない。

次回 review では、既存店売上、粗利率、仕入債務の反動、在庫増加、出店費用、人件費・物流費・電気料金を確認する。特に Q3 で営業 CF が急減する場合、cashflow-yield-discount の primary thesis は弱くなる。

## Capex / FCF quality

Candidate row の FCF は EDINET CFO 7,874 百万円から有形固定資産取得 2,382 百万円を差し引いた 5,492 百万円で、FCF yield 8.1%。設備投資後も FCF はプラスだが、同社は新規出店と関西エリア拡大を中期計画に置いており、投資負担は軽くない。

したがって primary thesis は「高 FCF の恒常化」ではなく、食品小売の増収増益、営業 CF、net cash、低 P/S の組み合わせによる starter entry とする。

## Earnings quality

公式 2026年7月期 Q2 決算短信では、売上高 101,189 百万円（YoY +9.0%）、営業利益 5,604 百万円（+12.7%）、経常利益 5,705 百万円（+13.0%）と増収増益だった。一方、親会社株主に帰属する中間純利益は特別損失 407 百万円により 2,508 百万円（-11.4%）へ減少した。

会社の通期予想は売上高 196,000 百万円（+5.3%）、営業利益 10,900 百万円（+8.5%）、経常利益 11,000 百万円（+8.4%）、親会社株主に帰属する当期純利益 7,000 百万円（+8.4%）で据え置き。Q2 の利益進捗は十分だが、Q3 決算前に追加 sizing するほどの余裕はない。

Source:

- https://contents.xj-storage.jp/xcontents/AS93761/0d65265e/93df/4e16/8293/b364405ff191/140120260313582015.pdf

## Shareholder return

2026年7月期の会社予想配当は、2025-11-01 の 1:2 株式分割後ベースで中間 12 円、期末 12 円、年間 24 円。100 株以上の株主優待は 1 年以上の継続保有が条件で、精肉関連商品、米、グループ商品券の選択肢がある。

配当と優待は long-hold fallback の補助材料になる。ただし今回の entry は Q3 決算前の starter であり、株主還元だけを理由に追加買いしない。

Source:

- https://jm-holdings.co.jp/ir/dividend.html
- https://jm-holdings.co.jp/ir/shareholder.html

## Entry

ユーザー確認により、Issue #176 の注文は 100 株、1,275 円で約定した。100 株の実 notional は 127,500 円で、実資金 5,000,000 円比 2.55%、tactical budget 1,000,000 円比 12.75%。Q3決算予定が近く、初回100株に限定する。

Issue:

- https://github.com/koumatsumoto/baibai-loop/issues/176

## Exit

初期 target は 1,450 円、stop は 1,200 円。1,450 円は 2026年4月下旬の戻り水準、1,200 円は entry から -5.9% の損切り線として置く。2026-06-12 予定の Q3 決算で既存店、粗利、営業 CF が崩れた場合は価格にかかわらず review する。

40 営業日 time stop は 2026-07-21 目安。7月中旬までに 1,400 円台へ戻れない場合、資金ロックを続ける理由を再確認する。

## Invalidation

- Q3 決算で売上・営業利益・営業 CF のいずれかが明確に悪化する。
- 仕入債務増による Q2 営業 CF 押し上げが反動で剥落する。
- 低価格志向、人件費、物流費、電気料金、原材料高により粗利率が継続的に低下する。
- 新規出店・関西展開の投資負担が FCF を継続的に薄める。
- 既存店売上、粗利率、営業CFの悪化でdefensive thesisが崩れる。

## Position size

- decision: approved。
- paper proxy: 1,000,000 円。
- 実資金 starter: 100 株、127,500 円。
- ADV participation: 1,000,000 / 1.8 億円 * 100 = 0.5556%。
- 実資金 concentration: 127,500 / 5,000,000 = 2.55%。
- tactical concentration: 127,500 / 1,000,000 = 12.75%。
- 制約: Q3決算前のため追加買いは保留する。累計200株はQ3後に再検討し、300株は別途thesis updateを要求する。
