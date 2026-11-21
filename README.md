# Baibai Loop

日本の個別株について、買える一株の権利に対して価格が安い機会を探す。日次処理が原本から比較材料を作り、人間が調査を開始すると、AIが企業価値・価格・資金の使い道を検討して売買案を作る。採否と発注は人間が行う。

| やりたいこと | 入口 |
| --- | --- |
| コードを読む・変更先を探す | [アーキテクチャ：処理の流れ・実装・保存の正本](docs/architecture.md) |
| 手元で動かす・テストする | [開発手順](docs/development.md)。通常のテストに実データや認証は不要。 |
| 日次・データ取得・公開・失敗からの再開 | [運用手順](jobs/OPERATIONS.md) |
| 企業を調べて提案する | [判断の基準](docs/doctrine.md) → [企業判断の開始・再開](.agents/skills/investment-judgment/SKILL.md) |
| 市場判断を書く・人間の取引報告を記録する | [市場判断](.agents/skills/market-context-assessment/SKILL.md)・[台帳記録](.agents/skills/ledger-record/SKILL.md) |
| 保有・成績・発行済みの提案を表示する | [Webの表示と実装](web/README.md) |

リポジトリの変更時は[AGENTS.md](AGENTS.md)に従う。
