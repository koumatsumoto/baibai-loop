# Macro World Model Stage A の判定：撤収

判定日 2026-08-18。判定の正本は本ファイルと #969、実行は #969 の PR。

## 判定

**Stage B へ昇格しない。pilot tooling と skill を撤収し、確定した知見だけを現行 skill と anti-pattern へ畳み込む。**

preregistration の昇格 5 基準は、cycle 3 を実行せずに 1（hard fail 0）が満たされないことが確定した時点で不成立である。cycle 3 と paired OP3 A/B は実行しない。

## cycle 3 を実行しない理由

**予算ではなく、実行しても採否が変わらないため。**

1. **typed lineage gate は独立レビューを置き換えない。** pilot の SKILL 自身が「schema pass は prose の semantic pass ではないので、独立 reviewer は evidence → state → hypothesis / edge → one-page → v4 / connection を縦に読み、宣言と実際の文言が一致することを別に確認する」と規定していた。gate はレビューの上に乗るものであり、gate を採っても同じレビュー費用が残る。
2. **v2 gate は author の自己申告を入力にする pure gate である。** cycle 2 の最重要欠陥は、`evidence-packs.yaml` / `states.yaml` が「当局介入か市場要因か一次情報で未確認」と書く一方、`one-page.md` と公開 `final-v4.yaml` が介入を発生済み fact として断定した点だった。この author は `source_qualifier` も fact として申告するため、qualifier-weakening 検査は通る。
3. **decision delta は 0 件。** cycle 1 / 2 の publish 後、world model 起因と確認できる completed OP3 の決定変更は観測されていない。
4. **paired OP3 A/B は設計上ほぼ確実に hygiene-only 分岐へ落ちる。** n=1 で両側を同じモデルが書き、A 側にも共通 hygiene を入れる契約であり、blind preference は usability gate へ降格済みだった。

## v2 honesty contract の検証状況

cycle 1 / 2 は frozen digest で version 1 に固定される設計だったため、`#943` で実装した v2 の lineage / qualifier / unit gate は実データを 1 度も通っていない。撤収時点の validator 出力は両 cycle に対して次を返す。

```
{"status": "ok", "projection_schema_version": 1, "workspace_schema_version": 1,
 "lineage_enforced": false, "material_claim_count": 0, ...}
```

## 畳み込んだ知見

| 知見 | 畳み込み先 |
| --- | --- |
| 上流で「未確認」と書いた事象を下流で fact として断定しない | `docs/anti-patterns.md` AP-01 の症状・根本原因・チェックリスト／`macro-context` skill の独立レビュー必須反証 1 |
| real / nominal・stock / flow・水準 / 変化・観測 / 期待の混同 | `docs/anti-patterns.md` AP-12 の症状・根本原因・チェックリスト／同レビュー必須反証 2 |
| percentile の実効窓（3 年 vs 10 年）を解釈へ反映する | 同レビュー必須反証 3 |
| 前回 head から消滅・demote した force / judgment を名指しする | 同レビュー必須反証 4 |
| `usd_jpy` / `jp.10y` の機械監視条件を standing exposure として常置する | 同レビュー必須反証 5 |
| 前回レポートの本文は結論確定後に開く（prior-blind の順序） | `macro-context` skill 手順 1 / 7 |
| 取得失敗の多くは host でなく fetch tool の遮断 | `docs/reference/data-sources.md` §取得失敗の切り分け |

## 撤収した資産

- `tools/experiments/macro_world_model/`（builder / renderer / validator）
- `tests/tools/test_macro_world_model.py`
- `.agents/skills/macro-world-model/` と `.claude/skills/macro-world-model`
- `tools/quality/drift/check_skill_inventory.py` の `EXPECTED` から除き、`OLD` へ移して再侵入を拒否する

## 残置する資産

本ディレクトリの `preregistration.md`・`baseline-audit.md`・`errata.md`・`cycles/2026-08-07`・`cycles/2026-08-12` は履歴として残す。frozen artifact は書き換えない。cycle 1 / 2 が publish した macro context revision（`macro-context-2026-08-07-labor-capex-divergence`・`macro-context-2026-08-12-yen-retracement-real-rate-squeeze`）は immutable であり、scorecard の採点対象として通常どおり残る。

## 撤収後の初回運転で 1 度だけ測ること

pass 別 wall-clock の記録規約は pilot skill にしか無く、撤収で消える。常設の記録義務は置かないが、**撤収の効果を判定するために初回運転だけ**次を本ディレクトリへ `cycles/2026-08-12/cost.yaml` と同形で 1 ファイル残す。

- `macro-context` skill 手順 0〜13 の pass 別 wall-clock（`docs/reference/data-sources.md` §取得失敗の切り分けが手順 3 の探索を減らしたかを、cycle 2 の evidence-packs 21 分と比べる）
- 手順 7 を開始した時刻（結論確定が先だったことを示す）
- 手順 11 の独立レビューが出した指摘の件数と内容

これで #969 の採否基準 T4 / T5 / T2 を判定できる。判定後は追加の計測義務を残さない。

## 未解決のまま残す事実確認

次に macro context を書き直すときの独立レビューが同じ検査を行う。撤収の判断は変えない。

- 財務省の月次公表による 2026-07-28〜08-03 の為替介入実績
- `jp.real_wage_index` の deflator（持家の帰属家賃を除く総合）と `jp.cpi.core_yoy` の一致可否
