# H7 rules variant と screen gate 選抜価値の後続検証（#301）

#291 プログラム WU4 で「不確定 / プログラム外」と記録した 2 点（issue #301）の検証記録。計測は [`../docs/operations/improvement-loop.md`](../docs/operations/improvement-loop.md) のサイクルに従う。

**着手条件からの逸脱（正直な記録）**: #301 は着手条件を「新ランキングの月次成績を 2–3 cohort 観測してから」としていたが、基盤改善プログラムの再開指示により観測を待たず着手する。design/confirm の事前登録判定は維持するため採否の誠実性は保たれるが、「新ランキングの実運用挙動を確認してから」という運用上の慎重さは放棄している。月次監視（top-5 逆転 #307）は継続する。

## 0. 採否基準（検証実行前に固定・本節を先に commit）

**判定の一般則**: design（cohort asof ≤ 2024-06-30）/ confirm（> 2024-06-30）の両方で基準充足のときだけ採用。片側のみは不確定、両側逆は棄却。有意性は主張しない（cohort 窓は重複）。効果量・cohort 勝率・trap 率のみで判定する。主計測は 6m、12m は補助（方向整合の確認のみ）。

**盲検性の限定**: er_population_top10 が er_ranked_top10 を design で上回る観察（+14.1% vs +8.0%、6m・2026-03 までの store）は WU4 レポートで公開済み・既知。price_change_60d 軸の D10 trap 最悪（26–38%）も baseline で既知。本検証の新規性は (a) H7 は「条件 B の 60 日下落要件を外した variant の pass 集合」という**未計測の構成**の計測、(b) gate 判定は 2026-06 まで延長した store での再確認、にある。

### H7（valuation-reversion 条件 B から 60 日下落要件を撤廃）の採用基準

variant rules = 現行 rules との差分は `price_change_60d_max` の実質無効化（999.0）のみ。判定指標は 6m mean median excess（対流動性母集団中央値）と mean trap rate。

1. **非劣性（撤廃の必要条件）**: variant の `er_ranked_top10` が現行の `er_ranked_top10` に対し、design/confirm 両窓で **−1.0pt 以上**（つまり 1pt を超えて劣化しない）。
2. **トラップ非悪化**: variant の `er_ranked_top10` mean trap rate が現行比 **+1.0pt 以下**の悪化に収まる（両窓）。
3. **本番形の確認**: variant の `recommended_rank_top10`（diversity 適用後）でも 1・2 と同方向（±1.0pt 基準を適用）。
4. **12m 補助**: variant − 現行の符号が 6m 判定と矛盾しない（両窓とも 6m と同符号、または差が ±0.5pt 内）。

採用時の含意: 計測既知の高トラップ軸（price_change_60d）への構造依存を、成績を害さずに外せるなら外す（条件の単純化）。1–4 をすべて満たせば撤廃を採用する。variant が両窓で **+1.0pt 以上**上回る場合は「改善」として記録する（採否はあくまで 1–4）。

### Screen gate 選抜価値（er_population vs er_ranked）の判定基準

現行 rules の store（2026-06 まで延長後）で判定する。

1. **gate 無価値の判定**: `er_population_top10`（screen gate なし・流動性母集団の E[r] 降順）が `er_ranked_top10`（gate 内）に対し design/confirm 両窓で **−0.5pt 以上**（劣化しない）かつ trap 差 **+2.0pt 以下**なら、「playbook screen gate は E[r] の上に選抜価値を足していない」と判定する。
2. **本 PR での扱い（事前固定）**: 判定が成立しても、universe 再設計（gate → 注記への格下げ）の実装は本 PR に含めない。select / candidates の安定契約変更を伴うため、判定結果と実装計画を added issue に登録して次サイクルで実施する。判定が不成立なら gate 維持を記録して #301 の該当項目を close する。

## 1. H7 判定（採用 — 4 条件すべて design/confirm 両方で充足）

計測: 現行 rules store（46 cohort・2026-06 まで）vs H7 variant store（`price_change_60d_max` 実質無効化のみの差分）。design = cohort ≤ 2024-06-30（22 cohort）/ confirm = > 2024-06-30（6m 18 cohort・12m 12 cohort）。

| §0 基準 | design 6m | confirm 6m | 判定 |
| --- | --- | --- | --- |
| ① 非劣性: variant er_ranked_top10 ≥ 現行 −1.0pt | +8.48% vs +8.04%（Δ+0.44pt） | +7.44% vs +6.56%（Δ+0.87pt） | ✓ |
| ② trap 非悪化 ≤ +1.0pt | 12.27% vs 12.27%（Δ0.00） | 10.56% vs 10.56%（Δ0.00） | ✓ |
| ③ 本番形 recommended_top10 同方向 | +7.27% vs +6.42%（Δ+0.86pt・trap 11.20% vs 12.12%） | +9.75% vs +9.47%（Δ+0.28pt・trap 同値） | ✓ |
| ④ 12m 補助（方向整合） | er_ranked_top10 Δ+1.21pt | Δ+1.29pt | ✓ |

- **採用**: 条件 B から 60 日下落要件を撤廃（条件 B = σギャップ × 悪化ゲートのみに単純化）。採用の主根拠は「計測既知の高トラップ軸（price_change_60d、D10 trap 26–38% で全軸最悪）への構造依存を、成績を害さずに外せる」こと。
- 付記 1（分布の開示）: confirm 6m の per-cohort 差分は 18 cohort 中 6 で非ゼロ・符号混在（−2.79〜+14.10pt）、mean +0.87pt。改善は 2025-08 cohort に集中しており、「変種が優越する」ではなく「非劣性での単純化」が正直な結論。
- 付記 2: top-5 は改善が相対的に大きい（er_ranked_top5 confirm +1.71%→+4.61%・trap 18.89%→17.78%、recommended_top5 confirm +3.20%→+4.69%）。#307（top-5 < top-10 逆転）の緩和に寄与する方向で、月次監視を継続する。
- 実装: `price_change_60d_max` field を削除し、`price_change_60d` は evidence hit の事実記録のみに残す（欠損は None）。最終 rules での store 再構築後の一致検証: 最終実装で store を全再構築（46 panel・rules_hash 統一）した design/confirm 評価は variant 計測と byte-identical（er_ranked / recommended_rank × top10 / top5 × 6m / 12m の全 32 セルで差 0.0000pt）。sentinel 無効化（999.0）と field 削除で pass 集合が完全一致するのは、較正 46 panel に「price_change_60d null × σギャップ充足」の該当が 0 件のため（null 除外の有無が結果に影響しない）。
- 運用テスト（asof 2026-07-01・run/select 新旧比較。旧側は撤廃前実装 + 旧 rules で実行）: 推奨 select 出力（emit 上限内の top-5）は集合・順位とも一致、新規参入・脱落なし。差分は候補 pool のみ（input 1625→1698・evidence filter 後 488→519）。新型ケース: 556A 犬猫生活（上場 69 日で price_change_60d null・condition_b_sigma_gap −1.21）が条件 B hit（撤廃前実装では null 除外）— top-5 圏外。candidates schema は null price_change_60d を許容し validation 0 error。

| rank | ticker | name | E[r] | playbook | price_change_60d | 新旧 |
| ---: | --- | --- | ---: | --- | ---: | --- |
| 1 | 4116 | 大日精化工業 | +18.39% | cashflow-yield-discount | −4.7% | 同順位 |
| 2 | 4008 | 住友精化 | +14.85% | cashflow-yield-discount | +6.7% | 同順位 |
| 3 | 8078 | 阪和興業 | +13.32% | cashflow-yield-discount | +6.2% | 同順位 |
| 4 | 6417 | SANKYO | +12.97% | cashflow-yield-discount | −19.8% | 同順位 |
| 5 | 5410 | 合同製鐵 | +12.07% | valuation-reversion | −30.6% | 同順位 |

## 2. Screen gate 選抜価値の判定（成立 — 実装は #309 へ）

現行 rules store（2026-06 まで延長・46 cohort）での er_population_top10（gate なし・流動性母集団の E[r] 降順）vs er_ranked_top10（gate 内）:

| 窓 | population | ranked | Δ | trap（pop / ranked） |
| --- | ---: | ---: | ---: | --- |
| design 6m | +14.11% | +8.04% | +6.07pt | 8.64% / 12.27% |
| confirm 6m | +6.69% | +6.56% | +0.13pt | 5.56% / 10.56% |
| design 12m | +30.09% | +19.15% | +10.94pt | 11.36% / 16.82% |
| confirm 12m | +14.58% | +13.88% | +0.70pt | 8.33% / 16.67% |

- §0 基準（両窓で −0.5pt 以上・trap +2.0pt 以内）で**「playbook screen gate は E[r] の上に選抜価値を足していない」判定が成立**（実際には design で大幅優位・trap は両窓で改善）。
- §0 の事前固定どおり、universe 再設計（gate → 注記格下げ）の実装は本 PR に含めず、判定と実装計画を issue #309 に登録した。#280（金融 lane gap）は #309 で自然解決見込み（金融の E[r] anchor 妥当性の較正確認込み）。

## 3. 検証・再現

```bash
# 現行 rules: 増分 build（2026-06 まで延長 + forward 再計算）
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-06-30
# H7 variant: 別 store に構築
uv run baibai-loop-screening calibration-build --rules-path .cache/h7-variant-rules.yaml \
  --start 2022-09-01 --end 2026-06-30 --calibration-dir data/screening/calibration-h7
# 評価（design/confirm × 現行/variant）
uv run baibai-loop-screening calibration-evaluate --horizon 6m --horizon 12m \
  --start 2022-09-01 --end 2024-06-30 --out .cache/h7-current-design.yaml
uv run baibai-loop-screening calibration-evaluate --horizon 6m --horizon 12m \
  --start 2024-07-01 --end 2026-06-30 --out .cache/h7-current-confirm.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-h7 \
  --horizon 6m --horizon 12m --start 2022-09-01 --end 2024-06-30 --out .cache/h7-variant-design.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-h7 \
  --horizon 6m --horizon 12m --start 2024-07-01 --end 2026-06-30 --out .cache/h7-variant-confirm.yaml
```

- 事前登録の順序は git history が正本（本 §0 の commit → 計測 → §1/§2 の追記）。
- レジーム注意: 計測窓はバリュー優位。判定は窓内の相対比較（variant vs 現行、population vs ranked）で、レジーム転換後の水準は #308 の監視対象。

## 4. 採用後の監視事項（月次サイクル §8 で追う）

- 条件 B 経由（非下落・σギャップ hit）の新規参入銘柄の実現品質（top-10 trap 率の推移）
- #307: top-5 < top-10 逆転の持続（H7 で緩和方向だが継続監視）
- #308: E[r] 実現率のレジーム依存（バリュー期 2.4x）
