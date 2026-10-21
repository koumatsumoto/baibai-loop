# Reports and published artifacts

| 配置 | 内容 |
| --- | --- |
| `studies/YYYY-MM-DD-slug/` | 事前登録、測定結果、study固有の`artifacts/` |
| `operations/YYYY-MM-DD-slug/` | 外部設定を伴う操作のbefore・確認・after |
| `published/` | 明示的なconsumerが読む、小さなmachine-readable artifact |

`reports/`直下の日付付きreportと`studies/`・`operations/`内のdated evidenceは当時の記録であり、現在の文体や値に書き換えません。本文の「現行」、コマンド、保存要件、未完了事項は記録時点を指し、現在の操作指示や実装保持義務にはしません。訂正は原記録を残して新しい記録またはIssueで示します。`published`はconsumerと有効性検証を持つため、古い日付やファイル名だけで削除しません。

現在のmethodは[method](../method/README.md)、実行時の正本は[stores](../stores/README.md)へ置きます。studyからproductionを自動更新しません。
