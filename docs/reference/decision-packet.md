---
title: "Decision packet reference"
summary: "5年総合リターン、永久損失、証拠状態、独立反証を持つ投資判断のcanonical contract。"
doc_type: reference
status: active
last_reviewed: 2026-07-15
---

# Decision packet

## Purpose and activation

Decision packetは、実購入候補の判断根拠を短い要約と再計算可能な詳細へ固定する。機械契約は`src/baibai_engine/research/decision_packet.py`、canonical revisionはapplication DBの`packet_id`で識別する。独立reviewは同じpublish transactionで`review_id`を得て、DBの外部キーで対象packet revisionへ束縛される。

decision packetは新規の購入判断と保有見直しの判断根拠を固定する。既存保有に判断根拠が必要になった場合は、その時点の一次情報と現値からpacketを作成する。

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

Selectionから機械転記するE[r]とFV anchorは観測factではないため、`facts`へ混ぜず`input_snapshot.screening_estimate`へ置く。このobjectは`origin: estimate`、model version、unit、assumptions、as-of、source IDsを保持し、E[r]は`annual_ratio`、FVは`JPY_per_share`で固定する。値はworkspaceの外部inputとしてhashで束縛したselection outputのaudit rowから転記し、編集可能なshortlistや表示用percent・丸め済みFVから逆算しない。selection、estimate snapshot、workspaceのas-ofは一致を必須とする。転記元が無い旧selectionやFV欠損を推測で埋めず、bridge telemetryの欠損だけでresearch・promotionを停止しない。

外部sourceはHTTPS URLを持つ。local dataは消失し得るファイルパスを参照せず、`provider`、`dataset`、`retrieved_at`を持つ。`retrieved_at`はAI proposal時刻以前でなければならず、提案後に得た情報を判断時点snapshotへ遡及混入できない。市場価格は`observed_at`と`price_basis`（realtime / 調整済み終値 / 未調整終値）を持つ。すべてのsourceはpacketと同じtickerを明示し、source/fact/scenarioがpacket as-ofより未来の場合、source IDが解決しない場合、価格・valuationのtypeまたはunitが不正な場合は`incomplete`とする。HTML、PR body、operation sessionは説明・ID参照にとどめ、判断入力の正本を複製しない。

## Scenario arithmetic

3年は予測可能性のsanity check、5年は主評価である。各horizonにbear/base/bullを1件ずつ要求する。

```text
terminal_earnings = starting_earnings * (1 + annual_earnings_growth)^years
terminal_shares = starting_shares * (1 + annual_share_count_change)^years
terminal_price = terminal_earnings / terminal_shares * terminal_valuation_multiple
total_return_CAGR = ((terminal_price + cumulative_dividend_per_share) / entry_price)^(1/years) - 1
```

`annual_share_count_change_pct`が正なら希薄化、負ならbuybackによる株数減少である。terminal priceは配当を含めず、累積配当をCAGR計算で1回だけ加える。入力が主張するterminal earnings、shares、price、CAGRを式から再計算し、不一致を`incomplete`にする。

### 5-year base break-even

`baibai-engine research evaluate`は5年base scenarioだけについて、packet schemaへ値を複製せず`five_year_base_break_even`を派生出力する。要求CAGRを`r`、entry priceを`P`、累積配当を`D`、5年後利益と株数を`E5`、`S5`とすると、境界値は次の式で求める。

```text
required_total_value = P * (1 + r)^5
break_even_terminal_multiple = (required_total_value - D) * S5 / E5
break_even_earnings_growth =
  (1 + annual_share_count_change)
  * (((required_total_value - D) * starting_shares)
     / (terminal_valuation_multiple * starting_earnings))^(1/5)
  - 1
```

`terminal_multiple_downside_buffer`はbase multipleからbreak-even multipleを引いた値、`earnings_growth_downside_buffer_pct_points`はbase growthからbreak-even growthを引いた値である。正なら、他の仮定を固定したときに要求CAGRまで悪化を吸収できる。境界判定には丸め前のraw入力と計算値を使い、出力だけを小数4桁へ丸める。累積配当だけで必要価値を満たす場合は無効な0倍・負のmultipleを表示せず`dividends_alone_sufficient`、model domain外の有限な境界は値を保持して`below_model_min`または`above_model_max`とする。

観測multipleとの比較は、利益basisが`net_income_attributable_to_owners`で、同一as-ofの`valuation_metric` / `ratio` factのうち、fact IDが`trailing-per`または`trailing-per-`で始まる一意な正値だけを使う。候補なし、複数候補、source未解決はfail-closedでstatusを返し、他のvaluation metricへfallbackしない。この派生出力は仮定感応度をreviewする材料であり、packet readiness、recommendation、execution policyを変更しない。

### Screening-to-research FV bridge

`estimates.screening_fv_bridge`は、screening FV anchorからresearch FVへ修正した主要説明要因1つと短いnoteだけを持つ。全要因の寄与率や乖離率をpacketへ複製しない。`baibai-engine research evaluate`は`screening_fv_revision_pct = (current_fair_value_yen / screening_estimate.fair_value_anchor_yen - 1) * 100`を派生計算し、負値をresearchによるFV引き下げ、正値を引き上げとして返す。bridgeが存在するのにbaseline FVが無い場合は不整合、baselineがあるのにbridgeが無い場合は改善telemetryのwarningであり、投資判断のhard blockではない。

## Planning-only execution pricing

`estimates.required_5y_base_cagr_pct`は、5年base scenarioに対してこの判断が要求する年率を明示する。`deep_discount_bps`を使う場合も同じpacketに保存し、後から別の値へ差し替えない。execution policyは表示用の上限価格や終値からの任意率を入力にせず、再計算した5年base terminal priceと累積配当から最大許容価格を求める。

```text
terminal_total_value = recalculated_5y_base_terminal_price
                     + cumulative_dividend_per_share_yen
max_acceptable_price = floor_to_tick(
  terminal_total_value / (1 + required_5y_base_cagr_pct / 100)^5
)
```

日常の寄り前proposalは`baibai-engine research plan-limit`を使う。target session直前の最新完全営業日のJPX raw/unadjusted closeをSQLiteから読み、packetの最大許容価格とboard lotへ接続する。regular session、realtime quote、板、5分freshnessは要求しない。

| condition | result |
| --- | --- |
| packet/review ready、corporate action resolved、close ≤ max price | `planned_limit`。主指値はclose |
| close > max price | `defer` |
| adjusted-only、non-1 adjustment factor、価格basis不明 | `defer` |
| 同一tickerのactive reservationあり | `defer`。元注文の再表示と追加注文を区別できないため新規注文を作らない |
| packet/review not readyまたはhash mismatch | `defer` |

quantityを考える注文額の目安は[`portfolio-management`](../portfolio-management.md#capital-guidance)を正本とする。1単元が上限を超えても1単元と超過warningを出し、より安い次点へ自動変更しない。cash、dry powder、concentration、既存保有、他tickerのreservationは人間向けwarning/annotationであり、投資価値rankingや最大許容価格を変えない。同一tickerのactive reservationだけは注文の重複を防ぐため`defer`にし、human resultによる約定またはreleaseのledger反映後に再実行する。

`planned_limit`のportfolio exposureは、proposalの`price_as_of`を全保有の共通評価日とし、同日のJPX raw/unadjusted close × 保有数量で一時的に再評価する。分母は、再評価した保有時価とavailable / reserved cashから同じbasisで再計算する。active reservationは市場価格ではなく`reserved_yen`を現在exposureに1回だけ加え、今回注文はprospective exposureの分子に1回だけ加える。注文はcashと保有の資産振替えなので分母に加算しない。

共通評価日のcloseがない、ledger評価日から共通評価日までの営業日barが欠ける、または`adjustment_factor`が未確認・非1の保有はledger評価額へfallbackする。出力はその銘柄を`ledger_fallback_tickers`とwarningの両方で明示し、`holding_valuation_status: mixed_with_ledger_fallback`としてraw closeとledger値の混在を黙示しない。fallbackやconcentration warningは人間のsizing判断に渡すが、`planned_limit`、投資価値ranking、最大許容価格を変えない。canonical ledgerも書き換えない。

common-factor exposureは、選定銘柄にpacketの現行classification、その他にledgerの宣言済みtagを使う。選定銘柄以外で`common_factors`が空の銘柄は`common_factor_empty_tickers`に列挙し、その場合のcommon-factor円額・比率は宣言済みtagだけに基づく下限値である。coverage warningを併記し、閾値未満を完全なfactor分散の保証として扱わない。

proposalは人間承認前の判断材料で、brokerを操作しない。AIはfill probability、当日価格方向、未報告broker状態を推定しない。人間から結果が報告された後だけledger draftを作る。既存`baibai-engine research evaluate --execution-input`は互換的なlive evaluationであり、通常の寄り前runbook入口ではない。

`plan-limit`出力はproposal作成用のephemeral inputである。`proposal create --packet-id`はimmutable packet/review IDとcurrent DB ledgerからplanning-limitを再検証する。`approve`時にも同じ条件を再計算し、packet、price、quantity、expiry、ledgerのいずれかが変わっていればno-writeで新proposalを要求する。

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
uv run baibai-engine research evaluate /tmp/packet-draft.yaml
uv run baibai-engine research status --workspace .cache/opportunity/YYYY-MM-DD
uv run baibai-engine research plan-limit --help
```
