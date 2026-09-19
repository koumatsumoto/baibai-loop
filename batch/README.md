# Batch

requestに起因しないproduction orchestrationを所有します。

| 項目 | 内容 |
| --- | --- |
| 所有 | job順序、store転送・merge、workflow input validation、watchdog、summary、notification |
| 所有しない | 投資domain logic、Web projection logic |
| 入口 | GitHub Actionsとskillから使う`baibai-batch`、operator用`scripts/` |
| 依存境界 | batchはengineの`batch_api` / `read_api`だけを使う。web、tools、engine internalへ直接依存しない |
| 変更先 | jobは`src/baibai_batch/jobs`、転送・mergeは`storage`と`scripts`、通知は`observability`、validationは`validation` |
| 正本・test | [architecture](../docs/architecture.md)、[store authority](../docs/architecture.md#store-authority)、[OPERATIONS](./OPERATIONS.md)、[ops skill](../.agents/skills/ops-maintenance/SKILL.md)、[tests/batch](../tests/batch)、[tests/contracts](../tests/contracts) |

batchはauthority規則に従ってmachine storeとserving artifactを転送します。application DBをcloud copyで上書きしてはいけません。
