# Baibai Loop

日本個別株の割安な機会を探し、複数モデルの観測と企業・市場の情報を統合して価値と条件付きの見返りを評価し、根拠付きの売買提案へつなげるシステムです。最終裁定とbroker操作は人間が行います。

**まず[アーキテクチャ](docs/architecture.md)を読んでください。** 情報収集・機械分析・投資判断の責務と、[現在の解析モデル](docs/glossary.md#現在の解析モデル)と候補政策の違いを理解すると、責務を反映したフォルダから調べる場所・変更する場所を辿れます。

名称と意味の区別は[用語集](docs/glossary.md)にまとめています。

投資と開発の判断基準は[doctrine](docs/doctrine.md)、環境準備と検証は[development](docs/development.md)にあります。実操作は[Investment Judgment](.agents/skills/investment-judgment/SKILL.md)、[取引事実の記録](.agents/skills/ledger-record/SKILL.md)、[市場判断](.agents/skills/market-context-assessment/SKILL.md)、[運用手順](jobs/OPERATIONS.md)から進めます。

コードと文書を変更するときは[実装と文書の整理方針](docs/documentation.md)に従います。実装の所有者と理由をコードの近くへ集め、文書は全体像・索引・必要な運用への入口に絞ります。
