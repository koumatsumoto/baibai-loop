# Baibai Loop

Baibai Loopは、一人で日本株を長期運用するための意思決定基盤です。AIが市場観測、候補抽出、一次情報調査、投資・保有判断の提案までを担い、人間が最終判断とbroker操作を行います。自動売買システムではありません。

目的・判断原則・AIと人間の責任境界は[`docs/doctrine.md`](./docs/doctrine.md)、repository構造・store・CLIの契約は[`docs/architecture.md`](./docs/architecture.md)を正本とします。

## 最初の入口

| やりたいこと | 入口 |
| --- | --- |
| 買い候補を探す | skill [`research-triage`](./.agents/skills/research-triage/SKILL.md)。人間がResearch Setを選んだ後は[`research`](./.agents/skills/research/SKILL.md) |
| 注文結果を記録する | skill [`ledger-record`](./.agents/skills/ledger-record/SKILL.md) |
| 保有銘柄を見直す | skill [`position-review`](./.agents/skills/position-review/SKILL.md) |
| Macro Contextを書く | skill [`macro-context`](./.agents/skills/macro-context/SKILL.md) |
| batch・storeを運用する | skill [`ops-maintenance`](./.agents/skills/ops-maintenance/SKILL.md) |
| screening・FV・E[r]の方法を改善する | [`estimate-calibration.md`](./docs/reference/estimate-calibration.md)に従うissue → PR |
| 開発・AI作業を始める | [`AGENTS.md`](./AGENTS.md) |
| 文書から正本を探す | [`docs/README.md`](./docs/README.md) |

候補なし、価格超過、一次情報不足による見送りは正常な結果です。AIは人間が報告していない注文状態を推定せず、ledgerを更新しません。

## 読み取り専用UI

frontendをbuildしてlocal UIを起動します。

```bash
cd web/frontend
npm run build
cd ../..
uv run baibai-web serve
```

`http://127.0.0.1:8712`を開きます。UIとAPIはapplication DBと各storeを読み取り専用で参照し、正本を更新しません。

## Repository map

| path | 役割 |
| --- | --- |
| `engine/` | domain処理、application service、正本への書き込み、read API |
| `web/` | 読み取り専用backend、frontend、edge、Web contract |
| `batch/` | 定期・offline処理、store転送、障害対応 |
| `method/` | Git管理のproduction methodology |
| `stores/` | application DBと実行時store |
| `reports/` | historical evidenceとpublished artifact |
| `docs/` | doctrine、governance、reference、運用入口 |
| `.agents/skills/` | repository-local skillの正本 |
| `tools/` | 開発・検証tool |

詳細な依存関係と配置先は[`docs/architecture.md#repository-map`](./docs/architecture.md#repository-map)および各moduleのREADMEを参照してください。

## 開発入口

Python 3.14と`uv`を使用します。変更前に[`AGENTS.md`](./AGENTS.md)を読み、push前に[`python-foundation.md` §9](./docs/reference/python-foundation.md#9-ci-and-local-parity)のfull local gateをCIと同じcommandで通します。

代表的なlocal subsetは次のとおりです。

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
uv run pytest
```

public CLIは`baibai-engine`、`baibai-web`、repository内部の`baibai-batch`です。domain・subcommand・optionは各`--help`を正本とします。

開発作業は[GitHub Issues](https://github.com/koumatsumoto/baibai-loop/issues)、運用taskはapplication DBの`baibai-engine task`で管理します。
