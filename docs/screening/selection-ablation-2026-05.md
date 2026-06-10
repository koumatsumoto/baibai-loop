---
title: "Selection ablation 計測 (2026-05)"
summary: "selection の ranking 構成要素と lane を 1 つずつ無効化した variant を replay し、各機能の forward return 寄与を計測する。"
doc_type: reference
status: active
last_reviewed: 2026-06-10
related_docs:
  - "./lane-cohorts-2026-05.md"
  - "./regime-lens-replay-2026-05.md"
  - "./mechanical.md"
---

# Selection ablation 計測 (2026-05)

`#215` の運用試験。selection の各構成要素(fast boost / long_hold / lane rank / strength / evidence count / diversity / prior-research suppression)と各 lane について、「その機能だけを無効化した variant」の推奨 queue を replay し、forward return への寄与を計測した。機能の削減・維持判断の根拠データにする。

- ツール: `baibai-loop-ledger selection-ablation`(価格基準・eval cap は [`replay-2026-05.md`](./replay-2026-05.md) と同一)
- 対象: `.cache/replay/candidates` の 2026-05 4 週、balanced profile、top 5、eval cap `2026-06-08`
- replay 同様 macro-agnostic。**market regime lens も外して計測**する(full = fast boost 常時 ON)。これは「素の構成要素」の寄与を測るためで、regime lens 自体の効果は [`regime-lens-replay-2026-05.md`](./regime-lens-replay-2026-05.md) で別途計測済み
- `Δfull` = variant の mean relative − full の mean relative(正 = その機能を切ると改善 = 機能が足を引っ張っている)
- `overlap` = full の推奨 queue との銘柄重複率

## スコアボード(mean relative %、4 週)

| variant | 1w rel | 1w Δfull | 4w rel | 4w Δfull | overlap |
| --- | ---: | ---: | ---: | ---: | ---: |
| full | -0.56 | — | -10.77 | — | 100% |
| no_fast_boost | -0.48 | +0.08 | -3.19 | **+7.58** | 35% |
| no_long_hold | +0.35 | +0.91 | -10.77 | +0.00 | 85% |
| no_lane_rank | -2.78 | -2.22 | -10.77 | +0.00 | 55% |
| no_strength | -1.55 | -0.99 | -7.98 | +2.79 | 55% |
| no_evidence_count | -0.56 | +0.00 | -10.77 | +0.00 | **100%** |
| no_diversity | -2.39 | -1.83 | -9.13 | +1.64 | 60% |
| no_prior_suppression | -0.56 | +0.00 | -10.77 | +0.00 | **100%** |
| drop_lane:valuation-reversion | -3.36 | -2.80 | -10.99 | -0.22 | 20% |
| drop_lane:strict-net-cash-discount | -0.56 | +0.00 | -10.77 | +0.00 | **100%** |
| drop_lane:fcf-yield-discount | -0.56 | +0.00 | -10.77 | +0.00 | **100%** |
| drop_lane:cash-rich-asset-discount | -0.68 | -0.12 | -12.30 | -1.53 | 90% |
| drop_lane:cashflow-yield-discount | -1.34 | -0.78 | -10.05 | +0.72 | 80% |
| drop_lane:sales-discount-growth | -0.77 | -0.21 | -10.77 | +0.00 | 95% |

## 観察された事実

1. **fast boost が最大の劣後要因**(4w Δ+7.58pt)。regime lens の検証([`regime-lens-replay-2026-05.md`](./regime-lens-replay-2026-05.md))と同じ結論を、構成要素単位の ablation でも再確認した
2. **`evidence_count` 成分は 4 週 × 全 variant で推奨 queue を 1 銘柄も変えていない**(overlap 100%)。sort key の死荷重
3. **prior-research suppression もこの 4 週では queue を変えていない**(research decision が 14 件しかなく、top5 と衝突しなかった)。ただし deferred/rejected の再登場抑制は運用規律としての存在理由が別にある
4. **strict-net-cash / fcf-yield lane は top5 に一度も届いていない**(drop しても無変化)。lane cohort 計測([`lane-cohorts-2026-05.md`](./lane-cohorts-2026-05.md))では両 lane が最も強い(対 baseline 4w +0.41 / +3.85pt)にもかかわらず、selection の `_LANE_RANK` は valuation-reversion(cohort 最弱、-2.17pt)を最優先しており、**コード内の lane 優先順位が実測の lane 品質と逆転**している。なお `research_selection_lane_order`(primary evidence 選択用)は strict-net-cash を先頭にしており、同一コードベース内に互いに矛盾する 2 つの lane 順序が存在する
5. long_hold ranking 成分は 4w で効果ゼロ、1w では外した方が +0.91pt(85% 同一 queue)。ranking への寄与は観測されない(annotation としての価値は別問題)
6. lane rank と diversity caps は 1w で有意にプラス寄与(外すと -2.22 / -1.83pt)。維持する根拠がある

## 解釈の限界

- 2026-05(rally 月)4 週・top5・balanced のみの観測。4w が解決済みなのは 2 週分
- variant は事前列挙した機能スイッチであり、閾値 sweep は行っていない(forward-only 原則)
- 「queue を変えない機能」は「この 4 週で発動機会がなかった」ことを意味し、機能の論理的不要を直ちに意味しない(prior suppression が典型)。削減判断では発動条件の論理とコード保守コストを併せて評価する

## 2026-06-10 の適用結果

本計測と lane cohorts / replay の実測、および利用状況調査に基づき、以下を適用した(#217):

- `evidence_count` sort 成分を削除(全 variant で queue 無変化)
- long_hold を sort key から外し annotation に限定(ranking 寄与の観測なし)
- lane 順序の正本を config `research_selection_lane_order` に一本化(コード内 `_LANE_RANK` を廃止)。順序値は queue 実測を優先して旧 `_LANE_RANK` 順(valuation-reversion 先頭)に据え置き、cohort 実測順への変更はサンプル蓄積後に再判断する
- built-in selection profile を `balanced` のみに削減(custom profile は `--profile-config` 経由)

適用後の replay(regime lens on、balanced top5、2026-05 4 週)は 1w mean relative -0.48 → +0.75pt / 4w -3.19 → +0.58pt と旧構成を上回った(queue 重複 1〜2/5。multi-hit 候補の primary lane 正規化が統一されたことで diversity cap の効きが変わった効果が大きい。in-sample・小サンプルの限界は他計測と同様)。

## 再現手順

```bash
uv run baibai-loop-ledger selection-ablation \
  --candidates-root .cache/replay/candidates \
  --out .cache/replay/selection-ablation-202605.yaml
```
