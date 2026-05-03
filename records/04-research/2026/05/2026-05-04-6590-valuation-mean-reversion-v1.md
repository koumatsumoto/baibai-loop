---
ticker: "6590"
name: "芝浦メカトロニクス"
playbook: valuation-mean-reversion-v1
decision: skipped
candidates_ref: records/03-candidates/2026/04/2026-04-24.yaml
outlook_ref: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
  - records/01-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
ai-draft: true
published_at: "2026-05-04T19:00:00+09:00"
tradable_at: "2026-05-15T09:00:00+09:00"
macro_gate: tailwind
position_size_oku: 0.005
adv_participation_pct: 0.00585
avg_turnover_oku: 85.4
market_cap_oku: 693
sector_33: "電気機器"
valuation:
  per_forward: 30.14
  per_trailing: 7.35
  pbr: 1.24
  ev_ebitda: null
  p_s: null
  pcfr: null
  primary_metric: ["per_trailing", "pbr"]
---

# Research: 2026-05-04 6590 芝浦メカトロニクス valuation-mean-reversion-v1

**成分**: 4 成分アーキテクチャの **(d) 個別銘柄リサーチ** ([`/docs/components/research.md`](/docs/components/research.md))

**Playbook**: valuation-mean-reversion-v1 (P-A)

**判定**: **見送り (skipped)**。詳細は §13 を参照。理由は ①candidates 由来の `price_change_60d: -0.8129` が 2026-03-01 効力発生の 1:5 株式分割を split 調整していない可能性が高く、「過剰売り」前提の P-A 仮説が成立しない、②sector relative strength percentile 1.0 は sector 全体の rank であり 6590 個別の相対強度ではないため、銘柄固有 mean-reversion edge を主張できない、③顧客別・地域別の事業構造を有報・決算説明資料で裏取りしないと反対仮説 (中国向け規制、メモリ向け縮小) の確度判定ができない。

## 1. Thesis (修正後)

電気機器 sector / japan-external-demand region の macro tailwind に属し、PER trailing 7.35 / PBR 1.24 は valuation 軸の絶対水準としては低い。ただし、candidates 4/24 由来の `price_change_60d: -0.8129` は 2026-02-05 開示 / 2026-02-28 基準 / 2026-03-01 効力の **1:5 株式分割を split 調整していない artifact である可能性が高い** (1:5 分割で株価が機械的に 1/5 = -80% 動く)。**P-A の中核根拠 (過剰売り) が成立しない可能性が極めて高いため、本 packet では skipped とする**。split 調整済みの candidates が再生成された後に再評価する。

## 2. Macro gate [最上位ゲート]

- **判定**: tailwind (P-A 採否とは独立に成立)
- **業種**: 電気機器 (outlook 2026-05-04 で `tailwind`)
- **地域**: japan-external-demand (outlook で `tailwind`)
- **保守側優先判定結果**: tailwind
- **outlook_ref**: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **brief_refs**: records/01-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- **1-2 行要約**: TSMC 1Q26 Capex 上方 + NVIDIA AI 需要持続で半導体製造装置・電子部品 sector に AI 軸 tailwind。日本側は短観製造業 +17 / 設備投資 +7.9% で底堅さ。電気機器 sector tailwind は P-A 採用条件には足りず、銘柄個別 edge が必要

## 3. Valuation snapshot (4 軸評価表)

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 3 年パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (trailing) | 7.35 | (~32.4) | -77.3% | 4.1% (下位 4.1%) | ✓ |
| PER (forward) | 30.14 | — | — | — (会社予想ベース、来期業績の不透明) | |
| PBR | 1.24 | (~1.99) | -37.8% | 4.1% (下位 4.1%) | ✓ |
| EV/EBITDA | null (TTM 不足) | — | — | — | |
| P/S | null (TTM 不足) | — | — | — | |
| PCFR | null (TTM 不足) | — | — | — | |

**primary metric**: per_trailing + pbr。

**注意**: PER trailing 7.35 は分割前の earnings ベースで計算されている可能性。業種中央値 (~32.4) との -77.3% 乖離は、業種中央値の earnings 集計タイミングと 6590 の trailing earnings 期間 / split 反映状況が揃っていない場合、artifact になりうる。**split 調整済み candidates での再計算が必要**。

## 4. 一時的割安の原因仮説 (P-A 必須)

**当初仮説 (split artifact 判明前)**: 60 日 -81.3% を「半導体装置サイクル懸念 + 業種ローテーションの過剰売り」と解釈していたが、これは split 調整未済の price_change の誤読である可能性が高い。

**修正後の仮説**: split 調整済み price で計算し直さない限り、本銘柄の「過剰売り」origin は判定不能。candidates の自動 screening pipeline が split 調整を行わずに ranking しているとすれば、他にも同様の split artifact が候補に混入している可能性。

## 5. 反対仮説 - 構造的理由 (必須)

- **本 packet の中核反対仮説 = split artifact**: candidates 由来の `price_change_60d: -0.8129` が 1:5 分割の technical 調整に対応している可能性が極めて高い。実際の adjusted close ベースで -10% 程度の小幅な調整しかしていなければ、P-A の「過剰売り」前提は不成立。`self_range_percentile` 4.1% も同様に split-unadjusted の自己レンジ評価なら artifact になる
- **forward PER 30 の意味**: 来期会社予想 EPS 想定では trailing PER 7.35 と大きく乖離。これは業績下方修正 / 一過性利益 / 会計年度 timing の差異の可能性があり、銘柄固有の業績 cycle 構造を有報・決算説明資料で確認しないと評価不能
- **顧客別売上構造の確認不足**: 公式製品ページ (https://www.shibaura.co.jp/products/semicon/) で確認できるのは洗浄・エッチング・ボンディング等の **製品領域** までで、TSMC / Samsung / Kioxia 等への顧客別売上比率や中国向け売上比率は本 packet 内では裏取りしていない。有報 (有価証券報告書) と決算説明資料の確認が必要
- **親会社 (東芝グループ) との取引比率**: 親会社東芝の事業再編・グループ内取引動向は連結ベースで追わないと評価できない、本 packet 内では未確認

## 6. Catalyst (P-A は空欄可)

P-A 純粋型のため catalyst なし。

ただし、3 月決算企業として 5 月中旬-下旬の通期決算 (会社予想 EPS) と決算説明資料で、forward PER 30 の織り込みが「一過性業績悪化」か「構造的業績調整」かを確認する。これは **本 packet を skipped に留める根拠の検証 trigger** となる。

## 7. Price reaction (修正後の取扱い)

| 対象 | 値 | 注釈 |
| --- | --- | --- |
| 60 営業日騰落 (candidates 由来) | -81.3% | **split 1:5 (2026-03-01 効力) 未調整の疑いが極めて高い**。adjusted close ベースで再計算必要 |
| 4 週騰落 (candidates 由来) | +8.9% | 4 週は 2026-03-25 〜 2026-04-22 程度なので split 後区間、信頼性は比較的高い |
| 平均出来高 (20 日) | 85.4 億円/日 | 流動性は十分 |
| 自己 750 日レンジ位置 | 4.1% | split 未調整なら artifact、再計算必要 |
| 業種相対強度 percentile | 1.0 | **これは sector 全体の return 順位** (`metrics.py:_rank_to_percentiles`) で、電気機器 sector が 33 業種中で強いことを示す。**6590 個別の同業種内相対強度ではない** |

**sector_relative_strength_percentile の正しい読み方**: `src/baibai_loop/screening/metrics.py:428` の `_rank_to_percentiles` は sector ごとに 1 つの値を計算し、`src/baibai_loop/screening/rules.py:91` は `<= 0.20` を「弱い sector」として扱う (sector ローテーション短期売り判定の input)。1.0 は電気機器 sector が **全 sector 中で最も強い** ことを示し、6590 個別の相対強度とは関係ない。

## 8. Crowding

| 指標 | 現値 | 60 日推移 | ソース |
| --- | --- | --- | --- |
| 空売り残高 (対発行済株式比) | 未確認 | — | [JPX](https://www.jpx.co.jp/markets/statistics-equities/short-selling/) |
| 日々公表信用指定 | 無 (candidates 通過時点) | — | [JPX](https://www.jpx.co.jp/markets/equities/special/index.html) |
| 特別注意 | 無 | — | [JPX](https://www.jpx.co.jp/markets/equities/special-caution/index.html) |
| 貸借銘柄状態 | 通常想定 | — | [JPX](https://www.jpx.co.jp/listing/stocks/loan/index.html) |

## 9. ミクロ 4 軸寄与度表

| 軸 | 寄与度 | 備考 |
| --- | --- | --- |
| Valuation | weak | per_trailing 業種比 -77% は split-unadjusted の earnings 比較の可能性、信頼性低 |
| Mean-Reversion | weak | 60 日 -81% は split artifact、過剰売り根拠不成立 |
| Catalyst | neutral | P-A 純粋型 |
| Crowding | neutral | 規制対象なし、ただし空売り残高未確認 |

## 10. Entry 条件

skipped のため定義しない。再評価時 (split 調整済み candidates 再生成後) に必要なら定義する。

## 11. Exit 条件

skipped のため定義しない。

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件 (本 packet が skipped であることを示す根拠)

- **split artifact が確定**: split 調整済み adj close ベースで `price_change_60d` を再計算すると -10% 〜 -15% 程度の小幅な調整しかしていなければ、P-A の「過剰売り」前提は不成立。本 packet の skipped が確定
- **業績下方修正の構造化**: 5 月通期決算で会社予想 EPS が現状 forward PER 30 想定をさらに下方修正すれば、構造的 trap で skipped 維持
- **顧客構造の不確実性**: 有報で TSMC / Samsung / Kioxia への露出と中国向け売上比率が確認できなければ、銘柄固有の判断材料が揃わず skipped 維持

### 12.2 再評価条件 (skipped → pending or accepted への昇格 trigger)

- split 調整済み candidates が再生成され、`price_change_60d` が adj close ベースで -25% 以上の真の過剰売りである場合
- 5 月通期決算で会社予想 EPS が市場想定を上回り、forward PER 30 が一過性であることが確認される場合
- 有報で AI / 半導体 capex 関連の顧客 / 製品 mix が明示され、TSMC Capex 上方の spillover が定量的に確認される場合
- 上記が揃った時点で再 packet 化して accepted/pending を判定する

## 13. Position size + 採用判定

- **時価総額**: 693 億円
- **時価総額帯**: 500-1,000 億円帯 (max 1.0%)
- **adv_participation 計算 (修正)**: position_size_oku 0.005 ÷ avg_turnover_oku 85.4 × 100 = **0.00585%** (流動性は十分余裕。前回記載の `0.585` は計算ミスで 100 倍ズレ、修正)
- **採用 position**: 0 (skipped のため建玉せず)
- **採用判定**: **見送り (skipped)**
- **判定理由**:
  - **核心**: candidates 4/24 由来の `price_change_60d: -0.8129` が 2026-03-01 効力 1:5 株式分割の split artifact である可能性が極めて高く、P-A の「過剰売り mean-reversion」前提が成立しない
  - **付随**: `sector_relative_strength_percentile: 1.0` は sector 全体の rank であり、6590 個別の相対強度ではない。当初の「同業種内で最強」解釈は誤読
  - **付随**: 顧客別 / 地域別 / 親会社取引比率が一次情報 (有報 / 決算説明資料) で裏取りされておらず、銘柄固有の事業構造判断ができない
  - **見送りの限定**: macro_gate tailwind は維持。本 packet の skipped は P-A 仮説の不成立による (gate 起因ではない)。split 調整済み candidates 再生成 + 5 月決算結果 + 有報確認後に再評価する余地あり

---

**Kill switch 確認** (skipped のため reference のみ):

- [x] 決算またぎエントリー想定なし (skipped)
- [x] 日銀 / FOMC 前日エントリー想定なし (skipped)
- [x] マクロゲート: tailwind (P-A 採否と独立)
- [x] 200-500 億円帯 P-A 制約は該当せず

---

**ledger 経路**: skipped 判定は `baibai-loop-ledger sync` で `_ledger/skipped/` に記録される ([`/docs/components/ledger.md`](/docs/components/ledger.md))。再評価時は新規 packet として `04-research/` に追加する。

参照: [`/docs/components/research.md`](/docs/components/research.md), [`/docs/screening/principles.md`](/docs/screening/principles.md), [`/records/_playbooks/valuation-mean-reversion-v1.md`](/records/_playbooks/valuation-mean-reversion-v1.md), [`/records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml`](/records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml), 芝浦メカトロニクス株式分割公告 (https://www.shibaura.co.jp/ir/report/pdf/koukoku_260213_SM.pdf, 2026-02-05 開示 / 2026-02-28 基準 / 2026-03-01 効力 1:5 分割)
