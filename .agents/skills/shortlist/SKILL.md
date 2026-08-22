---
name: shortlist
description: screening から review 済み shortlist を発行し、人間の primary-research set 選択を待つ。選択後は research skill。
---

# Shortlist

全上場銘柄から比較可能な候補を作る。終端は shortlist の発行と、人間による primary-research set の選択である。AI は broker 操作へ進まない。

## 前提

1. AGENTS.md に従い opportunity session を start または resume する。
2. canonical ledger から holding、reservation、cash を読む。
3. macro context head が古い、または[深度契約](../../../docs/reference/macro.md#depth-contract)を満たさないなら、先に `macro-context` skill を実行する。
4. 永久損失、5年期待値と FV 乖離、portfolio 追加価値、購入可能性の順に比較する。cash、集中、保有は annotation であり hard 除外条件ではない。

## 手順

1. **同期と preflight**

   market / runs / macro は `ops-maintenance` skill に従って cloud 正本を同期し、market を hydrate する。batch 走行中は pull しない。

   ```bash
   shortlist_preflight_dir=$(mktemp -d /tmp/baibai-shortlist-preflight.XXXXXX)
   batch/scripts/r2_transfer.sh pull-run-summary "$shortlist_preflight_dir/latest-run.json"
   uv run baibai-engine screening shortlist preflight \
     --asof <ASOF> --cloud-summary "$shortlist_preflight_dir/latest-run.json"
   ```

   preflight の指示に従う。`reuse` は既存 selection を読む。`resume-current-code` は select だけ、`rerun-current-code` は run と select を各1回実行する。`ambiguous` は previous run ID を指定して再判定する。`blocked`、HEAD / as-of drift、failed cloud run は解消するまで新しい run を作らない。同一 as-of の試行錯誤で retention を消費しない。

   ```bash
   uv run baibai-engine screening verify-cache-coverage --asof <ASOF>
   # 不足時だけ: uv run baibai-engine screening bootstrap-cache --asof <ASOF>
   uv run baibai-engine screening extract-edinet-metrics --asof <ASOF>
   ```

   future-dated / stale JPX は停止する。historical backfill 以外で `--allow-stale-jpx` を使わない。

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

   supply と breadth は別軸で報告し、`unmeasured` を 0 や異常なしへ変換しない。前回 shortlist と new / continued / exited を比較し、continued も再評価する。previous source が違う overlap を相互比較せず、`null` を重なり 0 と解釈しない。

4. **候補を review する**

   selected 候補の material disclosure を一次情報で確認し、OP3 narrative の `why`、`counter`、`what_changes_mind` を書く。次の annotation を必ず消化する。

   - FV convergence、full-year loss、stale financials、data-quality / durability warning は、見積りをどちらへ歪めるかを書く。
   - margin、capital-control、大量保有、TOB、buyback filing は観測文脈であり、単独で除外や rank 変更に使わない。`null` / `unknown` を否定事実へ変換しない。
   - TOB は届出書と意見表明を読み、価格収斂を割安と誤認しない。buyback carry は株数枠と金額枠の小さい残り、取得期間、直近取得、取得目的を一次開示で確認する。
   - 配当 carry の異常な跳ねは普通配当と特別配当、split basis、FCF coverage を確認する。special gain を反復収益にしない。
   - `deterioration_unmeasurable` は「悪化なし」ではない。一次開示で補う。
   - `next_earnings_status` の scheduled / estimated / unknown を区別し、直前の前倒し開示は一次情報で確認する。
   - 既に reject / defer した ticker は、価格が FV 以下、新規 material disclosure、unknown を解く決算、thesis entry から raw close が5%以上下落、新しい機械財務のいずれかがある場合だけ再研究候補にする。その他は `event_wait` とする。
   - E[r] 履歴帯は集団の記述統計としてだけ使い、個別予測にしない。
   - 機械 E[r] 順から rank を変える場合は理由を書く。rejected 全件に具体的理由と `reject_class` を付ける。

   selected は、upside の希望的前提を除いても期待値が正、利益が peak extrapolation でない、catalyst が日付または観測条件を持つ、現金保有より優れる理由がある、該当する macro connection を消化済み、の全条件を満たすことを確認する。

5. **発行と完了**

   [`assets/draft-template.yaml`](./assets/draft-template.yaml) を埋め、source `selection_id` に束縛して発行する。

   ```bash
   uv run baibai-engine screening shortlist publish <draft>
   ```

   narrative を書ける entry だけを selected にする。件数の下限はなく、0件も正常である。`er_annual` は publisher が bound run から焼き込むため draft に書かない。発行後、全 entry の E[r] と bound run を照合し、dated follow-up だけを重複確認後に task 化する。

   selected があれば checkpoint を更新して人間の選択を待つ。0件なら canonical shortlist を証拠に `completion_reason: no-shortlist-selection` で session を complete する。cloud 反映は `ops-maintenance` skill の application store 手順に従う。

## 停止条件

- operation session、canonical ledger、必要 store、previous identity の矛盾。
- future / stale coverage、preflight `blocked`、判断根拠を失う unresolved annotation。
- 人間の選択なしで research または broker 操作へ進むこと。

## 正本

- 判断境界と CLI: [`screening-runtime.md`](../../../docs/reference/screening-runtime.md)
- macro depth: [`macro.md`](../../../docs/reference/macro.md)
- 資本と ranking: [`portfolio-management.md`](../../../docs/portfolio-management.md)
- store 同期と batch: [`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md)
