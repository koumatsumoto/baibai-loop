---
title: "Position Review reference"
summary: "保有・売却を thesis health と税引後の代替機会費用で hold/add/reduce/exit へ落とす契約と算術。FV 到達は review trigger、価格下落単独は売却理由にしない。"
doc_type: reference
status: active
related_docs:
  - "../doctrine.md"
  - "../portfolio-management.md"
  - "./portfolio-ledger.md"
  - "./thesis.md"
---

# Position Review

<a id="purpose-and-activation"></a>

## 目的と適用

Position Review は、保有 1 件の売買判断を **thesis health** と **税引後の代替機会費用** から `hold / add / reduce / exit` へ落とす。機械契約は `engine/src/baibai_engine/position/position_review.py`、canonical revisionはapplication DBの`position_review_id`で識別する。draft は read-only の判断材料であり、最終判断と broker 操作は人間が行う。

売却の主因は **thesis break（事業毀損）** で、これは優先売却候補になる。**フェアバリュー到達は review trigger であって自動の全売りではない**。**価格下落そのものは売却理由にしない**。

Position ReviewはDB ledger、holding thesis、候補thesisをimmutable IDで必須参照する。review scalarはsource entityと切り離して信頼しない。

`position-review-build`はthesis IDからthesis/review readinessを確認してDB ledgerと結合する。load-bearing scalarはsourceから生成し、運用担当が手入力で変更しない。

<a id="inputs"></a>

## 入力

| block | responsibility |
| --- | --- |
| `thesis_health` | invalidation 状態、永久損失 7 軸（thesis と同一）、証拠鮮度、現値起点の 5 年期待総合リターン |
| `valuation_review` | 現値・FV・`current_price_yen >= fair_value_yen` から再計算した review trigger |
| `replacement_comparison` | 現保有と候補の 5 年期待総合リターン、確定/推定の exit 税、機会費用 edge |
| `add_context` | 押し目買増しの現値・最大許容価格・available cash・concentration 判定（任意） |
| `sources` | ledgerとcurrent/candidate thesisのimmutable ID。publish serviceはrevision driftをrejectする |

`thesis_health.permanent_loss_axes` は `funding_liquidity / debt_repayment / cash_flow / dilution / customer_concentration / structural_decline / governance_accounting` の 7 軸を各 1 回ちょうど持つ。1 つでも欠けると review は `incomplete` になる。`permanent_loss_conclusion` は **verified な adverse 軸**があるとき `elevated`、partially verified / unverified な adverse を含むとき `unknown`、それ以外は `acceptable` とする。`elevated` だけが全株 exit の条件であり、未確認の懸念で税負担を伴う全株売却を断定しない。

<a id="after-tax-replacement-arithmetic"></a>

## 税引後の代替算術

税は口座税制 engine を作らず、確定 cash flow（ledger `tax_confirmed`）と設定実効税率 estimate（ledger `estimated_exit_tax_rate_bps` + `estimated_exit_tax_basis: ledger_fifo_gross_unrealized_gain`）を分離したまま扱う。

```text
gain            = max(0, hold.market_value_yen - hold.deployed_cost_yen)
exit_tax_yen    = tax_yen                          # tax_basis: confirmed
                = gain * rate_bps // 10000          # tax_basis: estimated
                = unknown                           # tax_basis: unknown
redeployable    = hold.market_value_yen - exit_tax_yen
hold_terminal   = hold.market_value_yen * (1 + hold_forward_5y_cagr/100)^5
switch_terminal = redeployable          * (1 + candidate_forward_5y_cagr/100)^5
replacement_edge = switch_terminal - hold_terminal
```

`tax_basis: unknown`（NISA・損益通算で確定不能）のときは単一の verdict を出さず、感応度を示す：乗換が有利になる **breakeven 税率**（`replacement_edge = 0` となる税率）と、**税ゼロ時の edge** を出す。unknown の edge は `null` にし、reduce / exit を機械的に発火させない。

<a id="decision-table"></a>

## Actionの決定

価格下落単独は exit の理由にしない。最終判定は人間。

| 条件 | action |
| --- | --- |
| `invalidation_status: broken` または `permanent_loss_conclusion: elevated` | `exit`（優先売却候補） |
| `invalidation_status: at_risk` または concentration 超過 | `reduce`（毀損の進行または集中の解消を優先） |
| 代替候補が税引後で現保有を上回る（`replacement_edge > 0`、税 known） | `reduce`（thesis 健全なら全売りせず原資を回す） |
| thesis intact ＋ 現値 < 最大許容価格 ＋ available cash ＋ concentration 余地 | `add`（押し目買増し） |
| 上記いずれにも当たらない（FV 到達で勝る乗換先なし／含み損のみ 等） | `hold` |

`current_5y_estimate: unresolved` の holding は replacement comparison と add context を持てず、`reduce` / `add` を推定値から発火させない。未確認の永久損失軸（`permanent_loss_conclusion: unknown`）も買い増しを許可しない。記録した `action` は入力から再計算した action と一致しなければならない。不一致は error にし、draft が自分の入力と矛盾しないことを保証する。

<a id="commands"></a>

## Command

```bash
uv run baibai-engine position market-price-draft --db stores/application/baibai.sqlite --sqlite stores/market/market.sqlite --asof ASOF_DATE --out /tmp/market-price-draft.yaml
uv run baibai-engine position apply-draft /tmp/market-price-draft.yaml --db stores/application/baibai.sqlite --confirmed
uv run baibai-engine research position-prepare --db stores/application/baibai.sqlite --asof ASOF_DATE --ticker XXXX --workspace .cache/research/ASOF_DATE/position-XXXX
uv run baibai-engine research thesis-scaffold --workspace .cache/research/ASOF_DATE/position-XXXX --db stores/application/baibai.sqlite --ticker XXXX --sqlite-path stores/market/market.sqlite --target-session NEXT_SESSION_DATE
uv run baibai-engine research review-scaffold --workspace .cache/research/ASOF_DATE/position-XXXX --db stores/application/baibai.sqlite --ticker XXXX
uv run baibai-engine research promote --workspace .cache/research/ASOF_DATE/position-XXXX --db stores/application/baibai.sqlite --ticker XXXX
uv run baibai-engine position position-review-build --db stores/application/baibai.sqlite --thesis-id THESIS_ID --position-id POSITION_ID --out /tmp/position-review.yaml
uv run baibai-engine position position-review --db stores/application/baibai.sqlite --input /tmp/position-review.yaml
uv run baibai-engine position position-review publish /tmp/position-review.yaml --db stores/application/baibai.sqlite --thesis-id THESIS_ID
```

### 判断基準日とworkspaceの束縛

`ASOF_DATE`は価格draftの最新完全営業日、`NEXT_SESSION_DATE`はその次の取引sessionである。`market-price-draft`は、全open holdingの`ASOF_DATE` raw closeを同じcalendar dateで揃え、canonical ledgerを直接変更しない。

人間がdraftをcanonicalへ反映した後、`position-prepare`はledger entityとappend headに束縛した1銘柄固定workspaceを作る。holding market-price observationの日付が`--asof`と異なる場合は停止する。ledgerの非価格eventはmarket closeより新しくてよい。

### Gateと復旧

workspaceの各gate（`status`、`thesis-scaffold`、`review-scaffold`、`promote`）は、holdingと観測日の前提をcanonical ledgerに対して再証明する。manifestは書き換え可能なため、`purpose: position_review`の宣言だけで前提を飛ばせないようにする。

append headはledger eventだけを数え、market priceは別tableへ入れ替わる。price draftを再適用すると、headが動かないまま観測日だけが変わり得る。このworkspaceはbuildが拒否するため既に使えず、各gateはresearchを書く前に停止を知らせる。gateが再証明するのは観測日であり、同日のまま価格値だけを訂正した場合はbuildだけが停止できる。

停止したworkspaceは、新しい観測日で`position-prepare --asof <観測日> --force`を実行し、続けて`thesis-scaffold --force`でticker draftを作り直す。market priceは前進しか許さないため、古いas-ofへ戻してはいけない。作り直す前のthesis draftは、`status`が`thesis as_of ... does not match workspace as_of ...`として報告する。`thesis-scaffold`も、解決したraw close日がworkspaceの`as_of`と異なる場合は停止する。

### Buildとpublish

独立reviewをscaffoldして完成させ、`promote`が返す`THESIS_ID`をPosition Reviewへ渡す。buildは、thesisまたはreviewの欠落、revision drift、thesisとholding market-price observationの日付不一致、open holdingの欠落、raw/unadjusted price basis不一致で停止する。

draft生成後、`position-review --db ... --input`はcanonical DBからscalarとsource revisionを再構築して照合する。人間が確認したdraftだけをcanonical `thesis_id`へ束縛してpublishする。完全な手順はskill [`position-review`](../../.agents/skills/position-review/SKILL.md)が所有する。
