# Opportunity

## Trigger

買い候補、割安銘柄、指値、週次の機会確認を依頼されたときに使う。

## Canonical route

1. [`docs/operations/decision-cycle.md#opportunity-path`](../../../../docs/operations/decision-cycle.md#opportunity-path)を先頭から実行する。
2. screening固有の失敗は[`docs/workflow/screening.md`](../../../../docs/workflow/screening.md)、一次調査は[`docs/workflow/research.md`](../../../../docs/workflow/research.md)で解決する。
3. thesis authoring後の別role反証は[`independent-review.md`](./independent-review.md)、全lane統合後のcontent reviewは[`research-decision-report`](../../../../docs/reference/research-decision-report.md)へ渡す。

件数、比較field、除外理由、停止条件、command optionをこのreferenceから補わない。canonical routeとpublic `--help`が一致しなければ停止する。

## shortlist publish 前の機械的突合

`screening shortlist publish`はfieldの存在しか検査しない。draftを書き終えたら、publishの前に[選定の深度契約](../../../../docs/operations/decision-cycle.md#op3-depth-contract)の各項目をselected 1銘柄ずつ照合する。印象で「満たしているはず」とせず、箇条書きに対して機械的に突き合わせる。

1. `upside`から希望的前提を剥がしても期待値が正か（alpha込みで初めて成立するならそう書いてあるか）
2. `upside`の利益がピーク外挿でなく複数期の正常利益ベースか
3. `catalyst`が日付または特定可能なeventに結び付くか（datedなら`catalyst_date`が入っているか）
4. 深掘り〜想定保有初期にかかるdated event（決算・guidance・規制・macro contextのdated monitoring point）を消化したか
5. リスク調整後に現金保有へ勝るか。net cash・簿価を下値の床にしている銘柄で還元機構を確認したか
6. `data_quality_flags`が立つ銘柄で、そのflagが数値をどちら向きに歪めるかを書いたか
7. `rank`が機械E[r]降順から乖離する銘柄で、乖離の理由を書いたか
8. `macro`がconnectionのresearch優先度ヒント / sizing caution / estimate caveats / バーゲン地形のうち当該銘柄に該当するものを消化しているか（該当なしの判断を含む）

fail項目はdraftを直してから進む。突合を省いてpublishしない。

## Output check

source selectionへ束縛したshortlistがapplication DBにpublishされ、operation sessionのcurrent payload、一次source、thesis/review ID、review済みcompact bundle、統合content review、review済みHTML path、proposal IDまたは`no actionable bargain / defer`が揃っていれば完了。AIはbroker操作へ進まない。
