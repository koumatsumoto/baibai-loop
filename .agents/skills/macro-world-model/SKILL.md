---
name: macro-world-model
description: Stage A pilot で、prior-blind な world model workspace を先に固定し、同じセッションで macro context v4 を導出・publish するときに使う。
---

# Macro World Model

## 正本

最初に [`docs/reference/macro.md`](../../../docs/reference/macro.md)、[`docs/anti-patterns.md`](../../../docs/anti-patterns.md)、[`macro-context` skill](../macro-context/SKILL.md) を全文読む。store 同期は [`ops-maintenance` skill](../ops-maintenance/SKILL.md) に従う。Stage A の workspace と補助 tooling は `tools/experiments/macro_world_model/` に置かれ、production schema・read API・UI を構成しない。

## Trigger

人間が Stage A pilot の full revision を起動したときに使う。cadence は prior-blind な full revision だけであり、delta revision と update locality の測定を行わない。World Model pilot を使わない macro context 執筆は `macro-context` skill が担当する。

## Workspace

1 cycle は `reports/studies/<pilot>/cycles/<as_of>/` に置く。blind freeze の対象は次の 7 ファイルである。

- `charter.yaml`
- `evidence-snapshot.json`
- `evidence-packs.yaml`
- `states.yaml`
- `hypotheses.yaml`
- `evidence-matrix.yaml`
- `world-model.yaml`

同じ `as_of` の cycle directory または生成先が存在するときは上書きせず停止する。preflight、freeze 後の前回 head、scorecard、revision diff、v4、publish、cost、2 回目実行確認は同じ cycle directory に置けるが、blind freeze の対象へ混ぜない。

cycle 3 以降の machine contract は `tools/experiments/macro_world_model/README.md` の version 2
を使う。cycle 1 / 2 の frozen version 1 artifact は履歴として書き換えない。

## 手順

### 0. Prior-blind preflight

1. `batch/scripts/r2_transfer.sh pull-machine` で market / runs / macro store を同期する。application DB は local 正本なので pull しない。
2. `as_of` を市場データの最終完全営業日に固定する。
3. `baibai-engine macro context head` で head ID を得る。`context show` の出力は parser へ直接渡し、`as_of` だけを取り出す。本文、synthesis、connection、monitoring point の文言、scorecard 条件、確率を表示・保存しない。
4. `baibai-engine macro context triggers --context-id <head> --asof <as_of> --format json` の出力は parser へ直接渡し、`context_as_of` と `status == "fired"` の件数だけを取り出す。event、view_change、series、threshold を表示・保存しない。
5. go / no-go は head の `as_of` と fired が 1 件以上あるかだけで決める。head が判断時点に対して古い、または fired があるときに進む。それ以外の前回情報を理由にしない。人間が明示した trigger と矛盾するときは停止して確認する。

preflight の保存物は head ID、head `as_of`、評価 `as_of`、fired の有無と件数、go / no-go、理由だけを持つ。この時点では前回レポートを開かない。

### 1. Charter と coverage scan

1. `charter.yaml` に central questions をちょうど 6 件、`now / 0_3m / 3_12m / 12_24m` の horizon、conditioning assumption を書く。
2. standing questions は portfolio の構造 exposure から導出し、円、JGB / 割引率、米需要 / AI capex、中国 proxy、energy を最低限含める。charter は pilot の study directory に置く。
3. `macro reading --format json` を全系列について取得する。coverage config には standing coverage、analyst addition、各候補の selected / excluded と除外理由だけを書く。
4. 次を実行し、reading と L1 vintage を結合する。

```bash
python -m tools.experiments.macro_world_model.build_evidence_snapshot \
  --asof <as_of> \
  --previous-asof <head_as_of> \
  --macro-db stores/macro/macro.sqlite \
  --coverage-config <cycle>/coverage-config.yaml \
  --output <cycle>/evidence-snapshot.json
```

head `as_of` を取得できないときだけ `--previous-asof` を省略し、builder の 90 日 fallback を使う。

scan は全系列の stale / insufficient_history / flags / z_score / percentile、release 間変化、同一 `observed_at` の複数 vintage による revision を含む。snapshot に保存する revision は観測日が `as_of` から 24 か月以内かつ revision vintage が cycle 窓内のものに限り、series ごとに vintage 降順 20 件を上限として超過数を `revisions_truncated` に残す。リポジトリ内 source による派生再計算は `derived_recompute: true` として読む側が割り引けるようにする。この保存窓は `recent_revision` の materiality 判定を変えない。materiality 候補は deterministic rules、standing coverage、analyst addition の和集合である。除外には具体的な理由が必要であり、data health の異常を経済解釈より先に解決する。

snapshot v2 は reading の `window_years` / `window_observations` / `expected_observations`、
`statistic` / `statistic_unit` / `statistic_value`、`next_print_estimate` /
`print_due_in_days` を各系列に保持する。raw `unit` と `statistic_unit` を同じものとして扱わず、
欠落は builder error として解消してから進む。

### 2. Evidence packs と states

1. selected coverage を block に分け、一次 source を中心に支持証拠と反証証拠を集める。一次 source の最新公表日、観測期間、単位、取得日を確認する。Tier 1 が継続的に取得困難な系列だけ `docs/reference/data-sources.md` の Tier 2 例外を使う。
2. `evidence-packs.yaml` の `external_sources` に evidence ID、source tier、publisher、title、published_at、URL を記録する。各 pack は block、`evidence_for`、`evidence_against`、`open_questions` を持ち、selected series と使用する外部 source を漏れなく参照する。
3. `states.yaml` は geography / block ごとに reference frame を明示し、各 horizon について level / momentum / acceleration / breadth / persistence を分ける。evidence for / against を両方引用し、uncertainty を data / state / structural / policy に分解する。

### 3. Competing hypotheses と horizon paths

1. `hypotheses.yaml` に競合仮説を 2〜3 件置く。各仮説は mechanism summary、plausibility rank、evidence for / against、unexplained residuals、required assumptions、discriminating signposts、invalidation を持つ。
2. `evidence-matrix.yaml` で同じ evidence を全仮説に対して `supports / contradicts / mixed / neutral` のいずれかで評価する。
3. `world-model.yaml` の baseline path を `now / 0_3m / 3_12m / 12_24m` ごとに書く。scenario は mechanism を名前に含め、initial shock、persistence、propagation delta、policy reaction と growth / inflation / rates / credit / fx / balance sheet の path、signposts、invalidation、known omissions を持つ。cycle 3 以降は scenario をちょうど 3 件とし、各 scenario に `state_ids` / `hypothesis_ids` / `node_ids` / `edge_ids` / `evidence_ids` も持たせ、参照 edge の両 endpoint を同じ `node_ids` に含める。
4. key judgments は 3〜5 件に絞り、state、horizon、競合仮説、current-cycle evidence に接続する。
5. `world-model.yaml` の key judgment、scenario、baseline path など owner が読む summary 系 field は日本語で書く。workspace の中間的な分類・識別子・検査用 field は英語でよい。

### 4. Sparse graph と blind freeze

graph は最後に書く。node は 20 以下、edge は 30 以下に固定する。node は economy-level の状態・機構・結果として命名し、portfolio、保有、配分、cashflow など portfolio 固有の語彙を使わない。全 edge は `relation_kind`、`claim_strength`、`sign`、lag の最小・最大月数、今 cycle の evidence ID、観測可能な falsifier を持つ。falsifier は series または公表 event を固有名で指す。

`relation_kind` は関係の根拠に合わせる。`accounting_identity` は会計恒等と機械的換算だけに使い、valuation の DCF・multiple は `model_based_relation`、需要から売上などの行動的伝達は根拠に応じて `externally_identified_empirical_relation`、`internal_observational_association`、`judgmental_hypothesis` のいずれかにする。`claim_strength: identified` は identity または外部で同定済みの関係だけに使う。

workspace を検査し、create-only で freeze を作る。

```bash
python -m tools.experiments.macro_world_model.validate_world_model freeze \
  <cycle> \
  --output <cycle>/blind-freeze.json
```

freeze 成功後は対象 7 ファイルを編集しない。必要な修正が見つかった場合は publish へ進まず、別 cycle として最初から full revision を行う。

### 5. Freeze 後の scorecard settle と revision diff

ここで初めて前回 head を開く。

1. `baibai-engine macro context show --context-id <head> --asof <as_of>` で前回 report を開く。blind workspace には保存しない。
2. `baibai-engine macro context scorecard --context-id <head> --asof <as_of> --format json` を実行する。run 証明が不足する error のときだけ、条件 series を `macro refresh <series...> --start <前回as_of翌日> --end <as_of>` で取得して再実行する。`pending` だけなら refresh しない。
3. scorecard の `machine_snapshot` を逐語で v4 inputs に引用し、met / not_met / pending の内訳と、確率・成立実績の対応を書く。
4. frozen world model と前回 report を比較し、`revision-diff.yaml` に `schema_version: 1` と cycle と同じ `as_of` を置き、evidence-backed な追加・変更を `area` と `summary` で記録する。前回の dominant force / key judgment のうち消滅または demote したものは `dropped_or_demoted` に対象と理由を必ず記録する。diff は frozen workspace を変更しない。

### 6. 同一 session の v4 導出と publish

1. core 10 section の facts / judgment は states から、synthesis forces は key judgments から導出する。
2. risk environment の base / bear / bull は world model scenarios から導出し、ここで v4 probability trio を 0.05 刻みで付ける。world model の plausibility rank と probability 順を一致させ、`v4-projection.yaml` に対応を記録する。world model の adverse scenario は bear の複合条件または bear scorecard の signpost として必ず保存する。inverse mapping を使うときは、v4 trio に含まれない adverse tail を top-level `projection_note` に明示する。
   cycle 3 以降の projection は、one-page または v4 の material claim ごとに upstream state /
   hypothesis / judgment / scenario / node / edge / evidence、source qualifier と量の基準・unit、全 target
   field path、output qualifier を version 2 の shape で列挙する。state を使う claim は data / state /
   structural / policy の4 uncertaintyを source と output の両方へ残す。evidence entry は使う
   `measure` と `units` を明記する。external evidence の量がない prose は unit `not-applicable`、
   snapshot evidence は README の有限な measure path と、各 path に対応する raw unit・statistic
   unit・reading metadata の固定 unit を使う。
3. connection は core から書く。world model から直接、売買 timing、配分、個別 sizing を出さない。
4. freeze と確率順を再検査する。

```bash
python -m tools.experiments.macro_world_model.validate_world_model check \
  <cycle> \
  --freeze <cycle>/blind-freeze.json \
  --v4-projection <cycle>/v4-projection.yaml \
  --v4-document <cycle>/final-v4.yaml \
  --revision-diff <cycle>/revision-diff.yaml
```

version 2 check は judgment-bearing target の未 mapping、selected evidence の未使用、全 consumer から
未参照の graph node / edge、edge より強い claim、qualifier / uncertainty の弱化、real / nominal・
stock / flow・observation / expectation・unit の不一致を拒否する。schema pass は prose の semantic
pass ではないので、独立 reviewer は evidence → state → hypothesis / edge → one-page → v4 / connection
を縦に読み、宣言と実際の文言が一致することを別に確認する。
`final-v4.yaml`、projection、revision diff のいずれかを編集した後は check を再実行し、publish
直前の内容で pass させる。

5. [`macro-context` skill](../macro-context/SKILL.md) の step 8 以降へ合流し、connection、thesis impact、self-check (a)–(p)、`scaffold_inputs`、publish をすべて満たす。外部記事 15 本以上、8 象限、日本需要、通商・地政学・energy、日本株益回り − JGB 10y、market snapshot、scorecard settle、connection の要件を省略しない。monitoring の `machine_conditions` には `usd_jpy` を最低 1 件置き、原則として `jp.10y` も置く。zero-base の評価がテーマを demote しても、standing exposure の機械監視面は維持する。
6. draft は `baibai-engine macro context publish <draft> --check` で反復する。確定時に `macro context head` を再取得し、確認した ID を `--expected-head` に渡して実 publish する。
7. `batch/scripts/publish.sh push-app` を実行する。この command が返す cloud-materialize run URL を追跡し、追加 dispatch は行わない。workflow success、新 context ID が head であること、Macro tab が新 head を配信することを確認する。

### 7. Render、cost、2 回目実行確認

one-page を owner 向けの日本語で create-only に生成する。

```bash
python -m tools.experiments.macro_world_model.render_report \
  <cycle> \
  --freeze <cycle>/blind-freeze.json \
  --revision-diff <cycle>/revision-diff.yaml \
  --output <cycle>/one-page.md
```

`cost.yaml` には pass ごとの開始・終了・wall-clock、pass 数、v4 導出部分、取得できるときは token を記録する。初回 cycle の実測は次 cycle の予算 baseline になる。

publish 後、同じ手順を同じ `as_of` でもう一度なぞり、次を実測して `second-run-check.md` に記録する。

- 同じ生成先への snapshot、freeze、render が既存 artifact の上書きを拒否する。
- 同じ安定した store input から別の一時出力へ snapshot を連続 2 回作り、canonical hash が一致する。初回 freeze 後に scorecard proof refresh で store input が増えた場合は、初回 hash との差を非決定性とせず input mutation として別に記録する。
- publish 前に使った `--expected-head` でもう一度同じ draft を publish すると CAS が拒否する。

一時出力は material な証拠だけ study artifact へ転記し、生成物自体は commit しない。

## 完了

cycle workspace、blind-freeze、scorecard、revision diff、v4 draft / final、publish output、one-page、cost、2 回目実行確認を study directory に保存する。application DB の新 context ID が local と cloud の head であり、cloud-materialize が success のとき完了する。v4 は publish され続け、downstream contract を変更しない。
