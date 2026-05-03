# records/_playbooks/

Baibai-Loop で運用中の playbook 集合。各 playbook は `docs/components/research.md` の `playbook` front matter で参照される。

## 運用中の playbook

| playbook | type | status | 本体 |
| --- | --- | --- | --- |
| Valuation Mean-Reversion v1 | primary | active | [`valuation-mean-reversion-v1.md`](./valuation-mean-reversion-v1.md) |
| Valuation + Catalyst Confirmation v1 | supplementary | active | [`valuation-catalyst-confirmation-v1.md`](./valuation-catalyst-confirmation-v1.md) |

## 位置付け

- `records/_playbooks/` は **運用資産**。ドキュメント（`docs/`）ではなく、実際に研究判定で参照される active rule
- 新しい playbook を追加する場合は [`../docs/templates/playbook.md`](/docs/templates/playbook.md) をコピーして作成
- Playbook 改訂は月次 retro（[`../docs/components/reviews.md`](/docs/components/reviews.md)）の判断基準に従う
- サンプル数 10 件未満なら v1 据え置きを許容（#7 から継承）

## Future work

- Rerating Book 向け playbook（1〜6 か月保有）: v1 スコープ外、将来検討
- 追加の supplementary playbook: v1 運用後に retro feedback で判断

## 参考

- [`../docs/screening/principles.md`](/docs/screening/principles.md): スクリーニング原則
- [`../docs/components/research.md`](/docs/components/research.md): research 側の playbook 参照
- [`../docs/templates/playbook.md`](/docs/templates/playbook.md): 新規 playbook template
