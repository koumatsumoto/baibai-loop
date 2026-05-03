# sector-region-map.md

東証 33 業種を outlook の 4 region (`us` / `japan-domestic` / `japan-external-demand` / `emerging`) に対する **主たる感応度** で分類する参考表。research §2 Macro gate / §10 Entry 条件で「業種は neutral だが地域 tailwind の追い風を受ける」のような integrated judgement を組み立てる際の出発点として使う。

## 1. 位置付け

- ここで言う region は outlook が判定する **地域マクロ** であり、企業の所在地ではなく **業績の感応度の中心** を示す
- 同じ業種の中でも個別企業によって感応度は異なるため、本表は **default mapping**。research では銘柄個別の輸出比率・原価構造・顧客地域を調べて override する
- 33 業種を以下 3 区分に分ける:
  - **primarily japan-external-demand**: 売上の過半が海外向け、または USD/JPY や米需要に明確に連動
  - **primarily japan-domestic**: 国内消費 / 国内投資 / 国内人口動態が主たる driver
  - **mixed / context-dependent**: 海外・国内が混在、または商品市況・規制等で感応度が個別案件に依存

## 2. 業種一覧

### 2.1 primarily japan-external-demand

USD/JPY、米製造業需要、海外建設投資の影響を一次的に受ける業種。

| 業種 | 主な根拠 |
| --- | --- |
| 機械 | 設備投資輸出比率が高く、米欧アジアの製造業 capex に連動 |
| 電気機器 | 半導体製造装置・電子部品で海外売上比率高 |
| 輸送用機器 | 自動車・重工は北米・欧州・アジア向け輸出が中心 |
| 精密機器 | 検査機器・計測機器で海外売上比率高 |
| 鉄鋼 | 海外建設・自動車向け鋼材、原料炭・鉄鉱石は USD 建て |
| 非鉄金属 | 商品市況が USD 建て、海外鉱山事業 |
| 海運業 | 運賃が USD 建て、世界貿易量に連動 |
| ゴム製品 | タイヤ等は北米・欧州・アジア向け輸出比率高 |

### 2.2 primarily japan-domestic

国内需要・賃金・人口動態が主たる driver の業種。

| 業種 | 主な根拠 |
| --- | --- |
| 建設業 | 国内官公需・民需が中心 |
| 小売業 | 国内消費水準・賃金動向に連動 |
| 不動産業 | 国内金利・賃料・居住人口が driver |
| 銀行業 | 国内金利・国内貸出量が中心 (一部地銀は海外債券保有もあり) |
| 証券、商品先物取引業 | 国内取引フロー (ただし手数料体系は外資との競合) |
| 保険業 | 国内人口動態・国内金利・米債金利双方の影響 |
| 陸運業 | 国内物流・通勤需要 |
| 倉庫・運輸関連業 | 国内物流網 |
| 空運業 | 国内・国際路線の mix だが収益柱は国内 / インバウンド |
| 電気・ガス業 | 国内料金規制下、燃料は USD 建てだが pass-through 規制あり |
| サービス業 | 国内消費・国内 BPO・国内 IT サービスが中心 |
| 食料品 | 国内消費が中心 (一部輸出菓子・酒類は外需) |
| 情報・通信業 | 国内 SI・通信インフラが収益柱 (一部 SaaS は海外展開) |
| 水産・農林業 | 国内消費中心 (一部水産加工は輸出) |
| 卸売業 | 国内流通中心 (大手商社は外需だが本表では mixed 扱い) |

### 2.3 mixed / context-dependent

海外比率が銘柄ごとに大きく分かれる、または商品市況・規制感応度が支配的な業種。

| 業種 | 注記 |
| --- | --- |
| 化学 | 半導体材料・電子材料は外需、国内向け石化は内需。銘柄個別判断 |
| 医薬品 | 国内薬価制度と海外売上の組み合わせ。global pharma vs 国内特化 |
| 石油・石炭製品 | 商品市況 (原油 USD) 一次依存、内需 demand mix |
| ガラス・土石製品 | 自動車向け (外需) と国内建設 (内需) の mix |
| 金属製品 | 建材 (内需) と輸出向け工業製品 (外需) の mix |
| パルプ・紙 | 国内印刷需要縮小、衛生紙は内需、特殊紙は外需 |
| 繊維製品 | 高機能繊維 (外需) と国内アパレル (内需) の mix |
| その他製品 | 文具・楽器・スポーツ用品など、業態依存性高い |
| 鉱業 | 国内鉱山は限定的、海外鉱山投資・商品市況依存 |
| その他金融業 | リース・消費者金融・投資 fund など、case by case |

## 3. 使い方 (research での integration)

1. screened ticker の `sector_33` を本表で region 区分に対応させる
2. 該当 region の outlook 判定 (tailwind / neutral / headwind) を確認
3. **mixed / context-dependent** の業種は、`research §2 Macro gate` で銘柄個別の輸出比率や顧客地域を調べて region を確定する
4. 業種 outlook と region outlook の両方が **headwind** の場合のみ「採用不可」(両方 neutral 以上は採用可)
5. 一方が tailwind なら追い風として positive、もう一方が neutral なら大きな ambiguous でない限り採用可

## 4. 限界と更新ポリシー

- 本表は **default mapping** であり、銘柄個別の事実を上書きしない
- 業種別の輸出比率は構造的に変化するため (例: 海運業の運賃指数、自動車業界の現地生産比率)、半年〜1 年に 1 回のレビューを想定
- 大手商社 (卸売業) は本表で mixed としているが、実態は外需寄り。個別 research で region tailwind 適用するのが妥当

## 5. 関連

- [`../components/outlook.md`](../components/outlook.md): outlook の region 判定ルール
- [`../components/research.md`](../components/research.md) §2: Macro gate での integrated judgement
- [`./universe-rules.md`](./universe-rules.md): universe 境界条件
- [`./mechanical-v1.md`](./mechanical-v1.md): 機械的ふるい仕様
