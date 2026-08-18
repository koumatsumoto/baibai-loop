---
name: research
description: 人間が選んだ primary-research set の深掘り。一次情報 → thesis → 独立反証 → promote → plan-limit / proposal → bargain assessment → session complete まで。候補提示までは shortlist skill。
---

# Research

安く見える理由が一時的な誤解か構造的毀損かを一次情報で区別し、proposal（指値・数量・期限）または見送り（no_actionable_bargain / defer）の統合判断を人間へ提示する。approve と broker 操作は人間だけが行う。

## 前提

- active な opportunity session と、人間が選択した primary-research set（2〜4 件）。
- 選択・判断の記録: 人間の選択内容と予算条件を session checkpoint の `human_confirmation` へ先に記録する。

## 手順

1. **workspace prepare**:

   ```bash
   uv run baibai-engine research prepare --asof <ASOF> --selection-output <selection.yaml> \
     --db stores/application/baibai.sqlite --workspace .cache/opportunity/<ASOF>
   ```

   `--selection-output` は **workspace 外**（scratchpad 等）に置く。prepare は入力ファイルの
   sha256 を manifest に固定して workspace へコピーするので、workspace 内のパスを渡すと入力と
   コピーが同一ファイルになり、手順 2 の shortlist 記入編集が `input hash drift` で拒否される。

   selection output が手元に無ければ run store から read-only で取り出す（bound run が
   evict 済みでも取れる。`select` の再実行は新しい selection を publish してしまうので使わない）:

   ```bash
   uv run baibai-engine screening selection show --selection-id <ID> \
     --runs-db stores/screening/runs.sqlite --output-path <selection.yaml>
   ```

2. workspace の `selection.yaml` の `shortlist:` へ選択 ticker を `[{ticker: 'XXXX'}, ...]` で記入し、lane を作る:

   ```bash
   uv run baibai-engine research thesis-scaffold --workspace .cache/opportunity/<ASOF> \
     --db stores/application/baibai.sqlite --ticker XXXX --sqlite-path stores/market/market.sqlite \
     --target-session <次の取引session>
   uv run baibai-engine research review-scaffold --workspace .cache/opportunity/<ASOF> \
     --db stores/application/baibai.sqlite --ticker XXXX
   ```

   `research status --workspace ...` が常に次コマンドを教える。
3. **一次情報調査**（lane ごと。委譲するときは AGENTS.md の subagent 規律に従う）: 会社 IR・EDINET・決算資料の原文で load-bearing claim を検証する。TDnet・株探は 403 になりやすい（irbank の PDF ミラー等で代替し、裏取りできない項目は「未検証」と明示する）。落とした PDF は `uv run python -m baibai_engine.research.pdf_reader <pdf> --search <キーワード>` で読む（決算短信の AES 暗号化に対応済み。`--pages 1-3` で節を通読）。検索 snippet・外部 AI 要約を観測事実へ昇格しない。business-model guide の pilot 指定 lane だけ [`business-model-research.md`](../../../docs/reference/business-model-research.md) の lens を適用する。
4. **thesis 執筆**（契約・算術の正本は [`thesis.md`](../../../docs/reference/thesis.md)）。schema が語らない機械 gate:
   - scenario の starting earnings / share count は input_snapshot の**開示済み fact** に束縛される（正規化の主張は growth 側で表現する）。claimed_* は engine 再計算と一致が必須（CAGR は 2 桁丸め）。bear ≤ base ≤ bull の順序も検証される。claimed_* は手計算せず `uv run python -m baibai_engine.research.scenario_arithmetic --entry-price <P> --starting-earnings <E> --starting-shares <S> --scenario <name>:<3|5>:<growth>:<share_change>:<multiple>:<dividends> ... [--required-cagr-pct <要求 CAGR>]` で出す（engine と同じ関数を呼ぶので丸めがずれない。`--required-cagr-pct` は 5y base を要求 CAGR で割り戻した FV 候補を併記する）。
   - `permanent_loss_conclusion` は 7 軸から自動導出された期待値と一致が必須: adverse が 1 つでもあれば `elevated`、無ければ unknown 軸ありで `unknown`、それ以外 `acceptable`。
   - retrieved_at / proposed_at / reviewed_at は**現在時刻以前**。source 取得より前の proposed_at も拒否される。
   - scaffold が置いた構造は変えず、null と `TODO` だけを埋める（draft 冒頭の comment が gate の要求を持つ）。`facts[trailing-per]` は `scenario.base_3y_5y` の観測 multiple なので、比率を入れて利益側の一次 source を `source_ids` へ足す。`independent_review_ref` は review-scaffold が書き出す隣接ファイル名なので触らない。

   **macro estimate_caveats の消化**: 機械見積りの歪みは head の macro context が `affected_component` 付きで名指ししているので、FV アンカーと 5y base を置く前に読む。

   ```bash
   uv run baibai-engine macro context show --latest --asof <ASOF> \
     | uv run python -c 'import sys,yaml,json;print(json.dumps(yaml.safe_load(sys.stdin)["connection"]["estimate_caveats"],ensure_ascii=False,indent=2))'
   ```

   （`context show` の出力は JSON ではなく YAML なので `jq` を直接つながない）

   `affected_component`（`fv_anchor` / `reversion` / `carry` / `resilience`）が今回の見積りで使う成分に当たるものを消化し、**該当 scenario の `estimates.scenarios[].assumption` へ反映内容を書いて、その `source_ids` から macro context を引く**。source は `input_snapshot.sources` へ 1 件足す（`judgment` namespace は `extra="forbid"` で caveat 用の field を持たないので、そこには書けない）:

   ```yaml
   - source_id: macro_context_<YYYY_MM_DD>
     ticker: "XXXX"
     source_tier: local_data
     provider: baibai-loop
     dataset: macro-context
     as_of: <head の as_of>
     retrieved_at: <取得時刻>
     used_for: <どの caveat をどの成分へ効かせたか>
   ```

   `applies_to` がこの候補タイプに当たらない、`materiality: low`、または head が無いために消化しないなら、**その判断を `screening_fv_bridge.note` か base scenario の `assumption` に書く**（黙って落とさない）。**古さだけを理由に丸ごと飛ばさない** — 鮮度の基準は `screening select` と同じ `as_of` 45 日で、それを超えたときも「どの caveat がどう当てにならないか」を書く（[`macro.md`](../../../docs/reference/macro.md) §鮮度は読む側が判断する）。macro を数値ドライバー・採用 gate・自動 sizing にはしない（[`doctrine.md`](../../../docs/doctrine.md) 柱 2）。
5. **evaluate と独立反証**: `uv run baibai-engine research evaluate <thesis-draft>` のエラーを 0 にする。独立レビューは thesis author と別 role で実施し、次を必須反証にする: 上位候補の都合よい除外 / 構造衰退の一時割安誤認 / scenario・FV・CAGR・株数・配当の再計算（recalculated は engine の 2 桁丸め値と完全一致が必須）/ base・break-even・buffer と観測 trailing multiple の `scenario.base_3y_5y` check への記録 / multiple premium の一次根拠 / 7 軸 unknown・adverse の一次照合 / **macro estimate_caveats の該当分の消化（無視した場合はその判断が書かれているか）** / 代替候補 / portfolio marginal value / limit 整合。review が結論・価格を変えるなら `proposal_changed=true` で thesis へ戻し、hash 変更後は review を再生成する。
6. **promote**（lane ごと。買わない lane も canonical thesis を持つ）:

   ```bash
   uv run baibai-engine research promote --workspace .cache/opportunity/<ASOF> \
     --db stores/application/baibai.sqlite --ticker XXXX
   ```

   gate: checklist 全 complete・review hash = thesis core hash・schema valid。`research-comparison.yaml` に各 lane の FV / 5y base CAGR / countercase / disposition を記入し（`fv_gap_pct` は FV/price − 1）、`selected_ticker` は 0〜1 件。
7. **plan-limit と proposal**:

   ```bash
   uv run baibai-engine research plan-limit --thesis <thesis-draft> --db stores/application/baibai.sqlite \
     --sqlite-path stores/market/market.sqlite --target-session <日付> \
     --budget-min-yen <MIN> --budget-max-yen <MAX> --output <proposal-input.yaml>
   ```

   **starter band**: 5y base が `starter_band.required_return_ceiling_pct` に届かないが `starter_band.required_return_floor_pct` 以上で、永久損失が `elevated` でなく dated catalyst がある lane は、全件見送りの代わりに縮小 lot で建てる選択肢がある。thesis の `judgment` に `position_intent: starter` / `sizing_action: reduced` / `starter_catalyst_date` を置き、`estimates.required_5y_base_cagr_pct` をその lane で要求する水準（帯の下限以上・上限未満）にする。`plan-limit` が 1 注文の金額を `max_order_notional_yen` で切り、`proposal create` と `approve` が帯・1 注文上限・`max_bucket_pct` を検証する。要求利回りが帯の上限未満で `full` のままだと proposal は作れない（帯を開く代償を負わずに水準だけ下げる経路を残さないため）。**帯の実値と撤退基準は [`portfolio-management.md`](../../../docs/portfolio-management.md#starter-band) が正本**（この手順に数値を写さない）。7 軸・独立レビュー・human override は緩めない。starter を作ったら `starter_catalyst_date` を期日にした follow-up task を必ず `task add` する。

   機械契約: **指値 = 前営業日 raw close 固定**（gap を追う注文は作れない）、**`max_acceptable_price` = thesis 自身の 5y base FV を required return で割引いた値**。close > max なら `defer_reasons: close_above_max_acceptable_price` — これは正常な条件付き結論で、「終値 ≤ ceiling の日の夕方に proposal を起動する」watch に変換する。proposal create は thesis の recommendation が `buy` のときだけ通り、**adverse 軸・evidence gap・review 未完全検証がある buy には human evidence override（`approved_by: human`）が必須**（機械単独では buy を記録できない。人間の明示承認を取ってから記録する）。
8. **bargain assessment**: `uv run baibai-engine research assessment-scaffold --assessment-id bargain-assessment-<日付>-<slug> --asof <ASOF> --shortlist-id <ID> --thesis-id <ID>（lane ごとに反復）[--proposal-id <ID>] --out <draft>` で骨格を作り、散文（headline / comparison / entry_timing / forgone / lane 別 6 項目）を記入する。reject / defer lane にも `reject_class` 必須。`uv run baibai-engine research assessment-publish <draft> --db stores/application/baibai.sqlite --check` の `draft_sha256` を review block へ転記してから publish する（--check なしで実 publish）。
9. **完了**: follow-up task（defer の dated trigger）を `task add` し、session を complete する（opportunity の complete は `artifacts` 1 件以上が必須 — plan-limit の defer 証跡等を置く）。cloud 反映（`push-app` → materialize → success 確認）。報告には各 lane の判定・FV vs 価格・countercase・TradingView link・次の trigger を載せる。

## 人間境界

- 最新完全営業日の raw close で寄り前の指値を計画する。realtime 板・fill 確率を必須にしない。
- 人間報告前に broker 状態を推定せず ledger を変更しない。`approve / defer / reject` は人間の権限で、見送りも正常な結論である。

## 参照

- thesis 算術・review binding: [`docs/reference/thesis.md`](../../../docs/reference/thesis.md)
- 統合判断の契約: [`docs/reference/bargain-assessment.md`](../../../docs/reference/bargain-assessment.md)
- 資金・注文額 baseline: [`docs/portfolio-management.md`](../../../docs/portfolio-management.md)
