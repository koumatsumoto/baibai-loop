---
title: "Strategy 改善 PR 報告 — 5 Phase 統合"
summary: "5 並列調査 (reports/2026-06-19-strategy-improvement-ideas.md) で発見した A-F 候補のうち、forward 計測ベースで意味あり / 実装コスト適正 / 保守性 OK の 11 件 を 5 Phase commit で統合。ablation 4w mean rel -10.98pt → -1.89pt (+9.09pt)、benchmark 規律下 cohort rel +1.23pt (back-fill 3 件除外で +2.52pt 解像度向上)。"
doc_type: report
status: active
date: 2026-06-19
related_docs:
  - "./2026-06-19-strategy-improvement-ideas.md"
  - "../docs/operations/backtest-runbook.md"
  - "../docs/anti-patterns.md"
---

# Strategy 改善 PR 5 Phase 統合報告

## 1. 採用 (各 Phase で計測検証)

| Phase | 候補 ID | 内容 | 計測 | 採用 |
|---:|---|---|---|:-:|
| 1 | B1 | selection_ablation の market_regime bug 修正 | fast_boost Δfull の真値が計測可能に (旧 +10.1pt は misleading) | ✅ |
| 1 | A1 | lane order を `[cash-rich, valuation, cashflow, sales]` に並び替え | ablation full 4w mean rel -10.98pt → -2.44pt (+8.54pt) | ✅ |
| 1 | A2 | `max_recommended_per_sector` 1 → 2 | no_diversity ablation Δfull +1.9pt 根拠 | ✅ |
| 1 | A3 | `valuation-reversion.metrics` に `per_forward` 追加 | docs/screening/valuation-metrics.md mandate 整合 | ✅ |
| 2 | A4 | `cash-rich` から 卸売業 / 不動産業 を除外 | 2026-06-12 candidates で 卸売業 cash-rich 最大 cluster 49/292 | ✅ |
| 2 | C1 | `cashflow-yield` と `cash-rich` に operating_profit deterioration gate | OR-passing 構造の穴塞ぎ | ✅ |
| 2 | C2 | `sales-discount-growth` に operating margin floor (-0.05) | chronic loser escape hatch defang | ✅ |
| 2 | C4 | `cashflow-yield` に FCF-positive gate | OCF+/FCF- (重設備) の cohort underperform 排除 | ✅ |
| 3 | D2 | candidate metric に accruals_to_assets (Sloan 1996) | quality-cheap tilt の base、ranking 統合は次回 | ✅ (記録) |
| 3 | D3 | candidate metric に net_share_change_yoy | dilution / buyback signal の base | ✅ (記録) |
| 4 | B2 | trade record に cohort_tag + benchmark CLI --exclude-cohort-tags | 全 trade rel -1.29pt → 規律下 +1.23pt (+2.52pt 解像度) | ✅ |
| 5 | F1 / F3 | ablation の no_prior_suppression / no_stabilization variant 削除 | 両者 Δfull ≈ 0pt の dead code | ✅ |

## 2. 採用見送り (本 PR 内、各々理由付き)

| 候補 | 理由 | 次のアクション |
|---|---|---|
| E3 (sector hard cap) | 既存 `_check_portfolio_concentration` が severity=error で hard-enforce 済 | 実装不要、確認のみ |
| E5 (macro_fit neutral preflight) | 既存 entry-preflight が risk_on_rally を hard trigger として扱い、proceed disallow している | 過去 8 件全件 risk_on_rally なのに pass している原因は back-fill / discretionary override で preflight 未適用 → B2 で cohort 分離で対応済み |

## 3. Scope creep 回避で次回 PR に再分類

| 候補 | 内容 | 理由 | 想定実装コスト |
|---|---|---|---|
| B3 | closed trade の exit attribution forward 計測 | 全 8 件 open、closed sample ゼロで価値検証不可。新 sub-command 実装は scope 大 | 中-大 |
| E1 | research front matter に screening_context (rank/regime/short_signal/next_earnings_date) 構造化 | schema + validator 拡張、scope 中 | 中 |
| C3 | EDINET `operating_profit_ttm` を J-Quants single-period に優先 | schema + metrics + lane gate 拡張、season noise 改善 | 中 |
| D1 | realized vol (Parkinson) ranking 統合 | OHLV provider layer の field drop 問題で provider 拡張必要 | 中 |
| D4 | upper/lower limit flags 集計 | provider 拡張 + lens 新設 | 小 |
| D5 | 業種別 momentum + 1-month reversal | metrics 拡張 + lane evidence | 中 |
| D6 | PEAD (post-earnings drift) | catalyst lane + earnings calendar 拡張 | 中-大 |
| E2 | `planned_exit.event_reviews[]` field 化 | schema 拡張 + validator | 中 |
| E4 | time_stop を lane 別 horizon に分ける | trade schema + validator | 小-中 |
| E6 | conviction-driven sizing (Kelly fraction) | policy_config + validator + 実 trade history calibration | 大 |
| D2/D3 ranking 統合 | accruals / share issuance を ranking 補正に組み込む | metric 記録 → ranking calibration、sample 必要 | 中 |
| F4 | profile 抽象削除 (BUILTIN_SELECTION_PROFILES) | ablation の no_diversity が profile_overrides 経路で使用中 → 削除は ablation infra 修正必要 | 中 |
| F5 | 2026-06-12 candidates regenerate (config drift 解消) | 既に本 PR で 5 週分再生成済み、6/12 は再生成成功 | 完了 |

これらは「次回 strategy 改善 PR の Phase 1-3 候補」として明示的に登録。各々の forward 計測経路は本 PR で確立した base (B1 ablation fix + B2 cohort 分離) で評価可能。

## 4. 価値検証の数値 (5 Phase 累積)

### 4.1 ablation full variant (top=5, 6 週 candidates_phase2)

| 計測時点 | 1w mean rel | 4w mean rel |
|---|---:|---:|
| PR #249 後 main (旧 baseline) | -1.92pt (n=20) | **-10.98pt** (n=15) |
| Phase 1 後 (orig 6 weeks) | +0.29pt (n=20) | **-2.44pt** (n=15) |
| Phase 2 後 (regen 5 weeks) | +2.59pt (n=15) | **-1.89pt** (n=10) |
| Phase 3 後 (signal record only) | +2.59pt (n=15) | -1.89pt (n=10) |

→ **4w で +9.09pt 改善** (PR #249 → Phase 2)、1w で +2.21pt 改善 (PR #249 → Phase 1)。

注意:
- Phase 1 → Phase 2 で n が変わったのは 2026-05-01 が JPX coverage 不足で regen 失敗のため
- 4w n=10 は小サンプル、bootstrap CI 必要 (本 PR では未計算)

### 4.2 benchmark (asof 2026-06-12, 全 trade vs 規律下 cohort)

| cohort | 件数 | rel |
|---|---:|---:|
| 全 trade (混合) | 8 | **-1.29pt** |
| 規律下 (back-fill 3 件除外) | 5 | **+1.23pt** |

→ B2 で **+2.52pt 解像度向上**。「現行 workflow の純度」を計測可能に。

## 5. 残課題 / 注意点

1. **小サンプル**: 8 件 trade、forward 1-31bd の不均一、single regime (risk_on_rally) のみ → cohort 統計はノイズ多。bootstrap CI 計算が本 PR 未着手 (次回 PR で対応)
2. **2026-05-01 candidates regen 失敗**: JPX regulation source coverage 不足で 1 週欠損。bootstrap-cache --asof 2026-05-01 で復旧可能 (本 PR scope 外)
3. **D2/D3 metric は記録のみ**: ranking / lane gate に統合していない。forward calibration sample 蓄積を待つ
4. **B3 (exit attribution) 未着手**: closed trade ゼロでサンプル不在、ledger CLI 拡張は次回 PR で
5. **lane drop_lane の解釈変化**: Phase 1 後 ablation で cash-rich/valuation 両方が「drop すると悪化」になった。lane order の変更で primary evidence pick が変わり、各 lane の marginal contribution が変動。lane の本来評価は drop 単独でなく合成効果で見る必要

## 6. 各 Phase commit

| commit | Phase | 主な内容 |
|---|---|---|
| 7bd2234 | 1 | B1 ablation fix + A1 lane order + A2 diversity cap + A3 per_forward |
| fabc088 | 2 | A4 sector + C1 deterioration + C2 margin + C4 FCF |
| 9ba34a4 | 3 | D2 accruals + D3 share issuance (metric 記録) |
| fc41c4a | 4 | B2 cohort_tag + benchmark --exclude-cohort-tags |
| (this) | 5 | F1/F3 dead variant + docs + backtest-runbook §6 + 本 report |

## 7. レビュー結果 (最終 5 名 adversarial)

(本 PR 作成後に reviewer 起動、結果反映後追記)
