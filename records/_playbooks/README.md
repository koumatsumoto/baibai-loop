# records/_playbooks/

Baibai-Loop で運用中の playbook 集合。各 playbook は `docs/components/research.md` の
`playbook` front matter で参照される。docs 上の contract は
`docs/components/playbooks.md` を参照する。

## 運用中の playbook

| playbook | signal lane | status | 本体 |
| --- | --- | --- | --- |
| Valuation Reversion | `valuation-reversion` | active | [`valuation-reversion.md`](./valuation-reversion.md) |
| Cash-Rich Asset Discount | `cash-rich-asset-discount` | active | [`cash-rich-asset-discount.md`](./cash-rich-asset-discount.md) |
| Cashflow Yield Discount | `cashflow-yield-discount` | active | [`cashflow-yield-discount.md`](./cashflow-yield-discount.md) |
| Sales Discount Growth | `sales-discount-growth` | active | [`sales-discount-growth.md`](./sales-discount-growth.md) |

## 位置付け

- `records/_playbooks/` は **運用資産**。ドキュメント（`docs/`）ではなく、実際に研究判定で参照される active rule
- Playbook 名に version suffix は付けない
- Research packet は主 thesis として単一 `playbook` を選び、複数 signal hit は `supporting_signals` に残す
- Playbook 改訂は月次 retro（[`../docs/components/reviews.md`](/docs/components/reviews.md)）の判断基準に従う
- サンプル数 10 件未満なら据え置きを許容

## 参考

- [`../docs/screening/principles.md`](/docs/screening/principles.md): スクリーニング原則
- [`../docs/components/playbooks.md`](/docs/components/playbooks.md): playbook contract
- [`../docs/components/research.md`](/docs/components/research.md): research 側の playbook 参照
- [`../docs/templates/playbook.md`](/docs/templates/playbook.md): 新規 playbook template
