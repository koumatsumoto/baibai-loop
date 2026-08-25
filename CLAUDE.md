# Claude Code Instructions

このリポジトリで作業する際の手順は [`AGENTS.md`](./AGENTS.md) を読むこと。AI エージェント全般の規約はそこに集約する。

## 優先度ルール（明示）

- **優先はビジネス価値、次に保守性。** 価値は [`docs/doctrine.md#improvement-value-hierarchy`](./docs/doctrine.md#improvement-value-hierarchy) の T1 > T2 > T3 ≫ T4 で測り、規模・工数は棄却理由にしない（[`docs/doctrine.md#development-investment-policy`](./docs/doctrine.md#development-investment-policy)）。
- **価値の低い仕様・防御・監査・規則を足さない。** 検証や guard も同じ階層で測る。現在の判断の誤りをその場で防ぐもの（T1 / T2）だけを置き、監査・再現・lineage・将来の安全のためだけのもの（T4）は作らない。無人経路が止まってよいのは「必須入力が無い」「出力が壊れる」の 2 条件だけ（[`docs/architecture.md#failure-policy`](./docs/architecture.md#failure-policy)）。
- **足すより消す・統合する。** 機能・surface・規則は、増やす前に既存の削除と統合を先に考え、増やすなら [`AGENTS.md#効果と複雑性の均衡`](./AGENTS.md#効果と複雑性の均衡) で効果と複雑性を比べる。削除根拠は `rg` での参照有無と運用での使用実績で示す。後方互換・deprecation・migration shim・互換レイヤーは作らない — 1 人運用と git history が安全網。
- 残すもの: 実取引の order / fill、ledger correction、法務・規制・税務で必須の記録、schema migration log。
