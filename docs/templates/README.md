# docs/templates/

Baibai-Loop 各成分の記入テンプレート集。template をコピーして、各成分の最終 location（例: `brief/YYYY/MM/...`, `research/YYYY/MM/...`）にファイルを作成する。

## Template 一覧と最終 location

| Template | 最終 location | 対応 component |
| --- | --- | --- |
| `brief-world-weekly.md` | `brief/YYYY/MM/YYYY-MM-DD-world-weekly-*.md` | [`/docs/components/brief.md`](/docs/components/brief.md) |
| `brief-japan-monthly.md` | `brief/YYYY/MM/YYYY-MM-macro-monthly-*.md` | [`/docs/components/brief.md`](/docs/components/brief.md) |
| `brief-event.md` | `brief/YYYY/MM/YYYY-MM-DD-<kind>-*.md` | [`/docs/components/brief.md`](/docs/components/brief.md) |
| `view.md` | `view/YYYY/MM/view-YYYY-MM-DD-*.md` | [`/docs/components/view.md`](/docs/components/view.md) |
| `screened.md` | `screened/YYYY/MM/YYYY-MM-DD.md` | [`/docs/components/screened.md`](/docs/components/screened.md) |
| `research.md` | `research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md` | [`/docs/components/research.md`](/docs/components/research.md) |
| `trade.md` | `trades/YYYY/MM/YYYY-MM-DD-<ticker>.md` | [`/docs/components/trades.md`](/docs/components/trades.md) |
| `review.md` | `reviews/YYYY/MM/YYYY-MM-DD-<ticker>.md` | [`/docs/components/reviews.md`](/docs/components/reviews.md) |
| `retro-monthly.md` | `reviews/YYYY/retro-YYYYMM.md` | [`/docs/components/reviews.md`](/docs/components/reviews.md) |
| `playbook.md` | `playbooks/<playbook-slug>-v<n>.md` | [`/playbooks/`](/playbooks/) |

## リンク path 規約（重要）

各 template 内の markdown link（`[text](path)`）は、**リポジトリ root からの絶対 path**（`/docs/...`, `/playbooks/...` 等）で記述されている。GitHub はこの形式を標準サポートしており、**最終 location に関わらず常に正しく解決される**。

- 例: `research.md` template 内の `[/docs/components/research.md](/docs/components/research.md)` は、記入後の実ファイル `research/YYYY/MM/2026-04-25-7203-*.md` からでも、template ファイル自体（`docs/templates/research.md`）からでも、同じ target を指す
- **template を最終 location にコピーする際、相対 path の書き換え不要**
- 同一成分内の相互参照（例: brief から前週 brief へのリンク）は相対 path のまま（`../MM/YYYY-MM-DD-*.md`）で記述される。これは最終 location 内での相対参照のため

## 運用フロー

1. 作成したい成分に対応する template を `docs/templates/` からコピー
2. 最終 location に配置（上記表参照）
3. front matter と本文を埋める（AI 下書き可の項目は `ai-draft: true`）
4. 人間が最終確認（`ai-draft: false` に更新）
5. commit

## 参考

- [`/docs/philosophy.md`](/docs/philosophy.md): 思想
- [`/docs/architecture-v1.md`](/docs/architecture-v1.md): 全体構造
- [`/docs/components/`](/docs/components/): 各成分の運用仕様
- [`/docs/screening/`](/docs/screening/): スクリーニングサブシステム詳細
