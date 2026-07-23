# Result and Holding

## Trigger routing

- 人間から`open / filled / cancelled / expired`を受け取ったら[`docs/operations/decision-cycle.md#human-result-path`](../../../../docs/operations/decision-cycle.md#human-result-path)を実行する。
- 決算・material event後の保有判断は[`docs/operations/decision-cycle.md#earnings-and-material-event-path`](../../../../docs/operations/decision-cycle.md#earnings-and-material-event-path)を実行する。
- 年次比較は[`docs/operations/decision-cycle.md#annual-outcome-path`](../../../../docs/operations/decision-cycle.md#annual-outcome-path)を実行する。

required field、event意味、holding action、失敗条件をこのreferenceから補わない。position固有の意味は[`docs/workflow/position.md`](../../../../docs/workflow/position.md)、artifact contractは対応referenceを読む。

## Output check

人間報告前はledger無変更。報告後はcurrent DBへ束縛したvalidated draftを作り、人間確認後の`apply-draft --confirmed`だけでcanonical eventを追加する。保有見直しではcurrent thesis revisionとledgerから再構築したreviewを人間確認後にpublishする。年次outcomeはresolved結果だけをDBへpublishし、`unresolved`は不足理由を確認して正本へ保存せず再実行する。各canonical IDと結果をoperation sessionへ参照すれば完了する。
