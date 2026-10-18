# Runtime stores

実行時storeの置き場です。SQLite本体とlake objectはGit管理しません。

| 配置 | 内容 |
| --- | --- |
| `application/` | 判断・確認済み取引事実・運用状態 |
| `market/` | market dataのruntime copyとstore-local data |
| `market_context/` | 経済sourceの原本・native record・取得情報（`indicators.sqlite`）。L2系列は保存せず読取時に計算 |
| `screening/` | Screening Run・候補集合・calibration |
| `lake/` | local mirror・staging・cache |

正本と削除可否は[architecture](../docs/architecture.md#store-authority)、取得・転送・移行・復旧は[batch運用](../batch/OPERATIONS.md)を参照してください。
