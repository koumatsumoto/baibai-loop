# docs/templates/

Baibai-Loop 各成分の記入テンプレート集。template をコピーして、各成分の最終 location（例: `records/01-macro-context/YYYY/MM/...`, `records/05-thesis/YYYY/MM/...`）にファイルを作成する。

## Template 一覧と最終 location

| Template | 最終 location | 対応 component |
| --- | --- | --- |
| `macro-context.yaml` | `records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-*.yaml` | [`/docs/components/macro-context.md`](/docs/components/macro-context.md) |
| `candidates.yaml` | `records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml` | [`/docs/components/candidates.md`](/docs/components/candidates.md) |
| `thesis.md` | `records/05-thesis/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md` | [`/docs/components/thesis.md`](/docs/components/thesis.md) |
| `position.md` | `records/06-position/YYYY/MM/YYYY-MM-DD-<ticker>.md` | [`/docs/components/position.md`](/docs/components/position.md) |
| `playbook.md` | `records/_playbooks/<playbook-slug>-v<n>.md` | [`/docs/components/playbooks.md`](/docs/components/playbooks.md) |

## リンク path 規約（重要）

各 template 内の markdown link（`[text](path)`）は、**リポジトリ root からの絶対 path**（`/docs/...`, `/records/_playbooks/...` 等）で記述されている。GitHub はこの形式を標準サポートしており、**最終 location に関わらず常に正しく解決される**。

- 例: `thesis.md` template 内の `[/docs/components/thesis.md](/docs/components/thesis.md)` は、記入後の実ファイル `records/05-thesis/YYYY/MM/2026-04-25-7203-*.md` からでも、template ファイル自体（`docs/templates/thesis.md`）からでも、同じ target を指す
- **template を最終 location にコピーする際、相対 path の書き換え不要**
- 同一成分内の相互参照は相対 path のまま記述してよい。これは最終 location 内での相対参照のため

## 運用フロー

1. 作成したい成分に対応する template を `docs/templates/` からコピー
2. 最終 location に配置（上記表参照）
3. YAML または front matter と本文を埋める
4. 人間が最終確認する
5. commit

## 参考

- [`/docs/philosophy.md`](/docs/philosophy.md): 思想
- [`/docs/architecture/system-overview.md`](/docs/architecture/system-overview.md): 全体構造
- [`/docs/components/`](/docs/components/): 各成分の運用仕様
- [`/docs/components/playbooks.md`](/docs/components/playbooks.md): playbook component
- [`/docs/screening/`](/docs/screening/): スクリーニングサブシステム詳細
