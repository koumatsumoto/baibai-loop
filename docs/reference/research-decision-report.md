---
title: "Research decision report"
summary: "詳細リサーチ、横比較、購入方法を独立content review後にHTML projectionへ統合する契約。"
doc_type: reference
status: active
last_reviewed: 2026-07-14
---

# research-decision-report — 詳細リサーチ統合レポート

opportunity pathでprimary-research setを調査した後、各銘柄の詳細な調査結果と、全件比較から得た購入方法を一つのHTMLへ投影する。HTMLは人間が読む表示物であり、内容レビューの対象でもcanonical recordでもない。

## Artifact boundary

| artifact | 責務 | canonical / ephemeral |
| --- | --- | --- |
| lane `packet-draft.yaml` / promoted packet | sources、facts、3年/5年scenario、FV、7軸、AI value capture、countercase | promoted packetだけcanonical |
| `research-comparison.yaml` | 全laneの比較、disposition、非採用理由、最良0〜1件 | workspace内ephemeral judgment |
| `proposal.yaml` | packet/review/ledgerに束縛したraw close、max price、指値、数量、notional、expiry、warning/defer | ephemeral planning output |
| `findings.yaml` | ユーザー指定質問への回答、事業model、成長の質、domain固有分析、未開示事項、monitoring | ephemeral content input |
| `report-review-attempt-N.yaml` | 上記軽量入力への独立content reviewと入力hash | ephemeral review gate |
| `research-decision-report.html` | review済み内容の人間向けprojection | ephemeral display |
| operation Issueのreview bundle | manifest、findings、comparison、全lane packet、proposal、report reviewのreview済み内容 | persistent session checkpoint |

HTMLへ数値や結論を手入力しない。rendererは既存artifactをjoinするだけであり、packet / review / proposal / ledger / operation Issueを置き換えない。

## `findings.yaml` の責務

templateは[`tools/research_decision_report/findings-template.yaml`](../../tools/research_decision_report/findings-template.yaml)。各tickerで最低限以下を記録する。

- 人間が指定した質問と、一次情報から得た回答
- business modelと、売上が利益・FCF・一株価値へ届くvalue-capture経路
- volume / price / mix / upsell・churn / FXを開示範囲で分けたgrowth quality
- 財務耐久性
- 業種固有のload-bearing finding
- 最強countercase
- dated catalyst、未開示・blocked、monitoring trigger
- source document title、公表日、取得status（`ok / missing / stale / failed / blocked`）

各evidenceは`observed / derived / estimate / management_claim`を区別し、packetの`source_id`へjoinする。URL、retrieved_at、as-of、used_forはpacket sourceを正本とし、findingsへ重複させない。外部sourceのdocument title / published_at / statusだけを`source_metadata`で補う。未知source ID、不正ticker、shortlist不一致、as-of不一致は生成を停止する。

海外展開は少なくとも次の3層を分ける。所在地ベースの海外売上だけで国際分散を確定しない。

1. 商品・情報・供給網の海外coverage
2. 契約所在地、請求通貨、地域別売上
3. ultimate customer origin / 企業系列

業種固有の質問は固定fieldを増やさず`domain_findings`へ置く。例として、game会社はpipelineを発売済み / 発売日確定 / test中 / 年だけ公表 / 未定へ分け、完全新作 / 地域展開 / platform展開 / update / publishingを区別する。cash durabilityは営業CF、投資、配当、自社株買い、lease、運転資本寄与を混同しない。

## HTML前の独立content review

HTMLは大きくreviewに不向きなので、別roleは`findings.yaml`、`research-comparison.yaml`、各lane packet、`proposal.yaml`だけをreviewする。先にhash-bound draftを作る。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.research_decision_report.review_scaffold \
  --workspace .cache/opportunity/YYYY-MM-DD \
  --findings .cache/opportunity/YYYY-MM-DD/findings.yaml \
  --proposal .cache/opportunity/YYYY-MM-DD/XXXX/proposal.yaml \
  --out .cache/opportunity/YYYY-MM-DD/report-review-attempt-1.yaml
```

`selected_ticker: null`の`no actionable bargain`では`--proposal`を省略する。reviewerは以下を一つずつ`pass / fail`にし、`reviewer_identity`、別`reviewer_run_id`、`reviewed_at`、findingを記録する。

1. source freshness
2. user questions answered
3. primary source traceability
4. fact / estimate separation
5. countercase and unknowns
6. scenario and FV consistency
7. comparison and portfolio fit
8. purchase method binding

`changes_required`ならfindings / comparison / packet / proposalへ戻る。変更後はhashが変わるため、既存reviewを上書きせず`report-review-attempt-2.yaml`のようにattempt番号を増やして再scaffoldする。rendererの`--review`には最終attemptを明示する。全checkが`pass`でerror findingがなく、reviewed input hashが現在値と一致するときだけ`conclusion: pass`にする。

## HTML projection

HTML生成はこのrepositoryのreport contractを実装する次のcommandだけを使う。汎用HTML生成skillへ内容やrenderingを委譲せず、別templateで置き換えない。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.research_decision_report.render \
  --workspace .cache/opportunity/YYYY-MM-DD \
  --findings .cache/opportunity/YYYY-MM-DD/findings.yaml \
  --review .cache/opportunity/YYYY-MM-DD/report-review-attempt-N.yaml \
  --proposal .cache/opportunity/YYYY-MM-DD/XXXX/proposal.yaml \
  --out .cache/opportunity/YYYY-MM-DD/research-decision-report.html
```

no actionable bargainでは`--proposal`を省略する。selected tickerがあればproposalは必須で、rendererは次をfail closedで照合する。

- selected ticker、packet raw/core hash、independent review hash
- workspace manifestのledger hashとproposalの`source_ledger_sha256`
- target sessionとproposal expiry
- reviewが束縛したmanifest / findings / comparison / 全packet / proposal hash

HTMLは外部script/assetを持たず、CSPを設定し、全自由記述をescapeする。tickerは固定形式のTradingView URLだけへlinkする。一次source linkはHTTPSかつpublic hostだけを許し、hostnameを表示してuserinfo、localhost、private / link-local literalを拒否する。non-ok sourceのdecision-impact noteと、pass reviewに残るwarning / info findingを省略せず表示する。`missing / failed / blocked` sourceを`observed` evidenceの根拠には使えない。

comparisonの5年base CAGR / FV / FV gapはpacketから再計算し、proposalのboard lot / max price / raw close / quantity / notionalは`plan-limit`と同じpolicy・式から再導出する。review hashがfreshでも矛盾した手書き数値は拒否する。`planned_limit`は何を・いくらで・何株・想定いくら・いつまでを表示し、`defer` / `no actionable bargain`は購入提案なしを明示する。

## Storage and checkpoint

workspace上のfindings、comparison、non-promoted packet、proposal、report review、HTMLは`.cache`配下のephemeral artifactでcommitしない。ただしcache削除後も何をreviewしたか復元できるよう、`conclusion: pass`後、HTML生成前または同時に、manifest、findings、research-comparison、全shortlistのpacket-draft、proposal（no actionable bargainでは省略）、report-reviewの**内容そのもの**をreview bundleとして残す。promote済みselected packet/reviewはcanonical GitHub pathとhashをbundle summaryから参照し、同じ内容をIssueへ複製しない。その他のYAMLはGitHubの1 comment上限を超えない単位で`gh issue comment --body-file <path>`によりoperation Issueの別commentへ置き、summary checkpointにartifact名、SHA-256、comment URLまたはcanonical pathを対応付ける。repository visibilityと保存対象を外部書き込み前に確認し、local pathとhashだけでは完了にしない。

operation Issueのbundleはsession checkpointであり、投資判断のcanonical recordを置き換えない。選択laneはpromote済みpacket/reviewをrecordsの正本とし、非選択laneと統合contentは後日の比較・監査・再reviewに必要なreview時点snapshotとしてIssueへ保持する。HTMLの内容は保存せず再生成する。broker操作とledger更新は人間の結果報告後だけ行う。
