---
name: macro-analysis
description: 市場環境の評価（macro context report）を人間の判断のために書くときに使う。毎営業日の機械読み値（macro reading）は自動更新されるので、変化を確認したいだけならレポートは作らない。
---

# Macro Analysis

## 正本

最初に[`docs/workflow/macro.md`](../../../docs/workflow/macro.md)を読む。series、provider、readingの読み方、8分析レンズ、source tier、深度契約、record schemaをskillへ再転記しない。

## Trigger

レポートは**1種類・常にfull深度・人間の判断が起点**である。定例義務も更新義務もない。

- スポットの資産運用判断、またはopportunity cycle（OP3）の前に、head レポートが古い / 深度契約を満たさないと人間が判断したとき
- 米雇用統計の翌週など、環境認識を作り直す価値があると人間が判断したとき

「変化を確認したいだけ」ならレポートを作らない: `baibai-engine macro reading --asof <営業日>`が毎営業日の機械読み値を出す。「変化が小さい」ことを浅い分析の理由にもしない（書くなら深度契約を全項目満たす）。

## 手順

1. `baibai-engine macro context head`で現行 head を確認し、あれば`context show --latest --asof <date>`で`as_of`・監視ポイント・**前回のscorecard条件**を読む。head が無い（旧契約のrevisionだけがある）場合は新契約の最初のrevisionとして書く。
2. `baibai-engine macro refresh`で判断に使う主要seriesを直近窓ごと再取得し、`baibai-engine macro reading --asof <営業日>`を**全系列読む**。`stale`・`insufficient_history`・`flags`・極端な`z_score`を先に把握する（ここで見えるdata healthの異常は、以降の解釈より先に扱う）。
3. readingで見えた論点と8分析レンズから、確認すべき一次sourceを決めて取得する。series range、単位、公表日、取得日を確認し、結論を反証する系列も読む。
4. core 10セクションをworkflowの固定順で書く。各セクションでseries・fact・judgment・経済経路への接続を分け、セクション9でリスク選好環境の評価（stance・確度・**反証条件**）とbase/bear/bullを置く。各シナリオには機械照合可能な観測条件（series_id・比較演算・閾値・期限日）を2件以上付ける。深度契約（8象限被覆・Tier-1 15本以上・日本需要fact・円両側リスク・バリュエーションアンカー・energy/通商/地政学）を全項目満たす。
5. **前回scorecardの採点を接続する**: 今回の評価をゼロベースで確定した**後に**、前回条件をL1履歴で照合し、結果をレジーム要約の`previous_scorecard_review`へ書く（当たり外れの事実だけを書き、今回の解釈の前提にしない）。前回レポートがなければその旨を書く。
6. connectionセクションをcoreから導出する: research優先度ヒント（どの候補タイプ・sectorに効くかを`applies_to`で判別可能に）、sector tilt、sizing caution。引用できるseriesはcoreが引用済みのものだけで、依拠するcoreセクションを明示する。市場内部（`screening market-snapshot`）を引用してバーゲン地形を書く。
7. 個別thesisのどのscenario/claimを変えるかを1〜3行で示す。
8. publish前に敵対的self-checkを通す:
   - (a) 各judgmentが引用factの数値と整合するか（数値⇄結論を突合）
   - (b) **readingの機械的事実と自分の結論が矛盾していないか**。矛盾する場合はどちらも盲信せず、矛盾自体をjudgmentとして書く（機械読み値を無言で無視しない）
   - (c) 結論を反証する系列を実際に読んだか
   - (d) 為替・金利の判断が両側リスクを持つか
   - (e) 各research優先度ヒントが候補タイプを判別できる識別力を持つか
   - (f) 各factの公表日が当該統計の最新公表か
   - (g) scorecard条件が機械照合可能で、期限日がas_of以降か

   fail項目は修正してから進む。
9. strict contractを満たすdraftを作り、確認したheadを`--expected-head`へ渡して`baibai-engine macro context publish`する。新契約の初回publishはexpected headを省略する。

## 禁止

行動指示（売買タイミング・現金比率・配分指示）、candidate hard gate、sector自動tilt、統計的edge、個別sizingを出さない。coreセクションに日本株ループ固有の指示（sector tilt・research優先度・sizing caution）を書かない。screeningの機械rankingをmacroで変更しない。HTMLを必須成果物にしない。過去revisionの分析・結論・tiltを前提にしない（過去の客観的事実は前提にしてよい）。

## 手順自体の改善

使うたびにこの手順のテストになる。reading の閾値・実効窓（`method/macro-reading/`）が実データと噛み合わない、深度契約が実際の判断に対して不足している、self-checkが素通りする穴がある——を1件でも見つけたら、レポートを書き終えた後に[`improvement-loop`](../improvement-loop/SKILL.md)でissue化する。この節に作業メモを溜めない。
