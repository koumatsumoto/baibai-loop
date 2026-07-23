# Opportunity

## Trigger

買い候補、割安銘柄、指値、週次の機会確認を依頼されたときに使う。

## Canonical route

1. [`docs/operations/decision-cycle.md#opportunity-path`](../../../../docs/operations/decision-cycle.md#opportunity-path)を先頭から実行する。
2. screening固有の失敗は[`docs/workflow/screening.md`](../../../../docs/workflow/screening.md)、一次調査は[`docs/workflow/research.md`](../../../../docs/workflow/research.md)で解決する。
3. thesis authoring後の別role反証は[`independent-review.md`](./independent-review.md)、全lane統合後のcontent reviewは[`research-decision-report`](../../../../docs/reference/research-decision-report.md)へ渡す。

件数、比較field、除外理由、停止条件、command optionをこのreferenceから補わない。canonical routeとpublic `--help`が一致しなければ停止する。

## Output check

source selectionへ束縛したshortlistがapplication DBにpublishされ、operation sessionのcurrent payload、一次source、thesis/review ID、review済みcompact bundle、統合content review、review済みHTML path、proposal IDまたは`no actionable bargain / defer`が揃っていれば完了。AIはbroker操作へ進まない。
