---
ticker: "XXXX"
name: "..."
playbook: valuation-mean-reversion-v1 | valuation-catalyst-confirmation-v1
decision: accepted | skipped | pending
candidates_ref: records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml
outlook_ref: records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml
brief_refs: []                            # 任意、outlook 後の緊急 brief がある場合のみ
ai-draft: true
published_at: "YYYY-MM-DDTHH:MM:SS+09:00"
tradable_at: "YYYY-MM-DDTHH:MM:SS+09:00"
macro_gate: tailwind | neutral | headwind
macro_gate_override: "..."                # headwind / 200-500 億 P-A 採用時のみ
position_size_oku: 0.01                   # accepted/pending: > 0 必須。skipped: 0 強制 (validator)
hypothetical_position_size_oku: 0.005     # 任意。skipped で参考値として記録する場合のみ
avg_turnover_oku: 5.0                     # candidates 由来の 20 日平均売買代金。adv_participation_pct を書く場合は > 0 必須
adv_participation_pct: 0.2                # = position_size_oku / avg_turnover_oku * 100。skipped で position 0 ならここも 0
market_cap_oku: 936
sector_33: "情報・通信業"
valuation:
  per_forward: 8.2                        # null if 会社予想 EPS 未公表
  per_trailing: 9.5
  pbr: 0.72
  ev_ebitda: 4.8
  p_s: 0.6
  pcfr: 5.1
  primary_metric: ["per_forward", "pbr"]
---

# Research: YYYY-MM-DD XXXX [銘柄名] [playbook]

**成分**: 4 成分アーキテクチャの **(d) 個別銘柄リサーチ**（[`/docs/components/research.md`](/docs/components/research.md)）

**Playbook**: [valuation-mean-reversion-v1 | valuation-catalyst-confirmation-v1]

## 1. Thesis

一文で why now × why this stock。マクロゲート × valuation 軸を明示。

例: `マクロは {tailwind} の業種に属し、valuation は業種中央値比 {-X%} の {一時的割安}。{catalyst} で再評価が始まっている（P-B）/ 再評価 trigger 未定だが、過去 3 年レンジ下位 {X%} の過剰売り（P-A）`

## 2. Macro gate [最上位ゲート]

- **判定**: tailwind | neutral | headwind
- **業種**: [東証 33 業種]（outlook で {tailwind/neutral/headwind}）
- **地域**: [地域]（outlook で {tailwind/neutral/headwind}）
- **保守側優先判定結果**: [最終 gate 判定]
- **outlook_ref**: [outlook path]
- **brief_refs**（任意）: [outlook 後の緊急 brief があれば]
- **1-2 行要約**: [gate 判定の要点]

headwind の場合は原則採用不可。neutral は条件付き採用可。

## 3. Valuation snapshot（4 軸評価表、Valuation 軸）

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 3 年パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (forward) | 8.2 | 12.0 | -32% | 10%（下位 10%） | ✓ |
| PER (trailing) | 9.5 | 13.0 | -27% | 15%（下位 15%） | |
| PBR | 0.72 | 1.10 | -35% | 12%（下位 12%） | ✓ |
| EV/EBITDA | 4.8 | 7.2 | -33% | 20%（下位 20%） | |
| P/S | 0.6 | 1.1 | -45% | 8%（下位 8%） | |
| PCFR | 5.1 | 8.0 | -36% | 18%（下位 18%） | |

**primary metric**: [最も効いた 1-2 指標、例: per_forward + pbr]

## 4. 一時的割安の原因仮説（P-A 必須、P-B もできれば）

以下のどれか（または複数）:

- 市場全体の短期売り
- 業種ローテーションの一過性
- 一過性の悪材料（特定の懸念材料が具体的に短期のもの）
- インデックス構成変更
- 需給要因の一時的売り

**本銘柄の原因仮説**: [1-2 段落で記述、一時的である根拠を含む]

## 5. 反対仮説 - 構造的理由（必須、8 例示 + 自由記述）

以下のうち該当するものを検討:

- 構造的な成長鈍化
- ガバナンス懸念
- 技術的陳腐化
- accounting 警戒
- 業界需要の構造的縮小
- ESG / 規制リスク
- 大株主の売り圧力
- **その他（自由記述）**

**本銘柄の反対仮説**: [1-2 段落で記述。「この仮説が正しい場合、割安は trap である」と明記]

## 6. Catalyst（P-B 必須、P-A は空欄可）

- **種別**: [決算修正 / 自社株買い / 大口受注 / 東証開示 / 英語開示 / その他]
- **発生日**: YYYY-MM-DD
- **経過営業日**: XX 日（freshness ≦ 60 営業日）
- **一次ソース URL**: [URL]
- **要点**: [1-2 行]

P-A の場合は「catalyst なし（純粋な valuation mean-reversion 狙い）」と記載。

## 7. Price reaction

| 対象 | 値 | 変化 | ソース |
| --- | --- | --- | --- |
| 前日終値 | XX,XXX 円 | +X.X% | [J-Quants](URL) |
| 週次騰落 | — | +X.X% | 計算 |
| 60 営業日騰落 | — | -X.X% | 計算 |
| 出来高比（20 日平均） | — | X.Xx | 計算 |

## 8. Crowding

| 指標 | 現値 | 60 日推移 | ソース |
| --- | --- | --- | --- |
| 空売り残高 (対発行済株式比) | X.X% | ↑↓→ | [JPX](URL) |
| 日々公表信用指定 | [有/無] | — | [JPX](URL) |
| 特別注意 | [有/無] | — | [JPX](URL) |
| 貸借銘柄状態 | [正常/逼迫] | — | [JPX](URL) |

## 9. ミクロ 4 軸寄与度表

| 軸 | 寄与度 | 備考 |
| --- | --- | --- |
| Valuation | strong | primary: per_forward, pbr |
| Mean-Reversion | strong / weak / neutral | [根拠] |
| Catalyst | strong / weak / neutral | P-A は neutral 固定可 |
| Crowding | strong / weak / neutral | [踏み上げ余地 / 逆回転リスク] |

## 10. Entry 条件

- **価格レンジ**: XX,XXX 円 〜 XX,XXX 円
- **日付制約**: [kill switch で避ける日があれば列挙]
- **トリガー**: [出来高増の確認、2 日連続陽線、etc.]

## 11. Exit 条件

- **利確目標**: XX,XXX 円（+X%）
- **損切り**: XX,XXX 円（-X%）
- **時間切れ**: 最長 40 営業日（YYYY-MM-DD まで）

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件

- [業績下方修正が出た]
- [業種中央値が切り下がり相対割安が消えた]
- [マクロゲートが headwind に転じた]

### 12.2 Pre-mortem（3 営業日以内の無効化シナリオ）

- [3 営業日以内に entry を諦める条件を具体化]
- [マクロゲート reversal シナリオ]

## 13. Position size + 採用判定

- **時価総額**: XXX 億円
- **許容 position**: 2% / 1% / 0.5%（時価総額別上限）
- **採用 position**: X.X%
- **採用判定**: 採用 | 見送り | 保留
- **判定理由**: [1-2 段落、4 軸寄与度・反対仮説・kill switch 確認結果を踏まえて]

---

**Kill switch 確認**:

- [ ] 決算またぎエントリーではない
- [ ] 日銀会合前日エントリーではない
- [ ] FOMC 前日エントリーではない
- [ ] マクロゲート: tailwind または neutral（headwind なら採用不可）
- [ ] 200-500 億円帯なら P-B かつ catalyst freshness ≦ 10 営業日 かつ 出来高 1.5x 以上

全 check が ✓ の場合のみ採用可。

---

参照: [`/docs/components/research.md`](/docs/components/research.md), [`/docs/screening/principles.md`](/docs/screening/principles.md), [`/records/_playbooks/`](/records/_playbooks/)
