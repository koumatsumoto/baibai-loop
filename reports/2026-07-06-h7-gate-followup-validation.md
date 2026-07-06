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

## 1. H7 判定（§0 固定後に計測・追記）

（計測後に記入）

## 2. Screen gate 選抜価値の判定（§0 固定後に計測・追記）

（計測後に記入）

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
