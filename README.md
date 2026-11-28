# Baibai Loop

日本の普通株から、その時点の価格で買う価値のある銘柄を見つける。日次処理は原本から観測と比較材料を作り、依頼を受けたAIが調査対象を選び、割安の理由を検証して売買案にする。全銘柄の買い順位は作らない。採否と発注は人間が行う。

| やりたいこと | 入口 |
| --- | --- |
| コードを読む・変更先を探す | [アーキテクチャ：処理の流れ・実装・保存の正本](docs/architecture.md) |
| Windows（WSL）でセットアップ・ローカルテスト | [開発手順](docs/development.md)。通常のテストは鍵なし、実データが必要なときだけ設定を同期する。 |
| 日次・データ取得・公開・失敗からの再開 | [運用手順](jobs/OPERATIONS.md) |
| 企業を調べて提案する | [判断の基準](docs/doctrine.md) → [企業判断の開始・再開](.agents/skills/investment-judgment/SKILL.md) |
| 米国株を調べる・保存済み判断を読む | [米国株の調査CLIとUSD建て判断](docs/reference/us-research-mvp.md) |
| 市場判断を書く・人間の取引報告を記録する | [市場判断](.agents/skills/market-context-assessment/SKILL.md)・[台帳記録](.agents/skills/ledger-record/SKILL.md) |
| 保有・成績・発行済みの提案を表示する | [Webの表示と実装](web/README.md) |

リポジトリの変更時は[AGENTS.md](AGENTS.md)に従う。
