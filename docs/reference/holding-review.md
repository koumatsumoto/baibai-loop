---
title: "Holding review reference"
summary: "保有・売却を thesis health と税引後の代替機会費用で hold/add/reduce/exit へ落とす契約と算術。FV 到達は review trigger、価格下落単独は売却理由にしない。"
doc_type: reference
status: active
last_reviewed: 2026-07-12
related_docs:
  - "../doctrine.md"
  - "../portfolio-management.md"
  - "../workflow/position.md"
  - "./decision-packet.md"
  - "./portfolio-ledger.md"
---

# Holding review

## Purpose and activation

Holding review は、保有 1 件の売買判断を **thesis health** と **税引後の代替機会費用** から `hold / add / reduce / exit` へ落とす。公開 schema は `records/_schemas/holding-review.json`、実装は `src/baibai_loop/position/holding_review.py`。draft は read-only の判断材料であり、最終判断と broker 操作は人間が行う。

売却の主因は **thesis break（事業毀損）** で、これは優先売却候補になる。**フェアバリュー到達は review trigger であって自動の全売りではない**。**価格下落そのものは売却理由にしない**。

holding reviewは`ledger`、holding decision packet、候補packetを`ref + sha256`で必須参照する。review scalarはsource artifactと切り離して信頼しない。

`holding-review-build`はholding packetの隣接`independent_review_ref`をpath-confinedに読み、packet/review readinessとhashを確認してからledgerと結合する。load-bearing scalarはsourceから生成し、運用担当が手入力で変更しない。

## Inputs

| block | responsibility |
| --- | --- |
| `thesis_health` | invalidation 状態、永久損失 7 軸（decision packet と同一）、証拠鮮度、現値起点の 5 年期待総合リターン |
| `valuation_review` | 現値・FV・`current_price_yen >= fair_value_yen` から再計算した review trigger |
| `replacement_comparison` | 現保有と候補の 5 年期待総合リターン、確定/推定の exit 税、機会費用 edge |
| `add_context` | 押し目買増しの現値・最大許容価格・available cash・concentration 判定（任意） |
| `sources` | ledgerとcurrent/candidate decision packetのimmutable ref/hash。validatorはhash driftをrejectする |

`thesis_health.permanent_loss_axes` は `funding_liquidity / debt_repayment / cash_flow / dilution / customer_concentration / structural_decline / governance_accounting` の 7 軸を各 1 回ちょうど持つ。1 つでも欠けると review は `incomplete` になる。`permanent_loss_conclusion` は **verified な adverse 軸**があるとき `elevated`、partially verified / unverified な adverse を含むとき `unknown`、それ以外は `acceptable` とする。`elevated` だけが全株 exit の条件であり、未確認の懸念で税負担を伴う全株売却を断定しない。

## After-tax replacement arithmetic

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

## Decision table

価格下落単独は exit の理由にしない。最終判定は人間。

| 条件 | action |
| --- | --- |
| `invalidation_status: broken` または `permanent_loss_conclusion: elevated` | `exit`（優先売却候補） |
| `invalidation_status: at_risk` または concentration 超過 | `reduce`（毀損の進行または集中の解消を優先） |
| 代替候補が税引後で現保有を上回る（`replacement_edge > 0`、税 known） | `reduce`（thesis 健全なら全売りせず原資を回す） |
| thesis intact ＋ 現値 < 最大許容価格 ＋ available cash ＋ concentration 余地 | `add`（押し目買増し） |
| 上記いずれにも当たらない（FV 到達で勝る乗換先なし／含み損のみ 等） | `hold` |

`current_5y_estimate: unresolved` の holding は replacement comparison と add context を持てず、`reduce` / `add` を推定値から発火させない。未確認の永久損失軸（`permanent_loss_conclusion: unknown`）も買い増しを許可しない。記録した `action` は入力から再計算した action と一致しなければならない。不一致は error にし、draft が自分の入力と矛盾しないことを保証する。

## Commands

```bash
uv run baibai-loop-position market-price-draft --root . --ledger records/04-position/portfolio-ledger.yaml --sqlite data/screening/market.sqlite --asof ASOF_DATE --out .cache/position/ASOF_DATE-market-price-ledger.yaml
uv run baibai-loop-opportunity holding-prepare --asof ASOF_DATE --ledger records/04-position/portfolio-ledger.yaml --ticker XXXX --workspace .cache/opportunity/ASOF_DATE/holding-XXXX
uv run baibai-loop-opportunity packet-scaffold --workspace .cache/opportunity/ASOF_DATE/holding-XXXX --ticker XXXX --sqlite-path data/screening/market.sqlite --target-session NEXT_SESSION_DATE
uv run baibai-loop-position holding-review-build --packet records/03-thesis/YYYY/MM/ASOF_DATE-XXXX-decision.yaml --ledger records/04-position/portfolio-ledger.yaml --position-id POSITION_ID --out .cache/holding-review/ASOF_DATE-XXXX-attempt-N-review.yaml
uv run baibai-loop-position holding-review --root . --input .cache/holding-review/ASOF_DATE-XXXX-attempt-N-review.yaml
uv run baibai-loop-validation --target holding-review
```

`ASOF_DATE`は価格draftの最新完全営業日、`NEXT_SESSION_DATE`はその次の取引sessionである。`market-price-draft`は全open holdingの`ASOF_DATE` raw closeを同じcalendar dateで揃え、canonical ledgerを直接変更しない。人間がdraftをcanonicalへ反映した後、`holding-prepare`がledger hashに束縛した1銘柄固定workspaceを作り、holding market-price observationの日付が`--asof`と異なれば停止する。`packet-scaffold`も解決したraw close日がworkspace `as_of`と異なれば停止する。buildはpacket/review missing、hash drift、packetとholding market-price observationの日付不一致、ledgerにopen holdingなし、raw/unadjusted price basis不一致、source path escapeで停止する。ledgerの非価格eventはmarket closeより新しくてよい。draft生成後は`holding-review --root . --input`がsourceからscalarを再構築して照合する。人間が確認したdraftだけをcanonicalへcopyし、その後にvalidationを通す。完全な手順は[`../operations/decision-cycle.md#earnings-and-material-event-path`](../operations/decision-cycle.md#earnings-and-material-event-path)を正本とする。
