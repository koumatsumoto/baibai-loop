---
name: shortlist
description: screening からレビュー済み shortlist を発行し、人間が primary-research set を選ぶまで進める。選択後の深掘りは research skill。
---

# Shortlist

全上場銘柄から比較可能な候補を作る。この skill は shortlist を発行し、人間が primary-research set を選ぶまでを扱う。primary-research set は2〜4件を推奨し、上限は `research_selection_target_max` とする。AI は broker 操作へ進まない。

## 前提

1. AGENTS.md に従い opportunity session を開始または再開する。全 kind 共通の active session は `uv run baibai-engine operation show --status active` で確認し、0件のときだけ `uv run baibai-engine operation start --kind opportunity --as-of <ASOF>` を1回実行する。
2. canonical ledger から holding、reservation、cash を読む。
3. macro context head が古いか、[深度契約](../../../docs/reference/macro.md#depth-contract)を満たさない場合は、先に `macro-context` skill を実行する。
4. 永久損失、5年期待値と FV 乖離、portfolio 追加価値、購入可能性の順に比較する。cash、集中、保有は annotation であり、hard 除外条件ではない。

## 手順

1. **同期と preflight**

   market / runs / macro は `ops-maintenance` skill に従って cloud 上の正本と同期し、market を hydrate する。batch の実行中は pull しない。

   ```bash
   uv run baibai-engine screening shortlist preflight --asof <ASOF>
   ```

   preflight は pull 済みの run store だけを読み、同一 as-of・同一 HEAD の publication を探す。`reuse` は既存 selection を読む。`resume-current-code` は select だけ、`rerun-current-code` は run と select を各1回実行する。`previous` が `ambiguous` なら `--previous-run-revision-id` を指定して再判定する。`blocked`（worktree dirty、previous 未解決）は解消するまで新しい run を作らない。同一 as-of の試行錯誤で retention を消費しない。

   ```bash
   uv run baibai-engine screening verify-cache-coverage --asof <ASOF>
   # 不足時だけ: uv run baibai-engine screening bootstrap-cache --asof <ASOF>
   uv run baibai-engine screening extract-edinet-metrics --asof <ASOF>
   ```

   future-dated または stale な JPX データがあれば停止する。historical backfill 以外で `--allow-stale-jpx` を使わない。

2. **run と select**

   ```bash
   uv run baibai-engine screening run --asof <ASOF>
   uv run baibai-engine screening select --asof <ASOF> --run-revision-id <ID> \
     <PREFLIGHT_PREVIOUS_ARGS> --longlist-top 20 --output-path <workdir>/selection.yaml
   ```

   run の exit 2 は publish 済みの partial warning なので、警告を確認して継続する。select には preflight が返した previous 引数をそのまま渡す。`select` は再実行のたびに新しい selection を作るため、再取得には次を使う。

   ```bash
   uv run baibai-engine screening selection show --selection-id <ID> \
     --runs-db stores/screening/runs.sqlite --output-path <workdir>/selection.yaml
   ```

3. **比較文脈を作る**

   ```bash
   uv run python -m tools.experiments.measure_supply_context --selection-id <ID>
   uv run python -m baibai_engine.research_watch \
     --db stores/application/baibai.sqlite --sqlite-path stores/market/market.sqlite --asof <ASOF>
   ```

   supply と breadth は別々の軸で報告し、`unmeasured` を 0 や異常なしとみなさない。前回 shortlist と new / continued / exited を比較し、continued も再評価する。previous source が異なる overlap 値は比較せず、`null` を重なり 0 と解釈しない。

4. **候補をレビューする**

   [`screening-runtime.md#shortlist-writing`](../../../docs/reference/screening-runtime.md#shortlist-writing) と [`judgment-writing.md`](../../../docs/reference/judgment-writing.md) に従い、次の4段階を順に行う。active contract IDは`research-gate-v1`。

   1. **判断内容を確定する**

      selected 候補の material disclosure を一次情報で確認し、Gate 判断、根拠、競合する仮説、判断変更条件を確定する。selected は「一次リサーチ枠を使う価値がある」を意味し、購入すべきという判断ではない。

   2. **fieldの役割を確認する**

      `reason` / `prov`、`temporary` / `structural`、`unlock` / `catalyst`、`counter` / `research` の境界を確認する。Shortlist を mini Thesis にせず、Review Set 内の比較に必要な深さへ留める。

   3. **日本語を編集する**

      確定した判断内容を、各 field が単独で読める日本語へ編集する。この段階で新しい source、因果、見積り、採否判断を追加しない。新しい分析が必要になったら第1段階へ戻る。

   4. **判断内容を再検証する**

      編集前後で selected / rejected、rank、機械 E[r] / FV、数値・期間・qualifier、unknown、競合仮説が変わっていないことを claim ledger で確認する。field間の重複と、見出しが本文より強くなっていないことも確認する。claim ledger は作業用とし、恒久 artifact や validator にしない。

   次の annotation を必ず判断へ反映する。

   - FV convergence、full-year loss、stale financials、data-quality / durability warning は、見積りをどの方向へ歪めるかを書く。
   - margin、capital-control、大量保有、TOB、buyback filing は観測の文脈であり、単独で除外や rank 変更に使わない。`null` / `unknown` を否定事実へ変換しない。
   - TOB は届出書と意見表明を読み、価格収斂を割安と誤認しない。buyback carry は株数枠と金額枠の小さい残り、取得期間、直近取得、取得目的を一次開示で確認する。
   - 配当 carry の異常な跳ねは普通配当と特別配当、split basis、FCF coverage を確認する。special gain を反復収益にしない。
   - `deterioration_unmeasurable` は「悪化なし」ではない。一次開示で補う。
   - `next_earnings_status` の announced / scheduled / estimated / unknown を区別し、直前の前倒し開示は一次情報で確認する。
   - 既に reject / defer した ticker は、価格が FV 以下、新規 material disclosure、unknown を解消する決算、raw close が前回 thesis の entry から5%以上下落（`current_close_yen <= thesis_entry_price_basis_yen * 0.95`）、`fin_latest_disclosed_date > rows[].thesis_as_of` のいずれかが成立する場合だけ再研究候補にする。それ以外は `event_wait` とする。
   - E[r] 履歴帯は集団の記述統計としてだけ使い、個別予測にしない。
   - 機械 E[r] 順から rank を変える場合は理由を書く。rejected 全件に具体的理由と `reject_class` を付ける。

   selected は次の全条件を満たさなければならない。

   - upside の希望的前提を除いても期待値が正である
   - 利益が peak extrapolation ではない
   - catalyst に日付または観測条件がある
   - 現金保有より優れる理由がある
   - 該当する macro connection を判断へ反映している

5. **発行と完了**

   [`assets/draft-template.yaml`](./assets/draft-template.yaml) を埋め、source `selection_id` に束縛して発行する。

   ```bash
   uv run baibai-engine screening shortlist publish <draft>
   ```

   narrative を書ける entry だけを selected にする。件数の下限はなく、0件も正常である。`er_annual` は publisher が bound run から焼き込むため draft に書かない。発行後、全 entry の E[r] と bound run を照合する。dated follow-up は、既存 task との重複を確認してから task 化する。

   selected があれば `shortlist_id` と selected 一覧を人間へ提示し、checkpoint を更新して選択を待つ。人間が選べるのは selected の部分集合で、`research prepare --shortlist-id` がその境界を強制する。rejected 候補への異議は、同じ run で `select` を実行し直して新しい Shortlist を publish する経路で扱う。0件なら canonical shortlist を証拠に `completion_reason: no-shortlist-selection` で session を complete する。cloud 反映は `ops-maintenance` skill の application store 手順に従う。

## 停止条件

次の場合は停止する。

- operation session、canonical ledger、必要 store、previous identity に矛盾がある
- coverage が future / stale、preflight が `blocked`、または判断根拠となる annotation が unresolved である
- 人間が選択していないのに research または broker 操作へ進もうとしている

## 正本

- 判断境界と CLI: [`screening-runtime.md`](../../../docs/reference/screening-runtime.md)
- valuation annotation: [`valuation-metrics.md`](../../../docs/reference/valuation-metrics.md)
- macro depth: [`macro.md`](../../../docs/reference/macro.md)
- 資本と ranking: [`portfolio-management.md`](../../../docs/portfolio-management.md)
- store 同期と batch: [`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md)
