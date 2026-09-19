# Runtime stores

実行時storeの置き場です。SQLite本体とlake objectはGit管理しません。

| 配置 | 内容 |
| --- | --- |
| [application](./application/README.md) | 判断・確認済み取引事実・運用状態 |
| [market](./market/README.md) | market dataのruntime copyとstore-local data |
| [macro](./macro/README.md) | macro観測・vintage・取得情報 |
| [screening](./screening/README.md) | Screening Run・Review Set・calibration |
| `lake/` | local mirror・staging・cache |

正本と削除可否は[architecture](../docs/architecture.md#store-authority)、取得・転送・移行・復旧は[batch運用](../batch/OPERATIONS.md)を参照してください。
