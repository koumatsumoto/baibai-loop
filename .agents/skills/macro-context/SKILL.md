---
name: macro-context
description: 市場環境の評価（macro context report）を人間の判断のために書くときに使う。毎営業日の機械読み値（macro reading）は自動更新されるので、変化を確認したいだけならレポートは作らない。
---

# Macro Context

## 正本

最初に [`docs/reference/macro.md`](../../../docs/reference/macro.md) を読む。series・provider・reading の読み方、3 層構成（core / synthesis / connection）、8 分析レンズ、source tier、深度契約、record schema を skill へ再転記しない。

`macro context` / `macro reading` は `baibai-engine macro --help` の choices に出ないが動く（既知の hidden dispatch）。「`--help` と一致しない」ことを理由にここで停止しない。

## Trigger

レポートは **1 種類・常に full 深度・人間の判断が起点**である。定例義務も更新義務もない。

- スポットの資産運用判断、または opportunity cycle（OP3）の前に、head が古い / 深度契約を満たさないと人間が判断したとき
- 米雇用統計の翌週など、環境認識を作り直す価値があると人間が判断したとき

「変化を確認したいだけ」なら `baibai-engine macro reading --asof <営業日>` を読み、レポートは作らない。「変化が小さい」ことを浅い分析の理由にもしない（書くなら深度契約を全項目満たす）。

## 手順

1. `baibai-engine macro context head` で現行 head を確認し、あれば `context show --latest --asof <date>` で `as_of`・監視ポイント・前回の scorecard 条件と確率を読み、`context triggers --context-id <head> --asof <date>` で前回の無効化条件を機械照合する（`fired` は書き直しの根拠であって、今回の結論の前提ではない）。
2. `baibai-engine macro refresh <series...> --start <date> --end <asof>` で主要 series を直近窓ごと再取得し（**`--end` は必須**）、`macro reading --asof <営業日>` を**全系列読む**。`stale`・`insufficient_history`・`flags`・極端な `z_score` を先に把握し、`next_print_estimate` で判断・保有窓内の公表を確認する（data health の異常は解釈より先に扱う）。
3. **force 仮説を立てる**: reading の flags・|z| 極値・percentile 端・トレンド反転を束ね、8 分析レンズと突き合わせて「今の市場を動かす支配的な力」の候補を 2〜5 件名指しする。各候補について**支持する一次 source と反証する一次 source の両方**を web research で取得する（series range・単位・公表日・取得日を確認）。8 象限の被覆はこのリサーチと並行して満たす。WebSearch は日本語 query で unavailable になりやすい — 英語 query を先に試し、日本語一次資料は URL 直接 fetch で取る。
4. `screening market-snapshot` を**今回の as_of で**実行し、`inputs.machine_snapshots` の `MachineSnapshotInput` として引用する（自前の機械出力を articles に入れない。`snapshot_asof` が as_of の 7 日より古い snapshot は publish で拒否される）。
5. core 10 セクションを固定順で書く。**焦点 fact 規律**: fact_summary は判断を駆動する焦点 fact を先頭に、全 series の座標転記は末尾の座標 fact 1 件に隔離する。セクション 9 に stance・確度・反証条件と base / bear / bull（確率 0.05 刻み・合計 1.00・各 [0.05, 0.90]、scorecard 条件 2 件以上/シナリオ）、セクション 10 に監視ポイント（機械で測れる無効化条件は `machine_conditions` へ。セクション全体で最低 1 件が publish 要件）。
6. **synthesis を書く**: force 仮説のうち伝達チャネル 2 つ以上への波及を一次情報と series で実証できたものだけを dominant force として確定する（機序・伝達経路・counter_evidence・方向・確度）。名指しした各セクションへ**相異なる系列を 1 つずつ**割り当てられる引用にする。力同士の相互作用を最低 1 件、宣言済み force 2 件以上の名指しで書く。
7. **前回 scorecard の採点を接続する**: 今回の評価をゼロベースで確定した**後に** `baibai-engine macro context scorecard --context-id <前回id> --asof <今回asof> --format json` で照合する。run 証明のエラーは評価窓を覆う `macro refresh <series...> --start <前回as_of翌日> --end <今回asof>` で解消する（`pending` だけなら refresh 不要）。出力の `machine_snapshot` を**逐語で** inputs へ引用し（command を手書きしない — 絶対 path の完全一致を要求される）、`input_id` をレジーム要約の `previous_scorecard_snapshot_id` へ置き、確率と成立実績の噛み合いを `previous_scorecard_review` へ書く。
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
11. **publish**: `inputs.indicator_series` は手書きせず `tools/scaffold_macro_context_inputs.py`（セクション → series の spec から生成）を使う。draft の反復中は `baibai-engine macro context publish <draft> --check` で store に触れず検証し、確定したら確認済み head を `--expected-head` へ渡して publish する（head が無い初回だけ省略）。
12. **cloud 反映**: `tools/cloud/r2_transfer.sh push-app` → `gh workflow run cloud-materialize` → success 確認（Baibai App の Macro タブが新 head を配信する）。

## 禁止

行動指示（売買タイミング・現金比率・配分指示）、candidate hard gate、sector 自動 tilt、統計的 edge、個別 sizing を出さない。確率を統計的優位・自動 sizing の根拠として扱わない。screening の機械 ranking を macro で変更しない。過去 revision の分析・結論・tilt を前提にしない（過去の客観的事実と前回 scorecard の成立実績は前提にしてよい）。

## 手順自体の改善

使うたびにこの手順のテストになる。reading の閾値・実効窓（`method/macro-reading/`）が実データと噛み合わない、深度契約が判断に対して不足する、self-check が素通りする穴がある——を見つけたら、レポートを書き終えた後に self-contained issue として起票する（この節に作業メモを溜めない）。
