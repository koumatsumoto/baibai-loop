---
name: macro-context
description: 市場環境の評価（macro context report）を人間の判断のために書くときに使う。毎営業日の機械読み値（macro reading）は自動更新されるので、変化を確認したいだけならレポートは作らない。
---

# Macro Context

## 正本

最初に [`docs/reference/macro.md`](../../../docs/reference/macro.md) を読む。series・provider・reading の読み方、3 層構成（core / synthesis / connection）、8 分析レンズ、source tier、深度契約、record schema を skill へ再転記しない。

## Trigger

レポートは **1 種類・常に full 深度・人間の判断が起点**である。定例義務も更新義務もない。

- スポットの資産運用判断、または opportunity cycle（OP3）の前に、head が古い / 深度契約を満たさないと人間が判断したとき
- 米雇用統計の翌週など、環境認識を作り直す価値があると人間が判断したとき

「変化を確認したいだけ」なら `baibai-engine macro reading --asof <営業日>` を読み、レポートは作らない。「変化が小さい」ことを浅い分析の理由にもしない（書くなら深度契約を全項目満たす）。

## 手順

0. cloud 正本の store を読む前に pull する: `batch/scripts/r2_transfer.sh pull-machine` → `hydrate-market`（market / runs / macro。market store は hydrate まで通さないと lake 所有 17 table が空である。規律は ops-maintenance skill）。application DB は local が正本なので pull しない。
1. **前回の分析を見ずに起動する**（[`macro.md`](../../../docs/reference/macro.md) §分析の独立性）。`baibai-engine macro context head` で head ID を取り、trigger は**必ず投影して**読む。`triggers` の JSON / table は条件ごとに `event` と `view_change`（＝前回の結論そのもの）を含み、`fired` は property で serialize されないので、生出力を開くと前回の判断が context に入る。

   ```bash
   HEAD=$(uv run baibai-engine macro context head | sed 's/^context_id: //')
   uv run baibai-engine macro context triggers --context-id "$HEAD" --asof <date> --format json \
     | uv run python -c 'import sys,json;d=json.load(sys.stdin);print(d["context_as_of"], sum(r["status"]=="fired" for r in d["results"]))'
   ```

   **前回の synthesis・監視ポイント文言・scorecard 条件・確率は step 7 まで開かない**（開いてから「ゼロベースで確定した」と言えなくなる。順序が独立性を支える）。禁止は `context show` だけでなく**同じ内容へ至る全経路**に及ぶ — GitHub の issue / PR 本文とコメント、`reports/studies/` の分析記録、Baibai Loop の Macro タブ、read API、head を表示する他 skill の手順も含む（実際に、issue コメント経由の汚染で初回運転の担当を替えた例がある）。**汚染に気づいたら、その context では書かず、汚染のない別 session へ author を渡す**（気づかずに書くより安い）。書き直すかどうかは head の `as_of` と `fired` 件数だけで決める（`fired` は書き直しの根拠であって、今回の結論の前提ではない）。
2. `baibai-engine macro refresh <series...> --start <date> --end <asof>` で主要 series を直近窓ごと再取得し（**`--end` は必須**）、`macro reading --asof <営業日>` を**全系列読む**。`stale`・`insufficient_history`・`flags`・極端な `z_score` を先に把握し、`next_print_estimate` で判断・保有窓内の公表を確認する（data health の異常は解釈より先に扱う）。
3. **force 仮説を立てる**: reading の flags・|z| 極値・percentile 端・トレンド反転を束ね、8 分析レンズと突き合わせて「今の市場を動かす支配的な力」の候補を 2〜5 件名指しする。各候補について**支持する一次 source と反証する一次 source の両方**を web research で取得する（series range・単位・公表日・取得日を確認）。8 象限の被覆はこのリサーチと並行して満たす。WebSearch は日本語 query で unavailable になりやすい — 英語 query を先に試し、日本語一次資料は URL 直接 fetch で取る。**取得に失敗したら別 source を探す前に `curl` で 1 回試す**（WebFetch だけが 403 になる host が多い。実測表と BLS の代替経路は [`data-sources.md`](../../../docs/reference/data-sources.md) §取得失敗の切り分け）。
4. `screening market-snapshot` を**今回の as_of で**実行し、`inputs.machine_snapshots` の `MachineSnapshotInput` として引用する（自前の機械出力を articles に入れない。`snapshot_asof` が as_of の 7 日より古い snapshot は publish で拒否される）。
5. core 10 セクションを固定順で書く。**焦点 fact 規律**: fact_summary は判断を駆動する焦点 fact を先頭に、全 series の座標転記は末尾の座標 fact 1 件に隔離する。セクション 9 に stance・確度・反証条件と base / bear / bull（確率 0.05 刻み・合計 1.00・各 [0.05, 0.90]、scorecard 条件 2 件以上/シナリオ）、セクション 10 に監視ポイント（機械で測れる無効化条件は `machine_conditions` へ。セクション全体で最低 1 件が publish 要件）。
6. **synthesis を書く**: force 仮説のうち伝達チャネル 2 つ以上への波及を一次情報と series で実証できたものだけを dominant force として確定する（機序・伝達経路・counter_evidence・方向・確度）。名指しした各セクションへ**相異なる系列を 1 つずつ**割り当てられる引用にする。力同士の相互作用を最低 1 件、宣言済み force 2 件以上の名指しで書く。
7. **前回 scorecard の採点を接続する**: 今回の評価をゼロベースで確定した**後に**、ここで初めて `baibai-engine macro context show --context-id <前回id> --asof <今回asof>` で前回レポートを開く。続けて `baibai-engine macro context scorecard --context-id <前回id> --asof <今回asof> --format json` で照合する。run 証明のエラーは評価窓を覆う `macro refresh <series...> --start <前回as_of翌日> --end <今回asof>` で解消する（`pending` だけなら refresh 不要）。出力の `machine_snapshot` を**逐語で** inputs へ引用し（command を手書きしない — 絶対 path の完全一致を要求される）、`input_id` をレジーム要約の `previous_scorecard_snapshot_id` へ置き、確率と成立実績の噛み合いを `previous_scorecard_review` へ書く。
8. connection を core から導出する: research 優先度ヒント（`applies_to` で判別可能に）・sector tilt・sizing caution・`bargain_topography`（market-snapshot input 引用が必須）・`estimate_caveats`（`affected_component` + `applies_to` 付きで 1 件以上）。引用できる series は core が引用済みのものだけ。
9. 個別 thesis のどの scenario / claim を変えるかを 1〜3 行で示す。
10. publish 前に敵対的 self-check を通す:
    - (a) 各 judgment が引用 fact の数値と整合するか（数値⇄結論を突合）
    - (b) reading の機械的事実と結論が矛盾していないか。矛盾はどちらも盲信せず矛盾自体を judgment として書く
    - (c) 各 dominant force の counter_evidence が実在の一次情報・series に基づくか（形式的な反証でないか）
    - (d) 為替・金利の判断が両側リスクを持つか
    - (e) 各 research 優先度ヒントが候補タイプを判別できる識別力を持つか
    - (f) 各 fact の公表日が当該統計の最新公表か
    - (g) scorecard 条件が機械照合可能で、期限が「その系列がもう一度公表される」以降 18 か月以内か。同じ条件を 2 回書いていないか
    - (h) 機械で測れる監視条件が `machine_conditions` にもあるか（最低 1 件必須）。条件の series をそのセクションが引用しているか
    - (i) core・synthesis に日本株ループへの行動指示を書いていないか（schema は prose を止めない）
    - (j) 各 force が名指しした 2 つ以上のチャネルへ実際に波及しているか。1 チャネルの話を force に格上げしていないか
    - (k) 確率が stance・本文・監視条件と整合し、合計 1.00 か。「当てにいく数字」でなく「見立ての強さの正直な明示」か
    - (l) estimate_caveats が現 regime の実際の歪みを指しているか（毎回書ける一般論は caveat ではない）。該当 component の検算内容が具体的か
    - (m) bargain_topography が market-snapshot の数値（breadth・regime・業種騰落）に接地しているか
    - (n) 焦点 fact が各セクション先頭にあり、網羅転記が末尾 1 件に隔離されているか
    - (o) 自前の機械出力を `inputs.articles` に入れていないか（`machine_snapshots` が正しい枠）
    - (p) [深度契約](../../../docs/reference/macro.md#depth-contract)を 1 項目ずつ機械的に照合したか: 8 象限・外部記事 15 本以上（Tier-1 中心）・日本需要の必須系列（実質賃金または実質消費、鉱工業生産）・通商政策と地政学 tail・日本株バリュエーションアンカー（益回り − JGB 10y）

    fail 項目は修正してから進む。
11. **独立レビューを通す**（`research` skill step 5 の独立反証と同じ役割分離）。self-check は自己申告なので、**author と別 role** のレビュアが draft と引用 source を `inputs` → `fact_summary` → `judgment` → `synthesis` / `summary` → `connection` の順に縦読みし、次を必須反証にする:
    1. **qualifier carry-through**: fact 層で「一次情報で未確認」「残差不明」「推定」と書いた事象を、`summary` / `synthesis` / 各 `judgment` で確定事実として断定していないか
    2. **量の基準**: real / nominal・stock / flow・水準 / 変化・観測 / 期待を混ぜて比較していないか（[`anti-patterns.md`](../../../docs/anti-patterns.md) AP-12）
    3. **窓の含意**: percentile を引用した系列の `window_years` が 3 年か 10 年かを解釈へ反映しているか（3 年窓の同じ数字は 10 年窓より弱い含意しか持たない）
    4. **消えたものの明示**: 前回 head の dominant force / セクション judgment のうち今回消滅・demote したものを名指しし、理由を書いたか（ゼロベース評価で力が入れ替わるのは正常だが、黙って落とすと narrative drift が見えない）
    5. **standing exposure の監視面**: `machine_conditions` に `usd_jpy` が最低 1 件、原則として `jp.10y` も 1 件あるか（テーマが demote されても構造 exposure の機械監視は維持する）

    構造 validator と `publish --check` が `ok` を返すことを semantic pass に数えない（両方 ok の draft が上記 1・2 を含んでいた実例がある）。**レビューは別 session / subagent へ委譲する**（AGENTS.md の subagent 規律。同一 context で role を宣言し直しても self-check にしかならない）。レビュアには draft と引用 source だけを渡し、author の判断理由を渡さない。指摘が出たら draft へ戻す。**再レビューは直した箇所と上記 5 項目に限り、2 巡しても収束しなければ publish せず人間へ上げる**（ラウンドを無制限に回さない）。
12. **publish**: `inputs.indicator_series` は手書きせず `baibai_engine.macro.context.scaffold_inputs`（セクション → series の spec から生成）を使う。draft の反復中は `baibai-engine macro context publish <draft> --check` で store に触れず検証し（--check は文書契約と gate のみ。reading rules revision の実在・compare-and-swap・前回 scorecard digest 照合は実 publish でだけ検証される）、確定したら確認済み head を `--expected-head` へ渡して publish する（head が無い初回だけ省略）。
13. **cloud 反映**: `batch/scripts/r2_transfer.sh push-app` → `gh workflow run cloud-materialize` → success 確認（Baibai Loop の Macro タブが新 head を配信する）。

## 禁止

行動指示（売買タイミング・現金比率・配分指示）、candidate hard gate、sector 自動 tilt、統計的 edge、個別 sizing を出さない。確率を統計的優位・自動 sizing の根拠として扱わない。screening の機械 ranking を macro で変更しない。過去 revision の分析・結論・tilt を前提にしない（過去の客観的事実と前回 scorecard の成立実績は前提にしてよい）。

## 手順自体の改善

使うたびにこの手順のテストになる。reading の閾値・実効窓（`method/macro/reading/`）が実データと噛み合わない、深度契約が判断に対して不足する、self-check が素通りする穴がある——を見つけたら、レポートを書き終えた後に self-contained issue として起票する（この節に作業メモを溜めない）。
