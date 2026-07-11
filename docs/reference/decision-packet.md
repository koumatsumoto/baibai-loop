---
title: "Decision packet reference"
summary: "5年総合リターン、永久損失、証拠状態、独立反証を持つ投資判断のcanonical contract。"
doc_type: reference
status: active
last_reviewed: 2026-07-11
---

# Decision packet

## Purpose and activation

Decision packetは、実購入候補の判断根拠を短い要約と再計算可能な詳細へ固定する。公開schemaは`records/_schemas/decision-packet.json`と`decision-review.json`、実装は`src/baibai_loop/thesis/decision_packet.py`である。canonical pathは`records/03-thesis/YYYY/MM/YYYY-MM-DD-<ticker>-decision.yaml`、reviewはpacketの`independent_review_ref`が指す隣接YAMLとする。

この契約はactive thesisの移行前でも独立に検証できる。移行完了までは既存thesis Markdownとvalidatorを運用し、canonical packetを推測で生成しない。移行ではactive thesisを新contractへ再生成し、旧`thesis_payoff`、`durability_gate`、`entry_preflight`とその互換分岐を同一変更で削除する。

## Four namespaces

| namespace | responsibility |
| --- | --- |
| `input_snapshot` | ticker、判断基準日、判断時price、主要財務・valuation、source provenanceを固定した最小fact snapshot |
| `derived` | formula ID、input fact IDs、version、as-of、unit、assumptionを持つ機械再計算値 |
| `estimates` | 判断時に観測した入口価格、要求5年CAGR、model version・仮定を持つ3年/5年bear/base/bull |
| `judgment` | buy/defer/rejectのAI initial proposal、提案時刻、確信度、永久損失結論、最強反対仮説、sizing、AI value captureの企業別評価 |

この4つはdata/judgment namespaceである。`permanent_loss_risks`はjudgmentを構成する軸別評価、`independent_review_ref`は別artifactのsecond-pass review envelopeへの参照、`human_evidence_override`はreview後の人間によるrisk受容としてtop-levelに置く。最終発注判断はexecution contractの別artifactであり、AI proposalへ混ぜない。

ScreeningのE[r]とFV anchorは決定論的でも事実ではなくestimateである。candidate出力は`origin: estimate`、model version、unit、assumptionsを併記し、decision packetへ採用する値はscenario modelのsourceとして固定する。

`judgment.ai_value_capture`は、AIを企業価値へ変換できるかを企業別に評価する分析層である。roleは`enabler / infrastructure / complement / adopter / disrupted`を使い、value captureの持続性、競争優位、収益化、株主への帰属をsource付きで記述する。`not_material`ならroleも判断weightも持たず、AIだけで採用・順位・投入額を決めない。`disrupted`を記す場合は、同じ根拠で`structural_decline`の永久損失評価へ接続する。

## Input snapshot and lineage

Candidate YAMLはlocalで再生成する探索成果物であり、decision packetから参照しない。採用した入力だけを`input_snapshot`へ値として固定する。これによりpacketはgitignoredなcandidate fileやSQLite fileの存在に依存せず、clean checkout単体で判断時点の入力を検証できる。

`input_snapshot`は`snapshot_version`と`producer_model_version`、ticker、as-of、source、factを持つ。判断時市場価格は`market_price`を正確に1件、valuationは`valuation_metric`を1件以上要求する。factはunit、as-of、`source_ids`を持ち、scenarioの起点となる利益・株数も同じsnapshotに置く。`estimates.market_price_fact_id`は判断時市場価格へjoinする。

外部sourceはHTTPS URLを持つ。local dataは消失し得るファイルパスを参照せず、`provider`、`dataset`、`retrieved_at`を持つ。`retrieved_at`はAI proposal時刻以前でなければならず、提案後に得た情報を判断時点snapshotへ遡及混入できない。市場価格は`observed_at`と`price_basis`（realtime / 調整済み終値 / 未調整終値）を持つ。すべてのsourceはpacketと同じtickerを明示し、source/fact/scenarioがpacket as-ofより未来の場合、source IDが解決しない場合、価格・valuationのtypeまたはunitが不正な場合は`incomplete`とする。canonical filenameの日付・tickerもsnapshotと一致させる。HTML、PR body、proposal Issueは説明・リンクにとどめ、判断入力の正本を複製しない。

## Scenario arithmetic

3年は予測可能性のsanity check、5年は主評価である。各horizonにbear/base/bullを1件ずつ要求する。

```text
terminal_earnings = starting_earnings * (1 + annual_earnings_growth)^years
terminal_shares = starting_shares * (1 + annual_share_count_change)^years
terminal_price = terminal_earnings / terminal_shares * terminal_valuation_multiple
total_return_CAGR = ((terminal_price + cumulative_dividend_per_share) / entry_price)^(1/years) - 1
```

`annual_share_count_change_pct`が正なら希薄化、負ならbuybackによる株数減少である。terminal priceは配当を含めず、累積配当をCAGR計算で1回だけ加える。入力が主張するterminal earnings、shares、price、CAGRを式から再計算し、不一致を`incomplete`にする。

## Execution pricing

`estimates.required_5y_base_cagr_pct`は、5年base scenarioに対してこの判断が要求する年率を明示する。`deep_discount_bps`を使う場合も同じpacketに保存し、後から別の値へ差し替えない。execution policyは表示用の上限価格や終値からの任意率を入力にせず、再計算した5年base terminal priceと累積配当から最大許容価格を求める。

```text
terminal_total_value = recalculated_5y_base_terminal_price
                     + cumulative_dividend_per_share_yen
max_acceptable_price = floor_to_tick(
  terminal_total_value / (1 + required_5y_base_cagr_pct / 100)^5
)
```

`baibai-loop-decision --execution-input <yaml> --ledger <canonical-ledger>` は、この上限、provider-neutral quote、canonical ledgerから導出したcash・concentration snapshot、数量・期限を使い、`buy_now / shallow_limit / deep_limit / defer` を比較する。CLIはinput YAMLのcashやexposureがledger snapshotと一致しない場合に停止する。proposalは人間承認前の判断材料であり、brokerを操作しない。評価時刻はexecution inputに固定し、quoteとledger snapshotはその時刻の5分以内でなければならない。CLIはさらに実行時刻との差が5分以内であり、注文期限がまだ到来していないことを確認するため、過去のreplay inputを現在の発注案として使えない。stale・historical・synthetic quote、または可視bid/askの全てが上限を超える場合は`defer`にする。spread、visible depth、ADV、注文後concentrationはwarningであり、根拠のないfill probabilityや価格予測を作らない。

未約定の測定では、期限内の日中安値がlimitにtouchした事実とbroker fillを区別する。touchは約定証明ではない。same-basisの観測値が揃う場合だけ、期限後5 sessionの価格とdecision時askを比較してmissed upsideを記録する。この値は指値policyを改善する観測値であり、strategy performanceや確定損益ではない。

## Permanent-loss axes

必須軸は`funding_liquidity / debt_repayment / cash_flow / dilution / customer_concentration / structural_decline / governance_accounting`である。各軸は`acceptable / adverse / unknown`、`verified / partially_verified / unverified`、source、as-ofを持つ。

軸欠落、source/as-of欠落、400日を超える根拠の陳腐化、`adverse`または`unknown`と総合結論の矛盾は`incomplete`である。一次情報不足または`adverse`自体はwarningにできるが、buy提案は`reduced` sizingと有効期限内の理由付きhuman overrideなしにreadyにならない。不完全な証拠で`high` confidenceは許さない。

## Independent second pass

`buy`にはpacketと別ファイルの`independent_review_ref`を必須とする。reviewはcore packet SHA-256、reviewer identity、review run IDを持ち、別roleが次だけを構造化して返す。

- 6 scenario CAGRの独立再計算
- 一次source照合状態
- 最強反対仮説
- 代替候補比較状態
- 初期提案の変更有無と理由

hash不一致、算術不一致、reviewが提案変更を要求した状態はreadyにしない。変更後の初期packetを再生成し、新しいhashへreviewを取り直す。

hashとrun metadataが保証するのはartifactの整合性であり、reviewerが本当に独立していることの暗号学的証明ではない。運用では初期packetを作ったagentと異なるagent/sessionへreview artifact作成を割り当てる。reviewは`judgment.proposed_at`以後に行う。human evidence overrideはreview後に別envelopeとして追加し、`approved_by: human`、decision reference、認識したrisk axes、承認/失効時刻、proposal hash、review ID、review artifact hashを持つ。現在評価時刻がexpiry内で、参照するproposal/reviewが完全一致する場合だけbuy gateに使える。

## Commands

```bash
uv run baibai-loop-decision records/03-thesis/YYYY/MM/YYYY-MM-DD-XXXX-decision.yaml
uv run baibai-loop-decision records/03-thesis/YYYY/MM/YYYY-MM-DD-XXXX-decision.yaml --execution-input execution-input.yaml --ledger records/04-position/portfolio-ledger.yaml
uv run baibai-loop-validation --target decision-packet
```
