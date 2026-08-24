---
name: research
description: 人間が選んだ候補を一次情報で深掘りし、thesis、独立反証、proposal または見送り、統合判断まで確定する。候補の提示までは shortlist skill。
---

# Research

割安に見える理由を一次情報で検証し、proposal または見送りを人間へ提示する。approve と broker 操作は人間だけが行う。

## 前提

- active の opportunity session と、人間が選んだ primary-research set。
- primary-research set は canonical Shortlist の `selected` の部分集合であること。Research Gate が `rejected` とした ticker は深掘りしない。
- 選択内容と予算条件を session checkpoint の `human_confirmation` に記録済みであること。

## 手順

1. **workspace を固定する**

   ```bash
   uv run baibai-engine research prepare --asof <ASOF> --selection-output <selection.yaml> \
     --shortlist-id <SHORTLIST_ID> --db stores/application/baibai.sqlite --workspace .cache/opportunity/<ASOF>
   ```

   `--shortlist-id` は selection を判断した canonical Shortlist。prepare は selection と Shortlist の `selection_id` / as-of / run revision / Review Set membership を照合し、`selected` を admission 可能集合として workspace へ固定する。selection file は workspace 外に置く。手元にない場合は `screening selection show` で再取得し、`select` は再実行しない。

   workspace の `selection.yaml` の `shortlist` に選択 ticker を記入する。書けるのは `admissible_tickers`（= Shortlist selected）だけで、Review Set に居ても `rejected` なら scaffold 前に拒否される。各caseで `thesis-scaffold` と `review-scaffold` を実行し、次の操作は `research status` に従う。

   Research Gate の判定に異議がある場合は workspace で override しない。同じ run に対して `screening select` を実行し直し、修正した判断で Shortlist を publish して、その `shortlist_id` で prepare をやり直す。

2. **一次情報を調査する**

   会社 IR、EDINET、決算資料で load-bearing claim を検証する。検索 snippet、二次情報、外部 AI 出力を観測事実にしない。取得できない場合は代替 source で突合し、確認できない項目は未検証のまま残す。PDF は `baibai_engine.research.pdf_reader` で原文を読む。business-model guide は指定caseだけに適用する。

   各caseのShortlist v5 `machine_snapshot`にある`opportunity_lane_id`と
   `primary_evidence_pattern_id`を、[`method/research/playbooks/`](../../../method/research/playbooks/README.md)の
   active Research Playbookが明示する`applies_to_opportunity_lane_ids` /
   `applies_to_evidence_pattern_ids`へ照合し、一致するchecklistを適用する。同名slugからimplicitに
   対応を推測しない。明示mappingが無いcaseはscaffoldの共通checklistだけを使い、適用先を捏造しない。

3. **thesis を書いて検算する**

   scaffold の構造を変えず、[`thesis.md`](../../../docs/reference/thesis.md) の契約に従う。scenario の算術は `baibai_engine.research.scenario_arithmetic` で計算し、permanent-loss 7軸、terminal multiple、starting earnings、share basis、source date を照合する。

   macro context の該当 `estimate_caveats` と、`research-comparison.yaml` の E[r] 帯文脈を読む。採用する場合、適用外と判断する場合、古くて根拠が弱い場合のいずれも、thesis の scenario assumption または `screening_fv_bridge.note` に判断を残す。macro は数値 driver、採用 gate、自動 sizing にしない。

4. **evaluate と独立反証を通す**

   `research evaluate <thesis-draft>` の error を 0 にする。author とは別の role が、一次 source、算術、multiple premium、永久損失7軸、countercase、macro caveat、E[r] 帯との乖離、代替候補、portfolio marginal value、limit の整合性を反証する。結論または価格を変えた場合は thesis に戻り、新しい core hash で review を取り直す。

5. **canonical thesis にする**

   ```bash
   uv run baibai-engine research promote --workspace .cache/opportunity/<ASOF> \
     --db stores/application/baibai.sqlite --ticker XXXX
   ```

   buy / defer / reject の全caseを promote する。evidence が不足しているか adverse axis がある buy は、人間の evidence override と reduced sizing がなければ通さない。人間の判断を得られない場合は defer とし、dated trigger を assessment と task に残す。comparison には全caseの FV、5y base CAGR、countercase、disposition を記録し、selected は最大1件とする。

6. **proposal または見送りを確定する**

   buy case は次のコマンドで、最新の raw close、required return、canonical ledger から価格、数量、期限を計画する。

   ```bash
   uv run baibai-engine research plan-limit --thesis <thesis-draft> \
     --db stores/application/baibai.sqlite --sqlite-path stores/market/market.sqlite \
     --budget-min-yen <MIN> --budget-max-yen <MAX> \
     --target-session <日付> --output <proposal-input.yaml>
   ```

   starter は [`portfolio-management.md#starter-band`](../../../docs/portfolio-management.md#starter-band) の2経路に限る。7軸、独立 review、human override は緩めない。starter には `position_intent: starter`、`sizing_action: reduced`、`starter_catalyst_date` と同日期限の task が必要である。

   close が `max_acceptable_price` を超える場合は正常な defer とし、price watch に変換する。proposal は recommendation `buy` と必要な human override が揃う場合だけ作る。

7. **統合判断と session を完了する**

   `research assessment-scaffold` で全caseを束ね、reject / defer にも `reject_class` を記録する。`assessment-publish --check` の digest を review に束縛してから publish する。dated follow-up を task 化し、canonical artifact を含む final payload で session を complete する。cloud 反映は `ops-maintenance` skill に従う。

## 停止条件

次の場合は停止する。

- primary-research set または人間確認がない
- 人間の選択に Shortlist `selected` 以外の ticker が含まれる（正規の再判定経路へ戻す）
- 一次情報、source date、算術、review hash、ledger snapshot に矛盾がある
- 未検証事実を buy の根拠へ昇格するか、人間の approve 前に broker / ledger へ進もうとしている

## 正本

- thesis、算術、review binding: [`thesis.md`](../../../docs/reference/thesis.md)
- Assessment Case比較と統合判断: [`bargain-assessment.md`](../../../docs/reference/bargain-assessment.md)
- business model 調査: [`business-model-research.md`](../../../docs/reference/business-model-research.md)
- 資本、starter、注文額: [`portfolio-management.md`](../../../docs/portfolio-management.md)
