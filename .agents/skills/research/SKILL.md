---
name: research
description: 人間が選んだ候補を一次情報で深掘りし、thesis、独立反証、proposal または見送り、統合判断まで確定する。候補提示までは shortlist skill。
---

# Research

割安に見える理由を一次情報で検証し、proposal または見送りを人間へ提示する。approve と broker 操作は人間だけが行う。

## 前提

- active な opportunity session と、人間が選んだ primary-research set。
- 選択内容と予算条件を session checkpoint の `human_confirmation` に記録済みであること。

## 手順

1. **workspace を固定する**

   ```bash
   uv run baibai-engine research prepare --asof <ASOF> --selection-output <selection.yaml> \
     --db stores/application/baibai.sqlite --workspace .cache/opportunity/<ASOF>
   ```

   selection file は workspace 外に置く。無ければ `screening selection show` で再取得し、`select` は再実行しない。workspace の shortlist に選択 ticker を記入し、各 lane で `thesis-scaffold` と `review-scaffold` を実行する。次の操作は `research status` に従う。

2. **一次情報を調査する**

   会社 IR、EDINET、決算資料で load-bearing claim を検証する。検索 snippet、二次情報、外部 AI 出力を観測事実にしない。取得不能は代替 source で突合し、未確認は未検証のまま残す。PDF は `baibai_engine.research.pdf_reader` で原文を読む。business-model guide は指定 lane だけに適用する。

3. **thesis を書いて検算する**

   scaffold の構造を変えず、[`thesis.md`](../../../docs/reference/thesis.md) の契約に従う。scenario 算術は `baibai_engine.research.scenario_arithmetic` で計算し、permanent-loss 7軸、terminal multiple、starting earnings、share basis、source date を照合する。

   macro context の該当 `estimate_caveats` と、`research-comparison.yaml` の E[r] 帯文脈を読む。使う、適用外、古くて弱い、いずれの場合も thesis の scenario assumption または `screening_fv_bridge.note` に判断を残す。macro は数値 driver、採用 gate、自動 sizing にしない。

4. **evaluate と独立反証を通す**

   `research evaluate <thesis-draft>` の error を 0 にする。author と別 role が一次 source、算術、multiple premium、永久損失7軸、countercase、macro caveat、E[r] 帯との乖離、代替候補、portfolio marginal value、limit 整合を反証する。結論または価格を変えたら thesis に戻り、新しい core hash で review を取り直す。

5. **canonical thesis にする**

   ```bash
   uv run baibai-engine research promote --workspace .cache/opportunity/<ASOF> \
     --db stores/application/baibai.sqlite --ticker XXXX
   ```

   buy / defer / reject の全 lane を promote する。evidence 不足や adverse axis を持つ buy は、人間の evidence override と reduced sizing が無ければ通さない。人間判断を得られなければ defer とし、dated trigger を assessment と task に残す。comparison には全 lane の FV、5y base CAGR、countercase、disposition を記録し、selected は最大1件とする。

6. **proposal または見送りを確定する**

   buy lane は次の command で latest raw close、required return、canonical ledger から価格・数量・期限を計画する。

   ```bash
   uv run baibai-engine research plan-limit --thesis <thesis-draft> \
     --db stores/application/baibai.sqlite --sqlite-path stores/market/market.sqlite \
     --target-session <日付> --output <proposal-input.yaml>
   ```

   starter は [`portfolio-management.md#starter-band`](../../../docs/portfolio-management.md#starter-band) の2経路だけを許し、7軸、独立 review、human override を緩めない。starter には `position_intent: starter`、`sizing_action: reduced`、`starter_catalyst_date` と同日期限の task が必要である。

   close が `max_acceptable_price` を超える場合は正常な defer とし、price watch に変換する。proposal は recommendation `buy` と必要な human override が揃う場合だけ作る。

7. **統合判断と session を完了する**

   `research assessment-scaffold` で全 lane を束ね、reject / defer にも `reject_class` を記録する。`assessment-publish --check` の digest を review に束縛してから publish する。dated follow-up を task 化し、canonical artifact を持つ final payload で session を complete する。cloud 反映は `ops-maintenance` skill に従う。

## 停止条件

- primary-research set または人間確認がない。
- 一次情報、source date、算術、review hash、ledger snapshot の矛盾。
- 未検証事実を buy 根拠へ昇格する、または人間の approve 前に broker / ledger へ進むこと。

## 正本

- thesis、算術、review binding: [`thesis.md`](../../../docs/reference/thesis.md)
- lane 比較と統合判断: [`bargain-assessment.md`](../../../docs/reference/bargain-assessment.md)
- business model 調査: [`business-model-research.md`](../../../docs/reference/business-model-research.md)
- 資本、starter、注文額: [`portfolio-management.md`](../../../docs/portfolio-management.md)
