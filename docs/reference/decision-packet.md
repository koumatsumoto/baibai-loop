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
| `observed` | ticker、判断基準日、source、判断に使う最小fact snapshot |
| `derived` | formula ID、input fact IDs、version、as-of、unit、assumptionを持つ機械再計算値 |
| `estimates` | 判断上限または市場観測として種別を明示した入口価格と、model version・仮定を持つ3年/5年bear/base/bull |
| `judgment` | buy/defer/rejectのAI initial proposal、提案時刻、確信度、永久損失結論、最強反対仮説、sizing |

この4つはdata/judgment namespaceである。`permanent_loss_risks`はjudgmentを構成する軸別評価、`independent_review_ref`は別artifactのsecond-pass review envelopeへの参照、`human_evidence_override`はreview後の人間によるrisk受容としてtop-levelに置く。最終発注判断はexecution contractの別artifactであり、AI proposalへ混ぜない。

ScreeningのE[r]とFV anchorは決定論的でも事実ではなくestimateである。candidate出力は`origin: estimate`、model version、unit、assumptionsを併記し、decision packetへ採用する値はscenario modelのsourceとして固定する。

## Scenario arithmetic

3年は予測可能性のsanity check、5年は主評価である。各horizonにbear/base/bullを1件ずつ要求する。

```text
terminal_earnings = starting_earnings * (1 + annual_earnings_growth)^years
terminal_shares = starting_shares * (1 + annual_share_count_change)^years
terminal_price = terminal_earnings / terminal_shares * terminal_valuation_multiple
total_return_CAGR = ((terminal_price + cumulative_dividend_per_share) / entry_price)^(1/years) - 1
```

`annual_share_count_change_pct`が正なら希薄化、負ならbuybackによる株数減少である。terminal priceは配当を含めず、累積配当をCAGR計算で1回だけ加える。入力が主張するterminal earnings、shares、price、CAGRを式から再計算し、不一致を`incomplete`にする。

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
uv run baibai-loop-validation --target decision-packet
```
