---
ticker: "9682"
name: "ＤＴＳ"
playbook: valuation-mean-reversion-v1
decision: accepted
candidates_ref: records/03-candidates/2026/04/2026-04-24.yaml
outlook_ref: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
  - records/01-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai-draft: true
published_at: "2026-05-04T23:30:00+09:00"
tradable_at: "2026-05-07T09:00:00+09:00"
macro_gate: tailwind
position_size_oku: 0.002028
adv_participation_pct: 0.0579
avg_turnover_oku: 3.5
market_cap_oku: 1722
sector_33: "情報・通信業"
valuation:
  per_forward: 13.52
  per_trailing: 19.67
  pbr: null
  ev_ebitda: null
  p_s: null
  pcfr: null
  primary_metric: ["per_forward", "per_trailing"]
---

# Research: 2026-05-04 9682 ＤＴＳ valuation-mean-reversion-v1

**成分**: 4 成分アーキテクチャの **(d) 個別銘柄リサーチ**

**Playbook**: valuation-mean-reversion-v1 (P-A 純粋型)

## 1. Thesis

新 outlook で `情報・通信業 = tailwind`。DTS は国内 SI / DX / クラウド / AI 導入支援の中堅総合 SIer
で、2026年3月期実績は売上高 1,352.13 億円、営業利益 164.34 億円、経常利益 169.40 億円、親会社株主に
帰属する当期純利益 116.44 億円と増収増益だった。一方、2027年3月期会社計画は売上高 1,420 億円、
営業利益 170 億円、経常利益 173.5 億円、純利益 117 億円、EPS 75.00 円で、純利益成長率は +0.5% に
とどまる。株価下落の主因は業績悪化ではなく、2027年3月期の利益成長鈍化と 2028年3月期中計目標への
到達疑義による PER 切り下げと見る。

5/1 終値 1,014 円に対する会社予想 EPS 75.00 円ベースの forward PER は 13.52 倍。2027年3月期会社計画が
維持され、1Q-2Q で中計達成ペースへの再加速が確認できれば、1,100-1,200 円台への mean reversion を
狙える。2026-05-04 に 200 株成行買注文を提出したため、live order として accepted に更新する。ただし
2026-05-04 から 2026-05-06 は東証休場のため、約定価格は未確定。trade record では `ordered` として扱う。

## 2. Macro gate [最上位ゲート]

- **判定**: tailwind
- **業種**: 情報・通信業 (outlook で `tailwind`)
- **地域**: japan-domestic (outlook で `neutral`) -- 主に国内顧客 (金融 / 公共 / 製造 / 流通)
- **保守側優先判定結果**: tailwind (業種 tailwind、地域 neutral、headwind 不在)
- **outlook_ref**: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **brief_refs**: records/01-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
- **補足**: 情報・通信業 tailwind は AI / クラウド / データセンター需要が主因。ただし research では
  業種を問わず対象銘柄の会社IRを一次情報として確認し、sector 一括判断ではなく銘柄固有の margin /
  受注 / 還元 / 中計進捗を確認する。DTS では春闘 5% 超の賃上げ、人件費・外注費、顧客内製化、
  生成AIによる工数モデル変化を個別リスクとして扱う。

## 3. Valuation snapshot

| 指標 | 値 | 比較軸 | 判定 | primary |
| --- | ---: | --- | --- | --- |
| PER (forward) | 13.52 | 2027年3月期会社予想 EPS 75.00 円 / 5/1 終値 1,014 円 | 同業比で割安寄り | yes |
| PER (trailing) | 19.67 | 4/24 candidates: sector_median_gap -31.03% | 業種中央値比で割安 | yes |
| PBR | null | candidates 未取得 | 補助不可 | |
| EV/EBITDA | null | candidates 未取得 | 補助不可 | |
| P/S | null | candidates 未取得 | 補助不可 | |
| PCFR | null | candidates 未取得 | 補助不可 | |

- 公式 EPS 検算: 2027年3月期会社予想 EPS 75.00 円。1,014 / 75.00 = 13.52 倍。
- 旧 candidates の `per_forward: 15.32` は本決算前の snapshot。5/1 決算短信後は公式 EPS を優先する。
- trailing PER の sector median 逆算: 19.67 / (1 - 0.3103) = 28.52 倍。
- self_range_percentile 0.0102 = 750 営業日中の下位 1.02%。

### 3.1 PER scenario (EPS 75.00 円)

| PER | 株価 | 解釈 |
| ---: | ---: | --- |
| 13x | 975 円 | 991 円割れ時の下値確認帯 |
| 14x | 1,050 円 | 反転確認ライン |
| 15x | 1,125 円 | 足元の妥当化 |
| 16x | 1,200 円 | 中立回復の第1利確候補 |
| 17x | 1,275 円 | 1Q-2Q 好調で中計再評価 |
| 18x | 1,350 円 | 中計達成期待の回復 |

別AI分析の EPS 71 円前提は、5/1 決算短信の会社予想 EPS 75.00 円と一致しない。方向性は近いが、価格水準は
公式 EPS で再計算する。

## 4. 一時的割安の原因仮説

- **来期ガイダンスの弱さ**: 2026年3月期は良い決算だが、2027年3月期会社予想は売上 +5.0%、
  営業利益 +3.4%、純利益 +0.5%。市場は「好決算」より来期の伸び鈍化を嫌った可能性が高い。
- **中計達成への疑義**: 2028年3月期中計目標は売上高 1,600 億円、営業利益 187 億円、EBITDA 200 億円。
  2027年3月期会社予想からは、2028年3月期に売上 +12.7%、営業利益 +10.0% 程度の再加速が必要になる。
- **受注残の見え方**: 2026年3月期の受注高は 1,341.4 億円で前年比 +16.6 億円だが、受注残高は 378.0 億円で
  前年比 -15.2 億円。メガバンク案件の反動、大型AI/HPC案件完了の反動が混在している。
- **SIer共通の margin 懸念**: 生成AIインフラやHPCなどハードウェア色のある案件は売上を押し上げる一方、
  gross margin を薄める可能性がある。Q&A でも 4Q 売上総利益率の低さにハードウェア領域の影響が示された。

「一時的」と見る根拠は、公式資料でフォーカスビジネス売上高 850.9 億円、売上高比率 62.9% と中計最終年度
目標 57.0%以上を既に上回っていること、AI・生成AI領域が 20 億円から 78 億円へ拡大したこと、株主還元が
配当性向 50%以上 / 総還元性向 70%以上の方針を維持していること。

## 5. 反対仮説 - 構造的理由（必須）

- **低成長が一過性でなく構造化する**: 2027年3月期計画の利益成長鈍化が保守計画ではなく、国内 SIer の
  単価・工数・採用制約の構造問題なら、PER は 13-15 倍にとどまる。
- **成長投資による利益率低下**: 会社 Q&A では、2027年3月期は中計2年目として成長投資を実行するため、
  EBITDA margin / 営業利益率が若干低下する計画と説明されている。投資が将来売上につながらなければ
  margin 低下だけが残る。
- **AI / クラウドが SIer の収益モデルを侵食**: AI 導入支援は機会だが、顧客内製化や SaaS 直接利用が進むと、
  中間 SIer の工数課金モデルには逆風。DTSの強みはAI技術そのものではなく、金融・公共・製造・流通の
  業務知識と既存顧客接点である。
- **大型案件反動と受注残減少**: メガバンク案件や生成AI/HPC案件の反動減が続く場合、2027年3月期売上計画
  達成はできても 2028年3月期の再加速確度は下がる。

## 6. Catalyst

P-A 純粋型のため catalyst は必須にしない。ただし今回の再評価 catalyst 候補は以下。

- 2027年3月期 1Q (IRカレンダー上の通常時期は 8 月): 売上 +5%以上、営業利益率 11.5-12%台、
  プラットフォーム＆サービス / AI・生成AI領域の伸びを確認。
- 自己株式取得 50 億円の進捗と消却。
- OpenAI Japan 連携、GenAIアカデミー、AIエージェント / AI画像生成 / 建築AIアシスタントの受注・利益貢献。

## 7. Price reaction

| 対象 | 値 | 変化 | ソース |
| --- | ---: | ---: | --- |
| 5/1 終値 | 1,014 円 | -0.59% | candidates / market snapshot |
| YTD 高値 | 1,299 円 | 2026-01-14 | market snapshot |
| YTD 安値 | 991 円 | 2026-03-30 | market snapshot |
| 60 営業日騰落 | -- | -17.19% | candidates |
| 4 週騰落 | -- | +0.19% | candidates |
| 750 日自己レンジ位置 | -- | 1.02% | candidates |

5/1 終値は YTD 高値から -21.9%、YTD 安値から +2.3%。1,000 円前後は配当 38 円予想で利回り
3.75-3.80%台となり、株主還元面の下支えがある。ただし 991 円を終値で明確に割る場合は、
低成長ガイダンスを市場が構造問題として織り込むシナリオへ移る。

## 8. Crowding

| 指標 | 状況 |
| --- | --- |
| 空売り残高 | 未確認 |
| 日々公表信用指定 | candidates の `threshold_hit` に `crowding_alert` 不在 |
| 特別注意 | 無 |
| 流動性 | 4/24 candidates の avg_turnover 3.5 億円 / 日。5/1 screening では universe 外のため再確認対象 |

実注文は 200 株、5/1 終値 1,014 円換算で 202,800 円 = 0.002028 億円。4/24 avg_turnover 3.5 億円を
基準にした ADV 参加率は 0.002028 / 3.5 * 100 = 0.0579%。システム上の liquidity warning は残すが、
個人サイズの成行注文としては市場インパクトは小さい。

## 9. ミクロ 4 軸寄与度

| 軸 | 寄与度 | 備考 |
| --- | --- | --- |
| Valuation | strong | 公式 forward PER 13.52、trailing PER 業種中央値比 -31% |
| Mean-Reversion | strong | self_range 下位 1.02%、991 円近辺の底値確認 |
| Catalyst | neutral | P-A。1Q / 自社株買い / AI売上進捗が補助 catalyst |
| Crowding | neutral | 5/1 universe 外は警戒。ただし実注文サイズの ADV は小さい |

## 10. Entry 条件

- **注文**: 2026-05-04 に 200 株成行買注文を提出。
- **市場休場**: 2026-05-04 / 05 / 06 は東証休場。約定は最短で 2026-05-07 寄り付き。
- **価格レンジ**: 1,000-1,030 円を初回打診の主レンジとする。成行のため 5/7 寄り付き価格が
  1,050 円を大きく超える場合は約定後に entry reason を再確認する。
- **追加検討**: 970-990 円で下落理由が地合い・需給のみ、かつ減配 / 下方修正 / 受注悪化がなければ
  追加 100 株を検討。1,050 円終値回復 + 出来高増は反転確認。

## 11. Exit 条件

- **第1利確候補**: 1,145-1,200 円 (PER 15.3-16.0x、+13-18%程度)
- **第2利確候補**: 1,250-1,300 円 (PER 16.7-17.3x、中計再評価が必要)
- **警戒ライン**: 991 円を終値で明確に割る。即時機械損切りではなく、IR前提が維持されているかを確認。
- **撤退 / 追加停止**: 減配、配当方針後退、営業利益下方修正、受注鈍化、900 円割れ + 業績悪化、
  中計達成可能性の大幅低下。
- **時間切れ**: 最長 40 営業日。ただし配当・還元目的の保有に切り替える場合は、必ず trade log に
  thesis shift を追記する。

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件

- 2027年3月期会社計画 (売上高 1,420 億円、営業利益 170 億円、年間配当 38 円) が下方修正される。
- 配当性向 50%以上 / 総還元性向 70%以上の方針が後退する。
- 1Q-2Q で売上 +5%未満、営業利益率 11.5%未満、受注残の減少が継続し、中計再加速の道筋が弱まる。
- AI・生成AI売上が売上だけで利益率改善につながらず、ハードウェア案件の薄利化が目立つ。
- outlook 更新で情報・通信業または japan-domestic が headwind 化する。

### 12.2 Pre-mortem

失敗パターンは「配当利回りに惹かれて低成長 SIer のデレーティングを買う」ケース。株価だけでなく、
公式IRの売上成長、営業利益率、受注高・受注残、還元方針、AI・生成AI領域の利益貢献を追う。別AI分析の
ように二次サイトの同業倍率や市場予測を使う場合も、records へ反映する前に会社IR / 取引所 / 一次統計で
確認する。

## 13. Position size + 採用判定

- **時価総額**: 1,722 億円 (1000+ tier)
- **許容 position**: playbook 上は max 2.0%。今回の実注文は 200 株、5/1 終値基準で約 20.3 万円。
- **position_size_oku**: 0.002028 億円
- **avg_turnover**: 3.5 億円 / 日 (4/24 candidates)
- **adv_participation**: 0.0579%
- **採用判定**: accepted

採用理由: 公式IRで本決算実績・2027年3月期計画・中計・AI/生成AI進捗・株主還元を確認した結果、下落理由は
「業績崩壊」ではなく「来期利益成長鈍化と中計再加速待ち」と整理できる。1,000 円前後は forward PER
13 倍台かつ配当利回り 3.8%前後で、下値は 991 円を基準に監視可能。5/1 screening universe 外の流動性
警告は残るが、今回の 200 株注文では ADV 参加率が 0.1%未満のため執行リスクは許容範囲。

## 14. Source verification log

公式・一次に準じる確認先:

- DTS 2026年3月期 決算短信 (2026-05-01): https://contents.xj-storage.jp/xcontents/AS04298/2e3c1f84/446d/4e03/8195/1847f465502e/140120260430515331.pdf
- DTS 2026年3月期 決算説明会資料 (2026-05-01): https://contents.xj-storage.jp/xcontents/AS04298/a188f889/f048/4e1c/880e/065b1dee3cb4/20260501185643481s.pdf
- DTS 2026年3月期 決算説明会 Q&A (2026-05-01): https://contents.xj-storage.jp/xcontents/AS04298/6666dbf9/f86e/4e47/8b9f/ba8d6b77a79b/20260501185321841s.pdf
- DTS 中期経営計画 (2025-2027): https://contents.xj-storage.jp/xcontents/AS04298/3e218c42/7464/44e9/a946/b895c9eb4950/20250501154710471s.pdf
- DTS OpenAI Japan 連携リリース (2025-09-01): https://www.dts.co.jp/news/2025/press-20250901.html
- DTS IRカレンダー: https://www.dts.co.jp/ir/library/calendar/

別AI分析の検証結果:

- 概ね正しい: 2026年3月期実績は良い、2027年3月期計画は純利益 +0.5%で弱く見える、中計達成には
  2028年3月期の再加速が必要、配当 38 円 / 配当性向 50%以上 / 総還元性向 70%以上、OpenAI Japan
  連携と 2030年度 生成AI関連売上 100 億円規模。
- 修正が必要: EPS 71 円前提は公式会社予想 EPS 75.00 円と不一致。PERシナリオは EPS 75 円で再計算する。
  OpenAI連携は DTS公式では 2025-09-01 開始。AI・生成AI売上は公式決算説明資料では 2026年3月期に
  20 億円から 78 億円へ拡大と確認できる。
- records へ未採用: IDC / Gartner / 同業PER表などの外部二次情報は今回の判断補助としては有用だが、
  本 packet では公式IR・candidates・outlook に紐づく事実だけを採用した。

---

**Kill switch 確認**:

- [x] 決算またぎエントリーではない (2026年3月期本決算は 2026-05-01 に公表済み)
- [x] 日銀会合前日エントリーではない (次回 6/16-17 まで余裕)
- [x] FOMC 前日エントリーではない (次回 6/16-17 まで余裕)
- [x] マクロゲート: tailwind (sector tailwind + region neutral、保守側 = tailwind)
- [x] 200-500 億円帯該当せず (1,722 億円)

参照: [`/docs/components/research.md`](/docs/components/research.md), [`/docs/screening/principles.md`](/docs/screening/principles.md), [`/records/_playbooks/`](/records/_playbooks/)
