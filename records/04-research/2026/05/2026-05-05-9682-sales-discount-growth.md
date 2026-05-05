---
ticker: "9682"
name: "ＤＴＳ"
playbook: sales-discount-growth
supporting_signals: []
decision: accepted
candidates_ref: records/03-candidates/2026/05/2026-05-01.yaml
outlook_ref: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
  - records/01-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
  - records/01-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai-draft: true
published_at: "2026-05-05T13:31:15+09:00"
tradable_at: "2026-05-07T09:00:00+09:00"
macro_gate: tailwind
overrides: []
external_refs: []
position_size_oku: 0.01
avg_turnover_oku: 3.8
adv_participation_pct: 0.2632
market_cap_oku: 1663
sector_33: "情報・通信業"
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
  primary_metric: ["p_s"]
---

# Research: 2026-05-05 9682 ＤＴＳ sales-discount-growth

**成分**: 個別銘柄リサーチ

**Playbook**: sales-discount-growth

## Thesis

情報・通信業の macro gate は 2026-05-04 outlook で tailwind。9682 DTS は PER/PBR/CF ではなく、P/S 1.23、業種中央値比 -45.5%、売上 YoY +7.4%、営業黒字という `sales-discount-growth` 単独 signal で拾われた。5/1 の決算、増配、自己株式取得・消却が確認でき、今回の screening redesign が「PER/PBR 以外のお買い得」を拾う目的には合っている。

採用判定は accepted。ただし「9682 を最大確信銘柄として集中」ではなく、50 万円上限の実資金では 100 株だけ採用する。理由は、9682 は global rank 112/307、sales lane 40/114 の single signal で、P/S discount の一部は SIer 事業モデル差の可能性があるため。一方で、決算・増配・自己株式取得が同時に確認でき、次回 1Q は 8 月予定で決算またぎ kill switch まで時間がある。銘柄数を先に決めるのではなく、安定 SIer + shareholder return 枠として 100 株だけならお買い得候補として成立すると判断する。

## Macro gate

- **判定**: tailwind
- **業種**: 情報・通信業
- **outlook_ref**: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **根拠**: outlook は AI / クラウド / 半導体テーマの spillover を理由に、情報・通信業を neutral から tailwind に引き上げている。DTS は SIer なので、データセンター・AI 関連の直接感応度は高成長 SaaS より低いが、DX / AI / クラウド投資の継続は追い風。
- **保守側判定**: tailwind。ただし個別では「AI テーマ性」だけで採用しない。

## Sales / P/S snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| P/S | 1.23 | primary signal |
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
- candidates: records/03-candidates/2026/05/2026-05-01.yaml

## Margin bridge

営業利益は前年比 +13.4% で、売上 +7.4% を上回る。営業利益率は 2025/3 期 11.5% から 2026/3 期 12.2% へ改善している。P/S discount が単なる低採算売上の積み増しなら営業利益率が悪化しやすいが、今回の開示だけを見る限りは逆で、増収と margin improvement が同時に出ている。

反対側では、2027/3 期計画は売上 +5.0%、営業利益 +3.4% と増益率が鈍る。ここは「成長株」ではなく、堅調成長 + capital return + P/S discount の候補として扱うべきで、P/S rerating の上限を高く見すぎない。

## CFO / loss narrowing

営業黒字のため loss narrowing 条件は不要。営業 CF は 2026/3 期 8,929 百万円でプラスだが、前年の 9,181 百万円からは -2.7%。OCF yield 5.37% は悪くないが、cashflow-yield-discount lane には届いていない。

このため primary thesis は CF 割安ではない。営業 CF は「売上成長が会計上だけではないか」を見る補助証拠として使う。採用前には、営業 CF 減少が売掛金・契約資産・賞与支払など運転資本要因か、案件採算の悪化かを決算説明資料または有報で確認する。

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

自己株式取得の 50 億円は、候補 YAML の時価総額 1,663 億円に対して約 3.0%。取得上限株数 5,050,000 株は発行済株式総数 163,954,928 株に対して約 3.1%。single signal 銘柄としては、capital return が P/S discount の弱さを補う。

Source:

- 増配リリース: https://assets.minkabu.jp/news/article_media_content/urn%3Anewsml%3Atdnet.info%3A20260501516529/140120260501516529.pdf
- 自己株式取得・消却リリース: https://assets.minkabu.jp/news/article_media_content/urn%3Anewsml%3Atdnet.info%3A20260501516460/140120260501516460.pdf

## Entry

採用条件:

- 2026-05-05 と 2026-05-06 は JPX cash market holiday のため、最短 tradable_at は 2026-05-07 09:00。
- 2026-05-01 終値 1,014 円を基準に 100 株。5/7 寄りで 1,080 円を超える gap up なら追わず、1,000-1,080 円レンジの指値で待つ。
- 決算・増配・自己株式取得後に 1,000 円を明確に割り込む場合は、還元 catalyst が吸収されていないため見送り。
- P/S discount の比較対象を、情報・通信業全体ではなく SIer / IT services peer に絞っても割高ではないことを継続確認する。

50 万円上限の実資金では、銘柄数を 4 に固定しない。2026-05-05 時点の優先順位は以下:

| ticker | 銘柄 | 方針 | 5/1 終値基準の数量 | 参考金額 |
| --- | --- | --- | ---: | ---: |
| 9682 | DTS | 5/7 以降、1,080 円以下なら 100 株 | 100 | 101,400 円 |
| 9692 | シーイーシー | 5/7 以降、2,000 円以下なら 100 株 | 100 | 192,900 円 |
| 6310 | 井関農機 | 5/15 1Q 通過後、営業 CF thesis が崩れなければ 100 株 | 100 | 172,600 円 |
| 6835 | アライドテレシスHD | 5/15 1Q 通過後、FCF thesis が崩れなければ追加候補 | 100-300 | 26,200-78,600 円 |

9682 100 株、9692 100 株、6310 100 株までで 466,900 円。6835 は Q1 通過後に thesis が残れば、9682/9692/6310 の約定価格と残余資金を見て 100 株単位で追加する。無理に 4 銘柄へ合わせるため、弱い候補を買わない。

初期 paper proxy は 1.0% まで。実資金 100-200 万円では ADV cap は実質拘束しないが、記録上は 1 億円 proxy で ADV 0.263% とし、5% hard reject には十分余裕がある。

## Exit

- 利確目安: 1,180-1,230 円。2027/3 期 EPS 75 円に PER 15.7-16.4 倍、または P/S 1.4 台への小幅 rerating を想定する水準。
- 損切り目安: 950 円割れ。決算・還元 catalyst 後にも下落が続く場合、P/S discount は trap の可能性が上がる。
- 時間切れ: 40 営業日。自己株買い期間が 2026-09-18 まであるため、初動が鈍い場合でも一度は買付進捗を確認する。

## Invalidation

- 5/1 開示後の最初の取引で、出来高を伴って 1,000 円を明確に割り込む。
- 2027/3 期会社計画が市場期待を下回ったと市場が解釈し、株主還元を織り込んでも売りが継続する。
- SIer peer 比較では P/S 1.23 が割安ではないと判明する。
- 営業 CF 減少が運転資本ではなく採算悪化・回収遅延・大型不採算案件の兆候だったと確認される。
- 情報・通信業 gate が tailwind から neutral/headwind へ悪化し、AI / クラウド需要の spillover 仮説が後退する。

## Position size

- **decision**: accepted
- **primary signal**: sales-discount-growth
- **signal 数**: 1
- **market cap**: 1,663 億円
- **avg turnover**: 3.8 億円
- **paper proxy position**: 0.01 億円
- **ADV participation**: 0.2632%
- **許容上限**: single signal のため最大 1%

### Screening redesign 目的に対する検証

9682 は、旧 PER/PBR 中心の screening では拾いにくかったが、新設した P/S + growth intact lane で候補化された。候補化の理由は機械的に説明可能で、一次情報でも売上成長、営業黒字、営業利益率改善、増配、自己株式取得・消却が確認できる。したがって、「より広い観点でお買い得候補を拾う」という Issue #87 の目的には合致している。

一方で、9682 は sales lane 40/114、global rank 112/307、かつ single signal である。P/S discount は SIer 事業モデル差による恒久的 discount の可能性があり、これだけで大きく張る銘柄ではない。今回の変更は「候補発見」としては成功だが、実資金では 100 株に限定する。追加資金は、より signal が強い 9692 / 6310 / 6835 のリサーチ結果を優先する。
