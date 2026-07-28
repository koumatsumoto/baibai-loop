---
name: macro-analysis
description: 市場環境の評価（macro context report）を人間の判断のために書くときに使う。毎営業日の機械読み値（macro reading）は自動更新されるので、変化を確認したいだけならレポートは作らない。
---

# Macro Analysis

## 正本

最初に[`docs/workflow/macro.md`](../../../docs/workflow/macro.md)を読む。series、provider、readingの読み方、3層構成（core / synthesis / connection）、8分析レンズ、source tier、深度契約、record schemaをskillへ再転記しない。

## Trigger

レポートは**1種類・常にfull深度・人間の判断が起点**である。定例義務も更新義務もない。

- スポットの資産運用判断、またはopportunity cycle（OP3）の前に、head レポートが古い / 深度契約を満たさないと人間が判断したとき
- 米雇用統計の翌週など、環境認識を作り直す価値があると人間が判断したとき

「変化を確認したいだけ」ならレポートを作らない: `baibai-engine macro reading --asof <営業日>`が毎営業日の機械読み値を出す。「変化が小さい」ことを浅い分析の理由にもしない（書くなら深度契約を全項目満たす）。

## 手順

1. `baibai-engine macro context head`で現行 head を確認し、あれば`context show --latest --asof <date>`で`as_of`・監視ポイント・**前回のscorecard条件と確率**を読み、`context triggers --context-id <head> --asof <date>`で**前回の無効化条件が満たされているか**を機械照合する（`fired`は書き直しの根拠であって、今回の結論の前提ではない）。head が無ければ最初のrevisionとして書く。
2. `baibai-engine macro refresh`で判断に使う主要seriesを直近窓ごと再取得し、`baibai-engine macro reading --asof <営業日>`を**全系列読む**。`stale`・`insufficient_history`・`flags`・極端な`z_score`を先に把握し、`next_print_estimate` / `print_due_in_days` で判断・保有窓内に近い公表を確認する（ここで見えるdata healthの異常は、以降の解釈より先に扱う。公表目安は event calendar ではない）。
3. **force 仮説を立てる**: reading の flags・|z| 極値・percentile 端・トレンド反転を束ね、8分析レンズと突き合わせて「今の市場を動かす支配的な力」の候補を 2〜5 件名指しする。各候補について**支持する一次sourceと反証する一次sourceの両方**を決めて取得する（series range、単位、公表日、取得日を確認）。8象限の被覆はこのリサーチと並行して満たす。
4. `screening market-snapshot`を**今回の as_of で**実行し、**`inputs.machine_snapshots` の `MachineSnapshotInput` として引用する**（自前の機械出力を article に入れない。発行者も URL も無く、コマンドと日付が identity である。`snapshot_asof` が as_of の 7 日より古い snapshot は publish で拒否される——前回 draft からの持ち越しは接地にならない）。
5. core 10セクションをworkflowの固定順で書く。各セクションでseries・fact・judgment・経済経路への接続を分ける。**焦点 fact 規律**: fact_summary は判断を駆動する焦点 fact（1 fact = 1 観察）を先頭に置き、全 series の座標の網羅転記は末尾の座標 fact 1 件に隔離する。セクション9でリスク選好環境の評価（stance・確度・**反証条件**）とbase/bear/bullを置き、各シナリオに**確率（0.05刻み・合計1.00・各[0.05,0.90]）**と機械照合可能な観測条件（series_id・比較演算・閾値・期限日）2件以上を付ける。確率はstance・シナリオ本文と整合させる（neutralなのにbase 0.85のような不整合を書かない）。セクション10の監視ポイントは、**機械で測れる無効化条件を`machine_conditions`（series_id・比較演算・閾値。期限は持たない）に書く**（セクション全体で最低1件はpublishの要件。平均・持続を含む条件は単発クロスの proxy 閾値で書く）。
6. **synthesis を書く**: 手順3の force 仮説のうち、伝達チャネル2つ以上への波及を一次情報とseriesで実証できたものだけをdominant forceとして確定する（機序・伝達経路・**counter_evidence**・方向・確度）。引用できるseriesは名指ししたセクションが引用済みのものだけで、**名指しした各セクションへ相異なる系列を1つずつ割り当てられる引用**にする（複数セクションが共有する1系列だけでは経路横断の実証にならず publish で拒否される）。力同士の相互作用（同時成立が生むjoint risk）を最低1件、宣言済みforceを2件以上名指しして書く。
7. **前回scorecardの採点を接続する**: 今回の評価をゼロベースで確定した**後に**、`baibai-engine macro context scorecard --context-id <前回id> --asof <今回asof> --format json`で前回条件をL1履歴と照合する。settlement watermark / provider run / staleness error があれば採点不能を解消してから進む（run 証明のエラーは評価窓を覆う `macro refresh <series...> --start <前回as_of翌日>` で引き直すのが通常の解消。証明が要るのは成立・期限切れの条件だけで、`pending` だけなら refresh 不要）。出力の`machine_snapshot`をinputsへ引用し、その`input_id`をレジーム要約の`previous_scorecard_snapshot_id`へ置き、結果を`previous_scorecard_review`へ書く。**前回レポートに確率があれば、成立実績と置いた確率の噛み合い（当たり外れの度合い）まで書く**（当たり外れの事実だけを書き、今回の解釈の前提にしない）。前回レポートがなければその旨を書く。
8. connectionセクションをcoreから導出する: research優先度ヒント（どの候補タイプ・sectorに効くかを`applies_to`で判別可能に）、sector tilt、sizing caution。**`bargain_topography`**（この局面でミスプライスがどこに・なぜ出やすいか）をmarket-snapshot inputを引用して書き、**`estimate_caveats`**（今の環境がFV anchor / reversion / carry / 耐性判定をどの向きに歪めるか）を1件以上、`affected_component`と`applies_to`付きで書く。引用できるseriesはcoreが引用済みのものだけで、依拠するcoreセクションを明示する。
9. 個別thesisのどのscenario/claimを変えるかを1〜3行で示す。
10. publish前に敵対的self-checkを通す:
    - (a) 各judgmentが引用factの数値と整合するか（数値⇄結論を突合）
    - (b) **readingの機械的事実と自分の結論が矛盾していないか**。矛盾する場合はどちらも盲信せず、矛盾自体をjudgmentとして書く（機械読み値を無言で無視しない）
    - (c) 結論を反証する系列を実際に読んだか。**各dominant forceのcounter_evidenceが実在の一次情報・seriesに基づくか**（形式的な反証を書いていないか）
    - (d) 為替・金利の判断が両側リスクを持つか
    - (e) 各research優先度ヒントが候補タイプを判別できる識別力を持つか
    - (f) 各factの公表日が当該統計の最新公表か
    - (g) scorecard条件が機械照合可能で、期限日が「その系列がもう一度公表される」以降18か月以内か。同じ条件を2回書いていないか
    - (h) 各監視ポイントの`condition`が機械で測れる形なら`machine_conditions`にも書いてあるか（セクション全体で最低1件は必須）。条件のseriesをそのセクションが引用しているか
    - (i) coreのjudgment・fact要約に日本株ループへの行動指示（買え・売れ・sizeを落とせ）を書いていないか。**synthesisにも書いていないか**（synthesisはuse-case agnosticであり、schemaはproseを止めない）
    - (j) **各dominant forceが名指しした2つ以上のチャネルへ実際に波及しているか**（片方のセクションに根拠が無い名指しは装飾）。1チャネルの話をforceに格上げしていないか
    - (k) **確率がstance・シナリオ本文・監視条件と整合するか**。3件の合計が1.00か。確率を「当てにいく数字」でなく「見立ての強さの正直な明示」として置いたか
    - (l) **estimate_caveatsが現regimeの実際の歪みを指しているか**（毎回書ける一般論は caveat ではない）。該当componentの機械見積りを使うresearchが、この注意で具体的に何を検算すべきか分かるか
    - (m) **bargain_topographyがmarket-snapshotの数値（breadth・regime・業種騰落）に接地しているか**。外部記事の相場観の転写になっていないか
    - (n) **焦点 fact が各セクションの先頭にあり、数値の網羅転記が末尾の座標 fact 1 件に隔離されているか**（判断を駆動する数値が壁に埋もれていないか）
    - (o) 自前の機械出力（market-snapshot・scorecard）を`inputs.articles`に入れていないか（`machine_snapshots`が正しい枠）
    - (p) **深度契約チェックリストを 1 項目ずつ突合したか**: 8 象限すべてに fact があるか、外部記事 15 本以上か、日本需要の必須系列（実質賃金または実質消費、**鉱工業生産**）が入っているか、**通商政策（関税）** と地政学 tail の fact がセクション 4/5 にあるか、日本株バリュエーションアンカー（益回りと JGB 10y の対比）が明示されているか。印象で「満たしているはず」とせず、[深度契約](../../../docs/workflow/macro.md#depth-contract)の箇条書きに対して機械的に照合する（publish gate は presence しか測れず、被覆の欠落は self-check でしか捕まらない）

    fail項目は修正してから進む。
11. strict contractを満たすdraftを作る。`inputs.indicator_series`は手書きせず`tools/scaffold_macro_context_inputs.py`（セクション→series の spec から provider・観測日・vintage・実効窓を L1 store 由来で生成）を使い、draft の反復中は`baibai-engine macro context publish <draft> --check`で store に触れずに契約と gate を検証する。確定したら、確認したheadを`--expected-head`へ渡して`baibai-engine macro context publish`する。head が無いときだけ`--expected-head`を省略する。

## 禁止

行動指示（売買タイミング・現金比率・配分指示）、candidate hard gate、sector自動tilt、統計的edge、個別sizingを出さない。core・synthesisに日本株ループ固有の指示（sector tilt・research優先度・sizing caution）を書かない。**確率を統計的優位・有意性・自動sizingの根拠として扱わない**（主観ウェイトの明示と後からの採点可能性が目的）。screeningの機械rankingをmacroで変更しない。HTMLを必須成果物にしない。過去revisionの分析・結論・tiltを前提にしない（過去の客観的事実と前回scorecardの成立実績は前提にしてよい）。

## 手順自体の改善

使うたびにこの手順のテストになる。reading の閾値・実効窓（`method/macro-reading/`）が実データと噛み合わない、深度契約が実際の判断に対して不足している、self-checkが素通りする穴がある——を1件でも見つけたら、レポートを書き終えた後に[`improvement-loop`](../improvement-loop/SKILL.md)でissue化する。この節に作業メモを溜めない。
