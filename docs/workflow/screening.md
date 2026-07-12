---
title: "Workflow — screening"
summary: "point-in-time cacheからcandidate、audit pool、selectionを決定論的に生成し、一次IR shortlistへ渡す工程。"
doc_type: workflow
status: active
last_reviewed: 2026-07-12
related_docs:
  - "../operations/decision-cycle.md"
  - "../reference/screening-runtime.md"
  - "../reference/valuation-metrics.md"
---

# Workflow — screening

screeningは全上場銘柄から割安ゾーンを機械抽出し、observed、derived、estimateを由来付きで出すL2工程である。採用・因果・相場観を判断せず、decision packetの代わりにならない。

## Purpose and boundary

- `run`: point-in-timeの財務・価格・JPX factsからcandidate poolを作る。
- `select`: candidateを既存rulesでrankし、production recommendationsと監査用audit poolを出す。
- AI: audit poolから一次IR shortlistを最大5件作る。
- research: 一次情報、永久損失、3年/5年scenarioで最良0〜1件を決める。

macro context、ledger、予算はscreening rankを変更しない。後段のcontext/annotationとして扱う。

## Universe and ASOF

対象は東証Prime/Standard/Growthの普通株を基本とし、厳密なeligible universe、流動性、上場期間、業種相対ruleはversioned screening rulesを正本とする。ASOFは最新完全営業日。future dataとASOF後の開示を混ぜない。

historical backfill以外で`--allow-stale-jpx`を通常使用しない。ASOF、rules path/hash、SQLite coverage、output pathをoperation Issueへ残す。

## Cache coverage and refresh

正規順は次。

1. `verify-cache-coverage`
2. 不足時だけ`bootstrap-cache`
3. 不足時だけ`extract-edinet-metrics`
4. `verify-cache-coverage`を再実行
5. `run`
6. `select`

coverage commandは単独で実行し、後続commandのexit 0で失敗を隠さない。`run/select`はprovider APIへ暗黙fallbackせず、cache-onlyで決定論的に動く。

| coverage result | action |
| --- | --- |
| complete、ASOF一致 | runへ進む |
| source range不足 | 該当sourceだけrefreshして再検証 |
| future-dated row | stop。dataを修正する |
| stale JPX | historical backfill以外はstop |
| EDINET/price欠損 | 欠損範囲とcandidate影響を記録してstop/defer |

## Deterministic run

`run --asof`は同じcache、rules、ASOFから同じcandidate outputを作る。通常operationでは`--output-path /tmp/...`またはlocal workspaceへ出し、raw全量をcanonical judgmentとしてcommitしない。

candidateは次を区別する。

- **observed**: providerから観測したprice、financial、JPX fact。
- **derived**: formulaとinputを持つratio、percentile、relative metric。
- **estimate**: E[r]、FV anchor等のmodel output。事実ではない。

AI judgment、割安の原因、将来予測、採用結論をcandidateへ書かない。

## Select outputs

| output | 目的 | 読み手 | canonical |
| --- | --- | --- | --- |
| candidate pool | screen通過全件 | select/calibration | rebuildable |
| `recommendations` | production rule/cap適用後の通常表示 | operator | rebuildable |
| `audit_pool` | diversity/cap切断前のrank上位N件を監査 | AI/reviewer | rebuildable |
| IR shortlist | audit poolから最大5件を理由付き選定 | research | operation Issue/workspace |

`--audit-top 20`は候補抜けを監査するviewで、20件すべてを深掘りする命令ではない。`recommendations`のproduction capとIR shortlist最大5を混同しない。

## Ranking versus warnings

| fact/context | rankを変える | warning/annotationだけ |
| --- | --- | --- |
| versioned screening ruleとestimate components | yes | — |
| liquidity/durability rule | rulesが定義する範囲 | 詳細reasonを出す |
| held/reserved | no | portfolio annotation |
| monthly budget/cash/concentration | no | proposal warning |
| macro material delta | no | research context |
| corporate action unresolved | rankを都合よく変更しない | research/limitをblock |

上位候補をheld/reserved/予算だけで削除しない。一時的なFV乖離が大きく永久損失が低いなら買増し候補としてresearchへ残す。

## Corporate action and abnormal price

split/併合、株式交換、権利落ち、価格系列異常が疑われる場合はJPX/会社一次情報で効力日、株数、adjustment factor、price basisを確認する。adjusted seriesを指値へ使わない。unresolvedならresearch/plan-limitをdeferし、正常値を推測しない。

## No candidate and failure

- candidate 0件: screen条件とcoverageを確認し、正しければ正常終了。
- viable shortlist 0件: `no actionable bargain`。購入を強制しない。
- estimate missing: observed factで穴埋めせず、原因と影響を示す。
- output drift: rules/input hashを確認し、古いworkspaceをpromoteしない。

## Calibration handoff

E[r]とFV anchorの長期予測力は`calibration-build/evaluate`でpoint-in-time panelとforward returnを比較する。3m/6mはregression alert、1yはleading evidence、production変更は3y/5y evidenceを必要とする。個別opportunityの完了条件へcalibrationを入れない。

## Failure / stop conditions

- coverage incomplete/future/stale。
- schema/rules/public CLIが不明。
- corporate actionとprice basisが矛盾。
- estimateをfactとして扱う、またはAI judgmentをcandidateへ書こうとしている。

## Validation

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-validation --target candidates
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening verify-cache-coverage --asof YYYY-MM-DD
```

## Related

- [`../operations/decision-cycle.md#opportunity-path`](../operations/decision-cycle.md#opportunity-path)
- [`./research.md`](./research.md)
- [`../reference/screening-runtime.md`](../reference/screening-runtime.md)
- [`../reference/valuation-metrics.md`](../reference/valuation-metrics.md)
