---
playbook_id: "valuation-mean-reversion-v1"
version: 1
horizon: "5-40 営業日"
playbook_type: primary
status: active
updated_at: "2026-04-24T09:00:00+09:00"
---

# Playbook: Valuation Mean-Reversion v1 (P-A)

**Type**: Primary（本命）  
**成分**: `records/_playbooks/` の active rule。4 成分アーキテクチャの `(d) records/04-research/` で参照。

## 1. 概要

### 1.1 対象

数値 valuation が業種中央値・過去自己比較で **一時的に割安** と判定される銘柄（Catalyst の有無は問わない）。

### 1.2 狙い

市場の短期過剰売りによる **底値** を掴む。マクロ追い風下での一時的乖離を収斂に伴う戻りで取る。

### 1.3 保有期間

5〜40 営業日（2 か月以内）

## 2. 判定条件（entry 前の必須条件）

### 2.1 Valuation 条件

以下 5 指標のいずれかで業種中央値・過去自己比較から割安判定が成立:

- PER（forward 優先、会社予想ベース、未公表時は trailing のみで判定）
- PBR
- EV/EBITDA
- P/S
- PCFR

指標算出は [`../docs/screening/valuation-metrics.md`](/docs/screening/valuation-metrics.md) に従う。

### 2.2 Mean-Reversion 条件（3 種 OR、最低 1 つ）

[`../docs/screening/mechanical.md`](/docs/screening/mechanical.md) の閾値条件を継承:

- **条件 A**: PER / PBR / EV-EBITDA のいずれかが業種中央値比 -20% 以上 かつ 過去 3 年自己レンジ下位 20%
- **条件 B**: 過去 60 営業日で -15% 以上下落 かつ valuation が 1σ 以上下方（業績悪化なし）
- **条件 C**: セクター RS 下位 20% + 個別が業種平均下回り（業績悪化なし）

### 2.3 Catalyst 条件

- **本 playbook では catalyst を必須としない**（純粋な valuation mean-reversion 狙い）
- catalyst がある場合は P-B playbook（[`valuation-catalyst-confirmation-v1.md`](./valuation-catalyst-confirmation-v1.md)）を使う

### 2.4 Macro gate 条件

- 業種/地域のマクロ gate が `tailwind` または `neutral`
- `headwind` は **採用不可**（valuation trap リスク）
- 判定: [`../docs/screening/macro-gate-procedure.md`](/docs/screening/macro-gate-procedure.md)

### 2.5 Universe 条件

- 時価総額 200 億円以上
- 20 営業日平均売買代金 3 億円以上
- 上場 6 か月以上
- 特別注意 / 整理銘柄除外
- **200-500 億円帯は P-A 単独採用不可**（本 playbook の対象外、P-B のみ許可）
- 詳細: [`../docs/screening/universe-rules.md`](/docs/screening/universe-rules.md)

## 3. 原因仮説と反対仮説（research packet 必須）

### 3.1 一時的割安の原因仮説（必須）

以下のいずれかに該当することを research packet で明示:

- 市場全体の短期売り
- 業種ローテーションの一過性
- 一過性の悪材料
- インデックス構成変更
- 需給要因の一時的売り

### 3.2 反対仮説（必須、8 例示 + 自由記述）

以下のうち該当するものを検討:

- 構造的な成長鈍化
- ガバナンス懸念
- 技術的陳腐化
- accounting 警戒
- 業界需要の構造的縮小
- ESG / 規制リスク
- 大株主の売り圧力
- その他（自由記述）

## 4. 修飾因子

### 4.1 Crowding

- 空売り残高 / 日々公表信用 / 特別注意 / 貸借状態を評価
- 踏み上げ余地（ポジティブ）と逆回転リスク（ネガティブ）の両面で記録

### 4.2 Relative Strength

- 業種 RS と個別 RS の整合性
- 業種全体が下方 + 個別が業種平均を下回る場合、条件 C に該当する可能性

## 5. 無効化条件

以下のいずれかが発生したら exit 検討:

- 業績下方修正が出た
- 業種中央値自体が切り下がり、相対割安が消えた
- 出来高を伴わずさらに下落継続（valuation trap の兆候）
- マクロゲートが `headwind` に転じた（outlook 更新または緊急 brief 経由）

## 6. Exit 戦略

- **利確目標**: valuation が業種中央値に回帰（業種中央値比 ±0% 付近）
- **損切り**: entry 価格から -8% 〜 -10%
- **時間切れ**: 最長 40 営業日
- **無効化 exit**: 節 5 の無効化条件に該当したら即時 exit 検討

## 7. Position sizing

- 1,000 億円以上: 2%
- 500-1,000 億円: 1%
- **200-500 億円帯: P-A 単独採用不可**（P-B のみ）

## 8. Kill switch

- 決算またぎエントリー禁止
- 日銀会合前日エントリー禁止
- FOMC 前日エントリー禁止

## 9. AI の役割境界

[`../docs/components/research.md`](/docs/components/research.md) の AI 境界表を継承。核心:

- **AI 可**: Thesis ドラフト / valuation snapshot 数値取得 / 原因仮説・反対仮説ドラフト / price reaction / crowding
- **人間のみ**: Macro gate 確定 / 一次ソース URL 確認 / 最終採用判定 / 失敗分類確定

## 10. 参考

- [`../docs/screening/principles.md`](/docs/screening/principles.md): スクリーニング原則
- [`../docs/screening/mechanical.md`](/docs/screening/mechanical.md): 機械的ふるい閾値
- [`../docs/screening/valuation-metrics.md`](/docs/screening/valuation-metrics.md): 指標算出仕様
- [`../docs/screening/macro-gate-procedure.md`](/docs/screening/macro-gate-procedure.md): Macro gate 判定
- [`../docs/components/research.md`](/docs/components/research.md): research 運用仕様
- [`valuation-catalyst-confirmation-v1.md`](./valuation-catalyst-confirmation-v1.md): P-B 補助 playbook
