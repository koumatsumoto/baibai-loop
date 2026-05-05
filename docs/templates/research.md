---
ticker: "XXXX"
name: "..."
playbook: valuation-reversion | strict-net-cash-discount | fcf-yield-discount | cash-rich-asset-discount | cashflow-yield-discount | sales-discount-growth
supporting_signals: []                  # candidates.signals[].name のうち primary 以外
decision: accepted | skipped | pending
candidates_ref: records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml
outlook_ref: records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml
brief_refs: []                          # 任意、outlook 後の緊急 brief がある場合のみ
ai-draft: true
published_at: "YYYY-MM-DDTHH:MM:SS+09:00"
tradable_at: "YYYY-MM-DDTHH:MM:SS+09:00"
macro_gate: tailwind | neutral | headwind
macro_gate_override: "..."              # headwind 採用時のみ
overrides: []                           # system signal を上書きする場合は理由を記録
external_refs: []                       # 外部AI / 二次分析を参照する場合
position_size_oku: 0.01                 # accepted/pending: > 0 必須。skipped: 0 強制
hypothetical_position_size_oku: 0.005   # 任意。skipped で参考値として記録する場合のみ
avg_turnover_oku: 5.0
adv_participation_pct: 0.2
market_cap_oku: 936
sector_33: "情報・通信業"
valuation:
  per_forward: 8.2
  per_trailing: 9.5
  pbr: 0.72
  ev_ebitda: null
  p_s: 0.6
  pcfr: 5.1
  ocf_yield: 0.13
  cash_to_market_cap: 0.42
  price_to_equity: 0.82
  equity_ratio: 0.45
  primary_metric: ["pbr", "ocf_yield"]
---

# Research: YYYY-MM-DD XXXX [銘柄名] [playbook]

**成分**: 4 成分アーキテクチャの **(d) 個別銘柄リサーチ**（[`/docs/components/research.md`](/docs/components/research.md)）

**Playbook**: [valuation-reversion | strict-net-cash-discount | fcf-yield-discount | cash-rich-asset-discount | cashflow-yield-discount | sales-discount-growth]

## 1. Thesis

一文で why now × why this stock。マクロゲート、primary signal、主要な反対仮説を明示。

例: `マクロは {tailwind} の業種に属し、{cashflow-yield-discount} が出ている。営業 CF yield は {X%}、売上悪化は限定的で、構造悪化ではなく一時的な評価低下と見る。`

## 2. Macro gate [最上位ゲート]

- **判定**: tailwind | neutral | headwind
- **業種**: [東証 33 業種]（outlook で {tailwind/neutral/headwind}）
- **地域**: [地域]（outlook で {tailwind/neutral/headwind}）
- **保守側優先判定結果**: [最終 gate 判定]
- **outlook_ref**: [outlook path]
- **brief_refs**（任意）: [outlook 後の緊急 brief があれば]
- **1-2 行要約**: [gate 判定の要点]

headwind の場合は原則採用不可。採用する場合は `macro_gate_override` に system signal を上書きする理由を残す。

## 3. Candidate signals + valuation snapshot

### 3.1 Candidate signals

| signal | playbook | hit reasons | primary metric |
| --- | --- | --- | --- |
| valuation-reversion | valuation-reversion | sector_self_range | PER / PBR |
| strict-net-cash-discount | strict-net-cash-discount | net_cash_to_market_cap_price_to_equity_and_equity_ratio | net_cash_to_market_cap |
| fcf-yield-discount | fcf-yield-discount | fcf_yield_discount | fcf_yield |
| cash-rich-asset-discount | cash-rich-asset-discount | cash_to_market_cap_price_to_equity_and_equity_ratio | cash_to_market_cap |
| cashflow-yield-discount | cashflow-yield-discount | ocf_yield_discount | ocf_yield |
| sales-discount-growth | sales-discount-growth | ps_discount_growth_intact | P/S |

### 3.2 Valuation snapshot

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 3 年パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (forward) | 8.2 | 12.0 | -32% | 10% | |
| PER (trailing) | 9.5 | 13.0 | -27% | 15% | |
| PBR | 0.72 | 1.10 | -35% | 12% | ✓ |
| EV/EBITDA | unavailable | 7.2 | n/a | n/a | |
| P/S | 0.6 | 1.1 | -45% | 8% | |
| PCFR | 5.1 | 8.0 | -36% | 18% | |
| OCF yield | 13.0% | 8.0% | +5.0pt | n/a | ✓ |
| Cash / market cap | 42.0% | n/a | n/a | n/a | |
| Equity ratio | 45.0% | n/a | n/a | n/a | |

**primary metric**: [最も効いた 1-2 指標]

## 4. 割安の原因仮説

以下のどれか（または複数）:

- 市場全体の短期売り
- 業種ローテーションの一過性
- 一過性の悪材料
- 利益率低下・投資先行による短期的な見栄え悪化
- ネットキャッシュ / 資産価値 / CF 創出力の見落とし
- インデックス構成変更・需給要因

**本銘柄の原因仮説**: [1-2 段落で記述、一時的である根拠を含む]

## 5. 反対仮説 - 構造的理由（必須）

以下のうち該当するものを検討:

- 構造的な成長鈍化
- ガバナンス懸念
- 技術的陳腐化
- accounting 警戒
- 業界需要の構造的縮小
- ESG / 規制リスク
- 大株主の売り圧力
- 営業 CF の一過性要因
- 現金同等物を相殺する有利子負債・偶発債務
- その他（自由記述）

**本銘柄の反対仮説**: [1-2 段落で記述。「この仮説が正しい場合、割安は trap である」と明記]

## 6. Catalyst

- **種別**: [決算修正 / 自社株買い / 大口受注 / 東証開示 / 英語開示 / その他 / なし]
- **発生日**: YYYY-MM-DD
- **経過営業日**: XX 日
- **一次ソース URL**: [URL]
- **要点**: [1-2 行]

catalyst がない場合は、どの signal が catalyst 不在を補う margin of safety になっているかを明記する。

## 7. Price reaction

| 対象 | 値 | 変化 | ソース |
| --- | --- | --- | --- |
| 前日終値 | XX,XXX 円 | +X.X% | [J-Quants](URL) |
| 週次騰落 | - | +X.X% | 計算 |
| 60 営業日騰落 | - | -X.X% | 計算 |
| 出来高比（20 日平均） | - | X.Xx | 計算 |

## 8. Crowding

| 指標 | 現値 | 60 日推移 | ソース |
| --- | --- | --- | --- |
| 空売り残高 (対発行済株式比) | X.X% | ↑↓→ | [JPX](URL) |
| 日々公表信用指定 | [有/無] | - | [JPX](URL) |
| 特別注意 | [有/無] | - | [JPX](URL) |
| 貸借銘柄状態 | [正常/逼迫] | - | [JPX](URL) |

## 9. 株主還元確認

| 項目 | 確認結果 | 一次ソース |
| --- | --- | --- |
| 配当政策 | [累進 / DOE / 配当性向 / 未定] | [URL] |
| 自社株買い | [有 / 無 / 余地あり] | [URL] |
| DOE or 配当性向 | [X%] | [URL] |
| 減配リスク | [低 / 中 / 高] | [根拠] |

## 10. ミクロ 4 軸寄与度表

| 軸 | 寄与度 | 備考 |
| --- | --- | --- |
| Valuation | strong / weak / neutral | primary: pbr, ocf_yield |
| Mean-Reversion | strong / weak / neutral | [根拠] |
| Catalyst | strong / weak / neutral | [根拠] |
| Crowding | strong / weak / neutral | [踏み上げ余地 / 逆回転リスク] |

## 11. Entry 条件

- **価格レンジ**: XX,XXX 円 - XX,XXX 円
- **日付制約**: [kill switch で避ける日があれば列挙]
- **トリガー**: [出来高増の確認、2 日連続陽線、etc.]

## 12. Exit 条件

- **利確目標**: XX,XXX 円（+X%）
- **損切り**: XX,XXX 円（-X%）
- **時間切れ**: 最長 40 営業日（YYYY-MM-DD まで）

## 13. Invalidation + Pre-mortem

### 13.1 無効化条件

- [業績下方修正が出た]
- [営業 CF の一過性要因が判明した]
- [有利子負債確認により cash-rich 仮説が崩れた]
- [マクロゲートが headwind に転じた]

### 13.2 Pre-mortem（3 営業日以内の無効化シナリオ）

- [3 営業日以内に entry を諦める条件を具体化]
- [マクロゲート reversal シナリオ]

## 14. Position size + 採用判定

- **時価総額**: XXX 億円
- **signal 数**: 1 / 2+
- **許容 position**: single signal は最大 1%、複数 signal は最大 2%
- **ADV 参加率**: X.X%
- **採用 position**: X.X%
- **採用判定**: 採用 | 見送り | 保留
- **判定理由**: [1-2 段落、4 軸寄与度・反対仮説・kill switch 確認結果を踏まえて]

現在の実資金が 100-200 万円程度の場合、paper proxy の ADV cap は実運用ではほぼ拘束しない。混乱を避けるため、paper proxy の sizing は検証用の上限として扱い、実資金の集中度は trade 側の real fields で別管理する。

---

**Kill switch 確認**:

- [ ] 決算またぎエントリーではない
- [ ] 日銀会合前日エントリーではない
- [ ] FOMC 前日エントリーではない
- [ ] マクロゲート: tailwind または neutral（headwind なら override 理由を明示）
- [ ] 会社IRで直近決算 / 説明資料 / 株主還元方針を確認済み

全 check が ✓ の場合のみ採用可。

---

参照: [`/docs/components/research.md`](/docs/components/research.md), [`/docs/screening/principles.md`](/docs/screening/principles.md), [`/records/_playbooks/`](/records/_playbooks/)
