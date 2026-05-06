---
ticker: '9682'
name: ＤＴＳ
playbook_id: sales-discount-growth
playbook_snapshot:
  ref_path: records/_playbooks/sales-discount-growth/2026-05-01T000000+0900.md
  content_sha256: sha256:37358359da7bd9a56b029ac89ec734acc2a427c3b0c9a53df1c6a59afe03d4ad
  effective_from: '2026-05-01T00:00:00+09:00'
policy_snapshot:
  ref_path: records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md
  content_sha256: sha256:14b5b2838171f923ef8098ff1c501ee9c6ab30343e5e73d9b5ef874c1e12dc16
  effective_from: '2026-05-01T00:00:00+09:00'
portfolio_exposure_snapshot_ref:
  ref_path: records/_portfolio-exposure/2026/05/2026-05-05T133000+0900.yaml
  content_sha256: sha256:d36667a89ccd83fec9b6384c931bd11a7e40307766596d36307d49b4ed0e5408
selected_supporting_evidence_refs:
- source: candidate
  evidence_hit_id: candidate-2026-05-01-9682-sales-discount-growth
research_decision:
  outcome: approved
  posture: act_now
  reason_code: payoff_and_policy_pass
candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
  screen_run_id: screening-20260501-79ec46a8
  ticker: '9682'
  candidate_id: candidate-2026-05-01-9682
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai_draft: false
published_at: '2026-05-05T13:31:15+09:00'
recorded_at: '2026-05-05T13:31:15+09:00'
tradable_at: '2026-05-07T09:00:00+09:00'
macro_regime_gate:
  aggregate_status: supportive
  decision_effect: pass
  source_scope: sector
  reducer_id: macro-regime-reducer-v1
  inputs: []
policy_overrides:
- override_id: override-1
  type: decision_flip
  prior_state_ref: origin/main:records/05-research/2026/05/2026-05-05-9682-sales-discount-growth.md
  prior_state: 'research_decision.outcome: passed'
  new_state: 'research_decision.outcome: approved'
  reason: 2026-05-05 に 200 株成行注文済みだが、5/7 寄り前に 1,050 円以下の指値または寄指へ訂正する前提で、投資可能資金 500
    万円に対して最大 4.20%、当面の 100 万円 tactical cap に対して最大 21.0% に留まる。決算またぎではなく、増配・自己株式取得 catalyst
    が確認できるため approved とする。
external_refs: &id001
- records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md
candidate_evidence_decisions:
- evidence_hit_id: candidate-2026-05-01-9682-sales-discount-growth
  effective_sizing_eligible: true
  evaluated_at: '2026-05-05T13:31:15+09:00'
  reason_code: source_status_ok
research_evidence_hits:
- evidence_hit_id: research-9682-shareholder-return
  decision_role: catalyst_note
  evidence_polarity: supports
  evidence_family_set:
  - catalyst
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs: *id001
  recorded_at: '2026-05-05T13:31:15+09:00'
- evidence_hit_id: research-9682-risk-review
  decision_role: risk_evidence
  evidence_polarity: risk
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs: *id001
  recorded_at: '2026-05-05T13:31:15+09:00'
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
  real_order_intent_yen: 210000
  adv_participation_pct: 0.2632
  sizing_formula_id: policy-v1-paper-to-real-ladder
counterfactual: null
thesis_payoff:
  max_entry_price_yen: 1050
  target_price_yen: 1230
  stop_loss_yen: 950
  time_horizon_bd: 40
  invalidation_conditions:
  - Shareholder-return catalyst is absorbed and price breaks below 950 yen.
  entry_trigger: price_guard_or_revisit
  expected_upside_pct: 17.14
  expected_downside_pct: 10.53
  risk_reward_ratio: 1.63
tracking:
  mode: post_approval
  plus_15bd: null
  plus_30bd: null
market_cap_oku: 1663
sector_33: 情報・通信業
avg_turnover_oku: 3.8
valuation:
  per_forward: null
  per_trailing: 13.9
  pbr: 2.54
  ev_ebitda: null
  p_s: 1.23
  pcfr: 18.6
  ocf_yield: 0.0537
  fcf_yield: 0.0274
  net_cash_to_market_cap: null
  cash_to_market_cap: 0.1767
  price_to_equity: 2.5699
  equity_ratio: 0.7589
  primary_metric:
  - p_s
---

# Research: 2026-05-05 9682 ＤＴＳ sales-discount-growth

**成分**: 個別銘柄リサーチ

**Playbook id**: sales-discount-growth

## Thesis

情報・通信業の macro regime gate は 2026-05-04 outlook で supportive。9682 DTS は PER/PBR/CF ではなく、P/S 1.23、業種中央値比 -45.5%、売上 YoY +7.4%、営業黒字という `sales-discount-growth` 単独 evidence hit で拾われた。5/1 の決算、増配、自己株式取得・消却が確認でき、今回の screening redesign が「PER/PBR 以外のお買い得」を拾う目的には合っている。

採用判定は approved。ただし「9682 を最大確信銘柄として集中」ではなく、投資可能資金 500 万円のうち、当面の様子見上限 100 万円内で 200 株までの採用に留める。理由は、9682 は global rank 112/307、sales lane 40/114 の single evidence hit で、P/S discount の一部は SIer 事業モデル差の可能性があるため。一方で、決算・増配・自己株式取得が同時に確認でき、次回 1Q は 8 月予定で決算またぎ kill switch まで時間がある。200 株の買い注文は既に発注済みだが、5/7 寄りは休場中の macro headline をまとめて織り込むため、成行のままではなく 1,050 円以下の指値または寄指へ訂正する前提にする。5/1 終値 1,014 円参照では 202,800 円、1,050 円上限では最大 210,000 円で、総資金 500 万円に対して最大 4.20%、tactical cap 100 万円に対して最大 21.0%。1,080 円は許容上限ではなく、そこまで上がると目標 1,180-1,230 円 / stop 950 円に対する reward / risk が薄くなるため、200 株の「お買い得 entry」としては扱わない。

## Macro regime gate

- **判定**: supportive
- **業種**: 情報・通信業
- **outlook_ref**: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **根拠**: outlook は AI / クラウド / 半導体テーマの spillover を理由に、情報・通信業を neutral から supportive に引き上げている。DTS は SIer なので、データセンター・AI 関連の直接感応度は高成長 SaaS より低いが、DX / AI / クラウド投資の継続は追い風。
- **保守側判定**: supportive。ただし個別では「AI テーマ性」だけで採用しない。

### Portfolio macro risk budget

2026-05-04 outlook は、Base case 50% を「hawkish hold + ホルムズ高止まり + AI 需要持続」、Downside 30% を「スタグフレーション正面化 + クレジット調整」としている。2026-05-05 の deepresearch でも、米株先物は反発している一方、Brent は 110 ドル超、USD/JPY は介入警戒が残り、5/8 米雇用・5/12 米 CPI が控える。したがって、macro regime gate は supportive でも、総資金 500 万円を一気にフルインベストする局面ではない。

運用上の risk budget は以下:

- 投資可能資金全体は 500 万円。当面の様子見上限を 100 万円とし、主要 trigger 前は tactical cap の 40-45%、総資金の 8-9% 程度までを上限に初期投入する。
- 9682 200 株 + 9692 100 株で 395,700 円（5/1 終値基準）、総資金の 7.9%、tactical cap の 39.6%。9682 を 1,050 円上限で見ても合計 402,900 円、tactical cap の 40.3%。この範囲なら許容。
- 5/8 米雇用、5/12 米 CPI、5/15 6310/6835 の 1Q を通過するまでは、決算直前銘柄への大きな先行買いは避ける。
- 5/15 通過後に 6310 / 6835 の thesis が残れば、tactical cap の 60-70% まで上げる。ホルムズ悪化・米金利上振れ・円急騰なら 100 万円 cap 自体を広げない。
- 大規模買い場と判断するには、日本株全体が売られる一方で油価・為替・米金利の少なくとも 1 つが安定し、個別 thesis が壊れていないことを条件にする。その場合だけ 500 万円枠から段階投入を再検討する。

Source:

- outlook: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- deepresearch: records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md
- JPX holidays: https://www.jpx.co.jp/english/corporate/about-jpx/calendar/
- Reuters / MarketScreener 2026-05-05 market update: https://www.marketscreener.com/news/oil-eases-on-signs-us-is-loosening-iranian-closure-of-strait-of-hormuz-ce7f58dfdd80ff22

### External deepresearch verification log

| external_ref | 採用 / 修正 / 未採用 | この research での扱い |
| --- | --- | --- |
| records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md | 採用 | 5/7 は「買ってよいが全額投入ではない」、初期投入は tactical cap 40-45% 程度、9682 200 株 + 9692 100 株まで、6310 は 1Q 後、という risk budget を採用 |
| 同上 | 修正 | deepresearch 初版の「成行維持可 / 1,080 円程度の価格上限」は、価格上限なしでは休場明け gap risk を制御できず、1,080 円では reward / risk も薄くなるため、9682 200 株は 1,050 円以下の指値 / 寄指へ訂正する前提に修正 |
| 同上 | 未採用 | 6835 の 100 株 toe-hold は、research_decision: deferred かつ決算またぎ kill switch と衝突するため、この research の即時注文 slate からは除外 |

## Sales / P/S snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| P/S | 1.23 | primary evidence hit |
| P/S sector gap | -45.5% | 業種中央値比で十分安い |
| 売上高 TTM | 135,213 百万円 | 2026/3 期実績 |
| 売上 YoY | +7.4% | growth intact |
| 営業利益 | 16,434 百万円 | 営業黒字 |
| 営業利益率 | 12.2% | 前期 11.5% から改善 |
| PER trailing | 13.9 | 補助。valuation-reversion は未 hit |
| PBR | 2.54 | 資産割安ではない |
| OCF yield | 5.37% | CF 割安 lane は未 hit |
| FCF yield | 2.74% | FCF 割安 lane は未 hit |

一次確認:

- 2026/3 期決算短信: 売上高 135,213 百万円、営業利益 16,434 百万円、親会社株主帰属当期純利益 11,644 百万円、ROE 19.2%、営業利益率 12.2%、営業 CF 8,929 百万円、現金同等物 29,381 百万円。
- 2027/3 期会社計画: 売上高 142,000 百万円、営業利益 17,000 百万円、当期純利益 11,700 百万円、EPS 75.00 円。
- 候補 YAML: 2026-05-01 終値ベースで時価総額 1,663 億円、60 営業日 -18.8%、P/S 1.23。
- 新 screening selection: 9682 は after-outlook の global rank 112/307、sales-discount-growth lane 40/114。候補ではあるが、上位 lane candidate ではない。

Source:

- 決算短信 PDF: https://assets.minkabu.jp/news/article_media_content/urn%3Anewsml%3Atdnet.info%3A20260430515331/010120260430515331.pdf
- candidates: records/04-candidates/2026/05/2026-05-01.yaml

## Margin bridge

営業利益は前年比 +13.4% で、売上 +7.4% を上回る。営業利益率は 2025/3 期 11.5% から 2026/3 期 12.2% へ改善している。P/S discount が単なる低採算売上の積み増しなら営業利益率が悪化しやすいが、今回の開示だけを見る限りは逆で、増収と margin improvement が同時に出ている。

反対側では、2027/3 期計画は売上 +5.0%、営業利益 +3.4% と増益率が鈍る。ここは「成長株」ではなく、堅調成長 + capital return + P/S discount の候補として扱うべきで、P/S rerating の上限を高く見すぎない。

## CFO / loss narrowing

営業黒字のため loss narrowing 条件は不要。営業 CF は 2026/3 期 8,929 百万円でプラスだが、前年の 9,181 百万円からは -2.7%。OCF yield 5.37% は悪くないが、cashflow-yield-discount lane には届いていない。

このため primary thesis は CF 割安ではない。営業 CF は「売上成長が会計上だけではないか」を見る補助証拠として使う。2026/3 期決算短信では営業利益率が 11.5% から 12.2% へ改善し、現金及び現金同等物も 29,381 百万円あるため、営業 CF -2.7% だけで採用を止める兆候はない。一方、営業 CF 減少の内訳はこの packet では分解できていないため、約定後の監視項目として、売掛金・契約資産・賞与支払などの運転資本要因か、案件採算の悪化かを有報または 1Q で確認する。

## SIer peer check

P/S discount は、情報・通信業全体の中央値だけでなく、SIer / IT services peer と比べて恒久的な business model discount ではないかを確認する必要がある。5/1 終値と local J-Quants raw / candidates snapshot の rough check は以下。

| ticker | 銘柄 | P/S | 営業利益率 | 売上成長 | PER | 株主還元 / 備考 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 9682 | DTS | 1.23 | 12.2% | +7.4% | 13.9 | 配当 38 円予想、自己株買い 50 億円 |
| 9692 | シーイーシー | 1.03 | 11.1% | +17.2% | 11.6 | 配当 80 円予想、net cash 36.6% |
| 8056 | BIPROGY | 1.00 | 9.8% | +7.4% | 13.7 | 配当 140 円予想、OCF yield 13.2% |
| 9715 | トランス・コスモス | 0.42 | 4.2% | +4.8% | 10.9 | net cash 41.8%、ただし低 margin の service mix |
| 2327 | NS Solutions | 約 1.71 | 11.6% | n/a | 21.2 | 大型 peer。P/S は DTS より高い |
| 3626 | TIS | 約 1.78 | 12.6% | n/a | 20.3 | 3Q snapshot。P/S は DTS より高い |
| 4307 | NRI | 約 2.94 | 7.2% | n/a | 高い | 高 multiple peer。DTS の直接比較上限 |

結論として、9682 は SIer peer 内で「突出して安い」わけではない。9692 / BIPROGY / transcosmos の方が、P/S や CF では強い面がある。一方、DTS は margin 12% 台、増収増益、自己株買い 3% 規模が同時にあるため、P/S 1.23 は少なくとも割高ではない。したがって thesis は「P/S rerating の大勝ち」ではなく、「安定 SIer の小口 shareholder-return trade」として扱う。

## Growth durability

成長耐久性のプラス材料:

- 2026/3 期は売上・営業利益とも過去最高更新。
- 2027/3 期会社計画も売上 +5.0%、営業利益 +3.4% の増収増益。
- 2026-03-13 の組織変更で AI-CoE を設置し、AI 関連ビジネス拡大、人材育成、業務改革加速を明示。
- 中計関連資料ではクラウド&モダナイゼーション、データ活用、セキュリティ&マネージドサービス、Enterprise Application Services、IoT/エッジ、AI・生成AI、CX を重点領域としている。

反対仮説:

- P/S 1.23 が安いのは、DTS が伝統的 SIer であり、SaaS 的な高粗利・高リカーリングモデルではないためかもしれない。
- 2027/3 期計画は利益成長が一桁前半で、P/S multiple が大きく切り上がるにはやや弱い。
- 人月型 SI の受注採算、プロジェクト不採算、人的資本コスト増が margin を削るリスクがある。

Source:

- AI-CoE 組織変更: https://www.dts.co.jp/news/2026/press-202603130000.html
- 中期経営計画ページ: https://www.dts.co.jp/ir/management/middle/

## Shareholder return

| 項目 | 確認結果 | 判定 |
| --- | --- | --- |
| 配当政策 | 利益還元を重要課題とし、安定配当と自己株式取得を組み合わせる方針 | positive |
| 2026/3 期配当 | 年間 37 円、期末は直近予想 20 円から 22 円へ増配 | positive |
| 2027/3 期予想配当 | 年間 38 円 | positive |
| 配当性向 | 2026/3 期 50.7%、2027/3 期予想 50.7% | 下値支え |
| 自己株式取得 | 上限 5,050,000 株、50 億円、2026-05-02 から 2026-09-18、市場買付 | catalyst |
| 消却 | 取得株式の全株式数を 2026-09-30 に消却予定 | catalyst |

自己株式取得の 50 億円は、候補 YAML の時価総額 1,663 億円に対して約 3.0%。取得上限株数 5,050,000 株は発行済株式総数 163,954,928 株に対して約 3.1%。single evidence hit 銘柄としては、capital return が P/S discount の弱さを補う。

Source:

- 増配リリース: https://assets.minkabu.jp/news/article_media_content/urn%3Anewsml%3Atdnet.info%3A20260501516529/140120260501516529.pdf
- 自己株式取得・消却リリース: https://assets.minkabu.jp/news/article_media_content/urn%3Anewsml%3Atdnet.info%3A20260501516460/140120260501516460.pdf

## Entry

採用条件:

- 2026-05-05 と 2026-05-06 は JPX cash market holiday のため、最短 tradable_at は 2026-05-07 09:00。
- 2026-05-01 終値 1,014 円を基準に、200 株は 1,050 円以下でのみ買う。5/7 前に成行注文を 1,050 円以下の指値または寄指へ訂正する。訂正できない場合は、価格上限なしの成行ではなく一旦取り消す。
- 1,050 円超から 1,080 円以下は「絶対上限内」ではあるが、200 株の bargain entry ではない。買う場合でも 100 株以下に減量し、寄り前気配・同業比較・9692 の約定可否を見て別途再評価する。
- 1,080 円超では、新規 entry しない。
- 決算・増配・自己株式取得後に 1,000 円を明確に割り込む場合は、還元 catalyst が吸収されていないため見送り。
- P/S discount の比較対象を、情報・通信業全体ではなく SIer / IT services peer に絞っても割高ではないことを継続確認する。

投資可能資金 500 万円、当面の tactical cap 100 万円では、銘柄数を固定しない。2026-05-05 時点の優先順位は以下:

| ticker | 銘柄 | 方針 | 5/1 終値基準の数量 | 参考金額 |
| --- | --- | --- | ---: | ---: |
| 9682 | DTS | 5/7 前に 1,050 円以下の指値 / 寄指へ訂正。1,050 円超では 200 株を買わず、1,080 円超では見送り | 200 | 202,800 円、guard 上限 210,000 円 |
| 9692 | シーイーシー | 5/7 以降、2,000 円以下なら 100 株 | 100 | 192,900 円 |
| 6310 | 井関農機 | 5/15 1Q 通過後、営業 CF thesis が崩れなければ 100 株 | 100 | 172,600 円 |
| 6835 | アライドテレシスHD | 5/15 1Q 後。決算またぎ kill switch のため 5/7 先行買いはしない | 100-300 | 26,200-78,600 円 |
| 7613 | シークス | FCF/OCF は強いが、売上減・net debt・卸売 neutral のため 5/7 注文対象外 | 100 | 128,400 円 |

9682 200 株、9692 100 株、6310 100 株までで 568,300 円。これは総資金 500 万円では 11.4% だが、tactical cap 100 万円では 56.8% で、5/8 雇用・5/12 CPI・5/15 個別決算前としてはやや重い。したがって、5/7 時点では 9682 200 株 + 9692 100 株までを基本とし、6310 は Q1 後に回す。6835 は金額が小さく evidence hit も強いが、research_decision: deferred かつ決算またぎ kill switch と衝突するため、5/7 の先行買いはしない。Q1 通過後に thesis が残れば、9682/9692/6310 の約定価格と残余資金を見て 100 株単位で追加する。6310 は 1 単元 172,600 円かつ net debt / 低 margin / 1Q 直前のため、Q1 前の先行買いは避ける。

初期 paper proxy は 1.0% まで。現在の tactical cap 100 万円では ADV cap は実質拘束しないが、記録上は 1 億円 proxy で ADV 0.263% とし、5% hard reject には十分余裕がある。

## Exit

- 利確目安: 1,180-1,230 円。2027/3 期 EPS 75 円に PER 15.7-16.4 倍、または P/S 1.4 台への小幅 rerating を想定する水準。
- 損切り目安: 950 円割れ。決算・還元 catalyst 後にも下落が続く場合、P/S discount は trap の可能性が上がる。
- 時間切れ: 40 営業日。自己株買い期間が 2026-09-18 まであるため、初動が鈍い場合でも一度は買付進捗を確認する。

## Invalidation

- 5/1 開示後の最初の取引で、出来高を伴って 1,000 円を明確に割り込む。
- 2027/3 期会社計画が市場期待を下回ったと市場が解釈し、株主還元を織り込んでも売りが継続する。
- SIer peer 比較では P/S 1.23 が割安ではないと判明する。
- 営業 CF 減少が運転資本ではなく採算悪化・回収遅延・大型不採算案件の兆候だったと確認される。
- 情報・通信業 gate が supportive から neutral/adverse へ悪化し、AI / クラウド需要の spillover 仮説が後退する。

## Position size

- **research_decision**: approved
- **primary evidence hit**: sales-discount-growth
- **evidence hit count**: 1
- **market cap**: 1,663 億円
- **avg turnover**: 3.8 億円
- **paper proxy position**: 0.01 億円
- **ADV participation**: 0.2632%
- **許容上限**: single evidence hit のため最大 1%
- **実注文**: 2026-05-05 に 200 株成行を発注済み。ただし 2026-05-07 09:00 expected fill 前に、1,050 円以下の指値 / 寄指へ訂正する前提。5/1 終値参照 202,800 円、1,050 円上限では最大 210,000 円、総資金 500 万円に対して最大 4.20%、tactical cap 100 万円に対して最大 21.0%。

### Screening redesign 目的に対する検証

9682 は、旧 PER/PBR 中心の screening では拾いにくかったが、新設した P/S + growth intact lane で候補化された。候補化の理由は機械的に説明可能で、一次情報でも売上成長、営業黒字、営業利益率改善、増配、自己株式取得・消却が確認できる。したがって、Issue #90 の「新 screening で 9682 を再リサーチし、他に優先すべきお買い得候補がないか確認する」目的には合致している。

一方で、9682 は sales lane 40/114、global rank 112/307、かつ single evidence hit である。P/S discount は SIer 事業モデル差による恒久的 discount の可能性があり、これだけで大きく張る銘柄ではない。今回の変更は「候補発見」としては成功だが、実資金では 200 株に限定する。追加資金は、より evidence hit が強い 9692 / 6310 / 6835 のリサーチ結果を優先する。
