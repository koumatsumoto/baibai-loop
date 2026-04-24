---
playbook_id: "valuation-catalyst-confirmation-v1"
version: 1
horizon: "5-40 営業日"
playbook_type: supplementary
status: active
updated_at: "2026-04-24T09:00:00+09:00"
---

# Playbook: Valuation + Catalyst Confirmation v1 (P-B)

**Type**: Supplementary（補助）  
**成分**: `playbooks/` の active rule。4 成分アーキテクチャの `(d) research/` で参照。

## 1. 概要

### 1.1 対象

P-A（[`valuation-mean-reversion-v1.md`](./valuation-mean-reversion-v1.md)）の valuation 条件を満たしつつ、**短期 catalyst** が重なっている銘柄。

### 1.2 狙い

割安な銘柄に **再評価トリガー** が重なっているケースを拾う。P-A より confidence が高い代わりにサンプル数は少ない。

### 1.3 保有期間

5〜40 営業日（2 か月以内）

## 2. 判定条件（entry 前の必須条件）

### 2.1 Valuation 条件

P-A と同じ（[`valuation-mean-reversion-v1.md`](./valuation-mean-reversion-v1.md) 節 2.1 参照）。

### 2.2 Mean-Reversion 条件

P-A と同じ 3 種 OR 条件（節 2.2）。

### 2.3 Catalyst 条件（本 playbook の核心、P-A との差分）

以下のいずれかに該当する catalyst が存在し、**freshness ≦ 60 営業日**:

- **決算 / 業績予想修正**: 上方修正、会社予想の引き上げ
- **自社株買い**: 公表、増額
- **大口受注**: 具体的な契約公表
- **東証「資本コストや株価を意識した経営」改善行動の開示**: 新規 or 更新
- **英語開示追加**: IR 強化の具体的行動
- **その他 IR 強化**: 配当政策見直し（配当利回り自体は screening 対象外だが、政策変更イベントとしては catalyst 成立）

catalyst 種別 + 経過営業日 + 一次ソース URL を research packet に記録。

### 2.4 Macro gate 条件

P-A と同じ。業種/地域のマクロ gate が `tailwind` または `neutral`、`headwind` は採用不可。

### 2.5 Universe 条件

- 時価総額 300 億円以上
- 20 営業日平均売買代金 2 億円以上
- **300-500 億円帯は本 playbook で採用可**: ただし **catalyst freshness ≦ 10 営業日 かつ 前日比出来高 1.5x 以上** の追加条件
- 他の universe 条件は P-A と同じ

## 3. 原因仮説と反対仮説

### 3.1 一時的割安の原因仮説

P-A と同じ。本 playbook では原因仮説はできれば記入（catalyst で再評価が始まる場合、市場がまだ catalyst を織り込んでいない状態が「一時的割安」）。

### 3.2 反対仮説（必須、8 例示 + 自由記述）

P-A と同じ。加えて:

- **catalyst が織り込み済み**: 発表前に株価がすでに織り込んでいる
- **catalyst の実効性が限定的**: 発表は華やかだが実質的な業績改善に繋がらない

## 4. 修飾因子

### 4.1 Crowding

P-A と同じ。

### 4.2 Catalyst freshness

- freshness（経過営業日）が短いほど confidence 高
- 60 営業日を超える catalyst は「織り込み済み」の可能性

### 4.3 出来高増

- catalyst 後の出来高増は「市場反応あり」の signal
- 出来高を伴わない catalyst は「素通り」の可能性

### 4.4 業種 RS

- catalyst 発生時に業種全体が RS 上位に位置していると、re-rating 確度が高まる

## 5. 無効化条件

P-A の無効化条件に加えて:

- カタリスト後の出来高が伴わない（2 日間以上）
- 織り込み済みで追随買いが入らない（catalyst 発表後に株価反応がない）
- 決算またぎに該当する（kill switch で自動除外）
- 同業種他社が catalyst 発表後に下落している（業種全体で織り込み済み）

## 6. Exit 戦略

- **利確目標**: catalyst 織り込み完了と判断される水準（業種 re-rating + valuation 回帰）
- **損切り**: entry 価格から -8% 〜 -10%
- **時間切れ**: 最長 40 営業日
- **無効化 exit**: 節 5 の無効化条件に該当したら即時 exit 検討

## 7. Position sizing

- 1,000 億円以上: 2%
- 500-1,000 億円: 1%
- **300-500 億円帯: 0.5%**（P-B のみ、catalyst freshness ≦ 10 営業日 + 出来高 1.5x 以上を満たす場合）

## 8. Kill switch

P-A と同じ。

## 9. AI の役割境界

P-A と同じ。catalyst 欄の一次ソース URL 確認は **人間必須**。

## 10. 改訂履歴

| 版 | 日付 | 変更内容 | 判断根拠 |
| --- | --- | --- | --- |
| v1 | 2026-04-24 | 初版（#7 5.3 から正本化） | アーキテクチャ v1 統合 |

## 11. 参考

- [`valuation-mean-reversion-v1.md`](./valuation-mean-reversion-v1.md): P-A 本命 playbook
- [`../docs/screening/principles.md`](../docs/screening/principles.md): スクリーニング原則
- [`../docs/screening/mechanical-v1.md`](../docs/screening/mechanical-v1.md): 機械的ふるい閾値
- [`../docs/screening/macro-gate-procedure.md`](../docs/screening/macro-gate-procedure.md): Macro gate 判定
- [`../docs/components/research.md`](../docs/components/research.md): research 運用仕様
