# 事前登録: selection ranking の carry 偏重再評価 — reversion 順 vs E[r] 順（#480）

E[r] 合計降順の本番 ranking が carry（配当+自社株買い利回り）主導の銘柄を上位に出し、reversion（価格の割安解消）主導の dislocation 候補を沈める懸念（#480、運用テスト op-20260717-opportunity-1 で実測: audit pool 上位 20 中 12 銘柄が reversion 負〜ゼロ）を、improvement-loop の規律で検証する記録。構造的な背景: E[r] の reversion 成分は realization 0.10 × clip(upside, ±0.50) で年率 ±5% に制限される一方、carry 成分（配当利回り + clip(自社株買い, ±5%)）は 10% を超え得るため、合計順では carry が順位を支配できる。

計測基盤は #483 修正（snapshot 開始日以前の cohort を最古 snapshot へ fallback）後に `--force` 再構築した calibration store。

## 0. 採否基準（検証実行前に固定・本節を先に commit）

**判定の一般則**は #291 WU4 検証（`reports/2026-07-04-preregistered-ranking-validation.md`）と同じ: design（cohort asof ≤ 2024-06-30）/ confirm（> 2024-06-30）の時間分割で、両方同方向なら「支持」、片側のみ「不確定」、両側逆は「棄却」。有意性は主張しない（cohort 窓重複のため）。効果量・cohort 勝率・trap 率のみで判定する。primary horizon は 6m、1y は補助確認。

**盲検性の限定（正直な位置づけ）**: (a) `er_reversion_annual` の軸 rank IC が全窓で正であることは WU3/WU4 レポートで既知。未知なのは **reversion を順位付けに使った場合の top-N replay 超過**と **carry 軸単独の IC/decile**（本計測で初めて算出）。(b) 動機となった 2026-07-17 断面（carry 型が上位優占）は既知だが、これは現時点の並びであり forward return の情報を含まない。(c) 本 store は future_snapshot fallback による近似 universe（現存 membership）で、#291 計測時の「現在 master 固定」と実質同等の基盤。survivorship / delisting coverage は `not_assessed` のままで、絶対水準より view 間の相対比較を重視する。

**仮説と測定**（すべて pass_screen × E[r] 非 null 集合の再並べ替え。er_ranked と同一集合・同一手法）:

- **H-R1（reversion 主キー化）**: `er_reversion_annual` 降順 top-N（新 view `reversion_ranked_top{5,10}`）は `er_annual` 降順 top-N（既存 `er_ranked_top{5,10}`）より 6m forward excess が高い。
- **H-R2（blend key）**: cockpit 表示用 view score と同型の blend key = `er_reversion_annual + 0.5 × min(er_carry_annual, 0.15)`（品質 flag 減点は panel に無いため除外した近似）による新 view `view_score_ranked_top{5,10}` が er_ranked を上回る。
- **H-R3（carry 無効力）**: `er_carry_annual` の軸 rank IC（6m）は ≤ 0、または `er_reversion_annual` の IC を 0.05 以上下回る。carry-heavy E[r] は割安の証拠にならない（doctrine）の計測的裏づけ。

**本番 sort key 変更の採用 3 条件**（対象 view ごとに、すべて design/confirm 両方で充足した場合のみ採用）:

1. **効果量**: 対象 view の top-10 6m mean median excess が `er_ranked_top10` を **+2pt 以上**上回る
2. **方向整合**: 対象 view の top-5 も `er_ranked_top5` 以上（方向のみ・閾値なし）、かつ 1y でも top-10 が同方向
3. **trap 非悪化**: 対象 view top-10 の mean trap rate ≤ `er_ranked_top10` の trap rate

**採用の優先順位**: H-R1 と H-R2 の両方が 3 条件を通過した場合は top-10 6m 効果量の大きい方を採用する。H-R1 のみ → reversion 主キー（従キーに er_annual → 現行キー列を残す）。H-R2 のみ → blend 主キー。**どちらも不通過 → 本番 ranking は現状維持**（E[r] 降順）とし、carry の視認補正は cockpit の表示専用 view score（導入済み）に留め、#480 は計測記録をもって close する。H-R3 は単独では変更を駆動しない（解釈の補助のみ）。

**採用時の実装**: `selection/payload.py` の sort_key 主キーを対象 key へ変更（欠損は最後尾・従キーに現行 `er_annual` + playbook 順 + strength key を残す）。diversity cap・screen gate・E[r] モデル自体は変更しない（#480 対象外）。

## 3. 検証・再現

```bash
# store 再構築（#483 修正後・rules 2026-07-06）
uv run baibai-engine screening calibration-build --start 2022-09-01 --end 2026-06-30 --force
# design / confirm 分割の diagnostic 評価
uv run baibai-engine screening calibration-evaluate --start 2022-09-01 --end 2024-06-30 --out .cache/480-design.yaml
uv run baibai-engine screening calibration-evaluate --start 2024-07-01 --end 2026-06-30 --out .cache/480-confirm.yaml
```

- 事前登録の順序は git history が正本（§0 の commit → 計測 view の実装 → 計測 → §1 以降の追記）。
- 3y/5y は authority 契約で blocked（master snapshot 非 exact・survivorship not_assessed）のため、本判定は 6m/1y の diagnostic evidence で行い、production_decision run は使わない。
- レジーム注意: 計測窓全体がバリュー優位。判定結果は月次の再計測で追う。
